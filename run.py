# -*- coding: utf-8 -*-
"""本地启动入口。

    python3 run.py                # 重建演示数据（含续时冲突/少件/换桌样例）并启动
    python3 run.py --clean        # 只保留目录数据，不生成样例
    python3 run.py --port 8080
"""
from __future__ import annotations

import argparse
import base64
import os
import sys
import time
from datetime import datetime

from chessroom.catalog import seed_catalog
from chessroom.db import Database
from chessroom.services import Actor
from chessroom.web.server import build_server

# 1x1 占位 JPEG（损坏照片演示）
_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAP//////////////////////////////////////"
    "////////////////////////////////////////////2wBDAf//////////////////////"
    "//////////////////////////////////////////////////////wAARCAABAAEDASIA"
    "AAEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAn/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oA"
    "CAEBAAA/AKf/2Q=="
)


def _at(day0: int, h: int, m: int = 0) -> int:
    return day0 + h * 3600 + m * 60


def build_demo(db: Database) -> dict:
    """生成覆盖各类异常的演示样例，时间锚定今天。"""
    seed_catalog(db, reset=True)
    from chessroom.services import ChessRoomService
    svc = ChessRoomService(db)
    admin = Actor("管理员老郑", "admin")

    lt = time.localtime()
    day0 = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))

    ids: dict[str, int] = {}

    # 1) 迟到释放：A3 象棋 08:00 场，08:20 仍未开场
    r = svc.book("M003", 3, 2, _at(day0, 8), _at(day0, 9), 50, "迟到释放样例")
    ids["late"] = r["reservation"]["id"]
    svc.release_late(_at(day0, 8, 20))

    # 2) 上午正常场 → 散场 → 清洁完成
    r = svc.book("M005", 4, 2, _at(day0, 9), _at(day0, 10), 50, "清洁完成样例")
    rid = r["reservation"]["id"]
    ids["clean_done"] = rid
    svc.seat(rid, now=_at(day0, 9))
    svc.settle(rid, tea_charge=20, now=_at(day0, 10))
    cid = svc.db.one("SELECT id FROM cleaning WHERE reservation_id=?", (rid,))["id"]
    svc.start_cleaning(cid)
    svc.finish_cleaning(cid, now=_at(day0, 10, 10))

    # 3) 续时冲突：A1 09:00-10:00 场，后续 11:00 有预约
    r = svc.book("M001", 1, 1, _at(day0, 9), _at(day0, 10), 50, "续时样例")
    rid_ext = r["reservation"]["id"]
    ids["extend"] = rid_ext
    svc.seat(rid_ext, now=_at(day0, 9))
    r2 = svc.book("M002", 1, 1, _at(day0, 11), _at(day0, 12), 50, "续时的后续预约")
    ids["extend_next"] = r2["reservation"]["id"]
    try:  # 续到 11:30 必须被拒绝
        svc.extend(rid_ext, _at(day0, 11, 30))
    except Exception:
        pass
    svc.extend(rid_ext, _at(day0, 11))          # 端点相接允许
    svc.settle(rid_ext, now=_at(day0, 11))
    cid = svc.db.one("SELECT id FROM cleaning WHERE reservation_id=?", (rid_ext,))["id"]
    svc.start_cleaning(cid)
    svc.finish_cleaning(cid, now=_at(day0, 11, 5))

    # 4) 待清洁样例：B4 扑克上午场散场后未清洁（阻断新场）
    r = svc.book("M003", 8, 4, _at(day0, 9, 30), _at(day0, 10, 30), 50, "待清洁样例")
    rid = r["reservation"]["id"]
    ids["dirty"] = rid
    svc.seat(rid, now=_at(day0, 9, 30))
    svc.settle(rid, now=_at(day0, 10, 30))

    # 5) 少件 + 赔付争议：B1 麻将下午场
    r = svc.book("M004", 5, 3, _at(day0, 13), _at(day0, 15), 120, "少件与赔付争议样例")
    rid_short = r["reservation"]["id"]
    ids["shortage"] = rid_short
    # 开场即少一枚风箭牌（第 28 格空框）
    svc.seat(rid_short, present={"风箭牌": {28: 0}}, now=_at(day0, 13))
    svc.report_event(rid_short, "tea_spill", "茶水泼洒在桌边地垫，已清理")
    svc.report_event(rid_short, "device_fault", "呼叫铃接触不良，已报修")

    # 6) 跨场借件：B3 扑克场向麻将场借骰子 1 枚
    r = svc.book("M001", 7, 4, _at(day0, 13, 30), _at(day0, 15, 30), 50, "跨场借件样例")
    rid_loan = r["reservation"]["id"]
    ids["loan"] = rid_loan
    svc.seat(rid_loan, now=_at(day0, 13, 30))
    svc.loan_kit(rid_short, rid_loan, "骰子", 1, now=_at(day0, 13, 40))

    # 麻将场散场：万子牌缺 1 枚 + 借出骰子未还 + 牌面磕裂赔付争议
    photo_rel = svc.save_photo(_JPEG, "jpg")
    svc.settle(
        rid_short,
        present={"万子牌": {36: 0}},
        tea_charge=30,
        damages=[{"desc": "一张万子牌边角磕裂", "amount": 80, "photo": photo_rel}],
        disputed=True,
        now=_at(day0, 15),
    )

    # 7) 临时换桌：A2 围棋下午场，管理员调至 A1（A1 已清洁）
    r = svc.book("M002", 2, 1, _at(day0, 15), _at(day0, 17), 50, "临时换桌样例")
    rid_chg = r["reservation"]["id"]
    ids["change"] = rid_chg
    svc.seat(rid_chg, now=_at(day0, 15))
    svc.change_table(rid_chg, 1, admin, now=_at(day0, 15, 10))

    return ids


def main() -> int:
    ap = argparse.ArgumentParser(description="社区棋牌室桌台预约与牌具清点系统")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    ap.add_argument("--clean", action="store_true", help="不生成演示样例")
    ap.add_argument("--no-scheduler", action="store_true", help="关闭迟到释放/提醒后台任务")
    args = ap.parse_args()

    db = Database()
    if args.clean:
        seed_catalog(db)
        print("[init] 已确保目录数据")
    else:
        ids = build_demo(db)
        print("[demo] 已生成样例场次：")
        labels = {
            "late": "迟到释放", "clean_done": "清洁完成", "extend": "续时(冲突已记录)",
            "extend_next": "续时的后续预约", "dirty": "待清洁阻断",
            "shortage": "少件+赔付争议", "loan": "跨场借件未还", "change": "管理员换桌",
        }
        for k, v in ids.items():
            print(f"  - {labels[k]:14s} 场次 #{v}")

    httpd = build_server(args.host, args.port, db_path=db.path,
                         scheduler=not args.no_scheduler)
    print(f"\n♟ 社区棋牌室已启动：http://{args.host}:{args.port}")
    print("  平面图 / 日历 / 预约 / 库存 / 清洁 / 复核；右上角可切换 前台/管理员/社区负责人")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n再见")
    return 0


if __name__ == "__main__":
    sys.exit(main())
