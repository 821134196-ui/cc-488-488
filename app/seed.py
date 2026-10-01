"""演示数据：房间/桌台/牌具/会员，以及一条命令生成的样例场次。

样例覆盖：续时冲突、散场少件、临时换桌（同时有迟到释放/泼洒/借件/争议）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .service import Service, now_iso, parse

# 固定数量格模板
GO_BOARD = [("黑棋", 181), ("白棋", 180)]          # 361 子
XIANGQI = [("红子", 16), ("黑子", 16)]              # 32 子
MAHJONG = [("万子", 36), ("条子", 36), ("筒子", 36),
           ("风牌", 16), ("箭牌", 12), ("花牌", 8)]  # 144 张
POKER = [("牌张", 52), ("大小王", 2)]               # 54 张


def build_kit(conn, name, game, spec, home=None, timer=None):
    sid = conn.execute(
        "INSERT INTO sets_kit(name,game,home_table_id,timer_sn) VALUES(?,?,?,?)",
        (name, game, home, timer)).lastrowid
    n = 0
    for prefix, count in spec:
        for i in range(1, count + 1):
            n += 1
            label = f"{prefix}{i:03d}" if count > 20 else f"{prefix}{i}"
            conn.execute(
                "INSERT INTO kit_items(set_id,slot_no,label) VALUES(?,?,?)", (sid, n, label))
    if timer:
        n += 1
        conn.execute("INSERT INTO kit_items(set_id,slot_no,label) VALUES(?,?,?)",
                     (sid, n, f"计时器 {timer}"))
    return sid


MEMBERS = [
    ("M01", "青竹", "138-0000-0001", 500, "常客，围棋 3 段"),
    ("M02", "老白", "138-0000-0002", 320, "象棋爱好者"),
    ("M03", "四喜", "138-0000-0003", 200, "麻将局召集人"),
    ("M04", "阿乔", "138-0000-0004", 150, "扑克"),
    ("M05", "苏叶", "138-0000-0005", 80, "新会员"),
    ("M06", "闻笛", "138-0000-0006", 420, "喜欢安静包间"),
]

# (room, x, y, label, kind, seats, rate, tx, ty) — 坐标单位米，保持真实间距
TABLES = [
    ("大厅", 1.2, 1.5, "A1", "围棋", 4, 15, 0.0, 0.0),
    ("大厅", 4.2, 1.5, "A2", "围棋", 4, 15, 3.0, 0.0),
    ("大厅", 1.2, 4.5, "A3", "象棋", 2, 12, 0.0, 3.0),
    ("大厅", 4.2, 4.5, "A4", "象棋", 2, 12, 3.0, 3.0),
    ("大厅", 8.0, 1.5, "A5", "麻将", 4, 25, 6.8, 0.0),
    ("大厅", 8.0, 4.5, "A6", "麻将", 4, 25, 6.8, 3.0),
    ("竹影包间", 1.2, 1.5, "B1", "扑克", 6, 30, 0.0, 0.0),
    ("竹影包间", 4.5, 1.5, "B2", "麻将", 4, 28, 3.3, 0.0),
    ("听雨包间", 1.2, 1.5, "C1", "围棋", 2, 20, 0.0, 0.0),
]


def seed_catalog(svc: Service) -> dict:
    conn = svc.conn
    if conn.execute("SELECT COUNT(*) FROM tables").fetchone()[0]:
        return existing_ids(conn)
    room_ids = {}
    conn.execute("BEGIN")
    try:
        for m in MEMBERS:
            conn.execute("INSERT INTO members(code,name,phone,credit,note) VALUES(?,?,?,?,?)", m)
        rooms = {}
        for name, gx, gy, *_ in TABLES:
            if name not in rooms:
                rid = conn.execute("INSERT INTO rooms(name,x,y) VALUES(?,?,?)",
                                   (name, gx - 1.2, gy - 1.5)).lastrowid
                rooms[name] = rid
        tid = {}
        for room, gx, gy, label, kind, seats, rate, tx, ty in TABLES:
            tid[label] = conn.execute(
                "INSERT INTO tables(room_id,label,x,y,kind,seats,rate_30m) VALUES(?,?,?,?,?,?,?)",
                (rooms[room], label, tx, ty, kind, seats, rate)).lastrowid
        kit = {}
        kit["wei_a1"] = build_kit(conn, "云子围棋套装·A1", "围棋", GO_BOARD, tid["A1"], "T-01")
        kit["wei_a2"] = build_kit(conn, "云子围棋套装·A2", "围棋", GO_BOARD, tid["A2"], "T-02")
        kit["xiang_a3"] = build_kit(conn, "象棋套装·A3", "象棋", XIANGQI, tid["A3"], "T-03")
        kit["xiang_a4"] = build_kit(conn, "象棋套装·A4", "象棋", XIANGQI, tid["A4"], "T-04")
        kit["mj_a5"] = build_kit(conn, "竹骨麻将套装·A5", "麻将", MAHJONG, tid["A5"], "T-05")
        kit["mj_a6"] = build_kit(conn, "竹骨麻将套装·A6", "麻将", MAHJONG, tid["A6"], "T-06")
        kit["poker_b1"] = build_kit(conn, "扑克套装·B1", "扑克", POKER, tid["B1"], "T-07")
        kit["mj_b2"] = build_kit(conn, "竹骨麻将套装·B2", "麻将", MAHJONG, tid["B2"], "T-08")
        kit["wei_c1"] = build_kit(conn, "蛤碁石围棋套装·C1", "围棋", GO_BOARD, tid["C1"], "T-09")
        conn.execute("INSERT INTO sets_kit(name,game,home_table_id) VALUES(?,?,?)",
                     ("备用扑克（共享柜）", "扑克", None))
        sid = conn.execute("SELECT id FROM sets_kit WHERE name='备用扑克（共享柜）'").lastrowid
        n = 0
        for prefix, count in POKER:
            for i in range(1, count + 1):
                n += 1
                conn.execute("INSERT INTO kit_items(set_id,slot_no,label) VALUES(?,?,?)",
                             (sid, n, f"{prefix}{i}"))
        kit["poker_spare"] = sid
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"tables": tid, "kits": kit}


def existing_ids(conn) -> dict:
    tid = {r["label"]: r["id"] for r in conn.execute("SELECT id,label FROM tables")}
    kits = {r["name"]: r["id"] for r in conn.execute("SELECT id,name FROM sets_kit")}
    return {"tables": tid,
            "kits": {"wei_a1": kits.get("云子围棋套装·A1"), "wei_a2": kits.get("云子围棋套装·A2"),
                     "xiang_a3": kits.get("象棋套装·A3"), "xiang_a4": kits.get("象棋套装·A4"),
                     "mj_a5": kits.get("竹骨麻将套装·A5"), "mj_a6": kits.get("竹骨麻将套装·A6"),
                     "poker_b1": kits.get("扑克套装·B1"), "mj_b2": kits.get("竹骨麻将套装·B2"),
                     "wei_c1": kits.get("蛤碁石围棋套装·C1"),
                     "poker_spare": kits.get("备用扑克（共享柜）")}}


def seed_demo(svc: Service, base: datetime | None = None) -> dict:
    """生成完整样例故事线。

    历史场次通过可变时钟“回放”：开场时把时钟拨到开场时刻，
    散场/清洁拨到结束时刻，全部完成后恢复真实时间。
    """
    if base is None:
        base = svc.now().replace(minute=0, second=0, microsecond=0)
    ids = seed_catalog(svc)
    t, k = ids["tables"], ids["kits"]
    created = {}

    def ts(dt):
        return now_iso(dt)

    fake = [base]
    real_clock = svc._clock
    svc._clock = lambda: fake[0]

    def jump(dt):
        fake[0] = dt

    n = base
    # 1) 未来预约若干（日历/平面图展示）
    created["r_future_go"] = svc.create_reservation(
        t["C1"], "M06", ts(n + timedelta(hours=2)), ts(n + timedelta(hours=4)),
        "围棋", [k["wei_c1"]], tea_deposit=50, note="需要安静")
    created["r_future_mj"] = svc.create_reservation(
        t["A6"], "M03", ts(n + timedelta(hours=1, minutes=30)),
        ts(n + timedelta(hours=3, minutes=30)), "麻将", [k["mj_a6"]], tea_deposit=80)
    created["r_future_poker"] = svc.create_reservation(
        t["B1"], "M04", ts(n + timedelta(days=1, hours=1)),
        ts(n + timedelta(days=1, hours=3)), "扑克", [k["poker_b1"]], tea_deposit=40)

    # 2) 迟到释放样例：20 分钟前开场、未到场，时钟回到现在执行释放
    r_late = svc.create_reservation(
        t["A4"], "M05", ts(n - timedelta(minutes=20)), ts(n + timedelta(hours=1, minutes=40)),
        "象棋", [k["xiang_a4"]], tea_deposit=30)
    created["r_late"] = r_late
    jump(n)
    svc.release_late()

    # 3) 续时冲突样例：A1 场次进行中，结束点紧贴后续预约，续 60 分钟必被拒
    r_conf_base = n - timedelta(minutes=30)
    r_conf_end = n + timedelta(minutes=60)
    jump(r_conf_base)
    r_conf = svc.create_reservation(
        t["A1"], "M01", ts(r_conf_base), ts(r_conf_end), "围棋",
        [k["wei_a1"]], tea_deposit=50)
    created["r_ext_conflict"] = r_conf
    r_next = svc.create_reservation(
        t["A1"], "M06", ts(r_conf_end), ts(r_conf_end + timedelta(hours=2)),
        "围棋", [k["wei_a1"]], tea_deposit=50)
    created["r_next"] = r_next
    svc.seat(r_conf)
    svc.submit_open_check(r_conf, {})
    jump(n)
    try:
        svc.extend(r_conf, 60)
    except Exception:
        pass  # 拒绝结果已落 extensions / incidents

    # 4) 临时换桌样例：A5 麻将开场后因空调异响，管理员调至 A6
    #    （A6 的未来预约在 90 分钟后，调入场次在其开始前结束）
    r_move_base = n - timedelta(minutes=45)
    jump(r_move_base)
    r_move = svc.create_reservation(
        t["A5"], "M03", ts(r_move_base), ts(n + timedelta(hours=1)),
        "麻将", [k["mj_a5"]], tea_deposit=100, note="靠空调位有异响")
    created["r_move"] = r_move
    svc.seat(r_move)
    svc.submit_open_check(r_move, {})
    jump(n - timedelta(minutes=10))
    moved = svc.move_table(r_move, t["A6"], "管理员·林姐")
    created["r_move_new"] = moved["new_reservation_id"]

    # 5) 散场少件样例：A3 象棋完整开场 → 散场少一枚红子 + 茶水泼洒，会员申诉
    jump(n - timedelta(hours=2))
    r_miss = svc.create_reservation(
        t["A3"], "M02", ts(n - timedelta(hours=2)), ts(n - timedelta(minutes=10)),
        "象棋", [k["xiang_a3"]], tea_deposit=40)
    created["r_missing"] = r_miss
    svc.seat(r_miss)
    svc.submit_open_check(r_miss, {})
    jump(n - timedelta(minutes=10))
    marks = {str(k["xiang_a3"]): {"1": {"present": False}}}  # 红子001 缺失
    close_res = svc.submit_close_check(r_miss, marks, spill=True, spill_amount=20)
    created["missing_damages"] = close_res["damage_ids"]
    if close_res["damage_ids"]:
        svc.resolve_damage(close_res["damage_ids"][0], "dispute")  # 赔付争议挂起

    # 6) 跨场借件样例：为 A2 未来场次登记从共享柜借牌
    jump(n)
    r_borrow = svc.create_reservation(
        t["A2"], "M01", ts(n + timedelta(hours=5)), ts(n + timedelta(hours=7)),
        "围棋", [k["wei_a2"]], tea_deposit=30)
    created["r_borrow"] = r_borrow
    created["loan"] = svc.loan_item(k["poker_spare"], 1, r_borrow)

    # 7) 已完成并清洁的历史场（结算复核 / 清洁队列展示）
    jump(n - timedelta(hours=5))
    r_done = svc.create_reservation(
        t["B2"], "M03", ts(n - timedelta(hours=5)), ts(n - timedelta(hours=3, minutes=1)),
        "麻将", [k["mj_b2"]], tea_deposit=60)
    created["r_done"] = r_done
    svc.seat(r_done)
    svc.submit_open_check(r_done, {})
    jump(n - timedelta(hours=3))
    svc.submit_close_check(r_done, {})
    cid = svc.conn.execute(
        "SELECT c.id FROM cleanings c JOIN sessions s ON s.id=c.session_id "
        "WHERE s.reservation_id=? ORDER BY c.id DESC LIMIT 1", (r_done,)).fetchone()["id"]
    svc.start_cleaning(cid)
    svc.finish_cleaning(cid)
    created["cleaning_done"] = cid

    jump(base)
    svc._clock = real_clock
    svc.notify("样例", "演示样例已生成：续时冲突 / 散场少件争议 / 临时换桌 / 迟到释放 / 借件 / 已清洁历史场")
    return created
