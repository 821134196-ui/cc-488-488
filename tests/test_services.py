# -*- coding: utf-8 -*-
"""核心业务测试：双锁、并发开场、续时边界、清点差异、清洁阻断、退款幂等。"""
from __future__ import annotations

import threading

import pytest

from chessroom.services import Actor, ServiceError


def book(svc, table=1, kit=1, s=1_000_000, e=1_000_3600, member="M001", tea=50.0):
    return svc.book(member, table, kit, s, e, tea)["reservation"]


# --------------------------------------------------------------- 1. 双锁

class TestDoubleLock:
    def test_confirm_locks_table_and_kit(self, svc):
        r = book(svc)
        locks = svc.db.query("SELECT res_type, status FROM resource_locks WHERE reservation_id=?",
                             (r["id"],))
        assert {(x["res_type"], x["status"]) for x in locks} == {
            ("table", "hold"), ("kit", "hold")}

    def test_same_table_overlap_rejected(self, svc):
        book(svc, table=1, kit=1, s=1000, e=2000)
        with pytest.raises(ServiceError, match="占用"):
            book(svc, table=1, kit=1, s=1500, e=2500)

    def test_same_kit_overlap_rejected(self, svc):
        book(svc, table=1, kit=1, s=1000, e=2000)
        with pytest.raises(ServiceError, match="占用"):
            book(svc, table=2, kit=1, s=1500, e=2500)  # A2 围棋桌，桌不同但牌具冲突

    def test_back_to_back_allowed(self, svc):
        """端点相接不算重叠。"""
        r1 = book(svc, table=1, kit=1, s=1000, e=2000)
        r2 = book(svc, table=1, kit=1, s=2000, e=3000)
        assert r1["id"] != r2["id"]

    def test_different_table_and_kit_allowed(self, svc):
        book(svc, table=1, kit=1, s=1000, e=2000)
        r2 = book(svc, table=3, kit=2, s=1500, e=2500)  # 象棋桌+象棋套装
        assert r2["id"]

    def test_late_release_frees_both_locks(self, svc):
        r = book(svc, s=10_000, e=20_000)
        assert svc.release_late(now=10_000 + 16 * 60) == [r["id"]]
        active = svc.db.one(
            "SELECT COUNT(*) n FROM resource_locks WHERE reservation_id=? AND status IN ('hold','active')",
            (r["id"],))["n"]
        assert active == 0
        # 释放后同桌同套可重新预约
        r2 = book(svc, s=10_000 + 16 * 60, e=20_000 + 16 * 60)
        assert r2["id"] != r["id"]

    def test_late_release_only_after_grace(self, svc):
        r = book(svc, s=10_000, e=20_000)
        assert svc.release_late(now=10_000 + 14 * 60) == []
        assert svc._get("reservations", r["id"])["status"] == "booked"


# --------------------------------------------------------------- 2. 并发开场

class TestConcurrentSeat:
    def test_only_one_concurrent_seat_succeeds(self, svc):
        r = book(svc, s=1_000_000, e=1_003_600)
        results: list[bool] = []
        errs: list[Exception] = []

        def worker():
            try:
                out = svc.seat(r["id"], now=1_000_000 + 60)
                results.append(out["reservation"]["status"] == "seated")
            except ServiceError as ex:
                errs.append(ex)
                results.append(False)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results.count(True) == 1
        assert len(errs) == 7
        assert all("一次" in str(e) or "冲突" in str(e) for e in errs)

    def test_seat_upgrades_locks_to_active(self, svc):
        r = book(svc, s=1000, e=2000)
        svc.seat(r["id"], now=1000)
        locks = svc.db.query("SELECT res_type, status FROM resource_locks WHERE reservation_id=?",
                             (r["id"],))
        assert {x["status"] for x in locks} == {"active"}


# --------------------------------------------------------------- 3. 续时边界

class TestExtend:
    def test_extend_ok_when_no_next(self, svc):
        r = book(svc, s=1000, e=2000)
        svc.seat(r["id"], now=1000)
        svc.extend(r["id"], 3000, now=1500)
        assert (svc._get("reservations", r["id"])["plan_end_ts"]) == 3000

    def test_extend_rejected_on_overlap(self, svc):
        r1 = book(svc, table=1, kit=1, s=1000, e=2000)
        svc.seat(r1["id"], now=1000)
        r2 = book(svc, table=1, kit=1, s=3000, e=4000)
        with pytest.raises(ServiceError, match="续时冲突"):
            svc.extend(r1["id"], 3500, now=1500)
        # 锁不变、冲突事件已登记
        ev = svc.db.one("SELECT COUNT(*) n FROM events WHERE reservation_id=? AND kind='extend_conflict'",
                        (r1["id"],))["n"]
        assert ev == 1
        assert svc._get("reservations", r1["id"])["plan_end_ts"] == 2000

    def test_extend_boundary_touch_allowed(self, svc):
        """端点相接（我结束=他开始）不算重叠。"""
        r1 = book(svc, table=1, kit=1, s=1000, e=2000)
        svc.seat(r1["id"], now=1000)
        book(svc, table=1, kit=1, s=3000, e=4000)
        svc.extend(r1["id"], 3000, now=1500)   # 恰好接到下一场
        assert svc._get("reservations", r1["id"])["plan_end_ts"] == 3000

    def test_extend_kit_conflict_too(self, svc):
        r1 = book(svc, table=1, kit=1, s=1000, e=2000)
        svc.seat(r1["id"], now=1000)
        # A2 围棋桌 + 同一围棋套装：桌不冲突但牌具冲突
        book(svc, table=2, kit=1, s=3000, e=4000)
        with pytest.raises(ServiceError, match="续时冲突"):
            svc.extend(r1["id"], 3500, now=1500)



# --------------------------------------------------------------- 4. 清点差异

class TestInventoryCheck:
    def test_open_shortage_recorded_with_empty_frames(self, svc):
        r = book(svc, table=5, kit=3, s=1000, e=2000)  # 麻将
        out = svc.seat(r["id"], present={"风箭牌": {28: 0}}, now=1000)
        assert out["missing"] == {"风箭牌": 1}
        row = svc.db.one(
            "SELECT COUNT(*) n FROM checks WHERE reservation_id=? AND phase='open' AND present=0",
            (r["id"],))["n"]
        assert row == 1
        ev = svc.db.one("SELECT * FROM events WHERE reservation_id=? AND kind='shortage'",
                        (r["id"],))
        assert ev is not None

    def test_close_complete_releases_and_enqueues(self, svc):
        r = book(svc, s=1000, e=2000)
        svc.seat(r["id"], now=1000)
        # 围棋套装含计时器：180+180+1+2+1=364 格
        out = svc.settle(r["id"], now=1000 + 70 * 60)
        assert out["complete"] is True
        assert out["minutes"] == 75            # 70 分钟向上取整 15
        assert out["time_fee"] == 18.0 * 1.25
        held = svc.db.one(
            "SELECT COUNT(*) n FROM resource_locks WHERE reservation_id=? AND status IN ('hold','active')",
            (r["id"],))["n"]
        assert held == 0
        queue = svc.cleaning_queue()
        assert any(x["reservation_id"] == r["id"] for x in queue)

    def test_close_shortage_keeps_locked_and_out_of_queue(self, svc):
        r = book(svc, s=1000, e=2000)
        svc.seat(r["id"], now=1000)
        out = svc.settle(r["id"], present={"黑子": {17: 0}}, now=2000)
        assert out["complete"] is False
        held = svc.db.one(
            "SELECT COUNT(*) n FROM resource_locks WHERE reservation_id=? AND status IN ('hold','active')",
            (r["id"],))["n"]
        assert held == 2                       # 桌、套仍双锁
        assert not any(x["reservation_id"] == r["id"] for x in svc.cleaning_queue())
        # held 状态阻断新预约（桌或套重叠都不行）
        with pytest.raises(ServiceError):
            book(svc, s=1000, e=2000)

    def test_unreturned_loan_counts_as_shortage(self, svc):
        r1 = book(svc, table=1, kit=1, s=1000, e=5000)
        svc.seat(r1["id"], now=1000)
        r2 = book(svc, table=5, kit=3, s=1000, e=5000, member="M002")
        svc.seat(r2["id"], now=1000)
        # 桌1围棋套1 vs 桌5麻将套3 —— 借件要求出借套含组件
        svc.loan_kit(r2["id"], r1["id"], "骰子", 1, now=1100)
        out = svc.settle(r2["id"], now=5000)   # 骰子未还
        assert "骰子" in out["missing"]
        assert out["complete"] is False

    def test_returned_loan_does_not_count(self, svc):
        r1 = book(svc, table=1, kit=1, s=1000, e=5000)
        svc.seat(r1["id"], now=1000)
        r2 = book(svc, table=5, kit=3, s=1000, e=5000, member="M002")
        svc.seat(r2["id"], now=1000)
        loan = svc.loan_kit(r2["id"], r1["id"], "骰子", 1, now=1100)["loan"]
        svc.return_loan(loan["id"], now=1200)
        out = svc.settle(r2["id"], now=5000)
        assert "骰子" not in out["missing"]


# --------------------------------------------------------------- 5. 清洁阻断

class TestCleaning:
    def test_dirty_table_blocks_booking(self, svc):
        r = book(svc, s=1000, e=2000)
        svc.seat(r["id"], now=1000)
        svc.settle(r["id"], now=2000)          # 入待清洁
        with pytest.raises(ServiceError, match="清洁"):
            book(svc, s=2001, e=3000)

    def test_cleaning_state_progresses(self, svc):
        r = book(svc, s=1000, e=2000)
        svc.seat(r["id"], now=1000)
        svc.settle(r["id"], now=2000)
        cid = svc.db.one("SELECT id FROM cleaning WHERE reservation_id=?", (r["id"],))["id"]
        svc.start_cleaning(cid)
        # 清洁中仍阻断
        with pytest.raises(ServiceError, match="清洁"):
            book(svc, s=3000, e=4000)
        svc.finish_cleaning(cid, now=2600)
        assert book(svc, s=3000, e=4000)["id"]

    def test_finish_cleaning_allows_reuse(self, svc):
        r = book(svc, s=1000, e=2000)
        svc.seat(r["id"], now=1000)
        svc.settle(r["id"], now=2000)
        cid = svc.db.one("SELECT id FROM cleaning WHERE reservation_id=?", (r["id"],))["id"]
        svc.start_cleaning(cid)
        svc.finish_cleaning(cid, now=2600)
        r2 = book(svc, s=2601, e=3600)
        assert r2["id"]

    def test_change_table_old_goes_dirty(self, svc, admin):
        r1 = book(svc, table=2, kit=1, s=1000, e=5000)
        svc.seat(r1["id"], now=1000)
        svc.change_table(r1["id"], 1, admin, now=1100)
        # 旧桌 A2(2号) 待清洁；新桌 A1 被占用
        assert svc.db.one(
            "SELECT COUNT(*) n FROM cleaning WHERE table_id=2 AND status='dirty'")["n"] == 1
        with pytest.raises(ServiceError):
            book(svc, table=2, kit=1, s=2000, e=3000)
        with pytest.raises(ServiceError):
            book(svc, table=1, kit=1, s=2000, e=3000)

    def test_only_admin_can_change_table(self, svc, staff):
        r = book(svc, table=2, kit=1, s=1000, e=5000)
        svc.seat(r["id"], now=1000)
        with pytest.raises(ServiceError, match="管理员"):
            svc.change_table(r["id"], 1, staff, now=1100)


# --------------------------------------------------------------- 6. 退款幂等

class TestRefundIdempotency:
    def test_refund_idempotent(self, svc):
        r = book(svc, s=1000, e=2000, tea=50)
        svc.seat(r["id"], now=1000)
        svc.settle(r["id"], tea_charge=10, now=1000 + 60 * 60)  # 60min → 18
        # 收：押金50；最终：18+10=28，退 22
        refunds = svc.db.query(
            "SELECT * FROM payments WHERE reservation_id=? AND direction='refund'", (r["id"],))
        assert len(refunds) == 1
        assert refunds[0]["amount"] == 22
        # 适配器层再次退款相同金额返回同一凭证
        again = svc.pay.refund(r["id"], 22, tag="deposit_refund")
        assert again["ref_no"] == refunds[0]["ref_no"]
        total = svc.db.one(
            "SELECT COUNT(*) n FROM payments WHERE reservation_id=? AND direction='refund'",
            (r["id"],))["n"]
        assert total == 1

    def test_resolve_damage_payment_idempotent(self, svc, staff):
        r = book(svc, s=1000, e=2000, tea=50)
        svc.seat(r["id"], now=1000)
        svc.settle(r["id"], present={"黑子": {1: 0}},
                   damages=[{"desc": "棋子丢失", "amount": 40}], now=2000)
        svc.resolve_damage(r["id"], "pay", staff)
        with pytest.raises(ServiceError, match="重复"):
            svc.resolve_damage(r["id"], "pay", staff)
        pay_rows = svc.db.query(
            "SELECT * FROM payments WHERE reservation_id=? AND direction='pay'", (r["id"],))
        # 押金50 + 最终收款(计时0+赔付40-押金50=-10 → 退10?)；只保证赔付收取不重复
        assert len({p["idem_key"] for p in pay_rows}) == len(pay_rows)

    def test_relieve_requires_director(self, svc, admin, staff, director):
        r = book(svc, s=1000, e=2000)
        svc.seat(r["id"], now=1000)
        svc.settle(r["id"], damages=[{"desc": "棋罐裂纹", "amount": 60}],
                   disputed=True, now=2000)
        with pytest.raises(ServiceError, match="社区负责人"):
            svc.resolve_damage(r["id"], "relieve", admin, discount=30)
        out = svc.resolve_damage(r["id"], "relieve", director, discount=30)
        assert out["bill"]["damage_charge"] == 30
        assert out["bill"]["discount"] == 30

    def test_refund_not_exceed_paid(self, svc):
        """退款总额不得超过已收款（余额保护）。"""
        r = book(svc, s=1000, e=2000, tea=10)
        out = svc.pay.refund(r["id"], 9999, tag="x")
        assert out["status"] == "failed"


# --------------------------------------------------------------- 7. 提醒

class TestReminders:
    def test_due_reminders_once(self, svc):
        r = book(svc, s=10_000, e=20_000)
        first = [x["id"] for x in svc.due_reminders(600, now=9_500)]
        second = [x["id"] for x in svc.due_reminders(600, now=9_500)]
        assert first == [r["id"]]
        assert second == []
