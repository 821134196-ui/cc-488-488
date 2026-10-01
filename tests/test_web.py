# -*- coding: utf-8 -*-
"""Web 端到端冒烟测试：真实 HTTP 服务器 + 演示数据。"""
from __future__ import annotations

import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from chessroom.db import Database
from run import build_demo
from chessroom.web.server import AppState, Handler


@pytest.fixture()
def server(tmp_path):
    db = Database(str(tmp_path / "web.db"))
    ids = build_demo(db)
    state = AppState(db, scheduler=False)
    Handler.state = state
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", state, ids
    httpd.shutdown()


def _get(base, path, cookie=None):
    req = urllib.request.Request(base + path)
    if cookie:
        req.add_header("Cookie", cookie)
    with urllib.request.urlopen(req) as r:
        return r.status, r.read().decode("utf-8")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _post(base, path, data, cookie=None):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(base + path, data=body, method="POST")
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with _OPENER.open(req) as r:
            return r.status, r.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Location", "")


class TestWeb:
    def test_pages_render(self, server):
        base, _, _ = server
        for path in ("/", "/calendar", "/book", "/inventory", "/cleaning", "/review"):
            status, html = _get(base, path)
            assert status == 200 and "社区棋牌室" in html

    def test_booking_flow_and_checklist(self, server):
        base, _, _ = server
        start = time.strftime("%Y-%m-%dT%H:%M", time.localtime(time.time() + 86400))
        end = time.strftime("%Y-%m-%dT%H:%M", time.localtime(time.time() + 86400 + 3600))
        status, loc = _post(base, "/api/book", {
            "member_code": "M001", "table_id": "4", "kit_id": "2",
            "start": start, "end": end, "tea_deposit": "50"})
        assert status == 303 and "/r/" in loc
        rid = loc.split("/r/")[1].split("?")[0]
        _, html = _get(base, f"/open/{rid}")
        # 象棋：16+16+1棋盘+1计时器 = 34 个清点格
        import re
        assert len(re.findall(r'class="slot(?: |")', html)) == 34

    def test_director_only_relieve(self, server):
        base, _, ids = server
        rid = ids["shortage"]
        status, loc = _post(base, f"/api/damage-resolve/{rid}",
                            {"action": "relieve", "discount": "30"})
        assert "社区负责人" in urllib.parse.unquote(loc)
        status, loc = _post(base, f"/api/damage-resolve/{rid}",
                            {"action": "relieve", "discount": "30"},
                            cookie="role=director")
        assert status == 303 and "review" in loc

    def test_extend_conflict_rejected_and_logged(self, server):
        """进行中场次续时撞上后续预约：HTTP 拒绝并在事件流留痕。"""
        base, state, ids = server
        svc = state.svc
        day0 = int(time.mktime(time.strptime(
            time.strftime("%Y-%m-%d"), "%Y-%m-%d"))) + 20 * 3600
        r1 = svc.book("M001", 1, 1, day0, day0 + 3600, 50)["reservation"]
        svc.seat(r1["id"], now=day0)
        svc.book("M002", 1, 1, day0 + 3600, day0 + 7200, 50)
        status, loc = _post(base, f"/api/extend/{r1['id']}", {"minutes": "90"})
        assert status == 303 and "续时冲突" in urllib.parse.unquote(loc)
        ev = svc.db.one(
            "SELECT COUNT(*) n FROM events WHERE reservation_id=? AND kind='extend_conflict'",
            (r1["id"],))["n"]
        assert ev == 1
