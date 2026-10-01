"""SQLite 连接与表结构。

所有写操作走同一把进程内互斥锁 + IMMEDIATE 事务，配合
UNIQUE(tables_id, start, end_overlap) 无法直接表达区间重叠，
改用预约/场次的占用断言在服务层完成（见 service.py）。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    code        TEXT PRIMARY KEY,          -- 会员代号
    name        TEXT NOT NULL,
    phone       TEXT,
    credit      REAL NOT NULL DEFAULT 0,   -- 演示余额（元）
    note        TEXT
);

CREATE TABLE IF NOT EXISTS rooms (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    x           REAL NOT NULL,             -- 平面图坐标（米）
    y           REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tables (
    id          INTEGER PRIMARY KEY,
    room_id     INTEGER NOT NULL REFERENCES rooms(id),
    label       TEXT NOT NULL,             -- 桌号，如 A1
    x           REAL NOT NULL,             -- 平面图层坐标（米）
    y           REAL NOT NULL,
    kind        TEXT NOT NULL,             -- 围棋 / 象棋 / 麻将 / 扑克
    seats       INTEGER NOT NULL DEFAULT 4,
    rate_30m    REAL NOT NULL,             -- 每 30 分钟费用（元）
    status      TEXT NOT NULL DEFAULT 'READY'
        CHECK (status IN ('READY','OCCUPIED','CLEANING','MAINTENANCE')),
    note        TEXT
);

-- 牌具套装（如：A1 配套围棋套装、象棋套装、麻将套装、扑克牌套装）
CREATE TABLE IF NOT EXISTS sets_kit (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    game        TEXT NOT NULL,             -- 围棋/象棋/麻将/扑克
    home_table_id INTEGER REFERENCES tables(id), -- 常驻桌台，可空=共享柜
    status      TEXT NOT NULL DEFAULT 'IN_STOCK'
        CHECK (status IN ('IN_STOCK','LOCKED','IN_USE','AT_CLEANING','DAMAGED')),
    timer_sn    TEXT,                      -- 配套计时器编号（若有）
    note        TEXT
);

-- 套装内固定数量格：一行 = 一个清点位置（一枚棋子/一张牌/计时器）
CREATE TABLE IF NOT EXISTS kit_items (
    id          INTEGER PRIMARY KEY,
    set_id      INTEGER NOT NULL REFERENCES sets_kit(id),
    slot_no     INTEGER NOT NULL,          -- 固定格位编号（从 1 开始）
    label       TEXT NOT NULL,             -- 黑棋#1 / 红中 / 计时器
    UNIQUE(set_id, slot_no)
);

CREATE TABLE IF NOT EXISTS reservations (
    id          INTEGER PRIMARY KEY,
    table_id    INTEGER NOT NULL REFERENCES tables(id),
    member_code TEXT NOT NULL REFERENCES members(code),
    start_ts    TEXT NOT NULL,             -- ISO 本地时间
    end_ts      TEXT NOT NULL,
    status      TEXT NOT NULL
        CHECK (status IN ('HELD','CONFIRMED','SEATED','FINISHED','RELEASED_LATE','CANCELLED','MOVED')),
    game        TEXT NOT NULL,
    kit_ids     TEXT NOT NULL,             -- 逗号分隔
    tea_deposit REAL NOT NULL DEFAULT 0,   -- 茶水押金
    confirmed_at TEXT,
    released_at TEXT,
    release_reason TEXT,
    moved_to_reservation INTEGER REFERENCES reservations(id),
    created_at  TEXT NOT NULL,
    note        TEXT
);

-- 锁状态：已确认预约 / 进行中场次锁定桌台
CREATE TABLE IF NOT EXISTS table_locks (
    id          INTEGER PRIMARY KEY,
    table_id    INTEGER NOT NULL REFERENCES tables(id),
    reservation_id INTEGER NOT NULL REFERENCES reservations(id),
    start_ts    TEXT NOT NULL,
    end_ts      TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('RESERVE','PLAY','CLEANING')),
    UNIQUE(table_id, reservation_id)
);

-- 牌具锁：同一套装同一时段只能被一个预约/场次持有
CREATE TABLE IF NOT EXISTS kit_locks (
    id          INTEGER PRIMARY KEY,
    set_id      INTEGER NOT NULL REFERENCES sets_kit(id),
    reservation_id INTEGER NOT NULL REFERENCES reservations(id),
    start_ts    TEXT NOT NULL,
    end_ts      TEXT NOT NULL,
    UNIQUE(set_id, reservation_id)
);

-- 场次（开场后产生）
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY,
    reservation_id INTEGER NOT NULL UNIQUE REFERENCES reservations(id),
    table_id    INTEGER NOT NULL REFERENCES tables(id),
    member_code TEXT NOT NULL REFERENCES members(code),
    start_ts    TEXT NOT NULL,
    planned_end TEXT NOT NULL,
    actual_end  TEXT,
    status      TEXT NOT NULL
        CHECK (status IN ('PLAYING','EXTENDED','CHECKING','CLOSED')),
    rate_30m    REAL NOT NULL,
    tea_deposit REAL NOT NULL DEFAULT 0,
    minutes_used INTEGER,                  -- 散场时实际分钟
    total_fee   REAL,
    damage_fee  REAL NOT NULL DEFAULT 0,
    refund      REAL NOT NULL DEFAULT 0,   -- 押金应退
    compensated REAL NOT NULL DEFAULT 0,   -- 实际赔付净额
    settlement_note TEXT
);

-- 续时段
CREATE TABLE IF NOT EXISTS extensions (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    old_end     TEXT NOT NULL,
    new_end     TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    accepted    INTEGER NOT NULL DEFAULT 1,
    reject_reason TEXT
);

-- 开场/散场清点主记录（reservation+phase 唯一）
CREATE TABLE IF NOT EXISTS checks_master (
    id          INTEGER PRIMARY KEY,
    reservation_id INTEGER NOT NULL REFERENCES reservations(id),
    session_id  INTEGER REFERENCES sessions(id),
    phase       TEXT NOT NULL CHECK (phase IN ('OPEN','CLOSE')),
    done_at     TEXT NOT NULL,
    UNIQUE(reservation_id, phase)
);

-- 逐格清点结果：缺件位置以 present=0 保留空框
CREATE TABLE IF NOT EXISTS check_items (
    id          INTEGER PRIMARY KEY,
    check_id    INTEGER NOT NULL REFERENCES checks_master(id),
    set_id      INTEGER NOT NULL REFERENCES sets_kit(id),
    slot_no     INTEGER NOT NULL,
    label       TEXT NOT NULL,
    present     INTEGER NOT NULL,          -- 1 在位 0 缺失
    damage_note TEXT,
    UNIQUE(check_id, set_id, slot_no)
);

-- 事件日志：迟到释放/换桌/少件/泼洒/故障/借件/续时冲突/赔付争议
CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    reservation_id INTEGER REFERENCES reservations(id),
    table_id    INTEGER REFERENCES tables(id),
    set_id      INTEGER REFERENCES sets_kit(id),
    detail      TEXT NOT NULL,
    amount      REAL,
    resolved    INTEGER NOT NULL DEFAULT 0,
    approver    TEXT                        -- 社区负责人（减免批准）
);

-- 损坏赔付
CREATE TABLE IF NOT EXISTS damages (
    id          INTEGER PRIMARY KEY,
    reservation_id INTEGER NOT NULL REFERENCES reservations(id),
    set_id      INTEGER REFERENCES sets_kit(id),
    slot_no     INTEGER,
    reason      TEXT NOT NULL,             -- 少件/茶水泼洒/设备故障
    amount      REAL NOT NULL,
    waived      INTEGER NOT NULL DEFAULT 0,
    approver    TEXT,
    photo_path  TEXT,
    status      TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING','DISPUTED','APPROVED','WAIVED','PAID')),
    created_at  TEXT NOT NULL
);

-- 退款流水（幂等键：reservation+类型 唯一）
CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY,
    idem_key    TEXT NOT NULL UNIQUE,      -- refund:<res_id> / pay:<dam_id>
    reservation_id INTEGER NOT NULL REFERENCES reservations(id),
    kind        TEXT NOT NULL CHECK (kind IN ('REFUND','DAMAGE_PAY')),
    amount      REAL NOT NULL,
    status      TEXT NOT NULL,             -- SUCCESS / FAILED（模拟网关）
    created_at  TEXT NOT NULL
);

-- 清洁任务
CREATE TABLE IF NOT EXISTS cleanings (
    id          INTEGER PRIMARY KEY,
    table_id    INTEGER NOT NULL REFERENCES tables(id),
    session_id  INTEGER NOT NULL REFERENCES sessions(id),
    created_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'WAITING'
        CHECK (status IN ('WAITING','DOING','DONE'))
);

-- 跨场借件登记
CREATE TABLE IF NOT EXISTS kit_loans (
    id          INTEGER PRIMARY KEY,
    set_id      INTEGER NOT NULL REFERENCES sets_kit(id),
    slot_no     INTEGER NOT NULL,
    from_reservation_id INTEGER REFERENCES reservations(id),
    to_reservation_id   INTEGER REFERENCES reservations(id),
    ts          TEXT NOT NULL,
    returned    INTEGER NOT NULL DEFAULT 0
);
"""

_lock = threading.RLock()


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30,
                           isolation_level=None)  # autocommit；事务全部显式 BEGIN/COMMIT
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with _lock, conn:
        conn.executescript(SCHEMA)


def tx_lock():
    """供服务层使用的进程内互斥锁（串行化写操作）。"""
    return _lock
