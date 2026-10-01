"""测试：资源双锁、续时边界、清点差异、清洁阻断、退款幂等（含并发开场）。"""
from __future__ import annotations

import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta

from app.adapters import PaymentAdapter
from app.seed import seed_catalog
from app.service import Service, ServiceError, now_iso

T0 = datetime(2026, 10, 2, 10, 0)


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.clock = [T0]
        self.payment = PaymentAdapter()
        self.svc = Service(os.path.join(self.dir, "t.db"),
                           photo_dir=os.path.join(self.dir, "photos"),
                           payment=self.payment,
                           clock=lambda: self.clock[0])
        ids = seed_catalog(self.svc)
        self.t, self.k = ids["tables"], ids["kits"]

    def jump(self, minutes):
        self.clock[0] = T0 + timedelta(minutes=minutes)

    def book(self, table, member, s, e, kits, deposit=50.0):
        return self.svc.create_reservation(
            table, member, now_iso(T0 + timedelta(minutes=s)),
            now_iso(T0 + timedelta(minutes=e)),
            "围棋" if not kits else self.svc.conn.execute(
                "SELECT game FROM sets_kit WHERE id=?", (kits[0],)).fetchone()["game"],
            kits, deposit)

    def open_session(self, table, kits, s=0, e=120, member="M01", deposit=50.0):
        """创建预约并在 s 时刻开场（不清点）。返回 rid。"""
        rid = self.book(table, member, s, e, kits, deposit=deposit)
        self.jump(s)
        self.svc.seat(rid)
        return rid

    def open_checked(self, table, kits, s=0, e=120, member="M01", deposit=50.0):
        rid = self.open_session(table, kits, s, e, member, deposit)
        self.svc.submit_open_check(rid, {})
        return rid


# ---------------------------------------------------------------- 双锁
class TestDoubleLock(Case):
    def test_table_overlap_rejected(self):
        self.book(self.t["A1"], "M01", 0, 90, [self.k["wei_a1"]])
        with self.assertRaisesRegex(ServiceError, "已被预约"):
            self.book(self.t["A1"], "M02", 60, 150, [self.k["wei_a1"]])
        # 首尾相接不重叠：10:00-11:30 与 11:30-12:30 允许
        rid2 = self.book(self.t["A1"], "M02", 90, 150, [self.k["wei_a1"]])
        self.assertGreater(rid2, 0)

    def test_kit_overlap_rejected_even_different_table(self):
        # A1 锁住 wei_a1；C1 想借同一套 wei_a1 → 牌具锁拦截
        self.book(self.t["A1"], "M01", 0, 90, [self.k["wei_a1"]])
        with self.assertRaisesRegex(ServiceError, "牌具"):
            self.book(self.t["C1"], "M06", 30, 120, [self.k["wei_a1"]])
        # C1 用自己常驻的 wei_c1 则正常
        self.book(self.t["C1"], "M06", 30, 120, [self.k["wei_c1"]])

    def test_play_lock_after_seat(self):
        rid = self.open_session(self.t["A1"], [self.k["wei_a1"]], 0, 120)
        # 进行中：任何与 PLAY 锁重叠的新预约都进不来
        with self.assertRaises(ServiceError):
            self.book(self.t["A1"], "M02", 60, 150, [self.k["wei_a1"]])
        locks = self.svc.conn.execute(
            "SELECT kind FROM table_locks WHERE reservation_id=?", (rid,)).fetchall()
        self.assertEqual([r["kind"] for r in locks], ["PLAY"])

    def test_concurrent_seat_only_one(self):
        """并发开场只允许一次：两线程同时 seat，恰一个成功。"""
        rid = self.book(self.t["A1"], "M01", 0, 120, [self.k["wei_a1"]])
        wins, errs = [], []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            try:
                wins.append(self.svc.seat(rid))
            except ServiceError as e:
                errs.append(str(e))

        ts = [threading.Thread(target=worker) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(wins), 1, f"应只有一场成功，实际 wins={wins}")
        self.assertEqual(len(errs), 1)
        n = self.svc.conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE reservation_id=?", (rid,)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_late_release_frees_both_locks(self):
        rid = self.book(self.t["A4"], "M05", -20, 100, [self.k["xiang_a4"]], deposit=30)
        self.jump(16)  # 超过 15 分钟宽限
        self.svc.release_late()
        r = self.svc.conn.execute("SELECT status FROM reservations WHERE id=?", (rid,)).fetchone()
        self.assertEqual(r["status"], "RELEASED_LATE")
        self.assertEqual(self.svc.conn.execute(
            "SELECT COUNT(*) FROM table_locks WHERE reservation_id=?", (rid,)).fetchone()[0], 0)
        # 释放后他人可约同一时段之后的区间
        rid2 = self.book(self.t["A4"], "M02", 20, 90, [self.k["xiang_a4"]])
        self.assertGreater(rid2, 0)


# ---------------------------------------------------------------- 续时边界
class TestExtension(Case):
    def test_extend_ok_without_next(self):
        rid = self.open_session(self.t["C1"], [self.k["wei_c1"]], 0, 120)
        self.jump(60)
        res = self.svc.extend(rid, 60)
        self.assertFalse(res["truncated"])
        self.assertEqual(res["new_end"][-5:], "13:00")

    def test_extend_truncated_by_next_reservation(self):
        # 本场 10:00-12:00；后续 12:30 开始；申请 +60 → 只能给 30 分钟
        rid = self.open_session(self.t["C1"], [self.k["wei_c1"]], 0, 120, member="M06")
        self.book(self.t["C1"], "M01", 150, 240, [self.k["wei_c1"]])
        self.jump(90)
        res = self.svc.extend(rid, 60)
        self.assertTrue(res["truncated"])
        self.assertEqual(res["new_end"][-5:], "12:30")

    def test_extend_rejected_when_next_adjacent(self):
        rid = self.open_session(self.t["A1"], [self.k["wei_a1"]], 0, 120)
        self.book(self.t["A1"], "M06", 120, 240, [self.k["wei_a1"]])
        self.jump(90)
        with self.assertRaisesRegex(ServiceError, "续时冲突"):
            self.svc.extend(rid, 60)
        ext = self.svc.conn.execute("SELECT * FROM extensions ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(ext["accepted"], 0)
        self.assertIn("紧贴", ext["reject_reason"])
        # 被拒后结束时间不变
        sess = self.svc.conn.execute(
            "SELECT planned_end FROM sessions WHERE reservation_id=?", (rid,)).fetchone()
        self.assertEqual(sess["planned_end"][-5:], "12:00")

    def test_extend_blocked_by_kit_reservation_elsewhere(self):
        # 后续预约在别的桌台但占用同一牌具 → 续时同样被牌具边界截断
        rid = self.open_session(self.t["A1"], [self.k["wei_a1"]], 0, 120)
        # A2 预约 wei_a1（A2 自身无冲突），12:30 开始
        self.book(self.t["A2"], "M06", 150, 240, [self.k["wei_a1"]])
        self.jump(90)
        res = self.svc.extend(rid, 60)
        self.assertTrue(res["truncated"])
        self.assertEqual(res["new_end"][-5:], "12:30")


# ---------------------------------------------------------------- 清点差异
class TestCheckDiff(Case):
    def test_open_missing_creates_dispute_and_keeps_slot(self):
        rid = self.open_session(self.t["A3"], [self.k["xiang_a3"]], 0, 120, member="M02")
        self.svc.submit_open_check(rid, {str(self.k["xiang_a3"]): {"7": {"present": False}}})
        dmg = self.svc.conn.execute(
            "SELECT * FROM damages WHERE reservation_id=? AND reason LIKE '开场即缺%'", (rid,)).fetchone()
        self.assertIsNotNone(dmg)
        self.assertEqual(dmg["status"], "DISPUTED")
        # 缺件位置在清点记录里保留空框（present=0 的行仍存在）
        ci = self.svc.conn.execute(
            "SELECT present FROM check_items WHERE set_id=? AND slot_no=7",
            (self.k["xiang_a3"],)).fetchone()
        self.assertEqual(ci["present"], 0)
        total = self.svc.conn.execute(
            "SELECT COUNT(*) FROM check_items ci JOIN checks_master cm ON cm.id=ci.check_id "
            "WHERE cm.reservation_id=? AND cm.phase='OPEN'", (rid,)).fetchone()[0]
        self.assertEqual(total, 33)  # 固定格位一行不少

    def test_close_missing_charged_but_open_missing_not_double(self):
        rid = self.open_session(self.t["A3"], [self.k["xiang_a3"]], 0, 120, member="M02")
        self.svc.submit_open_check(rid, {str(self.k["xiang_a3"]): {"7": {"present": False}}})
        self.jump(100)
        # 开场已缺第7格（仍缺），散场新缺第8格 → 只赔第8格
        res = self.svc.submit_close_check(
            rid, {str(self.k["xiang_a3"]): {"8": {"present": False}}})
        self.assertEqual(len(res["damage_ids"]), 1)
        dmg = self.svc.conn.execute("SELECT * FROM damages WHERE id=?",
                                    (res["damage_ids"][0],)).fetchone()
        self.assertEqual(dmg["slot_no"], 8)
        self.assertEqual(dmg["amount"], 40)  # 象棋单价

    def test_spill_deducted_from_deposit(self):
        rid = self.open_session(self.t["A3"], [self.k["xiang_a3"]], 0, 120, member="M02")
        self.jump(100)
        res = self.svc.submit_close_check(rid, {}, spill=True, spill_amount=20)
        dmg_spill = self.svc.conn.execute(
            "SELECT id FROM damages WHERE reservation_id=? AND reason='茶水泼洒'", (rid,)).fetchone()
        self.svc.resolve_damage(dmg_spill["id"], "approve")  # 押金抵扣并自动结算
        sess = self.svc.conn.execute(
            "SELECT * FROM sessions WHERE reservation_id=?", (rid,)).fetchone()
        self.assertEqual(sess["status"], "CLOSED")
        self.assertEqual(sess["refund"], 30)      # 押金 50 - 泼洒 20
        self.assertEqual(sess["compensated"], 20)  # 净额计入赔付
        # 泼洒不走网关扣款
        self.assertIsNone(self.svc.conn.execute(
            "SELECT id FROM payments WHERE reservation_id=? AND kind='DAMAGE_PAY'",
            (rid,)).fetchone())


# ---------------------------------------------------------------- 清洁阻断
class TestCleaningBlock(Case):
    def test_settle_requires_clear_damages(self):
        rid = self.open_session(self.t["A3"], [self.k["xiang_a3"]], 0, 120, member="M02")
        self.jump(100)
        self.svc.submit_close_check(
            rid, {str(self.k["xiang_a3"]): {"8": {"present": False}}})
        with self.assertRaisesRegex(ServiceError, "赔案未终结"):
            self.svc.settle(rid)

    def test_cleaning_lock_blocks_booking_until_done(self):
        rid = self.open_session(self.t["B2"], [self.k["mj_b2"]], 0, 120, member="M03")
        self.jump(120)
        self.svc.submit_close_check(rid, {})  # 无差异 → 自动结算入清洁队列
        t = self.svc.conn.execute("SELECT status FROM tables WHERE id=?",
                                  (self.t["B2"],)).fetchone()
        self.assertEqual(t["status"], "CLEANING")
        # 清洁未完成 → 重叠预约被 CLEANING 锁挡住
        with self.assertRaisesRegex(ServiceError, "占用"):
            self.book(self.t["B2"], "M04", 130, 200, [self.k["mj_b2"]])
        cid = self.svc.conn.execute(
            "SELECT c.id FROM cleanings c JOIN sessions s ON s.id=c.session_id "
            "WHERE s.reservation_id=? ORDER BY c.id DESC LIMIT 1", (rid,)).fetchone()["id"]
        self.svc.start_cleaning(cid)
        self.svc.finish_cleaning(cid)
        # 清洁完成 → 桌台与牌具释放，可约
        rid2 = self.book(self.t["B2"], "M04", 130, 200, [self.k["mj_b2"]])
        self.assertGreater(rid2, 0)

    def test_maintenance_table_not_released_by_cleaning(self):
        rid = self.open_session(self.t["A3"], [self.k["xiang_a3"]], 0, 120, member="M02")
        self.jump(120)
        self.svc.submit_close_check(rid, {}, device_fault=True, fault_note="棋盘开裂")
        cid = self.svc.conn.execute(
            "SELECT c.id FROM cleanings c JOIN sessions s ON s.id=c.session_id "
            "WHERE s.reservation_id=? ORDER BY c.id DESC LIMIT 1", (rid,)).fetchone()["id"]
        self.svc.start_cleaning(cid)
        self.svc.finish_cleaning(cid)
        self.assertEqual(self.svc.conn.execute(
            "SELECT status FROM tables WHERE id=?", (self.t["A3"],)).fetchone()["status"],
            "MAINTENANCE")
        with self.assertRaisesRegex(ServiceError, "维修中"):
            self.book(self.t["A3"], "M01", 130, 200, [self.k["xiang_a3"]])
        self.svc.repair_table(self.t["A3"], "管理员·林姐")
        # 修复后无清洁锁残留，可正常预约
        self.book(self.t["A3"], "M01", 130, 200, [self.k["xiang_a3"]])


# ---------------------------------------------------------------- 退款幂等
class TestRefundIdempotent(Case):
    def _closed(self):
        rid = self.open_session(self.t["B2"], [self.k["mj_b2"]], 0, 90, member="M03")
        self.jump(95)  # 实际 95 分钟 → 向上取整到 120 分钟计费
        self.svc.submit_close_check(rid, {})
        return rid

    def test_fee_roundup_and_single_refund(self):
        rid = self._closed()
        sess = self.svc.conn.execute(
            "SELECT * FROM sessions WHERE reservation_id=?", (rid,)).fetchone()
        self.assertEqual(sess["minutes_used"], 95)
        self.assertEqual(sess["total_fee"], 28 * 4)   # 120 分钟 = 4 个计费块
        rows = self.svc.conn.execute(
            "SELECT * FROM payments WHERE idem_key=?", (f"refund:{rid}",)).fetchall()
        self.assertEqual(len(rows), 1)
        # 反复触发退款复核：网关只受理一次，其余全部幂等重放
        r1 = self.svc.retry_refund(rid)
        r2 = self.svc.retry_refund(rid)
        self.assertTrue(r1["idempotent_replay"])
        self.assertTrue(r2["idempotent_replay"])
        self.assertEqual(self.payment.attempts[f"refund:{rid}"], 3)  # 共 3 次请求
        self.assertEqual(self.payment.successful_keys(), [f"refund:{rid}"])  # 网关只受理 1 次
        self.assertEqual(self.svc.conn.execute(
            "SELECT COUNT(*) FROM payments WHERE idem_key=?", (f"refund:{rid}",)).fetchone()[0], 1)

    def test_failed_refund_retryable_then_once(self):
        self.payment.fail_rate = 1.0  # 网关全挂
        rid = self.open_session(self.t["B2"], [self.k["mj_b2"]], 0, 90, member="M03")
        self.jump(95)
        with self.assertRaisesRegex(ServiceError, "网关失败"):
            self.svc.submit_close_check(rid, {})
        self.assertEqual(self.svc.conn.execute(
            "SELECT COUNT(*) FROM payments WHERE idem_key=?", (f"refund:{rid}",)).fetchone()[0], 0)
        # 场次仍在 CHECKING（未结算），网关恢复后重试成功
        self.payment.fail_rate = 0.0
        out = self.svc.settle(rid)
        self.assertEqual(out["refund"], 50)
        # 再结算被状态拦截，但退款流水只有一条
        with self.assertRaises(ServiceError):
            self.svc.settle(rid)
        self.assertEqual(self.svc.conn.execute(
            "SELECT COUNT(*) FROM payments WHERE idem_key=?", (f"refund:{rid}",)).fetchone()[0], 1)


# ---------------------------------------------------------------- 调桌 / 减免 / 借件
class TestAdminFlows(Case):
    def test_move_table_and_clean_old(self):
        rid = self.open_session(self.t["A5"], [self.k["mj_a5"]], 0, 120, member="M03")
        self.jump(40)
        out = self.svc.move_table(rid, self.t["A6"], "管理员·林姐")
        new_rid = out["new_reservation_id"]
        self.assertEqual(self.svc.conn.execute(
            "SELECT status FROM reservations WHERE id=?", (rid,)).fetchone()["status"], "MOVED")
        self.assertEqual(self.svc.conn.execute(
            "SELECT status FROM tables WHERE id=?", (self.t["A5"],)).fetchone()["status"],
            "CLEANING")
        self.assertEqual(self.svc.conn.execute(
            "SELECT status FROM tables WHERE id=?", (self.t["A6"],)).fetchone()["status"],
            "OCCUPIED")
        # 牌具锁随客转移到新预约
        self.assertTrue(self.svc.conn.execute(
            "SELECT 1 FROM kit_locks WHERE reservation_id=? AND set_id=?",
            (new_rid, self.k["mj_a5"])).fetchone())

    def test_move_into_occupied_rejected(self):
        self.open_session(self.t["A5"], [self.k["mj_a5"]], 0, 120, member="M03")
        rid2 = self.open_session(self.t["A6"], [self.k["mj_a6"]], 0, 120, member="M01")
        with self.assertRaisesRegex(ServiceError, "OCCUPIED|占用"):
            self.svc.move_table(rid2, self.t["A5"], "管理员·林姐")

    def test_waive_requires_approver(self):
        rid = self.open_session(self.t["A3"], [self.k["xiang_a3"]], 0, 120, member="M02")
        self.jump(100)
        res = self.svc.submit_close_check(
            rid, {str(self.k["xiang_a3"]): {"8": {"present": False}}})
        with self.assertRaisesRegex(ServiceError, "社区负责人"):
            self.svc.resolve_damage(res["damage_ids"][0], "waive", "")
        self.svc.resolve_damage(res["damage_ids"][0], "waive", "社区负责人·周伯")
        # 减免后赔案清零 → 自动结算，押金全额退
        sess = self.svc.conn.execute(
            "SELECT * FROM sessions WHERE reservation_id=?", (rid,)).fetchone()
        self.assertEqual(sess["status"], "CLOSED")
        self.assertEqual(sess["refund"], 50)
        self.assertEqual(sess["compensated"], 0)

    def test_loan_and_return(self):
        rid = self.open_session(self.t["A2"], [self.k["wei_a2"]], 0, 120, member="M01")
        lid = self.svc.loan_item(self.k["poker_spare"], 3, rid)
        self.assertFalse(self.svc.conn.execute(
            "SELECT returned FROM kit_loans WHERE id=?", (lid,)).fetchone()["returned"])
        self.svc.return_loan(lid)
        self.assertTrue(self.svc.conn.execute(
            "SELECT returned FROM kit_loans WHERE id=?", (lid,)).fetchone()["returned"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
