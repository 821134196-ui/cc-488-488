# -*- coding: utf-8 -*-
"""服务端渲染的八个界面：平面图、日历、预约登记、开场清单、牌具库存、
计时端/续时、散场核验、清洁队列与结算复核。"""
from __future__ import annotations

import html
import time
from urllib.parse import urlencode

from ..db import loads
from ..services import ChessRoomService

H = html.escape
SCALE = 42  # 平面图 1 米 = 42 px

STATUS_CN = {
    "booked": "已预约", "seated": "进行中", "settled": "已散场",
    "released": "已释放", "held": "待赔付", "cancelled": "已取消",
}
EVENT_CN = {
    "book": "预约确认", "remind": "开场提醒", "late_release": "迟到释放",
    "change_table": "临时换桌", "shortage": "牌具少件", "tea_spill": "茶水泼洒",
    "device_fault": "设备故障", "kit_loan": "跨场借件",
    "extend": "续时", "extend_conflict": "续时冲突",
    "damage": "损坏赔付", "damage_dispute": "赔付争议", "note": "备注",
}


def fmt(ts: int | None) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "--:--"


def fmt_full(ts: int | None) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def layout(active: str, title: str, body: str, role: str = "staff",
           flash: tuple | None = None) -> str:
    nav = [
        ("/", "plan", "场地平面图"),
        ("/calendar", "calendar", "桌台日历"),
        ("/book", "book", "预约登记"),
        ("/inventory", "inventory", "牌具库存"),
        ("/cleaning", "cleaning", "清洁队列"),
        ("/review", "review", "结算复核"),
    ]
    links = "".join(
        f'<a href="{u}" class="{"active" if k == active else ""}">{n}</a>'
        for u, k, n in nav
    )
    role_opts = "".join(
        f'<option value="{r}" {"selected" if r == role else ""}>{n}</option>'
        for r, n in (("staff", "前台"), ("admin", "管理员"), ("director", "社区负责人"))
    )
    flash_html = ""
    if flash:
        kind, msg = flash
        flash_html = f'<div class="flash {kind}">{H(msg)}</div>'
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{H(title)} · 社区棋牌室</title>
<link rel="stylesheet" href="/static/style.css">
</head><body>
<header>
  <div class="brand">♟ 社区棋牌室</div>
  <nav>{links}</nav>
  <div class="spacer"></div>
  <label style="color:#d9c9a8;font-size:12px">当前身份
    <select id="roleSel" onchange="document.cookie='role='+this.value+';path=/';location.reload()">
      {role_opts}
    </select>
  </label>
</header>
<div class="wrap">{flash_html}{body}</div>
<script src="/static/app.js"></script>
</body></html>"""


# --------------------------------------------------------------- 场地平面图

def page_plan(svc: ChessRoomService, role: str) -> str:
    plan = svc.floor_plan()
    plan_parts = ['<div class="card"><h2>场地平面图 <span class="sub">等比绘制（1 米 = 42 px），预约条贴于桌号下方</span></h2>']
    max_x = max(r["x"] + r["w"] for r in plan["rooms"])
    max_y = max(r["y"] + r["h"] for r in plan["rooms"])
    W, Hgt = int(max_x * SCALE) + 30, int(max_y * SCALE) + 80
    plan_parts.append(f'<svg class="plan-svg" viewBox="0 0 {W} {Hgt}" role="img" aria-label="场地平面图">')
    for r in plan["rooms"]:
        x, y = r["x"] * SCALE + 10, r["y"] * SCALE + 30
        plan_parts.append(
            f'<rect class="room-rect" x="{x}" y="{y}" width="{r["w"]*SCALE}" height="{r["h"]*SCALE}" rx="10"/>'
            f'<text class="room-label" x="{x+14}" y="{y+24}">{H(r["name"])}</text>'
        )
    for t in plan["tables"]:
        cx = t["px"] * SCALE + 10
        cy = t["py"] * SCALE + 30
        rr = 23
        if t["cleaning"]:
            cls, tcls = "table-dirty", "tcode-light"
        elif t["lock"] and t["lock"]["lock_status"] == "active":
            cls, tcls = "table-active", "tcode-dark"
        elif t["lock"]:
            cls, tcls = "table-hold", "tcode-light"
        else:
            cls, tcls = "table-free", "tcode-light"
        plan_parts.append(f'<circle class="{cls}" cx="{cx}" cy="{cy}" r="{rr}"/>')
        plan_parts.append(
            f'<text class="tcode {tcls}" x="{cx}" y="{cy+4}">{H(t["code"])}</text>'
        )
        # 预约条直接贴在桌号下方
        lk = t["lock"]
        if lk:
            label = f'{H(lk["member_code"])} {fmt(lk["start_ts"])}-{fmt(lk["plan_end_ts"] or lk["end_ts"])}'
            bw = max(64, len(label) * 6.6 + 8)
            plan_parts.append(
                f'<a href="/r/{lk["res_id"]}">'
                f'<rect class="resbar" x="{cx-bw/2}" y="{cy+rr+4}" width="{bw}" height="15" rx="3"/>'
                f'<text x="{cx}" y="{cy+rr+15}" fill="#f3ead8" font-size="9" text-anchor="middle">{label}</text>'
                f'</a>'
            )
        elif t["cleaning"]:
            label = "待清洁" if t["cleaning"] == "dirty" else "清洁中"
            plan_parts.append(
                f'<text x="{cx}" y="{cy+rr+15}" fill="#8c8272" font-size="9" text-anchor="middle">{label}</text>'
            )
    plan_parts.append("</svg>")
    plan_parts.append(
        '<div class="legend" style="margin-top:10px">'
        '<span><i class="lg" style="background:#fffdf8"></i>空闲</span>'
        '<span><i class="lg" style="background:#c8862a;border-color:#c8862a"></i>已预约锁定</span>'
        '<span><i class="lg" style="background:#1f4d3a"></i>进行中</span>'
        '<span><i class="lg" style="background:#cfc4ad"></i>待清洁/清洁中（阻断使用）</span>'
        '</div></div>'
    )

    # 进行中计时（高频操作）
    timers = svc.timers()
    side = ['<div class="card"><h2>进行中场次</h2>']
    if not timers:
        side.append('<p class="muted">暂无进行中的场次。</p>')
    for tm in timers:
        mins = tm["remaining"] // 60
        side.append(
            f'<div class="row-flex" style="justify-content:space-between;margin-bottom:8px">'
            f'<div><b>{H(tm["table_code"])}</b> <span class="muted">{H(tm["member_code"])}</span></div>'
            f'<div class="money" style="font-size:16px;color:var(--ink)">剩余 {mins} 分钟</div>'
            f'<div class="row-flex"><a class="btn sm" href="/timer/{tm["id"]}">计时端</a>'
            f'<a class="btn sm ghost" href="/close/{tm["id"]}">散场</a></div></div>'
        )
    side.append('</div>')
    body = '<div class="grid2">' + "".join(plan_parts) + "".join(side) + "</div>"
    return layout("plan", "场地平面图", body, role)


# ------------------------------------------------------------------ 桌台日历

def page_calendar(svc: ChessRoomService, day0: int, role: str) -> str:
    H0, H1 = 8, 24
    day_end = day0 + 24 * 3600
    rows = svc.calendar(day0, day_end)
    tables = svc.db.query("SELECT * FROM tables ORDER BY code")
    span = (H1 - H0) * 3600

    head_cells = "".join(
        f'<div class="cal-hour" style="position:absolute;left:{(h-H0)/ (H1-H0) *100:.2f}%">{h:02d}:00</div>'
        for h in range(H0, H1 + 1)
    )
    body = [
        '<div class="card"><h2>桌台日历 <span class="sub">'
        + time.strftime("%Y-%m-%d", time.localtime(day0))
        + '</span></h2><div class="cal-scroll"><div class="cal">',
        f'<div class="cal-head" style="height:22px;margin-left:64px;position:relative">{head_cells}</div>',
    ]
    by_table: dict[str, list[dict]] = {}
    for r in rows:
        by_table.setdefault(r["table_code"], []).append(r)
    for t in tables:
        body.append(
            '<div class="cal-row"><div class="tlabel">' + H(t["code"]) + "</div>"
            '<div class="cal-track">'
        )
        for h in range(H0, H1 + 1):
            left = (h - H0) / (H1 - H0) * 100
            body.append(f'<div class="gridline" style="left:{left:.3f}%"></div>')
        for r in by_table.get(t["code"], []):
            s = max(r["start_ts"], day0 + H0 * 3600)
            e = min(r["plan_end_ts"] or r["end_ts"], day0 + H1 * 3600)
            left = (s - day0 - H0 * 3600) / span * 100
            width = max(1.2, (e - s) / span * 100)
            bar = (
                f'{H(r["member_code"])} {fmt(r["start_ts"])}-{fmt(r["plan_end_ts"] or r["end_ts"])}'
                f' {H(r["kit_name"])}'
            )
            body.append(
                f'<a class="cal-bar {r["status"]}" href="/r/{r["id"]}" '
                f'style="left:{left:.2f}%;width:{width:.2f}%" title="{H(bar)}">{H(bar)}</a>'
            )
        body.append("</div></div>")
    body.append("</div></div>")
    prevq = urlencode({"d": time.strftime("%Y-%m-%d", time.localtime(day0 - 86400))})
    nextq = urlencode({"d": time.strftime("%Y-%m-%d", time.localtime(day0 + 86400))})
    body.append(
        '<div class="row-flex" style="margin-top:10px">'
        f'<a class="btn sm ghost" href="/calendar?{prevq}">← 前一天</a>'
        f'<a class="btn sm ghost" href="/calendar?{nextq}">后一天 →</a></div></div>'
    )
    return layout("calendar", "桌台日历", "".join(body), role)


# ------------------------------------------------------------------ 预约登记

def page_book(svc: ChessRoomService, role: str, default: dict | None = None) -> str:
    default = default or {}
    tables = svc.db.query("SELECT * FROM tables ORDER BY code")
    kits = svc.db.query("SELECT * FROM kits ORDER BY code")
    members = svc.db.query("SELECT * FROM members ORDER BY code")
    topts = "".join(
        f'<option value="{t["id"]}" data-kind="{t["kind"]}">{H(t["code"])} · {H(t["kind"])} · ¥{t["rate"]}/时</option>'
        for t in tables
    )
    kopts = "".join(
        f'<option value="{k["id"]}" data-kind="{k["kind"]}">{H(k["code"])} {H(k["name"])}</option>'
        for k in kits
    )
    mopts = "".join(
        f'<option value="{H(m["code"])}">{H(m["code"])} {H(m["name"])}</option>'
        for m in members
    )
    body = f"""
<div class="card"><h2>登记预约</h2>
<form method="post" action="/api/book" class="grid3">
  <label class="fl">会员代号
    <select name="member_code">{mopts}</select></label>
  <label class="fl">桌台
    <select name="table_id" id="tbl">{topts}</select></label>
  <label class="fl">牌具套装（须与桌型一致）
    <select name="kit_id" id="kit">{kopts}</select></label>
  <label class="fl">开始时间
    <input type="datetime-local" name="start" value="{H(default.get("start",""))}" step="60"></label>
  <label class="fl">结束时间
    <input type="datetime-local" name="end" value="{H(default.get("end",""))}" step="60"></label>
  <label class="fl">茶水押金（演示金额 ¥）
    <input type="number" name="tea_deposit" value="50" min="0" step="10"></label>
  <label class="fl" style="grid-column:1/-1">备注
    <input name="note" placeholder="如：需靠窗、有老人"></label>
  <div style="grid-column:1/-1"><button class="btn" type="submit">确认并锁定桌台与牌具</button>
    <span class="muted">确认时预收茶水押金；锁为预约占用，迟到 15 分钟自动释放。</span></div>
</form></div>
<script>
// 选桌台时只允许同棋类牌具
(function(){{
  var t=document.getElementById('tbl'),k=document.getElementById('kit');
  function sync(){{var kind=t.options[t.selectedIndex].dataset.kind;
    Array.prototype.forEach.call(k.options,function(o){{
      o.disabled=o.dataset.kind!==kind; if(o.disabled&&o.selected)k.value='';
    }});
    if(!k.value)for(var i=0;i<k.options.length;i++)if(!k.options[i].disabled){{k.value=k.options[i].value;break;}}
  }}
  t.addEventListener('change',sync);sync();
}})();
</script>"""
    return layout("book", "预约登记", body, role)


# --------------------------------------------------------------- 场次详情枢纽

def _event_list(svc: ChessRoomService, rid: int) -> str:
    evs = svc.db.query(
        "SELECT * FROM events WHERE reservation_id=? ORDER BY id", (rid,))
    if not evs:
        return '<p class="muted">暂无事件记录。</p>'
    out = []
    for e in evs:
        appr = ""
        if e["approval"] == "pending":
            appr = ' <span class="tag pending">待社区负责人批准</span>'
        elif e["approval"] == "approved":
            appr = f' <span class="tag settled">已批准·{H(e["approver"])}</span>'
        amt = f' <b class="money">¥{e["amount"]:.2f}</b>' if e["amount"] else ""
        out.append(
            f'<div class="evt"><b>{H(EVENT_CN.get(e["kind"], e["kind"]))}</b> '
            f'{fmt_full(e["created_ts"])} — {H(e["detail"])}{amt}{appr}</div>'
        )
    return "".join(out)


def page_reservation(svc: ChessRoomService, rid: int, role: str) -> str:
    res = svc._get("reservations", rid)
    t = svc._get("tables", res["table_id"])
    k = svc._get("kits", res["kit_id"])
    room = svc._get("rooms", t["room_id"])
    actions = [f'<a class="btn sm" href="/calendar">返回日历</a>']
    if res["status"] == "booked":
        actions.append(f'<a class="btn" href="/open/{rid}">开场清点</a>')
    if res["status"] == "seated":
        limit = svc.extend_limit(rid)
        lim_txt = f'，后续预约 {fmt(limit)} 前可续' if limit else "，今日无后续预约"
        actions.append(
            f'<a class="btn amber" href="/timer/{rid}">计时端 / 续时{lim_txt}</a>'
            f'<a class="btn ghost" href="/close/{rid}">散场核验</a>'
        )
    body = f"""
<div class="card"><h2>场次 #{rid} <span class="tag {res["status"]}">{STATUS_CN[res["status"]]}</span></h2>
<table class="data"><tr><th>会员代号</th><td>{H(res["member_code"])}</td>
<th>房间/桌台</th><td>{H(room["name"])} · {H(t["code"])}（{H(t["kind"])}）</td></tr>
<tr><th>牌具套装</th><td>{H(k["code"])} {H(k["name"])}</td>
<th>预约时段</th><td>{fmt_full(res["start_ts"])} – {fmt_full(res["plan_end_ts"] or res["end_ts"])}</td></tr>
<tr><th>开场/散场</th><td>{fmt_full(res["seated_ts"])} / {fmt_full(res["actual_end_ts"])}</td>
<th>茶水押金</th><td class="money">¥{res["tea_deposit"]:.2f}</td></tr>
<tr><th>备注</th><td colspan="3">{H(res["note"]) or "—"}</td></tr></table>
<div class="row-flex" style="margin-top:12px">{"".join(actions)}</div></div>

<div class="grid2">
<div class="card"><h2>事件记录（{len(EVENT_CN)} 类异常全程留痕）</h2>{_event_list(svc, rid)}</div>
<div class="card">{_side_actions(svc, res, role)}</div>
</div>"""
    return layout("calendar", f"场次 #{rid}", body, role)


def _side_actions(svc, res, role: str) -> str:
    rid = res["id"]
    if res["status"] != "seated":
        return '<h2>现场操作</h2><p class="muted">仅进行中的场次可登记现场操作。</p>'
    seated = svc.db.query(
        """SELECT r.id, t.code FROM reservations r JOIN tables t ON t.id=r.table_id
           WHERE r.status='seated' AND r.id<>?""", (rid,))
    loan_opts = "".join(f'<option value="{r["id"]}">#{r["id"]} {H(r["code"])}</option>' for r in seated)
    comps = loads(svc._get("kits", res["kit_id"])["components"])
    comp_opts = "".join(f'<option>{H(name)}</option>' for name in comps)
    tables = svc.db.query("SELECT * FROM tables WHERE kind=? ORDER BY code",
                          (svc._get("tables", res["table_id"])["kind"],))
    tbl_opts = "".join(
        f'<option value="{t["id"]}">{H(t["code"])}</option>' for t in tables
        if t["id"] != res["table_id"]
    )
    admin_note = ' <span class="muted">（限管理员）</span>' if role != "admin" else ""
    return f"""
<h2>现场操作</h2>
<form method="post" action="/api/event" class="row-flex" style="margin-bottom:10px">
  <input type="hidden" name="rid" value="{rid}">
  <select name="kind"><option value="tea_spill">茶水泼洒</option>
  <option value="device_fault">设备故障</option></select>
  <input name="detail" placeholder="情况说明" style="flex:1">
  <button class="btn sm">登记</button>
</form>
<form method="post" action="/api/change-table" class="row-flex" style="margin-bottom:10px">
  <input type="hidden" name="rid" value="{rid}">
  <label class="fl">临时换桌{admin_note}<select name="new_table_id">{tbl_opts}</select></label>
  <button class="btn sm amber" style="margin-top:14px">调桌</button>
</form>
<form method="post" action="/api/loan" class="row-flex">
  <input type="hidden" name="from_rid" value="{rid}">
  <label class="fl">跨场借给<select name="to_rid">{loan_opts or "<option>无其他场次</option>"}</select></label>
  <label class="fl">件名<select name="component">{comp_opts}</select></label>
  <label class="fl">数量<input name="qty" type="number" value="1" min="1" style="width:70px"></label>
  <button class="btn sm ghost" style="margin-top:14px">借出登记</button>
</form>"""


# ------------------------------------------------------------------ 开场清单

def _slot_groups(svc, rid: int, phase: str) -> tuple[str, int]:
    res = svc._get("reservations", rid)
    kit = svc._get("kits", res["kit_id"])
    comps = svc.kit_components(kit)
    existing = {}
    for r in svc.db.query(
        "SELECT component, slot, present FROM checks WHERE reservation_id=? AND phase=?",
        (rid, phase),
    ):
        existing[(r["component"], r["slot"])] = r["present"]
    groups, total = [], 0
    for name, n in comps.items():
        cells = []
        for slot in range(1, n + 1):
            was = existing.get((name, slot), 1)
            cls = "present" if was else "missing closed-missing"
            mark = "✓" if was else ""
            readonly = "data-ro" if existing else ""
            cells.append(
                f'<div class="slot {cls} {"slot-tiny" if n > 40 else ""}" {readonly} '
                f'data-name="{H(name)}" data-slot="{slot}">{mark}</div>'
            )
            total += 1
        groups.append(
            f'<div class="slot-group"><h3>{H(name)} <span class="muted">应有 {n} 件</span></h3>'
            f'<div class="slots">{"".join(cells)}</div></div>'
        )
    return "".join(groups), total


def page_open(svc: ChessRoomService, rid: int, role: str) -> str:
    res = svc._get("reservations", rid)
    t = svc._get("tables", res["table_id"])
    if res["status"] != "booked":
        return page_reservation(svc, rid, role)
    groups, total = _slot_groups(svc, rid, "open")
    body = f"""
<div class="card"><h2>开场清单 · {H(t["code"])} <span class="sub">逐套清点棋子、牌张与计时器，点击格子标记缺件（空框保留）</span></h2>
<form method="post" action="/api/seat/{rid}" id="slotForm">
{groups}
<p class="slot-count-note">固定格位合计 {total} 个；缺件位置保留红色虚线空框，开场少件将自动登记。</p>
<button class="btn" type="submit">清点无误，开场（解锁房间）</button>
<a class="btn ghost" href="/r/{rid}">返回</a>
</form></div>"""
    return layout("book", f"开场 #{rid}", body, role)


# ------------------------------------------------------------------ 牌具库存

def page_inventory(svc: ChessRoomService, role: str) -> str:
    kits = svc.inventory()
    cards = []
    for k in kits:
        comps = k["components_map"]
        if k["timer"]:
            comps = {**comps, "计时器": 1}
        comp_txt = "、".join(f"{H(n)} {v}" for n, v in comps.items())
        if k["lock"]:
            tag = '<span class="tag seated">使用/预约中</span>'
            who = f'场次 <a href="/r/{k["lock"]["res_id"]}">#{k["lock"]["res_id"]}</a>'
        else:
            tag = '<span class="tag clean">在库可约</span>'
            who = "—"
        # 最近一次散场清点差异
        last_diff = svc.db.one(
            """SELECT c.reservation_id, COUNT(*) n FROM checks c
               JOIN reservations r ON r.id=c.reservation_id
               WHERE c.phase='close' AND c.present=0 AND r.kit_id=?
               GROUP BY c.reservation_id ORDER BY c.reservation_id DESC LIMIT 1""",
            (k["id"],))
        diff = (f'最近少件 {last_diff["n"]} 件（场次 '
                f'<a href="/r/{last_diff["reservation_id"]}">#{last_diff["reservation_id"]}</a>）'
                if last_diff else "最近散场清点无差异")
        cards.append(f"""
<div class="card"><h2>{H(k["code"])} {H(k["name"])} {tag}</h2>
<table class="data">
<tr><th>棋类</th><td>{H(k["kind"])}</td><th>牌具押金</th><td class="money">¥{k["deposit"]:.0f}</td></tr>
<tr><th>固定构成</th><td colspan="3">{comp_txt}</td></tr>
<tr><th>当前占用</th><td colspan="3">{who}</td></tr>
<tr><th>清点状态</th><td colspan="3">{diff}</td></tr>
</table></div>""")
    body = '<div class="grid2">' + "".join(cards) + "</div>"
    return layout("inventory", "牌具库存", body, role)


# ------------------------------------------------------------------ 计时端

def page_timer(svc: ChessRoomService, rid: int, role: str) -> str:
    res = svc._get("reservations", rid)
    t = svc._get("tables", res["table_id"])
    if res["status"] != "seated":
        return page_reservation(svc, rid, role)
    end = res["plan_end_ts"] or res["end_ts"]
    limit = svc.extend_limit(rid)
    lim_note = (
        f'<div class="muted" style="margin-top:8px">后续 {fmt(limit)} 有预约，续时不能超过该时刻</div>'
        if limit else '<div class="muted" style="margin-top:8px">后续无预约，可自由续时</div>'
    )
    body = f"""
<div class="card timer-page">
  <h2 style="justify-content:center">{H(t["code"])} · {H(res["member_code"])}</h2>
  <div class="timer-dial" id="dial" data-end="{end}">
    <div class="rem" id="rem">--:--</div>
    <div class="meta">剩余时间</div>
  </div>
  <div class="timer-actions">
    <form method="post" action="/api/extend/{rid}">
      <input type="hidden" name="minutes" value="30"><button class="btn">+30 分钟</button></form>
    <form method="post" action="/api/extend/{rid}">
      <input type="hidden" name="minutes" value="60"><button class="btn ghost">+60 分钟</button></form>
    <a class="btn red" href="/close/{rid}">散场结算</a>
  </div>
  {lim_note}
  <div class="muted" style="margin-top:6px">计时端仅显示剩余时间与高频操作；续时冲突会被拒绝并记录。</div>
</div>"""
    return layout("calendar", f"计时 #{rid}", body, role)


# ------------------------------------------------------------------ 散场核验

def page_close(svc: ChessRoomService, rid: int, role: str) -> str:
    res = svc._get("reservations", rid)
    t = svc._get("tables", res["table_id"])
    if res["status"] != "seated":
        return page_reservation(svc, rid, role)
    groups, total = _slot_groups(svc, rid, "close")
    loans = svc.db.query(
        "SELECT * FROM kit_loans WHERE from_res_id=? AND returned=0", (rid,))
    loan_warn = ""
    if loans:
        items = "、".join(f"{H(l['component'])}x{l['qty']}→#{l['to_res_id']}" for l in loans)
        loan_warn = (
            f'<div class="flash err">存在未归还跨场借件：{items}，散场将计为少件。'
            f'可先 <a href="/r/{rid}">回到场次页</a> 催还。</div>'
        )
    body = f"""
<div class="card"><h2>散场核验 · {H(t["code"])} <span class="sub">按实际时长（15 分钟进位）计费，逐格清点牌具</span></h2>
{loan_warn}
<form method="post" action="/api/settle/{rid}" id="slotForm">
<div class="grid2">
<div>{groups}<p class="slot-count-note">固定格位合计 {total} 个；缺件位置保留空框。</p></div>
<div>
  <label class="fl">茶水消费（¥）<input type="number" name="tea_charge" value="0" min="0" step="5"></label>
  <h3 style="color:var(--ink);margin:14px 0 6px">损坏/少件赔付</h3>
  <div id="damages">
    <div class="row-flex" style="margin-bottom:6px">
      <input name="d_desc" placeholder="损坏描述（如：棋罐裂纹）" style="flex:1">
      <input name="d_amount" type="number" placeholder="金额" min="0" style="width:90px">
      <input type="file" name="d_photo" accept="image/*" style="font-size:11px">
    </div>
  </div>
  <label class="row-flex" style="margin-top:10px">
    <input type="checkbox" name="disputed" value="1"> 会员对赔付有争议（桌台牌具保持锁定，转社区负责人）
  </label>
  <div style="margin-top:16px">
    <button class="btn red" type="submit">完成散场结算</button>
    <a class="btn ghost" href="/r/{rid}">返回</a>
  </div>
  <p class="muted" style="margin-top:8px">牌具完整且无争议 → 收款/退押金（幂等）→ 桌台进入待清洁，清洁完成后才可再用。</p>
</div></div></form></div>"""
    return layout("calendar", f"散场 #{rid}", body, role)


# ------------------------------------------------------------------ 清洁队列

def page_cleaning(svc: ChessRoomService, role: str) -> str:
    rows = svc.db.query(
        """SELECT c.*, t.code table_code, r.member_code FROM cleaning c
           JOIN tables t ON t.id=c.table_id JOIN reservations r ON r.id=c.reservation_id
           ORDER BY c.status DESC, c.created_ts""")
    trs = []
    for r in rows:
        if r["status"] == "dirty":
            btn = (f'<form method="post" action="/api/clean/start/{r["id"]}">'
                   f'<button class="btn sm amber">开始清洁</button></form>')
        elif r["status"] == "cleaning":
            btn = (f'<form method="post" action="/api/clean/finish/{r["id"]}">'
                   f'<button class="btn sm">完成清洁（恢复可约）</button></form>')
        else:
            btn = '<span class="muted">已完成</span>'
        block = "阻断开场/预约" if r["status"] in ("dirty", "cleaning") else "—"
        trs.append(
            f'<tr><td>{H(r["table_code"])}</td><td>{H(r["member_code"])}</td>'
            f'<td><span class="tag {r["status"]}">{ {"dirty":"待清洁","cleaning":"清洁中","clean":"已完成"}[r["status"]] }</span></td>'
            f'<td>{fmt_full(r["created_ts"])}</td><td>{fmt_full(r["finished_ts"])}</td>'
            f'<td>{block}</td><td>{btn}</td></tr>'
        )
    body = f"""
<div class="card"><h2>清洁队列 <span class="sub">散场且牌具完整后进入；完成清洁桌台才可供下一场使用</span></h2>
<table class="data"><thead><tr><th>桌台</th><th>场次会员</th><th>状态</th><th>入队</th><th>完成</th><th>占用影响</th><th>操作</th></tr></thead>
<tbody>{"".join(trs) or '<tr><td colspan="7" class="muted">队列为空</td></tr>'}</tbody></table></div>"""
    return layout("cleaning", "清洁队列", body, role)


# ------------------------------------------------------------------ 结算复核

def page_review(svc: ChessRoomService, role: str) -> str:
    bills = svc.db.query(
        """SELECT b.*, t.code table_code, r.member_code, r.status res_status
           FROM bills b JOIN reservations r ON r.id=b.reservation_id
           JOIN tables t ON t.id=r.table_id ORDER BY b.id DESC""")
    trs = []
    for b in bills:
        approve = ""
        if b["approval"] == "pending":
            approve = f"""
<form method="post" action="/api/damage-resolve/{b["reservation_id"]}" class="row-flex">
  <input name="discount" type="number" value="0" min="0" max="{b["damage_charge"]}"
         step="1" style="width:80px" title="减免金额（需社区负责人）">
  <button class="btn sm" name="action" value="pay">收取赔付</button>
  <button class="btn sm red" name="action" value="relieve">批准减免</button>
</form>"""
        else:
            approve = f'<span class="tag settled">{H(b["approver"] or "系统")}</span>'
        approval_label = {"pending": "待批准", "approved": "已批准", "": "自动核销"}.get(
            b["approval"], b["approval"])
        trs.append(
            f'<tr><td><a href="/r/{b["reservation_id"]}">#{b["reservation_id"]}</a></td>'
            f'<td>{H(b["table_code"])}</td><td>{H(b["member_code"])}</td>'
            f'<td>{b["minutes"]}</td><td class="money">{b["time_fee"]:.2f}</td>'
            f'<td class="money">{b["tea_charge"]:.2f}</td>'
            f'<td class="money">{b["damage_charge"]:.2f}'
            + (f'<div class="muted">减免 {b["discount"]:.2f}</div>' if b["discount"] else "")
            + '</td>'
            f'<td class="money">{b["total"]:.2f}</td>'
            f'<td class="money">{b["refund"]:.2f}</td>'
            f'<td><span class="tag {"pending" if b["approval"]=="pending" else "settled"}">'
            f'{H(approval_label)}</span></td>'
            f'<td>{approve}</td></tr>'
        )
    pays = svc.db.query("SELECT * FROM payments ORDER BY id DESC LIMIT 30")
    ptrs = "".join(
        f'<tr><td>{p["reservation_id"]}</td>'
        f'<td>{"退款" if p["direction"]=="refund" else "收款"}</td>'
        f'<td class="money">{p["amount"]:.2f}</td>'
        f'<td><span class="tag {"settled" if p["status"]=="success" else "pending"}">{p["status"]}</span></td>'
        f'<td><code>{H(p["ref_no"] or "—")}</code></td>'
        f'<td><code class="muted">{H(p["idem_key"])}</code></td></tr>'
        for p in pays
    )
    body = f"""
<div class="card"><h2>结算单复核 <span class="sub">按实际时长计费；赔付减免须社区负责人批准</span></h2>
<table class="data"><thead><tr><th>场次</th><th>桌台</th><th>会员</th><th>分钟</th>
<th>计时费</th><th>茶水</th><th>赔付</th><th>应收</th><th>退款</th><th>审批</th><th>操作</th></tr></thead>
<tbody>{"".join(trs) or '<tr><td colspan="11" class="muted">暂无结算单</td></tr>'}</tbody></table></div>
<div class="card"><h2>收退款流水（模拟付款适配器，按幂等键去重）</h2>
<table class="data"><thead><tr><th>场次</th><th>方向</th><th>金额</th><th>状态</th><th>凭证号</th><th>幂等键</th></tr></thead>
<tbody>{ptrs or '<tr><td colspan="6" class="muted">暂无流水</td></tr>'}</tbody></table></div>"""
    return layout("review", "结算复核", body, role)
