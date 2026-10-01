/* 社区棋牌室前端：八大视图 + 预约/清点/续时/赔付交互 */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const api = async (path, body) => {
  const opt = body === undefined
    ? { method: "GET" }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || "请求失败");
  return j;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const HM = (iso) => iso ? iso.slice(11, 16) : "";
const DATE = (iso) => iso ? iso.slice(0, 10) : "";
const badge = (s) => {
  const map = {
    READY: "就绪", OCCUPIED: "使用中", CLEANING: "待清洁", MAINTENANCE: "维修中",
    IN_STOCK: "在库", LOCKED: "已锁定", IN_USE: "使用中", AT_CLEANING: "待清洁", DAMAGED: "待处理",
    HELD: "暂留", CONFIRMED: "已确认", SEATED: "进行中", FINISHED: "已结束",
    RELEASED_LATE: "迟到释放", CANCELLED: "已取消", MOVED: "已调桌",
    PLAYING: "进行中", EXTENDED: "已续时", CHECKING: "核验中", CLOSED: "已关闭",
    WAITING: "待清洁", DOING: "清洁中", DONE: "已完成",
    PENDING: "待处理", DISPUTED: "争议中", APPROVED: "已认定", WAIVED: "已减免", PAID: "已赔付",
  };
  return `<span class="badge ${s}">${map[s] || s}</span>`;
};

let S = null;                 // 服务端快照
let view = "calendar";
let calDate = new Date().toISOString().slice(0, 10);
const tickTimers = [];

// ----------------------------------------------------------------- bootstrap
async function refresh(silent) {
  S = await api("/api/state");
  $("#clock").textContent = HM(S.now) + " · " + DATE(S.now).slice(5);
  renderNotices();
  render();
}
function renderNotices() {
  $("#notices").innerHTML = (S.notifications || []).slice(0, 4)
    .map(n => `<span class="notice-chip ${esc(n.kind)}">${esc(n.kind)} · ${esc(n.text.slice(0, 26))}</span>`)
    .join("");
}
function toast(msg, isErr) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  setTimeout(() => t.classList.add("hidden"), 2600);
}
async function act(fn, okMsg) {
  try { await fn(); await refresh(); if (okMsg) toast(okMsg); }
  catch (e) { toast(e.message, true); }
}

// ----------------------------------------------------------------- tabs
$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  view = b.dataset.view;
  $$("#tabs button").forEach(x => x.classList.toggle("active", x === b));
  render();
});

function render() {
  const fns = {
    calendar: viewCalendar, floor: viewFloor, timer: viewTimer, open: viewOpen,
    close: viewClose, cleaning: viewCleaning, inventory: viewInventory, review: viewReview,
  };
  $("#main").innerHTML = fns[view]();
  (viewHooks[view] || (() => {}))();
}
const viewHooks = {};

// ----------------------------------------------------------------- helpers
const tableById = (id) => S.tables.find(t => t.id === id);
const memberByCode = (c) => S.members.find(m => m.code === c);
const resById = (id) => S.reservations.find(r => r.id === id);
const kitById = (id) => S.sets.find(k => k.id === id);
const sessionByRes = (rid) => S.sessions.find(s => s.reservation_id === rid);
const kitList = (r) => (r.kit_ids || "").split(",").filter(Boolean).map(Number).map(kitById).filter(Boolean);
const STATUS_LABEL = {
  CONFIRMED: "已确认", SEATED: "进行中", FINISHED: "已结束", RELEASED_LATE: "迟到释放",
  CANCELLED: "已取消", MOVED: "已调桌",
};

// ============================================================ 1. 桌台日历
function viewCalendar() {
  const dates = [...new Set(S.reservations.map(r => DATE(r.start_ts)))].sort();
  const today = DATE(S.now);
  const opts = (dates.includes(today) ? dates : [today, ...dates]).sort()
    .map(d => `<option value="${d}" ${d === calDate ? "selected" : ""}>${d}</option>`).join("");
  return `
    <div class="flex-between">
      <h2 class="section" style="margin:0">桌台日历 · ${calDate}</h2>
      <div class="row">
        <label class="fld">日期 <select id="calDate">${opts}</select></label>
        <button class="btn" id="newRes">＋ 新建预约</button>
      </div>
    </div>
    <div class="card" style="margin-top:12px">
      <div class="cal-wrap"><div id="calBody"></div></div>
    </div>
    <p class="muted" style="margin-top:8px">绿条＝已确认锁定 · 黑条＝进行中 · 斜纹＝迟到释放 · 点击预约条查看/操作。预约确认即锁定桌台与牌具。</p>`;
}
viewHooks.calendar = () => {
  $("#calDate").onchange = (e) => { calDate = e.target.value; render(); };
  $("#newRes").onclick = () => openBooking();
  drawCalendar();
};

function drawCalendar() {
  // 与所选日期当天有区间重叠的预约
  const day0 = new Date(calDate + "T00:00"), day1 = new Date(calDate + "T23:59");
  const dayRes = S.reservations.filter(r => {
    const s = new Date(r.start_ts.replace(" ", "T")), e = new Date(r.end_ts.replace(" ", "T"));
    return s < day1 && e > day0;
  });
  let h0 = 8, h1 = 22;
  dayRes.forEach(r => {
    h0 = Math.min(h0, Number(HM(r.start_ts).split(":")[0]));
    h1 = Math.max(h1, Number(HM(r.end_ts).split(":")[0]) + 1);
  });
  const hours = [];
  for (let h = h0; h < h1; h++) hours.push(h);
  const PXH = 100; // 每小时像素
  const width = hours.length * PXH;
  const head = `<tr><th style="min-width:120px">桌台</th>${
    hours.map(h => `<th class="head-hour" style="width:${PXH}px">${String(h).padStart(2, "0")}:00</th>`).join("")
  }</tr>`;
  const rows = S.tables.map(t => {
    const bars = dayRes.filter(r => r.table_id === t.id).map(r => {
      const s = new Date(r.start_ts.replace(" ", "T"));
      const e = new Date(r.end_ts.replace(" ", "T"));
      const dayStart = new Date(calDate + "T00:00");
      const left = Math.max(0, (s - dayStart) / 36e5 * PXH - h0 * PXH);
      const w = Math.max(30, Math.min((e - s) / 36e5 * PXH, width - left - 2));
      const m = memberByCode(r.member_code);
      return `<div class="res-bar st-${r.status}" data-rid="${r.id}" style="left:${left + 2}px;width:${w - 4}px"
        title="#${r.id} ${r.game} · ${m ? m.name : r.member_code} ${HM(r.start_ts)}-${HM(r.end_ts)}">
        #${r.id} ${esc(m ? m.name : r.member_code)} ${HM(r.start_ts)}-${HM(r.end_ts)}</div>`;
    }).join("");
    return `<tr>
      <td class="tbl-cell">${t.room_name === "大厅" ? "" : esc(t.room_name) + " "}${esc(t.label)}
        <span class="st">${badge(t.status)}</span></td>
      <td colspan="${hours.length}" class="gridlines" style="position:relative;padding:0">
        <div style="position:absolute;inset:0">${bars}</div>
        ${DATE(S.now) === calDate ? nowLine(h0, PXH) : ""}
      </td></tr>`;
  }).join("");
  $("#calBody").innerHTML = `<table class="cal" style="min-width:${120 + width}px">
    <thead>${head}</thead><tbody>${rows}</tbody></table>`;
  $$("#calBody .res-bar").forEach(el => el.onclick = () => openRes(Number(el.dataset.rid)));
}
function nowLine(h0, pxh) {
  const n = HM(S.now).split(":").map(Number);
  const x = (n[0] + n[1] / 60 - h0) * pxh;
  if (x < 0) return "";
  return `<div class="now-line" style="left:${x}px"></div>`;
}

// ============================================================ 2. 场地平面图
function viewFloor() {
  const rooms = S.rooms.map(room => {
    const ts = S.tables.filter(t => t.room_id === room.id);
    const maxX = Math.max(...ts.map(t => t.x)) + 1.6;
    const maxY = Math.max(...ts.map(t => t.y)) + 2.2;
    const SCALE = 132; // 每米像素，保持真实间距
    const units = ts.map(t => {
      // 预约条：当前进行中或今天稍后的场次（取消/调桌不显示）
      const dayBars = S.reservations.filter(r => r.table_id === t.id
        && !["CANCELLED", "MOVED", "FINISHED", "RELEASED_LATE"].includes(r.status)
        && r.end_ts > S.now && DATE(r.start_ts) <= DATE(S.now));
      const bars = dayBars.slice(0, 3).map(r => {
        const m = memberByCode(r.member_code);
        return `<div class="res-bar st-${r.status}" data-rid="${r.id}" style="position:static">
          #${r.id} ${esc(m ? m.name : r.member_code)} ${HM(r.start_ts)}-${HM(r.end_ts)}</div>`;
      }).join("");
      // 椅子（按座位数环绕）
      const chairs = [0, 1, 2, 3].slice(0, Math.min(4, t.seats)).map((_, i) => {
        const pos = [{ top: -8, left: 31 }, { top: 31, left: 70 }, { top: 70, left: 31 }, { top: 31, left: -8 }][i];
        return `<span class="chair" style="top:${pos.top}px;left:${pos.left}px"></span>`;
      }).join("");
      return `<div class="table-unit clickable" data-tid="${t.id}"
          style="left:${t.x * SCALE}px;top:${t.y * SCALE}px">
        <div class="table-shape ${t.status}" title="${esc(t.note || "")}">${chairs}${esc(t.label)}</div>
        <div class="tbl-no">${esc(t.label)} <span class="muted" style="font-weight:400">¥${t.rate_30m}/30分</span></div>
        <div class="tbl-kind">${esc(t.kind)} · ${t.seats}座</div>
        <div class="tbl-bars">${bars}</div>
      </div>`;
    }).join("");
    return `<h2 class="section">${esc(room.name)}</h2>
      <div class="card">
        <div class="floor-legend">
          <span><i class="legend-dot" style="background:#e7d8ba;border:1px solid #26241f"></i>就绪</span>
          <span><i class="legend-dot" style="background:#26241f"></i>使用中</span>
          <span><i class="legend-dot" style="background:#d8c085"></i>待清洁</span>
          <span><i class="legend-dot" style="background:#c47d6a"></i>维修中</span>
          <span class="muted">间距按场地实际米制 1:132 绘制，预约条直接贴在桌号下方</span>
        </div>
        <div class="room-block" style="width:${maxX * SCALE + 60}px;height:${maxY * SCALE + 40}px">
          <span class="room-title">${esc(room.name)}</span>${units}
        </div>
      </div>`;
  }).join("");
  return rooms;
}
viewHooks.floor = () => {
  $$(".table-unit").forEach(u => u.onclick = (e) => {
    if (e.target.closest(".res-bar")) return;
    openBooking(Number(u.dataset.tid));
  });
  $$(".tbl-bars .res-bar").forEach(el => el.onclick = (e) => {
    e.stopPropagation(); openRes(Number(el.dataset.rid));
  });
};

// ============================================================ 3. 计时 / 续时
function viewTimer() {
  const playing = S.sessions.filter(s => s.status === "PLAYING" || s.status === "EXTENDED");
  if (!playing.length) return `<h2 class="section">计时端</h2><div class="card empty">当前没有进行中的场次</div>`;
  return `<h2 class="section">计时端 <span class="muted">仅显示剩余时间与高频操作</span></h2>
    <div class="grid cols-3">${playing.map(timerCard).join("")}</div>`;
}
function timerCard(s) {
  const t = tableById(s.table_id), r = resById(s.reservation_id), m = memberByCode(s.member_code);
  return `<div class="timer-card" data-sid="${s.id}">
    <div class="meta">桌台 <b>${esc(t.label)}</b> · #${s.reservation_id} ${esc(m ? m.name : s.member_code)} · ${esc(r.game)}</div>
    <div class="remain" data-end="${s.planned_end}">--:--</div>
    <div class="meta">至 ${HM(s.planned_end)} ${s.status === "EXTENDED" ? "· 已续时" : ""}</div>
    <div class="timer-actions">
      <button class="btn extend-q" data-min="30" data-rid="${s.reservation_id}">＋30分</button>
      <button class="btn extend-q" data-min="60" data-rid="${s.reservation_id}">＋60分</button>
      <button class="btn wood" data-rid="${s.reservation_id}" data-act="close">散场</button>
    </div>
  </div>`;
}
viewHooks.timer = () => {
  const now0 = new Date(S.now.replace(" ", "T")).getTime();
  const update = (elapsedSec) => $$(".timer-card .remain").forEach(el => {
    const end = new Date(el.dataset.end.replace(" ", "T")).getTime();
    let sec = Math.max(0, Math.round((end - now0) / 1000) - elapsedSec);
    const hh = String(Math.floor(sec / 3600)).padStart(2, "0");
    const mm = String(Math.floor(sec % 3600 / 60)).padStart(2, "0");
    const ss = String(sec % 60).padStart(2, "0");
    el.textContent = `${hh}:${mm}:${ss}`;
    el.closest(".timer-card").classList.toggle("urgent", sec < 600 && sec > 0);
    el.closest(".timer-card").classList.toggle("over", sec === 0);
  });
  update(0);
  let elapsed = 0;
  const iv = setInterval(() => { elapsed += 1; update(elapsed); }, 1000);
  tickTimers.push(iv);
  $$(".extend-q").forEach(b => b.onclick = () => act(async () => {
    const j = await api(`/api/reservations/${b.dataset.rid}/extend`, { minutes: Number(b.dataset.min) });
    if (j.truncated) toast("已续时，但因后续预约被截断避让");
  }, "续时成功（已避开后续预约）"));
  $$("[data-act='close']").forEach(b => b.onclick = () => { view = "close"; $$("#tabs button").forEach(x => x.classList.toggle("active", x.dataset.view === "close")); render(); });
};

// ============================================================ 4. 开场清单
function viewOpen() {
  const list = S.reservations.filter(r => r.status === "CONFIRMED" || r.status === "SEATED");
  if (!list.length) return `<h2 class="section">开场清单</h2><div class="card empty">暂无可开场场次</div>`;
  return `<h2 class="section">开场清单 · 逐套清点棋子 / 牌张 / 计时器</h2>
    <div class="grid cols-2">${list.map(r => {
      const t = tableById(r.table_id), m = memberByCode(r.member_code);
      const sess = sessionByRes(r.id);
      return `<div class="card flex-between">
        <div>
          <div><b>#${r.id} ${esc(t.label)}</b> ${badge(r.status)} <span class="tag-kind">${esc(r.game)}</span></div>
          <div class="muted" style="margin-top:4px">${esc(m ? `${m.code} · ${m.name}` : r.member_code)} ·
            ${HM(r.start_ts)}-${HM(r.end_ts)} · 牌具：${kitList(r).map(k => esc(k.name)).join("、") || "无"}</div>
        </div>
        <div class="row">
          ${!sess ? `<button class="btn" data-rid="${r.id}" data-act="seat">开场解锁</button>` : ""}
          ${sess && !openChecked(r.id) ? `<button class="btn ghost" data-rid="${r.id}" data-act="check">逐套清点</button>` : ""}
          ${!sess ? `<button class="btn small danger" data-rid="${r.id}" data-act="cancel">取消预约</button>` : ""}
        </div>
      </div>`;
    }).join("")}</div>`;
}
const openChecked = (rid) => !!(S.checks && S.checks[rid] && S.checks[rid].OPEN);
viewHooks.open = () => {
  $$("#main [data-act]").forEach(b => b.onclick = () => {
    const rid = Number(b.dataset.rid);
    if (b.dataset.act === "seat") act(() => api(`/api/reservations/${rid}/seat`), "门锁已开，祝对局愉快");
    if (b.dataset.act === "cancel") confirmModal("确认取消该预约？释放桌台与牌具。",
      () => act(() => api(`/api/reservations/${rid}/cancel`), "预约已取消"));
    if (b.dataset.act === "check") openChecklist(rid, "OPEN");
  });
};

// ============================================================ 5. 散场核验
function viewClose() {
  const list = S.sessions.filter(s => s.status === "PLAYING" || s.status === "EXTENDED" || s.status === "CHECKING");
  const checking = list.filter(s => s.status === "CHECKING");
  const playing = list.filter(s => s.status !== "CHECKING");
  const card = (s) => {
    const t = tableById(s.table_id), r = resById(s.reservation_id), m = memberByCode(s.member_code);
    return `<div class="card flex-between">
      <div>
        <div><b>${esc(t.label)}</b> 场次 #${s.id}（预约 #${s.reservation_id}） ${badge(s.status)}</div>
        <div class="muted" style="margin-top:4px">${esc(m ? m.name : s.member_code)} ·
          ${HM(s.start_ts)} 起 · 计划至 ${HM(s.planned_end)}</div>
      </div>
      <div class="row">
        ${s.status !== "CHECKING" ? `<button class="btn" data-rid="${s.reservation_id}" data-act="closecheck">逐套清点并散场</button>`
          : `<button class="btn ghost" data-rid="${s.reservation_id}" data-act="reviewdam">处理赔案后结算</button>`}
        <button class="btn wood small" data-rid="${s.reservation_id}" data-act="move">管理员调桌</button>
      </div>
    </div>`;
  };
  return `
    <h2 class="section">散场核验 · 按实际时长结算</h2>
    ${playing.length ? `<div class="grid cols-2">${playing.map(card).join("")}</div>` : `<div class="card empty">没有进行中的场次</div>`}
    ${checking.length ? `<h2 class="section">待处理赔案（牌具未核清，桌台保持占用/不可用）</h2>
      <div class="grid cols-2">${checking.map(card).join("")}</div>` : ""}`;
}
viewHooks.close = () => $$("#main [data-act]").forEach(b => b.onclick = () => {
  const rid = Number(b.dataset.rid);
  if (b.dataset.act === "closecheck") openChecklist(rid, "CLOSE");
  if (b.dataset.act === "reviewdam") { view = "review"; $$("#tabs button").forEach(x => x.classList.toggle("active", x.dataset.view === "review")); render(); }
  if (b.dataset.act === "move") openMove(rid);
});

// ============================================================ 6. 清洁队列
function viewCleaning() {
  const groups = ["WAITING", "DOING", "DONE"].map(st => {
    const items = S.cleanings.filter(c => c.status === st);
    const label = { WAITING: "待清洁（牌具完整后进入）", DOING: "清洁中", DONE: "今日已完成" }[st];
    return `<h2 class="section">${label} · ${items.length}</h2>
      ${items.length ? `<div class="grid cols-3">${items.map(c => `
        <div class="card">
          <div class="flex-between"><b>桌台 ${esc(c.table_label)}</b> ${badge(c.status)}</div>
          <div class="muted" style="margin:6px 0">场次 #${c.session_id} · 建队 ${HM(c.created_at)}</div>
          ${c.status === "WAITING" ? `<button class="btn small" data-cid="${c.id}" data-act="start">开始清洁</button>` : ""}
          ${c.status === "DOING" ? `<button class="btn small" data-cid="${c.id}" data-act="finish">完成清洁，恢复可预约</button>` : ""}
          ${c.status === "DONE" ? `<div class="muted">完成于 ${HM(c.finished_at)}</div>` : ""}
        </div>`).join("")}</div>` : `<div class="card empty">无</div>`}`;
  }).join("");
  return groups + `<p class="muted" style="margin-top:10px">清洁未完成前桌台被清洁锁占用，无法接受新场次；维修中的桌台即使清洁完成也不会自动开放。</p>`;
}
viewHooks.cleaning = () => $$("#main [data-act]").forEach(b => b.onclick = () =>
  act(() => api(`/api/cleanings/${b.dataset.cid}/${b.dataset.act}`),
    b.dataset.act === "finish" ? "清洁完成，桌台已释放" : "已开始清洁"));

// ============================================================ 7. 牌具库存
function viewInventory() {
  const loansOpen = S.loans.filter(l => !l.returned);
  return `<h2 class="section">牌具库存 · 固定数量格</h2>
    <div class="grid cols-3">${S.sets.map(k => `
      <div class="card">
        <div class="flex-between"><b>${esc(k.name)}</b> ${badge(k.status)}</div>
        <div class="muted" style="margin:6px 0">
          <span class="tag-kind">${esc(k.game)}</span>
          ${k.home_label ? `常驻 ${esc(k.home_label)}` : "共享柜"} · 共 ${k.item_count} 件
          ${k.timer_sn ? ` · 计时器 ${esc(k.timer_sn)}` : ""}
        </div>
      </div>`).join("")}</div>
    <h2 class="section">跨场借件登记 · ${loansOpen.length} 笔未归还</h2>
    <div class="card">${S.loans.length ? `<table class="data">
      <thead><tr><th>借据</th><th>牌具/格位</th><th>从场次</th><th>借入场次</th><th>时间</th><th>状态</th><th></th></tr></thead>
      <tbody>${S.loans.map(l => {
        const k = kitById(l.set_id);
        return `<tr><td>#${l.id}</td><td>${esc(k ? k.name : l.set_id)} 第${l.slot_no}格</td>
        <td>#${l.from_reservation_id ?? "—"}</td><td>#${l.to_reservation_id}</td>
        <td>${HM(l.ts)}</td><td>${l.returned ? badge("DONE") : badge("LOCKED")}</td>
        <td>${l.returned ? "" : `<button class="btn small" data-lid="${l.id}" data-act="ret">登记归还</button>`}</td></tr>`;
      }).join("")}</tbody></table>` : `<div class="empty">暂无借件记录</div>`}</div>`;
}
viewHooks.inventory = () => $$("[data-act='ret']").forEach(b => b.onclick = () =>
  act(() => api(`/api/loans/${b.dataset.lid}/return`), "借件已归位"));

// ============================================================ 8. 结算复核
function viewReview() {
  const stats = {
    open: S.damages.filter(d => ["PENDING", "DISPUTED"].includes(d.status)).length,
    fee: S.sessions.filter(s => s.total_fee != null).reduce((a, s) => a + s.total_fee, 0),
    refund: S.payments.filter(p => p.kind === "REFUND").reduce((a, p) => a + p.amount, 0),
  };
  return `
  <div class="grid cols-4" style="margin-bottom:14px">
    <div class="stat"><div class="v">${stats.open}</div><div class="l">未决赔案</div></div>
    <div class="stat"><div class="v">¥${stats.fee.toFixed(0)}</div><div class="l">累计台费</div></div>
    <div class="stat"><div class="v">¥${stats.refund.toFixed(0)}</div><div class="l">押金退款合计</div></div>
    <div class="stat"><div class="v">${S.cleanings.filter(c => c.status !== "DONE").length}</div><div class="l">未完成清洁</div></div>
  </div>
  <h2 class="section">赔付处理（减免需社区负责人批准）</h2>
  <div class="card">${damageTable(S.damages)}</div>
  <h2 class="section">场次结算与退款（幂等）</h2>
  <div class="card">${settlementTable()}</div>
  <h2 class="section">事件记录</h2>
  <div class="card">${S.incidents.length ? S.incidents.slice(0, 30).map(i =>
    `<div class="incident-line"><span class="k">${esc(i.kind)}</span>
      <span class="muted mono">${HM(i.ts)}</span> ${esc(i.detail)}
      ${i.approver ? `<span class="muted">｜批准：${esc(i.approver)}</span>` : ""}
      ${i.amount != null ? `<span class="pill-amount">¥${i.amount}</span>` : ""}</div>`).join("")
    : `<div class="empty">暂无事件</div>`}</div>`;
}
function damageTable(ds) {
  if (!ds.length) return `<div class="empty">暂无赔案</div>`;
  return `<table class="data"><thead><tr>
    <th>赔案</th><th>场次</th><th>牌具/格位</th><th>事由</th><th>金额</th><th>状态</th><th>照片</th><th>操作</th>
    </tr></thead><tbody>${ds.map(d => `<tr>
    <td>#${d.id}</td><td>#${d.reservation_id}</td>
    <td>${esc(d.set_name || "—")}${d.slot_no ? ` 第${d.slot_no}格` : ""}</td>
    <td>${esc(d.reason)}</td>
    <td class="amount dr">¥${d.amount.toFixed(0)}</td>
    <td>${badge(d.status)}${d.approver ? `<div class="muted">${esc(d.approver)}</div>` : ""}</td>
    <td>${d.photo_path ? `<img class="photo-thumb" src="/photos/${esc(esc(basename(d.photo_path)))}">`
      : `<button class="btn small ghost" data-did="${d.id}" data-act="photo">上传损坏照片</button>`}</td>
    <td class="row">
      ${["PENDING", "DISPUTED"].includes(d.status)
        ? `<button class="btn small" data-did="${d.id}" data-act="approve">认赔</button>
           <button class="btn small wood" data-did="${d.id}" data-act="dispute">申诉</button>
           <button class="btn small danger" data-did="${d.id}" data-act="waive">负责人减免</button>`
        : ""}
    </td></tr>`).join("")}</tbody></table>`;
}
function settlementTable() {
  const closed = S.sessions.filter(s => s.status === "CLOSED");
  if (!closed.length) return `<div class="empty">暂无已结算场次</div>`;
  return `<table class="data"><thead><tr>
    <th>场次</th><th>预约</th><th>桌台</th><th>实际用时</th><th>台费</th><th>赔扣</th><th>押金退</th><th>付款流水</th><th>退款复核</th>
    </tr></thead><tbody>${closed.map(s => {
      const pays = S.payments.filter(p => p.reservation_id === s.reservation_id);
      return `<tr>
      <td>#${s.id}</td><td>#${s.reservation_id}</td><td>${esc(tableById(s.table_id).label)}</td>
      <td>${s.minutes_used ?? "—"} 分钟</td>
      <td class="amount">¥${(s.total_fee || 0).toFixed(0)}</td>
      <td class="amount dr">¥${(s.compensated || 0).toFixed(0)}</td>
      <td class="amount cr">¥${(s.refund || 0).toFixed(0)}</td>
      <td>${pays.map(p => `<div class="mono">${p.kind === "REFUND" ? "退款" : "赔付"} ¥${p.amount.toFixed(0)} ${badge(p.status)}</div>`).join("") || "—"}</td>
      <td><button class="btn small ghost" data-rid="${s.reservation_id}" data-act="refund">再次退款（幂等）</button></td>
      </tr>`;
    }).join("")}</tbody></table>`;
}
viewHooks.review = () => $$("#main [data-act]").forEach(b => b.onclick = () => {
  const actName = b.dataset.act;
  if (actName === "photo") uploadPhoto(Number(b.dataset.did));
  if (actName === "approve") act(() => api(`/api/damages/${b.dataset.did}/resolve`, { action: "approve" }), "已扣款（模拟网关）");
  if (actName === "dispute") act(() => api(`/api/damages/${b.dataset.did}/resolve`, { action: "dispute" }), "已登记赔付争议");
  if (actName === "waive") {
    modal(`<h3>赔付减免 · 需社区负责人批准</h3>
      <label class="fld">批准人姓名/工号 <input id="approver" placeholder="如：社区负责人·周伯"></label>
      <div class="foot"><button class="btn ghost" data-close>取消</button>
      <button class="btn danger" id="okWaive">确认减免</button></div>`, () => {
      $("#okWaive").onclick = async () => {
        const approver = $("#approver").value.trim();
        if (!approver) return toast("必须填写社区负责人批准人", true);
        closeModal();
        await act(() => api(`/api/damages/${b.dataset.did}/resolve`, { action: "waive", approver }), "减免已批准并记录");
      };
    });
  }
  if (actName === "refund") act(async () => {
    const j = await api(`/api/reservations/${b.dataset.rid}/refund-retry`, {});
    toast(j.idempotent_replay ? "网关幂等重放：未重复出账" : "退款成功");
  });
});
const basename = (p) => p.split(/[\\/]/).pop();

// ============================================================ 预约详情弹窗
function openRes(rid) {
  const r = resById(rid); if (!r) return;
  const t = tableById(r.table_id), m = memberByCode(r.member_code);
  const sess = sessionByRes(rid);
  modal(`<h3>预约 #${r.id} ${badge(r.status)}</h3>
    <div class="card" style="background:#fffdf8">
      <p><b>桌台：</b>${esc(t.room_name)} · ${esc(t.label)}（${esc(t.kind)}，¥${t.rate_30m}/30分）</p>
      <p><b>会员：</b>${esc(m ? `${m.code} · ${m.name}（余额 ¥${m.credit}）` : r.member_code)}</p>
      <p><b>时段：</b><span class="mono">${r.start_ts.replace("T", " ")} ~ ${r.end_ts.replace("T", " ")}</span></p>
      <p><b>棋类：</b>${esc(r.game)}　<b>牌具：</b>${kitList(r).map(k => esc(k.name)).join("、") || "无"}</p>
      <p><b>茶水押金：</b>¥${r.tea_deposit}　${r.note ? `<b>备注：</b>${esc(r.note)}` : ""}</p>
      ${sess ? `<p><b>场次：</b>#${sess.id} ${badge(sess.status)} 计划至 ${HM(sess.planned_end)}</p>` : ""}
    </div>
    <div class="muted" style="margin:10px 0">事件：</div>
    <div>${S.incidents.filter(i => i.reservation_id === rid).slice(0, 8)
      .map(i => `<div class="incident-line"><span class="k">${esc(i.kind)}</span>${esc(i.detail)}</div>`).join("") || "<div class='empty'>无</div>"}</div>
    <div class="foot">
      ${r.status === "CONFIRMED" ? `<button class="btn ghost" id="mCancel">取消预约</button><button class="btn" id="mSeat">开场解锁</button>` : ""}
      ${sess && (sess.status === "PLAYING" || sess.status === "EXTENDED") ?
        `<button class="btn wood" id="mMove">管理员调桌</button><button class="btn" id="mExt">续时</button><button class="btn" id="mClose">散场清点</button>` : ""}
    </div>`, () => {
    $("#mCancel") && ($("#mCancel").onclick = () => { closeModal(); act(() => api(`/api/reservations/${rid}/cancel`), "预约已取消"); });
    $("#mSeat") && ($("#mSeat").onclick = () => { closeModal(); act(() => api(`/api/reservations/${rid}/seat`), "门锁已开"); });
    $("#mMove") && ($("#mMove").onclick = () => { closeModal(); openMove(rid); });
    $("#mExt") && ($("#mExt").onclick = () => {
      const min = Number(prompt("续时多少分钟？（系统将自动避开后续预约）", "30"));
      if (!min) return;
      closeModal();
      act(async () => { const j = await api(`/api/reservations/${rid}/extend`, { minutes: min });
        if (j.truncated) toast("已续时，但被后续预约截断"); }, "续时成功");
    });
    $("#mClose") && ($("#mClose").onclick = () => { closeModal(); openChecklist(rid, "CLOSE"); });
  });
}

// ============================================================ 新建预约
function openBooking(prefTable) {
  const tOpts = S.tables.map(t => `<option value="${t.id}" ${prefTable === t.id ? "selected" : ""}
    ${t.status === "MAINTENANCE" ? "disabled" : ""}>${esc(t.room_name)} · ${esc(t.label)}（${esc(t.kind)} ¥${t.rate_30m}/30分 ${t.status === "CLEANING" ? "清洁中" : t.status === "MAINTENANCE" ? "维修中" : ""}）</option>`).join("");
  const mOpts = S.members.map(m => `<option value="${m.code}">${m.code} · ${esc(m.name)}（余额¥${m.credit}）</option>`).join("");
  const start = S.now.slice(0, 16);
  const endD = new Date(S.now.replace(" ", "T")); endD.setMinutes(endD.getMinutes() + 120);
  const end = endD.toISOString().slice(0, 16);
  modal(`<h3>新建预约 <span class="muted">确认后同时锁定桌台与牌具</span></h3>
    <div class="grid cols-2">
      <label class="fld">桌台<select id="bTable">${tOpts}</select></label>
      <label class="fld">会员代号<select id="bMember">${mOpts}</select></label>
      <label class="fld">开始<input type="datetime-local" id="bStart" value="${start}"></label>
      <label class="fld">结束<input type="datetime-local" id="bEnd" value="${end}"></label>
      <label class="fld">茶水押金（演示）<input type="number" id="bDeposit" value="50" step="10"></label>
      <label class="fld">备注<input id="bNote" placeholder="如：需要安静"></label>
    </div>
    <label class="fld" style="margin-top:10px">牌具套装（按桌台过滤，可多选共享柜）<select id="bKits" multiple size="4" style="width:100%"></select></label>
    <div class="foot"><button class="btn ghost" data-close>取消</button>
      <button class="btn" id="bOk">确认预约（双锁）</button></div>`, () => {
    const fillKits = () => {
      const tid = Number($("#bTable").value);
      const t = tableById(tid);
      $("#bKits").innerHTML = S.sets.filter(k => k.status !== "DAMAGED" &&
        (k.home_table_id === tid || k.home_table_id == null || k.game === t.kind))
        .map(k => `<option value="${k.id}">${esc(k.name)}（${k.item_count}件）${badge(k.status)}</option>`).join("");
      $("#bKits").selectedIndex = 0;
    };
    $("#bTable").onchange = fillKits; fillKits();
    $("#bOk").onclick = async () => {
      const body = {
        table_id: Number($("#bTable").value),
        member_code: $("#bMember").value,
        start: $("#bStart").value, end: $("#bEnd").value,
        tea_deposit: Number($("#bDeposit").value || 0),
        note: $("#bNote").value,
        kit_ids: $$("#bKits option:checked").map(o => Number(o.value)),
      };
      closeModal();
      await act(() => api("/api/reservations", body), "预约成功：桌台与牌具已锁定");
    };
  });
}

// ============================================================ 调桌
function openMove(rid) {
  const r = resById(rid);
  const opts = S.tables.filter(t => t.id !== r.table_id)
    .map(t => `<option value="${t.id}" ${t.status === "MAINTENANCE" ? "disabled" : ""}>
      ${esc(t.room_name)} · ${esc(t.label)}（${esc(t.kind)} ${t.status}）</option>`).join("");
  modal(`<h3>管理员调桌 · 预约 #${rid}</h3>
    <p class="muted">牌具随客转移，旧桌立即进入清洁队列；目标桌台时段被占用时无法调入。</p>
    <div class="grid cols-2" style="margin:10px 0">
      <label class="fld">目标桌台<select id="mvTable">${opts}</select></label>
      <label class="fld">操作管理员<input id="mvApprover" value="管理员·林姐"></label>
    </div>
    <div class="foot"><button class="btn ghost" data-close>取消</button>
      <button class="btn wood" id="mvOk">执行调桌</button></div>`, () => {
    $("#mvOk").onclick = async () => {
      const approver = $("#mvApprover").value.trim();
      if (!approver) return toast("请填写管理员", true);
      const target = Number($("#mvTable").value);
      closeModal();
      await act(() => api(`/api/reservations/${rid}/move`, { target_table_id: target, approver }),
        "调桌完成，旧桌已进入清洁队列");
    };
  });
}

// ============================================================ 清点清单（固定数量格，缺件保留空框）
async function openChecklist(rid, phase) {
  const r = resById(rid);
  const slots = await api(`/api/reservations/${rid}/slots`).then(j => j.slots);
  const marks = {};
  const groups = Object.entries(slots).map(([sid, g]) => {
    marks[sid] = {};
    const cells = g.slots.map(sl => `
      <div class="slot present" data-sid="${sid}" data-slot="${sl.slot_no}">
        <span class="dot"></span><span class="lb" title="${esc(sl.label)}">${esc(sl.label)}</span>
      </div>`).join("");
    return `<div class="kit-group">
      <h3>${esc(g.name)} <span class="muted">${esc(g.game)} · ${g.slots.length} 件${g.timer_sn ? `，末格为计时器 ${esc(g.timer_sn)}` : ""}</span></h3>
      <div class="slot-grid">${cells}</div></div>`;
  }).join("") || `<div class="empty">本场次未选牌具（直接确认即可）</div>`;
  modal(`<h3>${phase === "OPEN" ? "开场清点" : "散场清点"} · 预约 #${rid}</h3>
    <p class="muted">点击格子切换 <b>在位 / 缺失</b>。缺件位置保留空框；开场即缺自动登记争议，散场少件生成赔案。</p>
    <div id="slotArea" style="max-height:52vh;overflow:auto">${groups}</div>
    ${phase === "CLOSE" ? `
      <div class="row" style="margin-top:12px">
        <label><input type="checkbox" id="cSpill"> 茶水泼洒（从押金扣清洁费 ¥<input type="number" id="cSpillAmt" value="20" style="width:64px">）</label>
        <label><input type="checkbox" id="cFault"> 设备故障</label>
        <input id="cFaultNote" placeholder="故障描述（如：自动麻将机卡牌）" style="flex:1;min-width:160px">
      </div>` : ""}
    <div class="foot"><button class="btn ghost" data-close>取消</button>
      <button class="btn" id="ckOk">${phase === "OPEN" ? "确认开场清单" : "确认散场并按实际时长结算"}</button></div>`, () => {
    $$("#slotArea .slot").forEach(el => el.onclick = () => {
      el.classList.toggle("present");
      el.classList.toggle("missing");
      const [sid, slot] = [el.dataset.sid, el.dataset.slot];
      marks[sid][slot] = { present: el.classList.contains("present") };
    });
    $("#ckOk").onclick = async () => {
      // 默认全部在位：仅提交被切为缺失的
      const payload = {};
      for (const [sid, mm] of Object.entries(marks)) {
        for (const [slot, v] of Object.entries(mm)) if (!v.present) (payload[sid] ||= {})[slot] = v;
      }
      closeModal();
      if (phase === "OPEN") {
        await act(() => api(`/api/reservations/${rid}/open-check`, { marks: payload }),
          "开场清点完成");
      } else {
        await act(async () => {
          const j = await api(`/api/reservations/${rid}/close-check`, {
            marks: payload,
            spill: $("#cSpill").checked,
            spill_amount: Number($("#cSpillAmt").value || 20),
            device_fault: $("#cFault").checked,
            fault_note: $("#cFaultNote").value,
          });
          if (!j.auto_settled) toast(`散场清点完成，${j.damage_ids.length} 笔赔案待处理后才能结算`);
          else toast("牌具完整，已结算并进入清洁队列");
        });
      }
    };
  });
}

// ============================================================ 照片上传（本地目录留存）
function uploadPhoto(did) {
  const inp = document.createElement("input");
  inp.type = "file"; inp.accept = "image/*";
  inp.onchange = () => {
    const f = inp.files[0]; if (!f) return;
    const rd = new FileReader();
    rd.onload = async () => {
      const b64 = rd.result.split(",")[1];
      await act(() => api(`/api/damages/${did}/photo`, { filename: f.name, data: b64 }),
        "损坏照片已保存到本地目录 data/photos");
    };
    rd.readAsDataURL(f);
  };
  inp.click();
}

// ============================================================ modal 基础
function modal(html, after) {
  $("#modal").innerHTML = html;
  $("#modalMask").classList.remove("hidden");
  $$("#modal [data-close]").forEach(b => b.onclick = closeModal);
  (after || (() => {}))();
}
function closeModal() { $("#modalMask").classList.add("hidden"); }
$("#modalMask").addEventListener("click", (e) => { if (e.target.id === "modalMask") closeModal(); });
function confirmModal(text, onOk) {
  modal(`<h3>请确认</h3><p>${esc(text)}</p>
    <div class="foot"><button class="btn ghost" data-close>取消</button>
    <button class="btn danger" id="cfOk">确认</button></div>`,
    () => $("#cfOk").onclick = () => { closeModal(); onOk(); });
}

// ============================================================ 轮询
setInterval(() => refresh(true).catch(() => {}), 20000);
refresh().catch(e => toast(e.message, true));
