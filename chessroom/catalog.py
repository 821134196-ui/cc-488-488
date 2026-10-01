# -*- coding: utf-8 -*-
"""演示目录：房间、桌台（含平面图真实坐标）、牌具套装（固定构成）、会员。

坐标单位为米，桌台坐标为桌心。平面图按 1 米 = 38 px 等比渲染，保持真实间距。
"""
from __future__ import annotations

from .db import Database, dumps, now_ts

ROOMS = [
    # name, x, y, w, h (米)
    ("清雅厅", 0.0, 0.0, 7.2, 6.0),
    ("对弈轩", 7.6, 0.0, 6.0, 6.0),
]

# room_index, code, kind, px, py, 每小时费用
TABLES = [
    (0, "A1", "围棋", 1.6, 1.5, 18.0),
    (0, "A2", "围棋", 5.4, 1.5, 18.0),
    (0, "A3", "象棋", 1.6, 4.6, 15.0),
    (0, "A4", "象棋", 5.4, 4.6, 15.0),
    (1, "B1", "麻将", 1.7, 1.8, 25.0),
    (1, "B2", "麻将", 4.5, 1.8, 25.0),
    (1, "B3", "扑克", 1.7, 4.5, 12.0),
    (1, "B4", "扑克", 4.5, 4.5, 12.0),
]

# 牌具套装。components 为固定件名与应有数量；散件逐格清点。
KITS = [
    # code, kind, name, components, deposit
    ("WQ-01", "围棋", "云子围棋套装",
     {"黑子": 180, "白子": 180, "棋盘": 1, "棋罐": 2}, 100),
    ("XQ-01", "象棋", "红黑象棋套装",
     {"红子": 16, "黑子": 16, "棋盘": 1}, 60),
    ("MJ-01", "麻将", "竹骨麻将套装",
     {"万子牌": 36, "条子牌": 36, "筒子牌": 36, "风箭牌": 28, "骰子": 2}, 120),
    ("PK-01", "扑克", "德州扑克套装",
     {"扑克牌": 108, "庄家码": 1, "切牌卡": 2}, 30),
]

# 会员代号（演示）
MEMBERS = [
    ("M001", "林砚秋", "138****0101", 500.0),
    ("M002", "沈伯安", "138****0102", 320.0),
    ("M003", "苏婉清", "138****0103", 88.0),
    ("M004", "周慕白", "138****0104", 260.0),
    ("M005", "社区活动室", "138****0000", 0.0),
]

TEA_DEPOSIT_DEFAULT = 50.0
LATE_GRACE_MIN = 15          # 迟到宽限分钟
REMIND_BEFORE_MIN = 10       # 开场前提醒
EXTEND_STEP_MIN = 30         # 常用续时步长


def seed_catalog(db: Database, reset: bool = False) -> None:
    with db.tx() as c:
        if reset:
            for t in (
                "payments", "bills", "cleaning", "kit_loans", "events", "checks",
                "resource_locks", "reservations", "kits", "tables", "rooms",
                "members", "door_logs",
            ):
                c.execute(f"DELETE FROM {t}")
        if c.execute("SELECT COUNT(*) n FROM rooms").fetchone()["n"]:
            return

        room_ids = []
        for name, x, y, w, h in ROOMS:
            cur = c.execute(
                "INSERT INTO rooms(name,x,y,w,h) VALUES (?,?,?,?,?)", (name, x, y, w, h)
            )
            room_ids.append(cur.lastrowid)

        for ri, code, kind, px, py, rate in TABLES:
            c.execute(
                "INSERT INTO tables(room_id,code,kind,px,py,rate) VALUES (?,?,?,?,?,?)",
                (room_ids[ri], code, kind, px, py, rate),
            )

        for code, kind, name, comps, deposit in KITS:
            c.execute(
                "INSERT INTO kits(code,kind,name,components,timer,deposit) VALUES (?,?,?,? ,1,?)",
                (code, kind, name, dumps(comps), deposit),
            )

        for code, name, phone, balance in MEMBERS:
            c.execute(
                "INSERT INTO members(code,name,phone,balance) VALUES (?,?,?,?)",
                (code, name, phone, balance),
            )
        _ = now_ts  # 保持导入引用
