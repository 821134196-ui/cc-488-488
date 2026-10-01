"""领域服务：预约双锁、开场清点、续时、散场、清洁、赔付、退款。

并发模型：所有写方法在进程内 RLock 下执行 BEGIN IMMEDIATE 事务，
SQLite 串行写入；桌台/牌具的区间占用在锁内断言，杜绝重叠。
"""
from __future__ import annotations

import base64
import math
import re
import sqlite3
import threading
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from . import db
from .adapters import DoorLockAdapter, PaymentAdapter

ISO = "%Y-%m-%dT%H:%M"

LATE_GRACE = timedelta(minutes=15)      # 迟到宽限
CLEANING_ESTIMATE = timedelta(minutes=90)


def now_iso(dt: datetime) -> str:
    return dt.strftime(ISO)


def parse(s: str) -> datetime:
    return datetime.fromisoformat(s)


def overlaps(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> bool:
    return a0 < b1 and b0 < a1


def ceil_minutes(minutes: int, block: int = 30) -> int:
    return max(block, math.ceil(minutes / block) * block)


class ServiceError(Exception):
    pass


class Service:
    def __init__(
        self,
        db_path: str,
        photo_dir: str | Path = "data/photos",
        door: DoorLockAdapter | None = None,
        payment: PaymentAdapter | None = None,
        clock=None,
    ) -> None:
        self.conn = db.connect(db_path)
        db.init_db(self.conn)
        self.door = door or DoorLockAdapter()
        self.payment = payment or PaymentAdapter()
        self.photo_dir = Path(photo_dir)
        self.photo_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock or datetime.now
        self.notifications: deque[dict] = deque(maxlen=200)
        self._reminded: set[int] = set()
        self._end_reminded: set[int] = set()

    # ------------------------------------------------------------------ utils
    def now(self) -> datetime:
        return self._clock().replace(second=0, microsecond=0)

    def notify(self, kind: str, text: str) -> None:
        self.notifications.appendleft({"ts": now_iso(self.now()), "kind": kind, "text": text})

    def log_incident(self, kind, detail, res_id=None, table_id=None, set_id=None,
                     amount=None, approver=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO incidents(ts,kind,reservation_id,table_id,set_id,detail,amount,approver)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (now_iso(self.now()), kind, res_id, table_id, set_id, detail, amount, approver),
        )
        self.notify(kind, detail)
        return cur.lastrowid

    def _rows(self, sql: str, args=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def _one(self, sql: str, args=()) -> sqlite3.Row:
        row = self.conn.execute(sql, args).fetchone()
        if row is None:
            raise ServiceError("记录不存在")
        return row

    # ------------------------------------------------------------- occupancy
    def _table_busy(self, table_id, start, end, exclude_res=None) -> sqlite3.Row | None:
        q = ("SELECT * FROM table_locks WHERE table_id=? AND start_ts < ? AND end_ts > ?")
        args = [table_id, now_iso(end), now_iso(start)]
        if exclude_res is not None:
            q += " AND reservation_id != ?"
            args.append(exclude_res)
        return self.conn.execute(q, args).fetchone()

    def _kit_busy(self, kit_ids, start, end, exclude_res=None) -> sqlite3.Row | None:
        if not kit_ids:
            return None
        marks = ",".join("?" * len(kit_ids))
        q = (f"SELECT k.*, s.name AS set_name FROM kit_locks k JOIN sets_kit s ON s.id=k.set_id "
             f"WHERE k.set_id IN ({marks}) AND k.start_ts < ? AND k.end_ts > ?")
        args = [*kit_ids, now_iso(end), now_iso(start)]
        if exclude_res is not None:
            q += " AND k.reservation_id != ?"
            args.append(exclude_res)
        return self.conn.execute(q, args).fetchone()

    # --------------------------------------------------------------- catalog
    def list_tables(self) -> list[dict]:
        rows = self._rows(
            "SELECT t.*, r.name AS room_name FROM tables t JOIN rooms r ON r.id=t.room_id "
            "ORDER BY r.id, t.label")
        return [dict(r) for r in rows]

    def list_sets(self) -> list[dict]:
        out = []
        for r in self._rows(
                "SELECT s.*, t.label AS home_label FROM sets_kit s "
                "LEFT JOIN tables t ON t.id=s.home_table_id ORDER BY s.id"):
            d = dict(r)
            d["item_count"] = self.conn.execute(
                "SELECT COUNT(*) FROM kit_items WHERE set_id=?", (r["id"],)).fetchone()[0]
            out.append(d)
        return out

    def set_slots(self, set_id: int) -> list[dict]:
        return [dict(r) for r in self._rows(
            "SELECT * FROM kit_items WHERE set_id=? ORDER BY slot_no", (set_id,))]

    # ------------------------------------------------------------- booking
    def create_reservation(self, table_id, member_code, start_s, end_s, game,
                           kit_ids, tea_deposit=0.0, note="") -> int:
        start, end = parse(start_s), parse(end_s)
        if end <= start:
            raise ServiceError("结束时间必须晚于开始时间")
        kit_ids = sorted(set(kit_ids))
        with db.tx_lock(), self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            table = self._one("SELECT * FROM tables WHERE id=?", (table_id,))
            if table["status"] == "MAINTENANCE":
                raise ServiceError(f"桌台 {table['label']} 设备故障维修中，暂不可约")
            self._one("SELECT 1 FROM members WHERE code=?", (member_code,))
            if blk := self._table_busy(table_id, start, end):
                owner = self._one("SELECT * FROM reservations WHERE id=?",
                                  (blk["reservation_id"],))
                raise ServiceError(
                    f"桌台 {table['label']} 在该时段已被预约 #{owner['id']} 占用")
            if blk := self._kit_busy(kit_ids, start, end):
                raise ServiceError(f"牌具套装「{blk['set_name']}」在该时段已被其他场次锁定")
            cur = self.conn.execute(
                "INSERT INTO reservations(table_id,member_code,start_ts,end_ts,status,game,"
                "kit_ids,tea_deposit,confirmed_at,created_at,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (table_id, member_code, now_iso(start), now_iso(end), "CONFIRMED", game,
                 ",".join(map(str, kit_ids)), tea_deposit, now_iso(self.now()),
                 now_iso(self.now()), note),
            )
            rid = cur.lastrowid
            self._lock_resources(rid, table_id, kit_ids, start, end, "RESERVE")
            self.conn.execute("UPDATE sets_kit SET status='LOCKED' WHERE id IN (%s)"
                              % ",".join("?" * len(kit_ids)), kit_ids) if kit_ids else None
        self.log_incident("预约", f"新预约 #{rid}：{member_code} 预订 {table['label']} "
                                 f"{start_s}~{end_s}（{game}）", rid, table_id)
        return rid

    def _lock_resources(self, rid, table_id, kit_ids, start, end, kind):
        self.conn.execute(
            "INSERT OR REPLACE INTO table_locks(table_id,reservation_id,start_ts,end_ts,kind)"
            " VALUES(?,?,?,?,?)",
            (table_id, rid, now_iso(start), now_iso(end), kind))
        for sid in kit_ids:
            self.conn.execute(
                "INSERT OR REPLACE INTO kit_locks(set_id,reservation_id,start_ts,end_ts)"
                " VALUES(?,?,?,?)", (sid, rid, now_iso(start), now_iso(end)))

    def _unlock_resources(self, rid):
        self.conn.execute("DELETE FROM table_locks WHERE reservation_id=?", (rid,))
        self.conn.execute("DELETE FROM kit_locks WHERE reservation_id=?", (rid,))

    def cancel_reservation(self, rid) -> None:
        with db.tx_lock(), self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            r = self._one("SELECT * FROM reservations WHERE id=?", (rid,))
            if r["status"] not in ("CONFIRMED", "HELD"):
                raise ServiceError("仅已确认未开场的预约可取消")
            self._unlock_resources(rid)
            self.conn.execute("UPDATE reservations SET status='CANCELLED' WHERE id=?", (rid,))
            kit_ids = [int(x) for x in r["kit_ids"].split(",") if x]
            self.conn.execute("UPDATE sets_kit SET status='IN_STOCK' WHERE id IN (%s)"
                              % ",".join("?" * len(kit_ids)), kit_ids) if kit_ids else None
        self.log_incident("预约", f"预约 #{rid} 已取消", rid, r["table_id"])

    # 迟到释放：由进程内调度器调用
    def release_late(self) -> list[int]:
        released = []
        cutoff = now_iso(self.now() - LATE_GRACE)
        with db.tx_lock(), self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            rows = self._rows(
                "SELECT * FROM reservations WHERE status='CONFIRMED' AND start_ts < ?", (cutoff,))
            for r in rows:
                self._unlock_resources(r["id"])
                kit_ids = [int(x) for x in r["kit_ids"].split(",") if x]
                if kit_ids:
                    self.conn.execute("UPDATE sets_kit SET status='IN_STOCK' WHERE id IN (%s)"
                                      % ",".join("?" * len(kit_ids)), kit_ids)
                self.conn.execute(
                    "UPDATE reservations SET status='RELEASED_LATE',released_at=?,"
                    "release_reason=? WHERE id=?",
                    (now_iso(self.now()), f"超过开场 {LATE_GRACE.seconds // 60} 分钟未到场", r["id"]))
                self.log_incident("迟到释放",
                                  f"预约 #{r['id']} 超时未到场，桌台与牌具已释放",
                                  r["id"], r["table_id"])
                released.append(r["id"])
        return released

    # ----------------------------------------------------------------- seat
    def seat(self, rid) -> int:
        """并发开场：同一预约只允许成功一次（session 唯一约束 + 状态断言）。"""
        with db.tx_lock(), self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            r = self._one("SELECT * FROM reservations WHERE id=?", (rid,))
            if r["status"] != "CONFIRMED":
                raise ServiceError(f"预约 #{rid} 当前状态 {r['status']}，不能开场")
            start = parse(r["start_ts"])
            if not (start - timedelta(minutes=15) <= self.now() <= start + LATE_GRACE):
                raise ServiceError("当前时间不在可开场窗口内")
            t = self._one("SELECT * FROM tables WHERE id=?", (r["table_id"],))
            sid = self.conn.execute(
                "INSERT INTO sessions(reservation_id,table_id,member_code,start_ts,"
                "planned_end,status,rate_30m,tea_deposit)"
                " VALUES(?,?,?,?,?, 'PLAYING', ?, ?)",
                (rid, r["table_id"], r["member_code"], now_iso(self.now()), r["end_ts"],
                 t["rate_30m"], r["tea_deposit"])).lastrowid
            # RESERVE 锁升级为 PLAY 锁（自实际开场起）
            self._unlock_resources(rid)
            kit_ids = [int(x) for x in r["kit_ids"].split(",") if x]
            self._lock_resources(rid, r["table_id"], kit_ids, self.now(), parse(r["end_ts"]),
                                 "PLAY")
            self.conn.execute("UPDATE reservations SET status='SEATED' WHERE id=?", (rid,))
            self.conn.execute("UPDATE tables SET status='OCCUPIED' WHERE id=?",
                              (r["table_id"],))
            if kit_ids:
                self.conn.execute("UPDATE sets_kit SET status='IN_USE' WHERE id IN (%s)"
                                  % ",".join("?" * len(kit_ids)), kit_ids)
            self.door.unlock(r["table_id"])
            self.log_incident("开场", f"预约 #{rid} 开场，桌台已解锁，场次 #{sid}",
                              rid, r["table_id"])
        return sid

    # ------------------------------------------------------------- checks
    def _check_marks(self, res_id, phase, marks):
        r = self._one("SELECT * FROM reservations WHERE id=?", (res_id,))
        kit_ids = [int(x) for x in r["kit_ids"].split(",") if x]
        cid = self.conn.execute(
            "INSERT INTO checks_master(reservation_id,session_id,phase,done_at)"
            " VALUES(?,(SELECT id FROM sessions WHERE reservation_id=?),?,?)",
            (res_id, res_id, phase, now_iso(self.now()))).lastrowid
        missing = []
        for sid in kit_ids:
            for slot in self._rows("SELECT * FROM kit_items WHERE set_id=? ORDER BY slot_no",
                                   (sid,)):
                m = marks.get(str(sid), {}).get(str(slot["slot_no"]), {})
                present = 1 if m.get("present", True) else 0
                note = m.get("damage_note") or None
                self.conn.execute(
                    "INSERT INTO check_items(check_id,set_id,slot_no,label,present,damage_note)"
                    " VALUES(?,?,?,?,?,?)",
                    (cid, sid, slot["slot_no"], slot["label"], present, note))
                if not present:
                    missing.append((sid, dict(slot)))
        return r, cid, missing

    def submit_open_check(self, res_id: int, marks: dict) -> dict:
        """marks: {set_id: {slot_no: {present: bool, damage_note: str}}}"""
        with db.tx_lock(), self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            r = self._one("SELECT * FROM reservations WHERE id=?", (res_id,))
            if r["status"] != "SEATED":
                raise ServiceError("仅进行中的场次可提交开场清点")
            if self.conn.execute("SELECT 1 FROM checks_master WHERE reservation_id=? AND phase='OPEN'",
                                 (res_id,)).fetchone():
                raise ServiceError("开场清点已提交，不可重复")
            r, cid, missing = self._check_marks(res_id, "OPEN", marks)
            for sid, slot in missing:
                self.log_incident("牌具少件",
                                  f"开场少件：套装#{sid} 第{slot['slot_no']}格「{slot['label']}」"
                                  f"空位，已保留空框并记录",
                                  res_id, r["table_id"], sid)
                self.conn.execute(
                    "INSERT INTO damages(reservation_id,set_id,slot_no,reason,amount,status,"
                    "created_at) VALUES(?,?,?,?,?, 'DISPUTED',?)",
                    (res_id, sid, slot["slot_no"],
                     f"开场即缺：{slot['label']}", 0.0, now_iso(self.now())))
                self.conn.execute("UPDATE sets_kit SET status='DAMAGED' WHERE id=?", (sid,))
        return {"missing": len(missing)}

    # ------------------------------------------------------------- extension
    def extend(self, res_id: int, minutes: int) -> dict:
        if minutes <= 0:
            raise ServiceError("续时时长需为正数")
        with db.tx_lock(), self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            r = self._one("SELECT * FROM reservations WHERE id=?", (res_id,))
            if r["status"] != "SEATED":
                raise ServiceError("仅进行中场次可续时")
            sess = self._one("SELECT * FROM sessions WHERE reservation_id=?", (res_id,))
            old_end = parse(sess["planned_end"])
            want_end = old_end + timedelta(minutes=minutes)
            if self.now() >= old_end:
                raise ServiceError("场次已到点，请先散场结算")
            kit_ids = [int(x) for x in r["kit_ids"].split(",") if x]
            # 找出区间内最早的阻挡（桌台或牌具），续时必须避开后续预约
            blocker_end = want_end
            blocker = None
            tl = self.conn.execute(
                "SELECT * FROM table_locks WHERE table_id=? AND start_ts >= ? AND start_ts < ?"
                " AND reservation_id != ? ORDER BY start_ts LIMIT 1",
                (r["table_id"], now_iso(old_end), now_iso(want_end), res_id)).fetchone()
            if tl:
                blocker, blocker_end = tl, parse(tl["start_ts"])
            if kit_ids:
                kl = self.conn.execute(
                    f"SELECT k.*, s.name AS set_name FROM kit_locks k JOIN sets_kit s ON s.id=k.set_id "
                    f"WHERE k.set_id IN ({','.join('?' * len(kit_ids))}) AND k.start_ts >= ?"
                    f" AND k.start_ts < ? AND k.reservation_id != ? ORDER BY k.start_ts LIMIT 1",
                    (*kit_ids, now_iso(old_end), now_iso(want_end), res_id)).fetchone()
                if kl and parse(kl["start_ts"]) < blocker_end:
                    blocker, blocker_end = kl, parse(kl["start_ts"])
            allowed = blocker_end
            reject = None
            if allowed <= old_end:
                self.conn.execute(
                    "INSERT INTO extensions(session_id,old_end,new_end,created_at,accepted,"
                    "reject_reason) VALUES(?,?,?,?,0,?)",
                    (sess["id"], now_iso(old_end), now_iso(want_end), now_iso(self.now()),
                     "后续预约紧贴本场结束，无可用续时空间"))
                self.log_incident("续时冲突",
                                  f"场次 #{sess['id']} 申请续时 {minutes} 分钟被拒："
                                  f"后续预约 {now_iso(old_end)} 开始，无避让空间",
                                  res_id, r["table_id"])
                reject = "续时冲突：后续预约紧贴，无法续时"
            else:
                granted = allowed if allowed < want_end else want_end
                # 延展桌台/牌具锁
                self.conn.execute(
                    "UPDATE table_locks SET end_ts=? WHERE reservation_id=? AND kind='PLAY'",
                    (now_iso(granted), res_id))
                self.conn.execute("UPDATE kit_locks SET end_ts=? WHERE reservation_id=?",
                                  (now_iso(granted), res_id))
                self.conn.execute("UPDATE sessions SET planned_end=?, status='EXTENDED' WHERE id=?",
                                  (now_iso(granted), sess["id"]))
                self.conn.execute("UPDATE reservations SET end_ts=? WHERE id=?",
                                  (now_iso(granted), res_id))
                self.conn.execute(
                    "INSERT INTO extensions(session_id,old_end,new_end,created_at,accepted)"
                    " VALUES(?,?,?,? ,1)",
                    (sess["id"], now_iso(old_end), now_iso(granted), now_iso(self.now())))
                truncated = granted < want_end
                detail = (f"场次 #{sess['id']} 续时至 {now_iso(granted)}"
                          + (f"（申请 {now_iso(want_end)}，因后续预约避让截断）" if truncated else ""))
                self.log_incident("续时", detail, res_id, r["table_id"])
        if reject:
            raise ServiceError(reject)
        return {"new_end": now_iso(granted), "truncated": truncated}

    # ------------------------------------------------------------- moving
    def move_table(self, res_id: int, target_table_id: int, approver: str) -> dict:
        """管理员调桌：牌具随客转移，旧桌进入清洁队列。"""
        with db.tx_lock(), self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            old = self._one("SELECT * FROM reservations WHERE id=?", (res_id,))
            if old["status"] != "SEATED":
                raise ServiceError("仅进行中的场次可以调桌")
            if target_table_id == old["table_id"]:
                raise ServiceError("目标桌台与当前桌台相同")
            tgt = self._one("SELECT * FROM tables WHERE id=?", (target_table_id,))
            sess = self._one("SELECT * FROM sessions WHERE reservation_id=?", (res_id,))
            start, end = self.now(), parse(sess["planned_end"])
            if tgt["status"] in ("OCCUPIED", "MAINTENANCE"):
                raise ServiceError(f"目标桌台 {tgt['label']} 当前 {tgt['status']}，不可调入")
            if blk := self._table_busy(target_table_id, start, end):
                raise ServiceError(f"目标桌台 {tgt['label']} 时段内被预约 #{blk['reservation_id']} 占用")
            kit_ids = [int(x) for x in old["kit_ids"].split(",") if x]
            # 新预约 + 新场次承接
            new_rid = self.conn.execute(
                "INSERT INTO reservations(table_id,member_code,start_ts,end_ts,status,game,"
                "kit_ids,tea_deposit,confirmed_at,created_at,note)"
                " VALUES(?,?,?,?, 'SEATED', ?,?,?,?, ?, '调桌承接')",
                (target_table_id, old["member_code"], now_iso(start), now_iso(end),
                 old["game"], ",".join(map(str, kit_ids)), old["tea_deposit"],
                 now_iso(self.now()), now_iso(self.now()))).lastrowid
            new_sid = self.conn.execute(
                "INSERT INTO sessions(reservation_id,table_id,member_code,start_ts,planned_end,"
                "status,rate_30m,tea_deposit,settlement_note)"
                " VALUES(?,?,?,?,?, 'PLAYING', ?, ?, ?)",
                (new_rid, target_table_id, old["member_code"], now_iso(start), now_iso(end),
                 tgt["rate_30m"], old["tea_deposit"], f"调桌承接自 #{res_id}")).lastrowid
            self.conn.execute(
                "UPDATE sessions SET settlement_note=? WHERE id=?",
                (f"调桌转场至 #{new_rid}，费用并入新场次", sess["id"]))
            self.conn.execute("UPDATE sessions SET status='CLOSED', actual_end=? WHERE id=?",
                              (now_iso(start), sess["id"]))
            # 搬迁锁
            self._unlock_resources(res_id)
            self._lock_resources(new_rid, target_table_id, kit_ids, start, end, "PLAY")
            self.conn.execute("UPDATE reservations SET status='MOVED',moved_to_reservation=? "
                              "WHERE id=?", (new_rid, res_id))
            self.conn.execute("UPDATE tables SET status='READY' WHERE id=?", (old["table_id"],))
            self.conn.execute("UPDATE tables SET status='OCCUPIED' WHERE id=?",
                              (target_table_id,))
            # 旧桌留客用痕迹 → 待清洁
            self.conn.execute(
                "INSERT INTO cleanings(table_id,session_id,created_at,status)"
                " VALUES(?,? ,?, 'WAITING')",
                (old["table_id"], sess["id"], now_iso(start)))
            self.conn.execute(
                "INSERT OR REPLACE INTO table_locks(table_id,reservation_id,start_ts,end_ts,kind)"
                " VALUES(?,?,?,?, 'CLEANING')",
                (old["table_id"], res_id, now_iso(start), now_iso(start + CLEANING_ESTIMATE)))
            self.conn.execute("UPDATE tables SET status='CLEANING' WHERE id=?",
                              (old["table_id"],))
            self.door.lock(old["table_id"])
            self.door.unlock(target_table_id)
            self.log_incident("临时换桌",
                              f"管理员 {approver} 将预约 #{res_id} 从桌台 #{old['table_id']} "
                              f"调至 {tgt['label']}（新场次 #{new_sid}），旧桌进入清洁队列",
                              new_rid, target_table_id, approver=approver)
        return {"new_reservation_id": new_rid}

    # ------------------------------------------------------------- loans
    def loan_item(self, set_id: int, slot_no: int, to_res_id: int) -> int:
        with db.tx_lock(), self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            slot = self._one("SELECT * FROM kit_items WHERE set_id=? AND slot_no=?",
                             (set_id, slot_no))
            owner = self.conn.execute(
                "SELECT reservation_id FROM kit_locks WHERE set_id=? ORDER BY start_ts DESC LIMIT 1",
                (set_id,)).fetchone()
            from_rid = owner["reservation_id"] if owner else None
            to = self._one("SELECT * FROM reservations WHERE id=?", (to_res_id,))
            lid = self.conn.execute(
                "INSERT INTO kit_loans(set_id,slot_no,from_reservation_id,to_reservation_id,ts)"
                " VALUES(?,?,?,?,?)",
                (set_id, slot_no, from_rid, to_res_id, now_iso(self.now()))).lastrowid
            self.log_incident("跨场借件",
                              f"借件：套装#{set_id} 第{slot_no}格「{slot['label']}」"
                              f"从场次 #{from_rid} 借至 #{to_res_id}（借据 #{lid}）",
                              to_res_id, to["table_id"], set_id)
        return lid

    def return_loan(self, loan_id: int) -> None:
        with db.tx_lock(), self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            loan = self._one("SELECT * FROM kit_loans WHERE id=?", (loan_id,))
            if loan["returned"]:
                raise ServiceError("该借件已归还")
            self.conn.execute("UPDATE kit_loans SET returned=1 WHERE id=?", (loan_id,))
            self.log_incident("跨场借件",
                              f"归还：借据 #{loan_id}（套装#{loan['set_id']} "
                              f"第{loan['slot_no']}格）已归位",
                              loan["to_reservation_id"], set_id=loan["set_id"])

    # ------------------------------------------------------- close & settle
    PIECE_PRICE = {"围棋": 15, "象棋": 40, "麻将": 25, "扑克": 5}

    def submit_close_check(self, res_id, marks, spill=False, spill_amount=20.0,
                           device_fault=False, fault_note="") -> dict:
        with db.tx_lock(), self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            r = self._one("SELECT * FROM reservations WHERE id=?", (res_id,))
            if r["status"] != "SEATED":
                raise ServiceError("仅进行中的场次可提交散场清点")
            if self.conn.execute("SELECT 1 FROM checks_master WHERE reservation_id=? AND phase='CLOSE'",
                                 (res_id,)).fetchone():
                raise ServiceError("散场清点已提交")
            r, cid, missing = self._check_marks(res_id, "CLOSE", marks)
            sess = self._one("SELECT * FROM sessions WHERE reservation_id=?", (res_id,))
            new_damages = []
            open_missing = {
                (d["set_id"], d["slot_no"]) for d in self._rows(
                    "SELECT set_id,slot_no FROM damages WHERE reservation_id=? AND reason LIKE '开场即缺%'",
                    (res_id,))
            }
            for sid, slot in missing:
                if (sid, slot["slot_no"]) in open_missing:
                    continue  # 开场即缺且散场仍缺：保留争议，不重复索赔
                price = self.PIECE_PRICE.get(
                    self._one("SELECT game FROM sets_kit WHERE id=?", (sid,))["game"], 20)
                dmg_id = self.conn.execute(
                    "INSERT INTO damages(reservation_id,set_id,slot_no,reason,amount,status,"
                    "created_at) VALUES(?,?,?, '散场少件', ?, 'PENDING',?)",
                    (res_id, sid, slot["slot_no"], price, now_iso(self.now()))).lastrowid
                new_damages.append(dmg_id)
                self.conn.execute("UPDATE sets_kit SET status='DAMAGED' WHERE id=?", (sid,))
                self.log_incident("牌具少件",
                                  f"散场少件：套装#{sid} 第{slot['slot_no']}格「{slot['label']}」"
                                  f"缺失，赔付待核 ¥{price}（赔案 #{dmg_id}）",
                                  res_id, r["table_id"], sid, amount=price)
            if spill:
                dmg_id = self.conn.execute(
                    "INSERT INTO damages(reservation_id,reason,amount,status,created_at)"
                    " VALUES(?, '茶水泼洒', ?, 'PENDING', ?)",
                    (res_id, spill_amount, now_iso(self.now()))).lastrowid
                new_damages.append(dmg_id)
                self.log_incident("茶水泼洒",
                                  f"场次散场报茶水泼洒，清洁/赔付 ¥{spill_amount}（赔案 #{dmg_id}）",
                                  res_id, r["table_id"], amount=spill_amount)
            if device_fault:
                self.conn.execute("UPDATE tables SET status='MAINTENANCE', note=? WHERE id=?",
                                  (fault_note or "设备故障待修", r["table_id"]))
                self.log_incident("设备故障",
                                  f"桌台 #{r['table_id']} 报设备故障：{fault_note or '待检修'}，"
                                  f"已暂停预约", res_id, r["table_id"])
            new_missing = [(sid, slot) for sid, slot in missing
                           if (sid, slot["slot_no"]) not in open_missing]
            self.conn.execute("UPDATE sessions SET status='CHECKING' WHERE id=?", (sess["id"],))
            # 无新增未决事项 → 事务提交后进入待清洁并结算（开场即缺的争议不阻塞）
            auto = not new_damages and not new_missing
        if auto:
            self.settle(res_id)
        return {"missing": len(missing), "damage_ids": new_damages, "auto_settled": auto}

    def resolve_damage(self, damage_id, action, approver="") -> None:
        """action: approve(会员认赔) / dispute(申诉) / waive(社区负责人批准减免)"""
        with db.tx_lock(), self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            d = self._one("SELECT * FROM damages WHERE id=?", (damage_id,))
            if d["status"] in ("PAID", "WAIVED"):
                raise ServiceError("赔案已终结")
            if action == "approve":
                if d["reason"] == "茶水泼洒":
                    # 泼洒清洁费从茶水押金抵扣，不走网关，结算时统一退押金
                    self.conn.execute(
                        "UPDATE damages SET status='PAID',approver='押金抵扣' WHERE id=?",
                        (damage_id,))
                    self.log_incident("赔付",
                                      f"赔案 #{damage_id} 茶水泼洒 ¥{d['amount']:.0f} 从押金抵扣",
                                      d["reservation_id"], set_id=d["set_id"], amount=d["amount"])
                else:
                    gw = self.payment.request(f"pay:{damage_id}", d["amount"])
                    if not gw["ok"]:
                        self.log_incident("赔付争议",
                                          f"赔案 #{damage_id} 扣款网关失败，可重试",
                                          d["reservation_id"], set_id=d["set_id"],
                                          amount=d["amount"])
                        raise ServiceError("支付网关失败，请重试（幂等键不变，不会重复扣款）")
                    self.conn.execute(
                        "INSERT INTO payments(idem_key,reservation_id,kind,amount,status,"
                        "created_at) VALUES(?,?, 'DAMAGE_PAY', ?, 'SUCCESS', ?)",
                        (f"pay:{damage_id}", d["reservation_id"], d["amount"],
                         now_iso(self.now())))
                    self.conn.execute("UPDATE damages SET status='PAID',approver=? WHERE id=?",
                                      (approver or "会员认赔", damage_id))
                    self.log_incident("赔付",
                                      f"赔案 #{damage_id} 会员认赔，扣款 ¥{d['amount']:.0f}",
                                      d["reservation_id"], set_id=d["set_id"],
                                      amount=d["amount"])
            elif action == "dispute":
                self.conn.execute("UPDATE damages SET status='DISPUTED' WHERE id=?", (damage_id,))
                self.log_incident("赔付争议", f"赔案 #{damage_id} 会员提出申诉，待社区负责人复核",
                                  d["reservation_id"], set_id=d["set_id"], amount=d["amount"])
            elif action == "waive":
                if not approver:
                    raise ServiceError("赔付减免必须填写社区负责人批准人")
                self.conn.execute(
                    "UPDATE damages SET status='WAIVED',waived=1,approver=? WHERE id=?",
                    (approver, damage_id))
                self.log_incident("赔付减免",
                                  f"赔案 #{damage_id} 经社区负责人 {approver} 批准减免 ¥{d['amount']:.0f}",
                                  d["reservation_id"], set_id=d["set_id"], amount=d["amount"],
                                  approver=approver)
            else:
                raise ServiceError("未知处理动作")
        # 若该场次所有赔案已终结，自动结算
        d = self._one("SELECT * FROM damages WHERE id=?", (damage_id,))
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM damages WHERE reservation_id=? AND status IN ('PENDING','DISPUTED')"
            " AND reason NOT LIKE '开场即缺%'", (d["reservation_id"],)).fetchone()[0]
        sess = self.conn.execute(
            "SELECT * FROM sessions WHERE reservation_id=?", (d["reservation_id"],)).fetchone()
        if pending == 0 and sess and sess["status"] == "CHECKING":
            self.settle(d["reservation_id"])

    def attach_photo(self, damage_id, filename, b64data) -> str:
        d = self._one("SELECT * FROM damages WHERE id=?", (damage_id,))
        raw = base64.b64decode(b64data)
        safe = re.sub(r"[^A-Za-z0-9._\-一-鿿]", "_", Path(filename).name)
        path = self.photo_dir / f"damage_{damage_id}_{safe}"
        path.write_bytes(raw)
        rel = str(path)
        with db.tx_lock(), self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute("UPDATE damages SET photo_path=? WHERE id=?", (rel, damage_id))
            self.log_incident("损坏照片", f"赔案 #{damage_id} 已留存损坏照片 {path.name}",
                              d["reservation_id"], set_id=d["set_id"])
        return rel

    def settle(self, res_id) -> dict:
        """牌具/争议结清后：算费、退款、桌台进入待清洁。"""
        with db.tx_lock(), self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            r = self._one("SELECT * FROM reservations WHERE id=?", (res_id,))
            sess = self._one("SELECT * FROM sessions WHERE reservation_id=?", (res_id,))
            if sess["status"] not in ("CHECKING", "PLAYING", "EXTENDED"):
                raise ServiceError("场次当前状态不可结算")
            pending = self.conn.execute(
                "SELECT COUNT(*) FROM damages WHERE reservation_id=? AND status IN ('PENDING','DISPUTED')"
                " AND reason NOT LIKE '开场即缺%'", (res_id,)).fetchone()[0]
            if pending:
                raise ServiceError(f"仍有 {pending} 笔赔案未终结，牌具未核清，不能进入清洁")
            end = self.now()
            minutes = int((end - parse(sess["start_ts"])).total_seconds() // 60)
            billed = ceil_minutes(minutes)
            fee = billed / 30 * sess["rate_30m"]
            paid_damage = self.conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM payments WHERE reservation_id=? AND kind='DAMAGE_PAY'",
                (res_id,)).fetchone()[0]
            # 茶水泼洒从押金抵扣；其余赔付已单独扣款
            spill = self.conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM damages WHERE reservation_id=? "
                "AND reason='茶水泼洒' AND status='PAID'", (res_id,)).fetchone()[0]
            refund = max(0.0, sess["tea_deposit"] - spill)
            # 退款幂等：同一预约只出一笔退款
            idem = f"refund:{res_id}"
            existed = self.conn.execute("SELECT 1 FROM payments WHERE idem_key=?",
                                        (idem,)).fetchone()
            if not existed:
                gw = self.payment.request(idem, refund)
                if not gw["ok"]:
                    raise ServiceError("退款网关失败，请用同一按钮重试（幂等）")
                self.conn.execute(
                    "INSERT INTO payments(idem_key,reservation_id,kind,amount,status,created_at)"
                    " VALUES(?,?, 'REFUND', ?, 'SUCCESS', ?)",
                    (idem, res_id, refund, now_iso(end)))
            self.conn.execute(
                "UPDATE sessions SET status='CLOSED',actual_end=?,minutes_used=?,total_fee=?,"
                "damage_fee=?,refund=?,compensated=?,"
                "settlement_note=COALESCE(settlement_note,'') WHERE id=?",
                (now_iso(end), minutes, fee, paid_damage, refund, paid_damage + spill,
                 sess["id"]))
            # 散场：PLAY 锁 → CLEANING 锁；牌具完整 → 待清洁
            kit_ids = [int(x) for x in r["kit_ids"].split(",") if x]
            self.conn.execute("DELETE FROM table_locks WHERE reservation_id=? AND kind='PLAY'",
                              (res_id,))
            self.conn.execute("DELETE FROM kit_locks WHERE reservation_id=?", (res_id,))
            table_status = "MAINTENANCE" if self._one(
                "SELECT status FROM tables WHERE id=?", (r["table_id"],))["status"] == "MAINTENANCE" \
                else "CLEANING"
            self.conn.execute("UPDATE tables SET status=? WHERE id=?",
                              (table_status, r["table_id"]))
            if table_status == "CLEANING":
                self.conn.execute(
                    "INSERT OR REPLACE INTO table_locks(table_id,reservation_id,start_ts,end_ts,kind)"
                    " VALUES(?,?,?,?, 'CLEANING')",
                    (r["table_id"], res_id, now_iso(end), now_iso(end + CLEANING_ESTIMATE)))
            if kit_ids:
                self.conn.execute(
                    "UPDATE sets_kit SET status=CASE WHEN status='DAMAGED' THEN 'DAMAGED' "
                    "ELSE 'AT_CLEANING' END WHERE id IN (%s)" % ",".join("?" * len(kit_ids)),
                    kit_ids)
            self.conn.execute(
                "INSERT INTO cleanings(table_id,session_id,created_at,status)"
                " VALUES(?, ?, ?, 'WAITING')",
                (r["table_id"], sess["id"], now_iso(end)))
            self.conn.execute("UPDATE reservations SET status='FINISHED' WHERE id=?", (res_id,))
            self.door.lock(r["table_id"])
            self.log_incident("散场结算",
                              f"场次 #{sess['id']} 结算：用时 {minutes} 分钟（计 {billed} 分钟）"
                              f"台费 ¥{fee:.0f}，赔扣 ¥{paid_damage + spill:.0f}，"
                              f"押金退 ¥{refund:.0f}；桌台待清洁",
                              res_id, r["table_id"], amount=refund)
        return {"minutes": minutes, "billed": billed, "fee": fee, "refund": refund,
                "replayed": bool(existed)}

    def retry_refund(self, res_id) -> dict:
        """结算复核页的幂等演示按钮：重复退款只回放首次结果。"""
        with db.tx_lock(), self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self._one("SELECT * FROM payments WHERE idem_key=?", (f"refund:{res_id}",))
        gw = self.payment.request(f"refund:{res_id}", row["amount"])
        self.notify("退款复核", f"预约 #{res_id} 退款复核请求：网关返回 {gw['status']}"
                               f"{'（幂等重放，未重复出账）' if gw.get('idempotent_replay') else ''}")
        return gw

    # ------------------------------------------------------------- cleaning
    def start_cleaning(self, cleaning_id) -> None:
        with db.tx_lock(), self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            c = self._one("SELECT * FROM cleanings WHERE id=?", (cleaning_id,))
            if c["status"] != "WAITING":
                raise ServiceError("仅待清洁任务可以开始")
            self.conn.execute("UPDATE cleanings SET status='DOING' WHERE id=?", (cleaning_id,))

    def finish_cleaning(self, cleaning_id) -> None:
        with db.tx_lock(), self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            c = self._one("SELECT * FROM cleanings WHERE id=?", (cleaning_id,))
            if c["status"] != "DOING":
                raise ServiceError("仅清洁中的任务可以完成")
            sess = self._one("SELECT * FROM sessions WHERE id=?", (c["session_id"],))
            r = self._one("SELECT * FROM reservations WHERE id=?",
                          (sess["reservation_id"],))
            self.conn.execute(
                "UPDATE cleanings SET status='DONE',finished_at=? WHERE id=?",
                (now_iso(self.now()), cleaning_id))
            self.conn.execute("DELETE FROM table_locks WHERE reservation_id=? AND kind='CLEANING'",
                              (r["id"],))
            t = self._one("SELECT * FROM tables WHERE id=?", (c["table_id"],))
            if t["status"] != "MAINTENANCE":
                self.conn.execute("UPDATE tables SET status='READY' WHERE id=?",
                                  (c["table_id"],))
            kit_ids = [int(x) for x in r["kit_ids"].split(",") if x]
            if kit_ids:
                # DAMAGED 维持；有未来预约锁 → LOCKED；否则回库
                locked_now = {row[0] for row in self.conn.execute(
                    f"SELECT DISTINCT set_id FROM kit_locks WHERE set_id IN "
                    f"({','.join('?' * len(kit_ids))}) AND end_ts > ?",
                    (*kit_ids, now_iso(self.now()))).fetchall()}
                for sid in kit_ids:
                    st = self._one("SELECT status FROM sets_kit WHERE id=?", (sid,))["status"]
                    if st == "DAMAGED":
                        continue
                    self.conn.execute("UPDATE sets_kit SET status=? WHERE id=?",
                                      ("LOCKED" if sid in locked_now else "IN_STOCK", sid))
            self.log_incident("清洁完成",
                              f"桌台 #{c['table_id']} 清洁完成，"
                              f"{'但设备仍在维修' if t['status'] == 'MAINTENANCE' else '恢复可预约'}",
                              r["id"], c["table_id"])

    def repair_table(self, table_id, approver) -> None:
        with db.tx_lock(), self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            t = self._one("SELECT * FROM tables WHERE id=?", (table_id,))
            if t["status"] != "MAINTENANCE":
                raise ServiceError("该桌台不在维修状态")
            self.conn.execute("UPDATE tables SET status='READY',note='' WHERE id=?", (table_id,))
            self.log_incident("设备修复", f"桌台 {t['label']} 经 {approver} 确认修复，恢复使用",
                              table_id=table_id, approver=approver)

    # ------------------------------------------------------------- scheduler
    def reminders(self) -> None:
        """开场提醒（提前 10 分钟）与到点提醒（前 10 分钟）。"""
        n = self.now()
        for r in self._rows("SELECT * FROM reservations WHERE status='CONFIRMED'"):
            start = parse(r["start_ts"])
            rid = r["id"]
            if timedelta(0) <= start - n <= timedelta(minutes=10) and rid not in self._reminded:
                self._reminded.add(rid)
                self.notify("场次提醒", f"预约 #{rid} 将于 {r['start_ts'][11:]} 开场，请准备")
        for s in self._rows("SELECT * FROM sessions WHERE status IN ('PLAYING','EXTENDED')"):
            end = parse(s["planned_end"])
            sid = s["id"]
            if timedelta(0) <= end - n <= timedelta(minutes=10) and sid not in self._end_reminded:
                self._end_reminded.add(sid)
                self.notify("场次提醒", f"场次 #{sid} 将于 {s['planned_end'][11:]} 到点，可续时或散场")

    def tick(self) -> None:
        self.reminders()
        self.release_late()

    # ------------------------------------------------------------- snapshots
    def state(self) -> dict:
        def table(sql, args=()):
            return [dict(r) for r in self._rows(sql, args)]

        members = table("SELECT * FROM members ORDER BY code")
        rooms = table("SELECT * FROM rooms ORDER BY id")
        tables = table(
            "SELECT t.*, r.name AS room_name FROM tables t JOIN rooms r ON r.id=t.room_id ORDER BY r.id,t.label")
        sets = self.list_sets()
        reservations = table("SELECT * FROM reservations ORDER BY start_ts DESC")
        sessions = table("SELECT * FROM sessions ORDER BY id DESC")
        damages = table(
            "SELECT d.*, s.name AS set_name FROM damages d LEFT JOIN sets_kit s ON s.id=d.set_id "
            "ORDER BY d.id DESC")
        cleanings = table(
            "SELECT c.*, t.label AS table_label FROM cleanings c JOIN tables t ON t.id=c.table_id "
            "ORDER BY c.id DESC LIMIT 30")
        incidents = table("SELECT * FROM incidents ORDER BY id DESC LIMIT 40")
        loans = table("SELECT * FROM kit_loans ORDER BY id DESC LIMIT 20")
        payments = table("SELECT * FROM payments ORDER BY id DESC LIMIT 40")
        extensions = table("SELECT * FROM extensions ORDER BY id DESC LIMIT 20")
        checks = table("SELECT reservation_id, phase FROM checks_master")
        check_map = {}
        for c in checks:
            check_map.setdefault(c["reservation_id"], {})[c["phase"]] = True
        return {
            "now": now_iso(self.now()),
            "members": members, "rooms": rooms, "tables": tables, "sets": sets,
            "reservations": reservations, "sessions": sessions, "damages": damages,
            "cleanings": cleanings, "incidents": incidents, "loans": loans,
            "payments": payments, "extensions": extensions, "checks": check_map,
            "notifications": list(self.notifications),
            "door_log": self.door.log_tail(10),
        }

    def slots_payload(self, res_id) -> dict:
        r = self._one("SELECT * FROM reservations WHERE id=?", (res_id,))
        kit_ids = [int(x) for x in r["kit_ids"].split(",") if x]
        out = {}
        for sid in kit_ids:
            s = self._one("SELECT * FROM sets_kit WHERE id=?", (sid,))
            out[str(sid)] = {"name": s["name"], "game": s["game"], "timer_sn": s["timer_sn"],
                             "slots": self.set_slots(sid)}
        return out
