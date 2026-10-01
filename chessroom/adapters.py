# -*- coding: utf-8 -*-
"""模拟适配器：门锁与付款。均落库，便于测试与复核。"""
from __future__ import annotations

import hashlib

from .db import Database, now_ts


class DoorAdapter:
    """模拟电子门锁：记录开关锁结果。真实环境可替换为 HTTP 门锁网关。"""

    def __init__(self, db: Database):
        self.db = db

    def _log(self, room_id: int, action: str, ok: bool) -> bool:
        self.db.conn.execute(
            "INSERT INTO door_logs(room_id, action, ok, created_ts) VALUES (?,?,?,?)",
            (room_id, action, 1 if ok else 0, now_ts()),
        )
        return ok

    def unlock(self, room_id: int) -> bool:
        # 模拟：房间号 900 视为离线故障（用于设备故障样例），其余成功
        ok = room_id != 900
        return self._log(room_id, "unlock", ok)

    def lock(self, room_id: int) -> bool:
        ok = room_id != 900
        return self._log(room_id, "lock", ok)


class PaymentAdapter:
    """模拟付款：按幂等键去重；同键重复请求直接返回首次结果。

    退款金额不超过该预约已收净额。编号末尾为 'F' 的演示卡返回失败。
    """

    def __init__(self, db: Database):
        self.db = db

    def _idem_key(self, reservation_id: int, direction: str, amount: float, tag: str) -> str:
        raw = f"{reservation_id}:{direction}:{float(amount):.2f}:{tag}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    def _execute(
        self,
        reservation_id: int,
        direction: str,
        amount: float,
        tag: str,
        fail: bool = False,
    ) -> dict:
        amount = round(amount, 2)
        key = self._idem_key(reservation_id, direction, amount, tag)
        existing = self.db.one("SELECT * FROM payments WHERE idem_key=?", (key,))
        if existing is not None:
            return dict(existing) | {"idempotent_replay": True}

        if amount <= 0:
            status, ref = ("success", f"ZERO-{key[:8]}")
        elif fail:
            status, ref = ("failed", "")
        else:
            status, ref = ("success", f"MOCK-{key.upper()}")

        cur = self.db.conn.execute(
            """INSERT INTO payments(idem_key, reservation_id, direction, amount,
                                    method, status, ref_no, created_ts)
               VALUES (?,?,?,?, 'mock', ?, ?, ?)""",
            (key, reservation_id, direction, amount, status, ref, now_ts()),
        )
        row = self.db.one("SELECT * FROM payments WHERE id=?", (cur.lastrowid,))
        return dict(row) | {"idempotent_replay": False}

    def pay(self, reservation_id: int, amount: float, tag: str = "bill", fail: bool = False) -> dict:
        return self._execute(reservation_id, "pay", amount, tag, fail)

    def refund(self, reservation_id: int, amount: float, tag: str = "deposit") -> dict:
        # 退款幂等 + 余额保护：总退款不得超过总成功收款
        amount = round(amount, 2)
        if amount > 0:
            paid = self.db.one(
                """SELECT COALESCE(SUM(CASE WHEN direction='pay' THEN amount ELSE -amount END),0) AS bal
                   FROM payments WHERE reservation_id=? AND status='success'""",
                (reservation_id,),
            )["bal"]
            if amount > round(paid, 2) + 1e-9:
                key = self._idem_key(reservation_id, "refund", amount, tag)
                existing = self.db.one("SELECT * FROM payments WHERE idem_key=?", (key,))
                if existing:
                    return dict(existing) | {"idempotent_replay": True}
                self.db.conn.execute(
                    """INSERT INTO payments(idem_key, reservation_id, direction, amount,
                                            method, status, ref_no, created_ts)
                       VALUES (?,?,?,?,'mock','failed','',?)""",
                    (key, reservation_id, "refund", amount, now_ts()),
                )
                row = self.db.one("SELECT * FROM payments WHERE idem_key=?", (key,))
                return dict(row) | {"idempotent_replay": False}
        return self._execute(reservation_id, "refund", amount, tag)
