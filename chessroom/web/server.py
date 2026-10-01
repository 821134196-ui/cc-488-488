# -*- coding: utf-8 -*-
"""HTTP 服务器：路由表单到业务服务，页面由 views 渲染。"""
from __future__ import annotations

import base64
import http.cookies
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from . import views
from ..catalog import seed_catalog
from ..db import Database
from ..scheduler import Scheduler
from ..services import Actor, ChessRoomService, ServiceError

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

_MIME = {".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8",
         ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml"}


def parse_dt(s: str) -> int:
    return int(time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M")))


class AppState:
    def __init__(self, db: Database, scheduler: bool = False):
        seed_catalog(db)
        self.svc = ChessRoomService(db)
        self.scheduler = Scheduler(self.svc) if scheduler else None
        if self.scheduler:
            self.scheduler.start()


class Handler(BaseHTTPRequestHandler):
    state: AppState = None  # 由 main 注入

    def log_message(self, fmt, *args):  # 静默默认日志
        pass

    # ------------------------------------------------------------ 基础工具

    def _cookies(self) -> dict:
        raw = self.headers.get("Cookie", "")
        try:
            return {k: m.value for k, m in http.cookies.SimpleCookie(raw).items()}
        except http.cookies.CookieError:
            return {}

    def _actor(self) -> Actor:
        role = self._cookies().get("role", "staff")
        name = {"staff": "前台小周", "admin": "管理员老郑", "director": "社区负责人陈主任"}
        return Actor(name.get(role, "前台小周"), role)

    def _send(self, status: int, body: str | bytes, ctype: str = "text/html; charset=utf-8",
              extra: dict | None = None) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, path: str, msg: str = "", err: str = "") -> None:
        q = ""
        if msg or err:
            q = "?" + quote(("msg=" + msg) if msg else ("err=" + err), safe="=&")
        self._send(303, b"", extra={"Location": path + q})

    def _form(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def _flash(self) -> tuple | None:
        q = parse_qs(urlparse(self.path).query)
        if q.get("msg"):
            return ("ok", q["msg"][0])
        if q.get("err"):
            return ("err", q["err"][0])
        return None

    # ------------------------------------------------------------------ GET

    def do_GET(self) -> None:
        u = urlparse(self.path)
        p, svc, role = u.path, self.state.svc, self._cookies().get("role", "staff")
        try:
            if p == "/":
                self._send(200, views.page_plan(svc, role))
            elif p == "/calendar":
                q = parse_qs(u.query)
                if q.get("d"):
                    day0 = int(time.mktime(time.strptime(q["d"][0], "%Y-%m-%d")))
                else:
                    lt = time.localtime()
                    day0 = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))
                self._send(200, views.page_calendar(svc, day0, role))
            elif p == "/book":
                self._send(200, views.page_book(svc, role))
            elif p == "/inventory":
                self._send(200, views.page_inventory(svc, role))
            elif p == "/cleaning":
                self._send(200, views.page_cleaning(svc, role))
            elif p == "/review":
                self._send(200, views.page_review(svc, role))
            else:
                m = re.fullmatch(r"/r/(\d+)", p)
                if m:
                    self._send(200, views.page_reservation(svc, int(m.group(1)), role))
                    return
                m = re.fullmatch(r"/open/(\d+)", p)
                if m:
                    self._send(200, views.page_open(svc, int(m.group(1)), role))
                    return
                m = re.fullmatch(r"/timer/(\d+)", p)
                if m:
                    self._send(200, views.page_timer(svc, int(m.group(1)), role))
                    return
                m = re.fullmatch(r"/close/(\d+)", p)
                if m:
                    self._send(200, views.page_close(svc, int(m.group(1)), role))
                    return
                if p.startswith("/static/"):
                    self._static(p)
                elif p == "/photos" or p.startswith("/photos/"):
                    self._photo(p)
                else:
                    self._send(404, "<h2>404</h2>")
        except ServiceError as e:
            self._send(400, f"<h2>操作未完成</h2><p>{e}</p><p><a href='/'>返回</a></p>")

    def _static(self, p: str) -> None:
        name = os.path.normpath(p[len("/static/"):])
        path = os.path.join(STATIC_DIR, name)
        if not path.startswith(STATIC_DIR) or not os.path.isfile(path):
            self._send(404, "nope")
            return
        with open(path, "rb") as f:
            self._send(200, f.read(), _MIME.get(os.path.splitext(path)[1], "application/octet-stream"))

    def _photo(self, p: str) -> None:
        rel = os.path.normpath(p[len("/"):])
        path = os.path.abspath(os.path.join(os.path.dirname(self.state.svc.photo_dir), rel))
        if not path.startswith(os.path.abspath(self.state.svc.photo_dir)) or not os.path.isfile(path):
            self._send(404, "nope")
            return
        with open(path, "rb") as f:
            self._send(200, f.read(), _MIME.get(os.path.splitext(path)[1], "application/octet-stream"))

    # ----------------------------------------------------------------- POST

    def do_POST(self) -> None:
        p = urlparse(self.path).path
        f, svc = self._form(), self.state.svc
        try:
            if p == "/api/book":
                r = svc.book(
                    f["member_code"], int(f["table_id"]), int(f["kit_id"]),
                    parse_dt(f["start"]), parse_dt(f["end"]),
                    float(f["tea_deposit"]), f.get("note", ""), self._actor(),
                )
                self._redirect(f"/r/{r['reservation']['id']}", msg="预约已确认，桌台与牌具已锁定")
                return

            m = re.fullmatch(r"/api/seat/(\d+)", p)
            if m:
                rid = int(m.group(1))
                import json
                present = json.loads(f.get("present_json") or "{}")
                r = svc.seat(rid, present)
                note = "开场成功，房间已解锁"
                if r["missing"]:
                    note += "；开场少件已登记：" + "，".join(
                        f"{k}缺{v}" for k, v in r["missing"].items())
                self._redirect(f"/r/{rid}", msg=note)
                return

            m = re.fullmatch(r"/api/extend/(\d+)", p)
            if m:
                rid = int(m.group(1))
                res = svc._get("reservations", rid)
                new_end = (res["plan_end_ts"] or res["end_ts"]) + int(f["minutes"]) * 60
                svc.extend(rid, new_end)
                self._redirect(f"/timer/{rid}", msg=f"已续时 {f['minutes']} 分钟")
                return

            if p == "/api/change-table":
                rid = int(f["rid"])
                svc.change_table(rid, int(f["new_table_id"]), self._actor())
                self._redirect(f"/r/{rid}", msg="临时换桌完成，原桌已转入待清洁")
                return

            if p == "/api/event":
                rid = int(f["rid"])
                svc.report_event(rid, f["kind"], f.get("detail", ""))
                self._redirect(f"/r/{rid}", msg="现场事件已记录")
                return

            if p == "/api/loan":
                loan = svc.loan_kit(int(f["from_rid"]), int(f["to_rid"]),
                                    f["component"], int(f["qty"]))
                self._redirect(f"/r/{f['from_rid']}", msg=f"跨场借件已登记（#{loan['loan']['id']}）")
                return

            m = re.fullmatch(r"/api/settle/(\d+)", p)
            if m:
                rid = int(m.group(1))
                import json
                present = json.loads(f.get("present_json") or "{}")
                photo_rel = None
                b64 = f.get("photo_b64", "")
                if b64:
                    photo_rel = svc.save_photo(base64.b64decode(b64), "jpg")
                damages = []
                if f.get("d_desc") and float(f.get("d_amount") or 0) > 0:
                    damages.append({"desc": f["d_desc"], "amount": float(f["d_amount"]),
                                    "photo": photo_rel})
                r = svc.settle(rid, present, float(f.get("tea_charge") or 0),
                               damages, disputed=bool(f.get("disputed")))
                if r["complete"]:
                    self._redirect(f"/r/{rid}",
                                   msg=f"已散场：{r['minutes']} 分钟，计时费 ¥{r['time_fee']:.2f}，桌台进入待清洁")
                else:
                    self._redirect(f"/r/{rid}", err="散场少件或赔付争议：桌台牌具保持锁定，请到结算复核处理")
                return

            m = re.fullmatch(r"/api/damage-resolve/(\d+)", p)
            if m:
                rid = int(m.group(1))
                action = f.get("action", "pay")
                svc.resolve_damage(rid, action, self._actor(),
                                   float(f.get("discount") or 0))
                self._redirect("/review", msg="赔付已复核，桌台释放并进入待清洁")
                return

            m = re.fullmatch(r"/api/clean/start/(\d+)", p)
            if m:
                svc.start_cleaning(int(m.group(1)))
                self._redirect("/cleaning", msg="已开始清洁")
                return

            m = re.fullmatch(r"/api/clean/finish/(\d+)", p)
            if m:
                svc.finish_cleaning(int(m.group(1)))
                self._redirect("/cleaning", msg="清洁完成，桌台恢复可约")
                return

            self._send(404, "nope")
        except ServiceError as e:
            back = self.headers.get("Referer", "/")
            self._redirect(back.split("?")[0] if back else "/", err=str(e))


def build_server(host: str = "127.0.0.1", port: int = 8000,
                 db_path: str | None = None, scheduler: bool = True) -> ThreadingHTTPServer:
    db = Database(db_path)
    state = AppState(db, scheduler=scheduler)
    Handler.state = state
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.state = state  # type: ignore[attr-defined]
    return httpd
