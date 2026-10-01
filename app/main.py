"""本地启动入口。

用法：
    python3 -m app.main                # 首次启动自动生成全套演示样例
    python3 -m app.main --reset        # 清空数据库后重新生成样例
    python3 -m app.main --port 8080
"""
from __future__ import annotations

import argparse
from pathlib import Path

from .server import serve


def main() -> None:
    ap = argparse.ArgumentParser(description="社区棋牌室桌台预约与牌具清点系统")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="data/chessroom.db")
    ap.add_argument("--photos", default="data/photos")
    ap.add_argument("--reset", action="store_true", help="清空数据库后重新生成演示样例")
    ap.add_argument("--no-demo", action="store_true", help="不自动生成演示样例")
    args = ap.parse_args()

    if args.reset:
        for p in Path(args.db).parent.glob(Path(args.db).name + "*"):
            p.unlink()
        print(f"已清空 {args.db}*")

    httpd, scheduler = serve(args.db, args.photos, host=args.host, port=args.port,
                             demo=not args.no_demo)
    print("=" * 64)
    print("  社区棋牌室 · 桌台预约与牌具清点系统")
    print(f"  地址：http://{args.host}:{args.port}")
    print("  演示样例：续时冲突 / 散场少件争议 / 临时换桌 / 迟到释放 / 借件")
    print("  Ctrl+C 退出")
    print("=" * 64)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        scheduler.stop()
        httpd.shutdown()


if __name__ == "__main__":
    main()
