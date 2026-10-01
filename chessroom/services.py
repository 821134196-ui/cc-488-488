# -*- coding: utf-8 -*-
"""业务服务层：双锁预约、开场清点、续时、换桌、借件、散场结算、清洁与赔付审批。

所有写操作都在 BEGIN IMMEDIATE 事务内完成；进程内 commit_lock 串行化，
SQLite 文件锁兜底跨进程，保证并发开场只成功一次、桌台/牌具不被重叠占用。
"""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass

from .adapters import DoorAdapter, PaymentAdapter
from .catalog import LATE_GRACE_MIN
from .db import Database, loads, now_ts

ACTIVE_STATUSES = ("booked", "seated")
OPEN_LOCKS = ("hold", "active")
TIMER_COMP = "计时器"


class ServiceError(Exception):
    """业务规则错误。"""


@dataclass
class Actor:
    name: str
    role: str = "staff"          # staff / admin（管理员）/ director（社区负责人）


def bill_minutes(used_seconds: int) -> int:
    """按实际时长计费，向上取整到 15 分钟。"""
    if used_seconds <= 0:
        return 0
    return int(math.ceil(used_seconds / 60 / 15.0)) * 15


class ChessRoomService:
    def __init__(self, db: Database, photo_dir: str | None = None):
        self.db = db
        self.door = DoorAdapter(db)
        self.pay = PaymentAdapter(db)
        self.photo_dir = photo_dir or os.path.join(
            os.path.dirname(__file__), "..", "photos"
        )
        os.makedirs(self.photo_dir, exist_ok=True)

    # ------------------------------------------------------------------ 工具

    def _get(self, table: str, rid: int) -> dict:
        row = self.db.one(f"SELECT * FROM {table} WHERE id=?", (rid,))
        if row is None:
            raise ServiceError(f"{table}#{rid} 不存在")
        return dict(row)

    def kit_components(self, kit: dict, include_timer: bool = True) -> dict:
        comps = loads(kit["components"])
        if include_timer and kit["timer"]:
            comps = {**comps, TIMER_COMP: kit["timer"]}
        return comps

    def _overlap(
        self, c, table_id: int, kit_id: int, start_ts: int, end_ts: int,
        exclude_res: int | None = None,
    ) -> list[dict]:
        q = """SELECT r.*, t.code table_code, k.code kit_code FROM reservations r
               JOIN tables t ON t.id=r.table_id JOIN kits k ON k.id=r.kit_id
               WHERE r.status IN ('booked','seated','held')
                 AND r.start_ts < ? AND r.end_ts > ?
                 AND (r.table_id=? OR r.kit_id=?)"""
        args: list = [end_ts, start_ts, table_id, kit_id]
        if exclude_res is not None:
            q += " AND r.id<>?"
            args.append(exclude_res)
        return [dict(r) for r in c.execute(q, args).fetchall()]

    def _table_dirty(self, c, table_id: int, start_ts: int) -> dict | None:
        """未完成清洁阻断：只要存在未完成的清洁记录，新开始时刻一律阻断。"""
        row = c.execute(
            """SELECT * FROM cleaning WHERE table_id=? AND status IN ('dirty','cleaning')
               AND (finished_ts IS NULL OR finished_ts > ?)
               ORDER BY id DESC LIMIT 1""",
            (table_id, start_ts),
        ).fetchone()
        return dict(row) if row else None

    def _event(self, c, kind: str, detail: str = "", amount: float = 0.0,
               reservation_id: int | None = None, approval: str = "",
               approver: str = "") -> int:
        cur = c.execute(
            """INSERT INTO events(reservation_id,kind,detail,amount,approval,approver,created_ts)
               VALUES (?,?,?,?,?,?,?)""",
            (reservation_id, kind, detail, amount, approval, approver, now_ts()),
        )
        return cur.lastrowid

    def save_photo(self, data: bytes, suffix: str = "jpg") -> str:
        """损坏照片留存本地目录，返回相对路径。"""
        name = f"damage_{now_ts()}_{abs(hash(data)) % 10_000_000:07d}.{suffix}"
        path = os.path.join(self.photo_dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return os.path.relpath(path, os.path.dirname(self.photo_dir))

    # ------------------------------------------------------------ 登记/确认

    def book(
        self, member_code: str, table_id: int, kit_id: int,
        start_ts: int, end_ts: int, tea_deposit: float,
        note: str = "", actor: Actor | None = None,
    ) -> dict:
        """登记并确认预约：同时锁定桌台与牌具（双锁），收取茶水押金。"""
        if end_ts <= start_ts:
            raise ServiceError("结束时间必须晚于开始时间")
        with self.db.tx() as c:
            member = c.execute(
                "SELECT * FROM members WHERE code=?", (member_code,)
            ).fetchone()
            if member is None:
                raise ServiceError(f"会员代号 {member_code} 不存在")
            table = self._get("tables", table_id)
            kit = self._get("kits", kit_id)
            if kit["kind"] != table["kind"]:
                raise ServiceError(f"{kit['name']} 与 {table['code']} 桌型不匹配")
            if self._overlap(c, table_id, kit_id, start_ts, end_ts):
                raise ServiceError("桌台或牌具在该时段已被占用，无法确认预约")
            if self._table_dirty(c, table_id, start_ts):
                raise ServiceError("桌台尚在清洁/待清洁，暂不可预约该时段")

            cur = c.execute(
                """INSERT INTO reservations(member_code,table_id,kit_id,start_ts,end_ts,
                       status,tea_deposit,note,created_ts,confirmed_ts,plan_end_ts)
                   VALUES (?,?,?,?,?, 'booked', ?, ?, ?, ?, ?)""",
                (member_code, table_id, kit_id, start_ts, end_ts,
                 tea_deposit, note, now_ts(), now_ts(), end_ts),
            )
            rid = cur.lastrowid
            for rtype, rid_ in (("table", table_id), ("kit", kit_id)):
                c.execute(
                    """INSERT INTO resource_locks(res_type,res_id,reservation_id,status,locked_ts)
                       VALUES (?,?,?, 'hold', ?)""",
                    (rtype, rid_, rid, now_ts()),
                )
            self._event(c, "book", f"{actor.name if actor else '前台'}确认预约 "
                                   f"{table['code']}+{kit['code']}", reservation_id=rid)
            # 茶水押金（模拟收款）
            pay_result = self.pay.pay(rid, tea_deposit, tag="tea_deposit")
            res = dict(c.execute("SELECT * FROM reservations WHERE id=?", (rid,)).fetchone())
        return {"reservation": res, "deposit_payment": pay_result}

    # ---------------------------------------------------------------- 开场

    def open_checklist(self, reservation_id: int) -> dict:
        """开场清单：按套装展开固定数量格（含计时器）。"""
        res = self._get("reservations", reservation_id)
        kit = self._get("kits", res["kit_id"])
        table = self._get("tables", res["table_id"])
        comps = self.kit_components(kit)
        return {
            "reservation": res, "kit": kit, "table": table,
            "slots": {name: list(range(1, n + 1)) for name, n in comps.items()},
            "expected_total": sum(comps.values()),
        }

    def seat(
        self, reservation_id: int,
        present: dict[str, dict[int, int]] | None = None,
        now: int | None = None,
    ) -> dict:
        """并发安全开场：CAS 状态 booked→seated，逐格记录，缺件保留空框并登记。

        present: {件名: {格位: 1/0}}；未提交的格位一律视为在场。
        """
        present = present or {}
        now = now or now_ts()
        with self.db.tx() as c:
            res = c.execute(
                "SELECT * FROM reservations WHERE id=?", (reservation_id,)
            ).fetchone()
            if res is None:
                raise ServiceError("预约不存在")
            res = dict(res)
            if res["status"] == "seated":
                raise ServiceError("该场已开场，并发开场只允许一次")
            if res["status"] != "booked":
                raise ServiceError(f"预约状态为 {res['status']}，不能开场")
            if now > res["start_ts"] + LATE_GRACE_MIN * 60:
                raise ServiceError("已超过迟到宽限期，预约已失效")
            if self._table_dirty(c, res["table_id"], now):
                raise ServiceError("桌台未完成清洁，不能开场")

            kit = self._get("kits", res["kit_id"])
            comps = self.kit_components(kit)
            missing: dict[str, int] = {}
            for name, total in comps.items():
                marks = present.get(name, {})
                for slot in range(1, total + 1):
                    ok = 0 if int(marks.get(slot, 1)) == 0 else 1
                    if not ok:
                        missing[name] = missing.get(name, 0) + 1
                    c.execute(
                        """INSERT INTO checks(reservation_id,phase,component,slot,present,marked_ts)
                           VALUES (?, 'open', ?, ?, ?, ?)""",
                        (reservation_id, name, slot, ok, now),
                    )

            # CAS：只有一个并发请求能把 booked 改成 seated
            upd = c.execute(
                "UPDATE reservations SET status='seated', seated_ts=? WHERE id=? AND status='booked'",
                (now, reservation_id),
            )
            if upd.rowcount != 1:
                raise ServiceError("开场冲突：该场次刚刚已被开场")
            c.execute(
                "UPDATE resource_locks SET status='active' WHERE reservation_id=? AND status='hold'",
                (reservation_id,),
            )
            if missing:
                self._event(
                    c, "shortage", f"开场少件：" +
                    "，".join(f"{k}缺{v}" for k, v in missing.items()),
                    reservation_id=reservation_id,
                )
            room = c.execute(
                "SELECT room_id FROM tables WHERE id=?", (res["table_id"],)
            ).fetchone()
            door_ok = self.door.unlock(room["room_id"])
            if not door_ok:
                self._event(c, "device_fault", "房间门锁离线，开门失败，已通知管理员",
                            reservation_id=reservation_id)
            res = dict(c.execute("SELECT * FROM reservations WHERE id=?",
                                 (reservation_id,)).fetchone())
        return {"reservation": res, "missing": missing, "door_opened": door_ok}

    # ---------------------------------------------------------------- 续时

    def extend(self, reservation_id: int, new_end_ts: int,
               now: int | None = None) -> dict:
        """续时：必须严格避开后续预约（端点相接允许），冲突则登记续时冲突。"""
        now = now or now_ts()
        with self.db.tx() as c:
            res = c.execute("SELECT * FROM reservations WHERE id=?",
                            (reservation_id,)).fetchone()
            if res is None:
                raise ServiceError("预约不存在")
            res = dict(res)
            if res["status"] != "seated":
                raise ServiceError("只有进行中的场次可以续时")
            cur_end = res["plan_end_ts"] or res["end_ts"]
            if new_end_ts <= cur_end:
                raise ServiceError("续时结束时间必须晚于当前结束时间")

            clashes = self._overlap(
                c, res["table_id"], res["kit_id"], res["start_ts"], new_end_ts,
                exclude_res=reservation_id,
            )
            if clashes:
                nxt = min(clashes, key=lambda r: r["start_ts"])
                detail = (
                    f"申请续至{new_end_ts}，与后续预约 {nxt['table_code']}/"
                    f"{nxt['kit_code']} {nxt['start_ts']} 冲突，最迟可续至 {nxt['start_ts']}"
                )
                self._event(c, "extend_conflict", detail, reservation_id=reservation_id)
                conflict_start = nxt["start_ts"]
                conflict_logged = True
            else:
                conflict_start = conflict_logged = False
        if conflict_logged:
            raise ServiceError(
                f"续时冲突：{time.strftime('%H:%M', time.localtime(conflict_start))} "
                f"已有后续预约，最迟可续至该时刻"
            )
        with self.db.tx() as c:
            c.execute("UPDATE reservations SET end_ts=?, plan_end_ts=? WHERE id=?",
                      (new_end_ts, new_end_ts, reservation_id))
            self._event(c, "extend",
                        f"续时至 {time.strftime('%H:%M', time.localtime(new_end_ts))}",
                        reservation_id=reservation_id)
            res = dict(c.execute("SELECT * FROM reservations WHERE id=?",
                                 (reservation_id,)).fetchone())
        return {"reservation": res}

    def extend_limit(self, reservation_id: int) -> int | None:
        """返回最迟可续时间（下一场预约开始时刻；无则 None）。"""
        res = self._get("reservations", reservation_id)
        rows = self.db.query(
            """SELECT start_ts FROM reservations
               WHERE status IN ('booked','seated') AND id<>?
                 AND (table_id=? OR kit_id=?) AND start_ts >= ?
               ORDER BY start_ts LIMIT 1""",
            (reservation_id, res["table_id"], res["kit_id"],
             res["plan_end_ts"] or res["end_ts"]),
        )
        return rows[0]["start_ts"] if rows else None

    # ---------------------------------------------------------------- 换桌

    def change_table(self, reservation_id: int, new_table_id: int, actor: Actor,
                     now: int | None = None) -> dict:
        """管理员临时换桌：目标桌台须空闲且已清洁；旧桌转入待清洁。"""
        now = now or now_ts()
        if actor.role not in ("admin", "director"):
            raise ServiceError("仅管理员可以调整桌台")
        with self.db.tx() as c:
            res = dict(c.execute("SELECT * FROM reservations WHERE id=?",
                                 (reservation_id,)).fetchone())
            if res["status"] != "seated":
                raise ServiceError("只有进行中的场次可以换桌")
            old = dict(c.execute("SELECT * FROM tables WHERE id=?",
                                 (res["table_id"],)).fetchone())
            new = dict(c.execute("SELECT * FROM tables WHERE id=?",
                                 (new_table_id,)).fetchone())
            if new["id"] == old["id"]:
                raise ServiceError("新桌台与原桌台相同")
            if new["kind"] != old["kind"]:
                raise ServiceError("换桌须保持相同棋类桌型")
            if self._overlap(c, new_table_id, res["kit_id"],
                             res["start_ts"], res["plan_end_ts"] or res["end_ts"],
                             exclude_res=reservation_id):
                raise ServiceError("目标桌台在该时段已被占用")
            if self._table_dirty(c, new_table_id, now):
                raise ServiceError("目标桌台尚未完成清洁")

            c.execute(
                "UPDATE resource_locks SET status='done', released_ts=? "
                "WHERE reservation_id=? AND res_type='table' AND status IN ('hold','active')",
                (now, reservation_id),
            )
            c.execute(
                """INSERT INTO resource_locks(res_type,res_id,reservation_id,status,locked_ts)
                   VALUES ('table', ?, ?, 'active', ?)""",
                (new_table_id, reservation_id, now),
            )
            c.execute("UPDATE reservations SET table_id=? WHERE id=?",
                      (new_table_id, reservation_id))
            # 旧桌已使用，转入待清洁
            c.execute(
                """INSERT INTO cleaning(table_id,reservation_id,status,created_ts)
                   VALUES (?,?,'dirty',?)""",
                (old["id"], reservation_id, now),
            )
            self._event(c, "change_table",
                        f"{actor.name} 将 {old['code']} 临时调整至 {new['code']}",
                        reservation_id=reservation_id)
            res = dict(c.execute("SELECT * FROM reservations WHERE id=?",
                                 (reservation_id,)).fetchone())
        return {"reservation": res}

    # ---------------------------------------------------------------- 借件

    def loan_kit(self, from_res_id: int, to_res_id: int, component: str,
                 qty: int, now: int | None = None) -> dict:
        """跨场借件：双方须在使用中；散场前须归还，否则计入少件。"""
        now = now or now_ts()
        if qty <= 0:
            raise ServiceError("借用数量须大于 0")
        with self.db.tx() as c:
            r_from = dict(c.execute("SELECT * FROM reservations WHERE id=?",
                                    (from_res_id,)).fetchone())
            r_to = dict(c.execute("SELECT * FROM reservations WHERE id=?",
                                  (to_res_id,)).fetchone())
            if r_from["status"] != "seated" or r_to["status"] != "seated":
                raise ServiceError("跨场借件要求两场都在进行中")
            if r_from["kit_id"] == r_to["kit_id"]:
                raise ServiceError("同一场/同一套装无需借件")
            kit = dict(c.execute("SELECT * FROM kits WHERE id=?",
                                 (r_from["kit_id"],)).fetchone())
            comps = self.kit_components(kit)
            if component not in comps:
                raise ServiceError(f"出借套装不含 {component}")
            cur = c.execute(
                """INSERT INTO kit_loans(kit_id,from_res_id,to_res_id,component,qty,created_ts)
                   VALUES (?,?,?,?,?,?)""",
                (r_from["kit_id"], from_res_id, to_res_id, component, qty, now),
            )
            self._event(c, "kit_loan",
                        f"向场次#{to_res_id} 借出 {component} x{qty}",
                        reservation_id=from_res_id)
            self._event(c, "kit_loan",
                        f"从场次#{from_res_id} 借入 {component} x{qty}",
                        reservation_id=to_res_id)
            loan = dict(c.execute("SELECT * FROM kit_loans WHERE id=?",
                                  (cur.lastrowid,)).fetchone())
        return {"loan": loan}

    def return_loan(self, loan_id: int, now: int | None = None) -> dict:
        now = now or now_ts()
        with self.db.tx() as c:
            c.execute(
                "UPDATE kit_loans SET returned=1, returned_ts=? WHERE id=? AND returned=0",
                (now, loan_id),
            )
        return {"loan_id": loan_id, "returned": True}

    # ------------------------------------------------------------ 杂项事件

    def report_event(self, reservation_id: int, kind: str, detail: str,
                     amount: float = 0.0) -> dict:
        """茶水泼洒 / 设备故障等现场记录。"""
        allowed = {"tea_spill", "device_fault", "note"}
        if kind not in allowed:
            raise ServiceError(f"事件类型 {kind} 不允许由此登记")
        with self.db.tx() as c:
            eid = self._event(c, kind, detail, amount, reservation_id=reservation_id)
        return {"event_id": eid}

    # ---------------------------------------------------------------- 散场

    def close_checklist(self, reservation_id: int) -> dict:
        res = self._get("reservations", reservation_id)
        kit = self._get("kits", res["kit_id"])
        comps = self.kit_components(kit)
        loans_out = [dict(r) for r in self.db.query(
            "SELECT * FROM kit_loans WHERE from_res_id=? AND returned=0",
            (reservation_id,))]
        return {
            "reservation": res, "kit": kit,
            "slots": {name: list(range(1, n + 1)) for name, n in comps.items()},
            "expected_total": sum(comps.values()),
            "loans_out": loans_out,
        }

    def settle(
        self, reservation_id: int,
        present: dict[str, dict[int, int]] | None = None,
        tea_charge: float = 0.0,
        damages: list[dict] | None = None,
        disputed: bool = False,
        now: int | None = None,
    ) -> dict:
        """散场结算：按实际时长计费；逐格清点，缺件留空框。

        damages: [{desc, amount, photo?}]  photo 为已留存的相对路径。
        牌具完整 → 释放双锁并进入待清洁；少件/争议 → 保持锁定，待赔付处理后放行。
        """
        present = present or {}
        damages = damages or []
        now = now or now_ts()
        with self.db.tx() as c:
            res = dict(c.execute("SELECT * FROM reservations WHERE id=?",
                                 (reservation_id,)).fetchone())
            if res["status"] != "seated":
                raise ServiceError("只有进行中的场次可以散场结算")
            table = dict(c.execute("SELECT * FROM tables WHERE id=?",
                                   (res["table_id"],)).fetchone())
            kit = dict(c.execute("SELECT * FROM kits WHERE id=?",
                                 (res["kit_id"],)).fetchone())
            comps = self.kit_components(kit)

            missing: dict[str, int] = {}
            for name, total in comps.items():
                marks = present.get(name, {})
                for slot in range(1, total + 1):
                    ok = 0 if int(marks.get(slot, 1)) == 0 else 1
                    if not ok:
                        missing[name] = missing.get(name, 0) + 1
                    c.execute(
                        """INSERT INTO checks(reservation_id,phase,component,slot,present,marked_ts)
                           VALUES (?, 'close', ?, ?, ?, ?)""",
                        (reservation_id, name, slot, ok, now),
                    )

            # 跨场借出未归还，计入少件
            loans_out = [dict(r) for r in c.execute(
                "SELECT * FROM kit_loans WHERE from_res_id=? AND returned=0",
                (reservation_id,))]
            for loan in loans_out:
                missing[loan["component"]] = (
                    missing.get(loan["component"], 0) + loan["qty"]
                )

            used = now - (res["seated_ts"] or res["start_ts"])
            minutes = bill_minutes(used)
            time_fee = round(table["rate"] * minutes / 60.0, 2)
            damage_charge = round(sum(float(d.get("amount", 0)) for d in damages), 2)
            total = round(time_fee + tea_charge + damage_charge, 2)

            approval = "pending" if (missing or disputed) else ""
            cur = c.execute(
                """INSERT INTO bills(reservation_id,minutes,time_fee,tea_deposit,tea_charge,
                       damage_charge,discount,total,refund,approval,created_ts)
                   VALUES (?,?,?,?,?,?,?,?,0,?,?)""",
                (reservation_id, minutes, time_fee, res["tea_deposit"], tea_charge,
                 damage_charge, 0.0, total, approval, now),
            )
            bill_id = cur.lastrowid

            for d in damages:
                self._event(
                    c, "damage",
                    f"{d.get('desc','损坏')} 赔付提议 ¥{d.get('amount',0)}"
                    + (f"（照片 {d['photo']}）" if d.get("photo") else ""),
                    amount=float(d.get("amount", 0)),
                    reservation_id=reservation_id,
                    approval="pending" if (missing or disputed) else "",
                )
            if missing:
                self._event(c, "shortage",
                            "散场少件：" + "，".join(f"{k}缺{v}" for k, v in missing.items()),
                            reservation_id=reservation_id, approval="pending")
            if disputed:
                self._event(c, "damage_dispute",
                            f"会员对赔付 ¥{damage_charge} 提出争议，待社区负责人复核",
                            amount=damage_charge, reservation_id=reservation_id,
                            approval="pending")

            c.execute(
                """UPDATE reservations SET status=?, settled_ts=?,
                       actual_end_ts=?, plan_end_ts=? WHERE id=?""",
                ("held" if (missing or disputed) else "settled",
                 now, now, res["plan_end_ts"] or res["end_ts"], reservation_id),
            )

            complete = not missing
            payments: list[dict] = []
            if complete and not disputed:
                payments = self._finalize_money(c, reservation_id, bill_id)
                self._release_and_enqueue(c, res, table, now)
            else:
                # 少件/争议：桌台与牌具保持占用（锁不释放），清洁暂缓
                self._event(
                    c, "note",
                    "牌具未完整或赔付有争议：桌台、牌具暂保持锁定，待放行后清洁",
                    reservation_id=reservation_id,
                )
            door_ok = self.door.lock(table["room_id"])
            if not door_ok:
                self._event(c, "device_fault", "散场上锁失败，门锁离线",
                            reservation_id=reservation_id)

            bill = dict(c.execute("SELECT * FROM bills WHERE id=?",
                                  (bill_id,)).fetchone())
        return {
            "bill": bill, "missing": missing, "minutes": minutes,
            "time_fee": time_fee, "complete": complete and not disputed,
            "door_locked": door_ok, "payments": payments,
        }

    def _finalize_money(self, c, reservation_id: int, bill_id: int,
                        discount: float = 0.0) -> list[dict]:
        """核销账单并收退款。幂等：重复调用走支付适配器幂等键，不产生重复流水。"""
        bill = dict(c.execute("SELECT * FROM bills WHERE id=?", (bill_id,)).fetchone())
        damage_charge = round(bill["damage_charge"] - discount, 2)
        total = round(bill["time_fee"] + bill["tea_charge"] + damage_charge, 2)
        deposit = bill["tea_deposit"]
        net = round(total - deposit, 2)

        payments: list[dict] = []
        if net > 0:
            payments.append(self.pay.pay(reservation_id, net, tag="final"))
        elif net < 0:
            payments.append(self.pay.refund(reservation_id, -net, tag="deposit_refund"))

        c.execute(
            """UPDATE bills SET damage_charge=?, discount=?, total=?,
                   refund=?, approval='approved' WHERE id=?""",
            (damage_charge, round(discount, 2), total, max(0.0, -net), bill_id),
        )
        return payments

    def _release_and_enqueue(self, c, res: dict, table: dict, now: int) -> None:
        c.execute(
            "UPDATE resource_locks SET status='done', released_ts=? "
            "WHERE reservation_id=? AND status IN ('hold','active')",
            (now, res["id"]),
        )
        exists = c.execute(
            "SELECT 1 FROM cleaning WHERE table_id=? AND status IN ('dirty','cleaning')",
            (table["id"],),
        ).fetchone()
        if not exists:
            c.execute(
                """INSERT INTO cleaning(table_id,reservation_id,status,created_ts)
                   VALUES (?,?,'dirty',?)""",
                (table["id"], res["id"], now),
            )

    def resolve_damage(
        self, reservation_id: int, action: str, actor: Actor,
        discount: float = 0.0,
    ) -> dict:
        """处理少件/赔付争议：pay 按提议赔付；relieve 减免（须社区负责人批准）。

        放行条件：账单 approved → 释放双锁、桌台进入待清洁。支付/退款幂等。
        """
        now = now_ts()
        with self.db.tx() as c:
            res = dict(c.execute("SELECT * FROM reservations WHERE id=?",
                                 (reservation_id,)).fetchone())
            bill_row = c.execute("SELECT * FROM bills WHERE reservation_id=?",
                                 (reservation_id,)).fetchone()
            if bill_row is None:
                raise ServiceError("结算单不存在")
            bill = dict(bill_row)
            if bill["approval"] not in ("pending",):
                raise ServiceError("该赔付已复核，请勿重复处理")

            if action == "relieve":
                if actor.role != "director":
                    raise ServiceError("赔付减免须社区负责人批准")
                if not 0 <= discount <= bill["damage_charge"]:
                    raise ServiceError("减免金额超出赔付范围")
                c.execute("UPDATE bills SET approver=? WHERE id=?",
                          (actor.name, bill["id"]))
                self._event(c, "damage_dispute",
                            f"社区负责人 {actor.name} 批准减免 ¥{discount}",
                            amount=discount, reservation_id=reservation_id,
                            approval="approved", approver=actor.name)
            elif action == "pay":
                c.execute("UPDATE bills SET approver=? WHERE id=?",
                          (actor.name, bill["id"]))
                self._event(c, "damage",
                            f"{actor.name} 见证赔付收取 ¥{bill['damage_charge']}",
                            amount=bill["damage_charge"],
                            reservation_id=reservation_id, approval="approved",
                            approver=actor.name)
                discount = 0.0
            else:
                raise ServiceError("action 须为 pay 或 relieve")

            payments = self._finalize_money(c, reservation_id, bill["id"], discount)
            table = dict(c.execute("SELECT * FROM tables WHERE id=?",
                                   (res["table_id"],)).fetchone())
            self._release_and_enqueue(c, res, table, now)
            c.execute("UPDATE reservations SET status='settled' WHERE id=? AND status='held'",
                      (reservation_id,))
            bill = dict(c.execute("SELECT * FROM bills WHERE id=?",
                                  (bill["id"],)).fetchone())
        return {"bill": bill, "payments": payments}

    # ---------------------------------------------------------------- 清洁

    def cleaning_queue(self) -> list[dict]:
        rows = self.db.query(
            """SELECT c.*, t.code table_code, r.member_code FROM cleaning c
               JOIN tables t ON t.id=c.table_id
               JOIN reservations r ON r.id=c.reservation_id
               WHERE c.status IN ('dirty','cleaning') ORDER BY c.created_ts""")
        return [dict(r) for r in rows]

    def start_cleaning(self, cleaning_id: int) -> dict:
        with self.db.tx() as c:
            c.execute("UPDATE cleaning SET status='cleaning' WHERE id=? AND status='dirty'",
                      (cleaning_id,))
        return {"cleaning_id": cleaning_id, "status": "cleaning"}

    def finish_cleaning(self, cleaning_id: int, now: int | None = None) -> dict:
        now = now or now_ts()
        with self.db.tx() as c:
            row = c.execute("SELECT * FROM cleaning WHERE id=?", (cleaning_id,)).fetchone()
            if row is None:
                raise ServiceError("清洁任务不存在")
            c.execute("UPDATE cleaning SET status='clean', finished_ts=? WHERE id=?",
                      (now, cleaning_id))
        return {"cleaning_id": cleaning_id, "status": "clean"}

    # ------------------------------------------------------------ 迟到/提醒

    def release_late(self, now: int | None = None) -> list[int]:
        """超过宽限期未开场的预约：释放双锁、保持门锁关闭、登记迟到释放。"""
        now = now or now_ts()
        released: list[int] = []
        with self.db.tx() as c:
            rows = c.execute(
                """SELECT * FROM reservations WHERE status='booked'
                   AND start_ts + ? < ?""",
                (LATE_GRACE_MIN * 60, now),
            ).fetchall()
            for r in rows:
                rid = r["id"]
                c.execute(
                    "UPDATE reservations SET status='released', released_ts=? WHERE id=?",
                    (now, rid),
                )
                c.execute(
                    "UPDATE resource_locks SET status='done', released_ts=? "
                    "WHERE reservation_id=? AND status='hold'",
                    (now, rid),
                )
                self._event(c, "late_release",
                            f"超过{LATE_GRACE_MIN}分钟未到场，预约释放，桌台/牌具恢复可约",
                            reservation_id=rid)
                released.append(rid)
        return released

    def due_reminders(self, lead_seconds: int, now: int | None = None) -> list[dict]:
        """开场前提醒（幂等：已提醒过的不重复）。"""
        now = now or now_ts()
        out: list[dict] = []
        with self.db.tx() as c:
            rows = c.execute(
                """SELECT r.*, t.code table_code FROM reservations r
                   JOIN tables t ON t.id=r.table_id
                   WHERE r.status='booked' AND r.start_ts BETWEEN ? AND ?
                     AND NOT EXISTS (SELECT 1 FROM events e
                                     WHERE e.reservation_id=r.id AND e.kind='remind')""",
                (now, now + lead_seconds),
            ).fetchall()
            for r in rows:
                self._event(c, "remind",
                            f"场次提醒：{r['table_code']} 即将开场",
                            reservation_id=r["id"])
                out.append(dict(r))
        return out

    # ------------------------------------------------------------ 查询视图

    def floor_plan(self, moment: int | None = None) -> dict:
        moment = moment or now_ts()
        rooms = [dict(r) for r in self.db.query("SELECT * FROM rooms ORDER BY id")]
        tables = [dict(t) for t in self.db.query(
            "SELECT * FROM tables ORDER BY code")]
        for t in tables:
            # 锁代表真实占用状态（预约/进行/待赔付），不受当前时刻过滤
            lock = self.db.one(
                """SELECT rl.status lock_status, r.id res_id, r.member_code,
                          r.start_ts, r.end_ts, r.plan_end_ts, r.status res_status
                   FROM resource_locks rl JOIN reservations r ON r.id=rl.reservation_id
                   WHERE rl.res_type='table' AND rl.res_id=? AND rl.status IN ('hold','active')
                   ORDER BY rl.id DESC LIMIT 1""",
                (t["id"],),
            )
            t["lock"] = dict(lock) if lock else None
            # 待清洁/清洁中：无未释放锁时才显示为待清洁
            clean = self.db.one(
                """SELECT status FROM cleaning WHERE table_id=?
                   AND status IN ('dirty','cleaning') ORDER BY id DESC LIMIT 1""",
                (t["id"],))
            t["cleaning"] = (clean["status"] if clean and not lock else None)
        return {"rooms": rooms, "tables": tables, "moment": moment}

    def calendar(self, day_start: int, day_end: int) -> list[dict]:
        rows = self.db.query(
            """SELECT r.*, t.code table_code, k.name kit_name, m.name member_name
               FROM reservations r
               JOIN tables t ON t.id=r.table_id JOIN kits k ON k.id=r.kit_id
               JOIN members m ON m.code=r.member_code
               WHERE r.start_ts < ? AND r.end_ts > ?
               ORDER BY t.code, r.start_ts""",
            (day_end, day_start),
        )
        return [dict(r) for r in rows]

    def timers(self) -> list[dict]:
        rows = self.db.query(
            """SELECT r.id, r.member_code, r.plan_end_ts, r.end_ts, t.code table_code
               FROM reservations r JOIN tables t ON t.id=r.table_id
               WHERE r.status='seated' ORDER BY r.plan_end_ts""")
        now = now_ts()
        out = []
        for r in rows:
            end = r["plan_end_ts"] or r["end_ts"]
            out.append({**dict(r), "remaining": max(0, end - now)})
        return out

    def inventory(self) -> list[dict]:
        kits = [dict(k) for k in self.db.query("SELECT * FROM kits ORDER BY code")]
        for k in kits:
            lock = self.db.one(
                """SELECT rl.status lock_status, r.id res_id FROM resource_locks rl
                   JOIN reservations r ON r.id=rl.reservation_id
                   WHERE rl.res_type='kit' AND rl.res_id=? AND rl.status IN ('hold','active')
                   ORDER BY rl.id DESC LIMIT 1""",
                (k["id"],))
            k["lock"] = dict(lock) if lock else None
            k["components_map"] = loads(k["components"])
        return kits
