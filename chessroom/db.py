# -*- coding: utf-8 -*-
"""SQLite 持久层：预约、桌台/牌具锁定、清点、赔付、清洁与支付记录。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Iterator

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 房间
CREATE TABLE IF NOT EXISTS rooms (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    x           REAL NOT NULL,          -- 平面图坐标(米)
    y           REAL NOT NULL,
    w           REAL NOT NULL,
    h           REAL NOT NULL
);

-- 桌台
CREATE TABLE IF NOT EXISTS tables (
    id          INTEGER PRIMARY KEY,
    room_id     INTEGER NOT NULL REFERENCES rooms(id),
    code        TEXT NOT NULL UNIQUE,   -- 桌号，如 A1
    kind        TEXT NOT NULL,          -- 围棋/象棋/麻将/扑克
    px          REAL NOT NULL,          -- 桌心在平面图上的坐标(米)
    py          REAL NOT NULL,
    rate        REAL NOT NULL DEFAULT 0 -- 每小时计时费(演示金额)
);

-- 会员(演示数据)
CREATE TABLE IF NOT EXISTS members (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,   -- 会员代号
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL DEFAULT '',
    balance     REAL NOT NULL DEFAULT 0
);

-- 牌具套装：每套有固定构成 components(JSON: {名称: 数量})
CREATE TABLE IF NOT EXISTS kits (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL,          -- 围棋/象棋/麻将/扑克/通用
    name        TEXT NOT NULL,
    components  TEXT NOT NULL,          -- JSON {"黑子":180,...}
    timer       INTEGER NOT NULL DEFAULT 1, -- 是否含计时器
    deposit     REAL NOT NULL DEFAULT 0 -- 牌具押金
);

-- 预约
CREATE TABLE IF NOT EXISTS reservations (
    id            INTEGER PRIMARY KEY,
    member_code   TEXT NOT NULL,
    table_id      INTEGER NOT NULL REFERENCES tables(id),
    kit_id        INTEGER NOT NULL REFERENCES kits(id),
    start_ts      INTEGER NOT NULL,     -- epoch 秒
    end_ts        INTEGER NOT NULL,
    -- booked 已预约(锁定) / seated 已开场 / settled 已散场 /
    -- released 迟到释放 / cancelled 取消
    status        TEXT NOT NULL DEFAULT 'booked',
    tea_deposit   REAL NOT NULL DEFAULT 0,
    note          TEXT NOT NULL DEFAULT '',
    created_ts    INTEGER NOT NULL,
    confirmed_ts  INTEGER,
    seated_ts     INTEGER,
    settled_ts    INTEGER,
    released_ts   INTEGER,
    plan_end_ts   INTEGER,              -- 续时后的预计结束
    actual_end_ts INTEGER               -- 散场实际时间
);
CREATE INDEX IF NOT EXISTS idx_res_table ON reservations(table_id, start_ts, end_ts);
CREATE INDEX IF NOT EXISTS idx_res_status ON reservations(status);

-- 资源锁：桌台与牌具双锁。status: hold(预约锁定)/active(使用中)/done(已释放)
CREATE TABLE IF NOT EXISTS resource_locks (
    id            INTEGER PRIMARY KEY,
    res_type      TEXT NOT NULL,        -- table / kit
    res_id        INTEGER NOT NULL,
    reservation_id INTEGER NOT NULL REFERENCES reservations(id),
    status        TEXT NOT NULL,
    locked_ts     INTEGER NOT NULL,
    released_ts   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_lock_res ON resource_locks(res_type, res_id, status);

-- 开场/散场逐格清点记录。slot 为固定格位编号；缺件时 present=0，保留空框
CREATE TABLE IF NOT EXISTS checks (
    id            INTEGER PRIMARY KEY,
    reservation_id INTEGER NOT NULL REFERENCES reservations(id),
    phase         TEXT NOT NULL,        -- open 开场 / close 散场
    component     TEXT NOT NULL,
    slot          INTEGER NOT NULL,
    present       INTEGER NOT NULL,
    marked_ts     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_check_res ON checks(reservation_id, phase);

-- 事件流水：迟到释放/换桌/少件/泼洒/故障/借件/续时冲突/赔付争议
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY,
    reservation_id INTEGER REFERENCES reservations(id),
    kind          TEXT NOT NULL,
    detail        TEXT NOT NULL DEFAULT '',
    amount        REAL NOT NULL DEFAULT 0,
      -- 赔付审批：pending / approved(社区负责人) / rejected
    approval      TEXT NOT NULL DEFAULT '',
    approver      TEXT NOT NULL DEFAULT '',
    created_ts    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_res ON events(reservation_id);

-- 跨场借件
CREATE TABLE IF NOT EXISTS kit_loans (
    id            INTEGER PRIMARY KEY,
    kit_id        INTEGER NOT NULL REFERENCES kits(id),
    from_res_id   INTEGER NOT NULL REFERENCES reservations(id),
    to_res_id     INTEGER NOT NULL REFERENCES reservations(id),
    component     TEXT NOT NULL,
    qty           INTEGER NOT NULL,
    returned      INTEGER NOT NULL DEFAULT 0,
    created_ts    INTEGER NOT NULL,
    returned_ts   INTEGER
);

-- 清洁队列
CREATE TABLE IF NOT EXISTS cleaning (
    id            INTEGER PRIMARY KEY,
    table_id      INTEGER NOT NULL REFERENCES tables(id),
    reservation_id INTEGER NOT NULL REFERENCES reservations(id),
    -- dirty 待清洁 / cleaning 清洁中 / clean 已完成可再用
    status        TEXT NOT NULL DEFAULT 'dirty',
    created_ts    INTEGER NOT NULL,
    finished_ts   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_clean_table ON cleaning(table_id, status);

-- 结算单
CREATE TABLE IF NOT EXISTS bills (
    id            INTEGER PRIMARY KEY,
    reservation_id INTEGER NOT NULL UNIQUE REFERENCES reservations(id),
    minutes       INTEGER NOT NULL,
    time_fee      REAL NOT NULL,
    tea_deposit   REAL NOT NULL,
    tea_charge    REAL NOT NULL DEFAULT 0,   -- 茶水消费(泼洒赔付另计)
    damage_charge REAL NOT NULL DEFAULT 0,   -- 损坏/少件赔付
    discount      REAL NOT NULL DEFAULT 0,   -- 批准减免
    total         REAL NOT NULL,
    refund        REAL NOT NULL DEFAULT 0,   -- 退回押金
    -- 赔付争议减免：pending / approved / rejected
    approval      TEXT NOT NULL DEFAULT '',
    approver      TEXT NOT NULL DEFAULT '',
    created_ts    INTEGER NOT NULL
);

-- 支付/退款结果(模拟适配器)。幂等键唯一
CREATE TABLE IF NOT EXISTS payments (
    id            INTEGER PRIMARY KEY,
    idem_key      TEXT NOT NULL UNIQUE,
    reservation_id INTEGER REFERENCES reservations(id),
    direction     TEXT NOT NULL,        -- pay 收款 / refund 退款
    amount        REAL NOT NULL,
    method        TEXT NOT NULL DEFAULT 'mock',
    status        TEXT NOT NULL,        -- success / failed / pending
    ref_no        TEXT NOT NULL DEFAULT '',
    created_ts    INTEGER NOT NULL
);

-- 门锁操作日志(模拟适配器)
CREATE TABLE IF NOT EXISTS door_logs (
    id            INTEGER PRIMARY KEY,
    room_id       INTEGER NOT NULL,
    action        TEXT NOT NULL,        -- lock / unlock
    ok            INTEGER NOT NULL,
    created_ts    INTEGER NOT NULL
);
"""


class Database:
    """封装 SQLite 连接。check_same_thread=False + 进程内锁，供 Web 线程与后台任务共用。"""

    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get(
            "CHESSROOM_DB", os.path.join(os.path.dirname(__file__), "..", "data", "chessroom.db")
        )
        self.path = os.path.abspath(self.path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._tl = threading.local()
        self._commit_lock = threading.RLock()
        self.init_schema()

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._tl, "conn", None)
        if c is None:
            c = self._new_conn()
            self._tl.conn = c
        return c

    def init_schema(self) -> None:
        c = self._new_conn()
        try:
            c.executescript(_SCHEMA)
        finally:
            c.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """串行化事务：进程内锁 + BEGIN IMMEDIATE，保证并发开场只成功一次。"""
        c = self.conn
        self._commit_lock.acquire()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        finally:
            self._commit_lock.release()

    # --- 便捷查询 ---
    def query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()


def now_ts() -> int:
    return int(time.time())


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def loads(s: str):
    return json.loads(s)
