"""HTTP 服务：JSON API + 静态页面，零第三方依赖。"""
from __future__ import annotations

import json
import mimetypes
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .seed import seed_catalog, seed_demo
from .service import Service, ServiceError
from .scheduler import Scheduler

WEB_DIR = Path(__file__).parent / "web"


class App:
    def __init__(self, svc: Service, scheduler: Scheduler | None = None) -> None:
        self.svc = svc
        self.scheduler = scheduler


def make_handler(app: App):
    svc = app.svc

    class Handler(BaseHTTPRequestHandler):
        server_version = "ChessRoom/1.0"

        def log_message(self, fmt, *args):  # 静默默认日志
            pass

        # ------------------------------------------------------------ helpers
        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length", 0))
            if not n:
                return {}
            raw = self.rfile.read(n)
            try:
                return json.loads(raw.decode())
            except json.JSONDecodeError:
                raise ServiceError("请求体不是合法 JSON")

        def _error(self, exc: Exception):
            self._json({"ok": False, "error": str(exc)}, 400)

        # ------------------------------------------------------------ routing
        def do_GET(self):
            try:
                url = urlparse(self.path)
                path, qs = url.path, parse_qs(url.query)
                if path == "/api/state":
                    return self._json({"ok": True, **svc.state()})
                m = re.fullmatch(r"/api/reservations/(\d+)/slots", path)
                if m:
                    return self._json({"ok": True, "slots": svc.slots_payload(int(m.group(1)))})
                m = re.fullmatch(r"/photos/(.+)", path)
                if m:
                    return self._photo(m.group(1))
                if path.startswith("/api/"):
                    return self._json({"ok": False, "error": "未知接口"}, 404)
                return self._static(path)
            except Exception as exc:
                return self._error(exc)

        def do_POST(self):
            try:
                path = urlparse(self.path).path
                b = self._body()
                result = self._dispatch(path, b)
                self._json({"ok": True, **(result or {})})
            except ServiceError as exc:
                self._error(exc)
            except Exception as exc:
                self._json({"ok": False, "error": f"服务器错误: {exc}"}, 500)

        def _dispatch(self, path: str, b: dict) -> dict:
            s = svc
            if path == "/api/reservations":
                rid = s.create_reservation(
                    int(b["table_id"]), b["member_code"], b["start"], b["end"],
                    b["game"], [int(x) for x in b.get("kit_ids", [])],
                    float(b.get("tea_deposit", 0)), b.get("note", ""))
                return {"reservation_id": rid}

            m = re.fullmatch(r"/api/reservations/(\d+)/cancel", path)
            if m:
                s.cancel_reservation(int(m.group(1))); return {}

            m = re.fullmatch(r"/api/reservations/(\d+)/seat", path)
            if m:
                return {"session_id": s.seat(int(m.group(1)))}

            m = re.fullmatch(r"/api/reservations/(\d+)/open-check", path)
            if m:
                return s.submit_open_check(int(m.group(1)), b.get("marks", {}))

            m = re.fullmatch(r"/api/reservations/(\d+)/extend", path)
            if m:
                return s.extend(int(m.group(1)), int(b["minutes"]))

            m = re.fullmatch(r"/api/reservations/(\d+)/move", path)
            if m:
                return s.move_table(int(m.group(1)), int(b["target_table_id"]),
                                    b.get("approver", ""))

            m = re.fullmatch(r"/api/reservations/(\d+)/close-check", path)
            if m:
                return s.submit_close_check(
                    int(m.group(1)), b.get("marks", {}),
                    bool(b.get("spill")), float(b.get("spill_amount", 20)),
                    bool(b.get("device_fault")), b.get("fault_note", ""))

            m = re.fullmatch(r"/api/reservations/(\d+)/refund-retry", path)
            if m:
                return s.retry_refund(int(m.group(1)))

            m = re.fullmatch(r"/api/damages/(\d+)/resolve", path)
            if m:
                s.resolve_damage(int(m.group(1)), b["action"], b.get("approver", ""))
                return {}

            m = re.fullmatch(r"/api/damages/(\d+)/photo", path)
            if m:
                rel = s.attach_photo(int(m.group(1)), b["filename"], b["data"])
                return {"photo_path": rel}

            m = re.fullmatch(r"/api/cleanings/(\d+)/(start|finish)", path)
            if m:
                cid = int(m.group(1))
                (s.start_cleaning if m.group(2) == "start" else s.finish_cleaning)(cid)
                return {}

            m = re.fullmatch(r"/api/tables/(\d+)/repair", path)
            if m:
                s.repair_table(int(m.group(1)), b.get("approver", "")); return {}

            if path == "/api/loans":
                lid = s.loan_item(int(b["set_id"]), int(b["slot_no"]),
                                  int(b["to_reservation_id"]))
                return {"loan_id": lid}

            m = re.fullmatch(r"/api/loans/(\d+)/return", path)
            if m:
                s.return_loan(int(m.group(1))); return {}

            if path == "/api/admin/tick":
                if app.scheduler:
                    app.scheduler.run_once_now()
                else:
                    s.tick()
                return {}

            if path == "/api/admin/seed":
                return {"ids": seed_demo(s)}

            raise ServiceError(f"未知接口: {path}")

        # ------------------------------------------------------------ assets
        def _static(self, path: str):
            if path in ("/", ""):
                path = "/index.html"
            fp = (WEB_DIR / path.lstrip("/")).resolve()
            if not str(fp).startswith(str(WEB_DIR.resolve())) or not fp.is_file():
                self.send_error(404, "Not Found")
                return
            ctype = mimetypes.guess_type(str(fp))[0] or "application/octet-stream"
            data = fp.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type",
                             ctype + ("; charset=utf-8" if ctype.startswith("text/")
                                      or "javascript" in ctype else ""))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _photo(self, name: str):
            fp = (svc.photo_dir / name).resolve()
            if not str(fp).startswith(str(svc.photo_dir.resolve())) or not fp.is_file():
                self.send_error(404, "Photo Not Found")
                return
            data = fp.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def serve(db_path: str, photo_dir: str, host="127.0.0.1", port=8000,
          demo=True, scheduler_interval=20.0) -> tuple[ThreadingHTTPServer, Scheduler]:
    svc = Service(db_path, photo_dir=photo_dir)
    seed_catalog(svc)
    if demo and svc.conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0:
        seed_demo(svc)
    scheduler = Scheduler(svc, scheduler_interval)
    scheduler.start()
    app = App(svc, scheduler)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    return httpd, scheduler
