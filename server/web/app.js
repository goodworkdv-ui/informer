"use strict";
/* Портфельный Информер v4 — веб-клиент (PWA). Данные — с домашнего сервера, опрос раз в 300 с. */

const $ = s => document.querySelector(s);
const main = $("#main");
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const store = {
  get(k, d) { try { const v = localStorage.getItem("pi_" + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("pi_" + k, JSON.stringify(v)); } catch (e) {} },
};
const S = { route: "portfolio", pid: store.get("pid", null), instId: null, tf: "1d", offline: null, busy: false,
            options: null, lastLoad: null };

const CLASS_RU = { equity_us: "Акция США", equity_ru: "Акция", bond_ru: "Облигация", futures: "Фьючерс", currency: "Валюта" };
const RATING_RU = { strong_buy: "Активно покупать", buy: "Покупать", neutral: "Нейтрально", sell: "Продавать", strong_sell: "Активно продавать", na: "н/д" };
const SIG_RU = { buy: "Покупать", sell: "Продавать", neutral: "Нейтрально", na: "н/д" };

/* ---------- форматирование ---------- */
const nf = (v, d = 2, min) => v == null || !isFinite(v) ? "—" :
  Number(v).toLocaleString("ru-RU", { minimumFractionDigits: min ?? d, maximumFractionDigits: d });
function fmtPrice(p) {
  if (p == null) return "—";
  const a = Math.abs(p), d = a >= 10000 ? 0 : a >= 100 ? 2 : a >= 1 ? 4 : 6;
  return nf(p, d, Math.min(d, 2));
}
const cur = c => c === "USD" ? "$" : "₽";
const money = (v, c) => v == null ? "—" : nf(v, 2) + " " + cur(c);
const signed = (v, d = 2) => v == null || !isFinite(v) ? "—" : (v > 0 ? "+" : v < 0 ? "−" : "") + nf(Math.abs(v), d);
const cls = v => v > 0 ? "up" : v < 0 ? "down" : "";
const val4 = v => v == null ? "н/д" : Math.abs(v) >= 1000 ? nf(v, 2) : Math.abs(v) >= 1 ? nf(v, 4, 2) : nf(v, 4);
function mskTime(iso, withDate = true) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("ru-RU", { timeZone: "Europe/Moscow", hour: "2-digit", minute: "2-digit",
    ...(withDate ? { day: "2-digit", month: "2-digit" } : {}) });
}
const dmy = s => s ? s.slice(0, 10).split("-").reverse().join(".") : "—";

/* ---------- API ---------- */
class AuthError extends Error {}
async function api(path, opts = {}) {
  const token = store.get("token", "");
  const init = { method: opts.method || "GET", headers: { "X-Token": token } };
  if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  let r;
  try {
    r = await fetch(path, init);
  } catch (e) {
    if (init.method === "GET") {
      const c = store.get("c:" + path, null);
      if (c) { S.offline = c.t; return c.data; }
    }
    throw new Error("Нет связи с сервером. Проверьте, что домашний ПК включён и туннель работает.");
  }
  if (r.status === 401) throw new AuthError("Нужен токен доступа");
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : "Ошибка " + r.status);
  if (init.method === "GET") { S.offline = null; store.set("c:" + path, { t: new Date().toISOString(), data }); }
  return data;
}

/* ---------- маршруты ---------- */
function parseHash() {
  const h = location.hash.slice(1);
  let m;
  if ((m = h.match(/^inst-(\d+)$/))) return { route: "inst", instId: +m[1] };
  if ((m = h.match(/^portfolio-(\d+)$/))) return { route: "portfolio", pid: +m[1] };
  if (h === "alerts" || h === "status") return { route: h };
  return { route: "portfolio" };
}
window.addEventListener("hashchange", () => { Object.assign(S, parseHash()); if (S.route === "inst") S.tf = "1d"; load(); });

function setChrome(title, back) {
  $("#title").textContent = title;
  $("#backBtn").hidden = !back;
  const tab = S.route === "inst" ? "portfolio" : S.route;
  document.querySelectorAll("nav.tabs button").forEach(b => b.dataset.go === tab ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current"));
  $("#banner").innerHTML = S.offline ? `<div class="banner">Нет связи с сервером. Показаны сохранённые данные от ${esc(mskTime(S.offline))} МСК.</div>` : "";
  $("#stamp").innerHTML = S.lastLoad ? "Обновлено<br>" + S.lastLoad.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }) : "";
}

async function load() {
  if (S.busy) return;
  S.busy = true; $("#refreshBtn").classList.add("spin");
  try {
    if (S.route === "inst") await viewInstrument();
    else if (S.route === "alerts") await viewAlerts();
    else if (S.route === "status") await viewStatus();
    else await viewPortfolio();
    S.lastLoad = new Date();
  } catch (e) {
    if (e instanceof AuthError) viewToken();
    else main.innerHTML = `<div class="panel"><h3>Не удалось загрузить</h3><p>${esc(e.message)}</p><div class="btnrow"><button class="btn" data-act="reload">Повторить</button></div></div>`;
  } finally {
    S.busy = false; $("#refreshBtn").classList.remove("spin");
    const titles = { alerts: "Алерты", status: "Состояние" };
    setChrome(titles[S.route] || $("#title").textContent, S.route === "inst");
  }
}

/* ---------- токен ---------- */
function viewToken() {
  main.innerHTML = `<div class="panel">
    <h3>Вход</h3>
    <p>Введите токен доступа — значение APP_TOKEN из файла .env на домашнем ПК. Он сохранится только на этом устройстве.</p>
    <label class="f" for="tokenIn">Токен<input class="i" id="tokenIn" type="password" autocomplete="current-password" autocapitalize="off"></label>
    <div class="btnrow"><button class="btn" data-act="saveToken">Войти</button></div></div>`;
}

/* ---------- портфели ---------- */
async function viewPortfolio() {
  const list = await api("/api/portfolios");
  if (!list.length) {
    setChrome("Портфели", false);
    main.innerHTML = `<div class="group"><div class="list"><div class="empty">Портфелей пока нет.
      <button class="btn" data-act="newPortfolio">Создать портфель</button></div></div></div>`;
    return;
  }
  if (S.pid == null || !list.some(p => p.id === S.pid)) S.pid = list[0].id;
  store.set("pid", S.pid);
  const v = await api("/api/portfolios/" + S.pid);
  const chips = list.map(p => `<button class="chip" data-pid="${p.id}" aria-pressed="${p.id === S.pid}">${esc(p.name)}</button>`).join("")
    + `<button class="chip add" data-act="newPortfolio">+ Портфель</button>`;
  const totals = ["USD", "RUB"].filter(c => v.totals[c].count).map(c => {
    const t = v.totals[c];
    return `<div class="total"><div class="cur">Итого ${c}</div><div class="val">${money(t.value, c)}</div>
      <div class="pnl ${cls(t.pnl)}">${signed(t.pnl)} ${cur(c)}${t.pnl_pct != null ? ` · ${signed(t.pnl_pct)}%` : ""}</div>
      ${t.missing ? `<div class="muted" style="font-size:12px">без котировки: ${t.missing}</div>` : ""}</div>`;
  }).join("");
  const imb = v.imbalance.length ? `<div class="banner" style="margin-top:10px">Дисбаланс (порог ${nf(v.threshold, 0)}%):<br>${v.imbalance.map(esc).join("<br>")}</div>` : "";
  const rows = v.positions.map(posRow).join("");
  main.innerHTML = `<div class="chips">${chips}</div>
    ${totals ? `<div class="totals">${totals}</div>` : ""}${imb}
    <div class="muted" style="font-size:12px;margin-top:8px">Котировки: ${esc(v.delay_label)}, обновление раз в 5 мин</div>
    <div class="group"><h2><span>${v.positions.length} поз.</span>
      <span class="btnrow"><button class="txtbtn" data-act="pfMenu">Портфель…</button><button class="txtbtn" data-act="addPos">+ Позиция</button></span></h2>
      <div class="list">${rows || `<div class="empty">В портфеле нет позиций.<button class="btn" data-act="addPos">Добавить позицию</button></div>`}</div></div>`;
  setChrome(v.portfolio.name, false);
}

function posRow(r) {
  const unit = r.asset_class === "equity_ru" || r.asset_class === "currency" ? ` лот × ${nf(r.lot_size, 0)}` :
               r.asset_class === "futures" ? " контр." : " шт.";
  const meta = [];
  meta.push(`<span class="muted num" style="font-size:12.5px">${fmtPrice(r.price)}${r.asset_class === "bond_ru" ? "%" : ""}
    ${r.change_pct != null ? `<span class="${cls(r.change_pct)}">${signed(r.change_pct)}%</span>` : ""}</span>`);
  if (r.asset_class === "bond_ru") meta.push(`<span class="pill">YTM ${r.yield != null ? nf(r.yield) + "%" : "н/д"}</span>`);
  else meta.push(`<span class="pill ${r.rating ? r.rating.rating : ""}">${r.rating ? esc(r.rating.label) : "ТА: нет данных"}</span>`);
  if (r.share_pct != null) meta.push(`<span class="muted" style="font-size:12px">доля ${nf(r.share_pct, 1)}%</span>`);
  if (r.expired) meta.push(`<span class="pill sell">истёк</span>`);
  else if (r.liquidity_warning) meta.push(`<span class="pill warn">экспирация ${dmy(r.expiry_date)}</span>`);
  if (r.age_min != null && r.age_min > 20) meta.push(`<span class="pill warn">данные ${Math.round(r.age_min)} мин назад</span>`);
  meta.push(`<button class="txtbtn" data-edit="${r.id}" style="margin-left:auto">Изменить</button>`);
  return `<div class="row">
    <a class="tick" href="#inst-${r.instrument_id}" style="color:inherit;text-decoration:none;display:grid;gap:1px;min-width:0">
      <span class="tick">${esc(r.code)} <span class="badge ${r.exchange === "NASDAQ" ? "us" : ""}">${r.exchange}</span>
      <span class="muted num" style="font-weight:500;font-size:12.5px">${r.quantity > 0 && r.asset_class === "futures" ? "+" : ""}${r.quantity}${unit}</span></span>
      <span class="sub">${esc(r.name || "")} · вход ${fmtPrice(r.avg_price)}</span></a>
    <div class="r"><div class="big">${r.asset_class === "futures" ? "вар. " : ""}${money(r.value, r.currency)}</div>
      <div class="${cls(r.pnl)}" style="font-size:13px">${signed(r.pnl)}${r.pnl_pct != null ? ` (${signed(r.pnl_pct)}%)` : ""}</div></div>
    <div class="meta">${meta.join("")}</div></div>`;
}

/* ---------- карточка бумаги ---------- */
async function viewInstrument() {
  const c = await api(`/api/instruments/${S.instId}?tf=${S.tf}`);
  const i = c.instrument, q = c.quote || {};
  setChrome(i.code, true);
  const ch = q.last && q.prev ? 100 * (q.last / q.prev - 1) : null;
  let html = `<div class="pricebox"><span class="p">${fmtPrice(q.last)}${i.asset_class === "bond_ru" ? "%" : " " + cur(i.currency)}</span>
    <span class="c ${cls(ch)}">${signed(ch)}%</span></div>
    <div class="muted" style="font-size:12.5px">${esc(i.name || "")} · ${CLASS_RU[i.asset_class] || ""} · ${i.exchange}<br>
      Данные: ${esc(mskTime(q.ts))} МСК · ${esc(c.delay_label)}${c.age_min != null && c.age_min > 20 ? ` · получены ${Math.round(c.age_min)} мин назад` : ""}</div>
    <div class="btnrow" style="margin-top:10px"><button class="btn small ghost" data-newalert="PRICE">Алерт по цене</button>
      ${i.asset_class !== "bond_ru" ? `<button class="btn small ghost" data-newalert="INDICATOR">Алерт по индикатору</button>
      <button class="btn small ghost" data-newalert="SIGNAL_CHANGE">Алерт на смену сигнала</button>` : ""}</div>`;
  if (c.warning) html += `<div class="banner" style="margin-top:10px">${esc(c.warning)}</div>`;
  if (i.asset_class === "futures") html += futuresBlock(c);
  if (i.asset_class === "bond_ru") html += bondBlock(c);
  else html += taBlock(c);
  main.innerHTML = html;
}

function futuresBlock(c) {
  const i = c.instrument;
  return `<div class="panel"><h3>Контракт</h3>
    ${c.expired ? `<div class="banner">Контракт истёк. Удалите позицию вручную.</div>` :
      c.liquidity_warning ? `<div class="banner">До экспирации ${c.trading_days_left} торг. дн.: ликвидность падает.</div>` : ""}
    <dl class="kv"><dt>Шаг цены</dt><dd>${val4(i.min_step)}</dd><dt>Стоимость шага</dt><dd>${nf(i.step_price, 5, 2)} ₽</dd>
    <dt>ГО (справочно)</dt><dd>${money(i.initial_margin, "RUB")}</dd><dt>Экспирация</dt><dd>${dmy(i.expiry_date)}</dd>
    <dt>Торговых дней до экспирации</dt><dd>${c.trading_days_left ?? "—"}</dd></dl></div>`;
}

function bondBlock(c) {
  const i = c.instrument, b = c.bond || {};
  const today = new Date().toISOString().slice(0, 10);
  const sched = (c.schedule || []).filter(s => s.date >= today).slice(0, 12);
  const TY = { coupon: "Купон", amortization: "Амортизация", redemption: "Погашение", offer: "Оферта" };
  return `<div class="panel"><h3>Облигация</h3><dl class="kv">
    <dt>Чистая цена</dt><dd>${nf(b.clean_pct, 3)}% · ${money(b.clean_rub, "RUB")}</dd>
    <dt>НКД</dt><dd>${money(b.accrued_interest, "RUB")}</dd>
    <dt>Полная цена</dt><dd><b>${money(b.full_price_rub, "RUB")}</b></dd>
    <dt>Доходность к погашению</dt><dd>${b.yield != null ? nf(b.yield) + "%" : "н/д"}</dd>
    <dt>Ставка купона</dt><dd>${i.coupon_rate != null ? nf(i.coupon_rate) + "%" : "—"}${i.coupon_value ? " · " + money(i.coupon_value, "RUB") : ""}</dd>
    <dt>Ближайший купон</dt><dd>${dmy(i.next_coupon)}</dd>
    <dt>Погашение</dt><dd>${dmy(i.maturity_date)}</dd>${i.offer_date ? `<dt>Оферта</dt><dd>${dmy(i.offer_date)}</dd>` : ""}
    <dt>Текущий номинал</dt><dd>${money(i.face_value, "RUB")}</dd></dl>
    <p>Технический анализ для облигаций не выполняется (решение 6). Ниже — цена по дням.</p>
    ${lineChart(c.history || [])}</div>
    <div class="group"><h2><span>График выплат</span><span>${sched.length} ближайших</span></h2>
      <div class="tablewrap"><table class="ind"><tr><th>Дата</th><th>Тип</th><th style="text-align:right">На 1 бумагу</th></tr>
      ${sched.map(s => `<tr><td>${dmy(s.date)}</td><td>${TY[s.type] || s.type}</td><td class="v">${s.amount != null ? money(s.amount, "RUB") : "—"}</td></tr>`).join("") ||
        `<tr><td colspan="3" class="muted">Нет данных о выплатах</td></tr>`}</table></div></div>`;
}

function lineChart(pts) {
  if (pts.length < 2) return `<p>История цены пока не загружена.</p>`;
  const W = 600, H = 180, P = 28;
  const ys = pts.map(p => p.c), lo = Math.min(...ys), hi = Math.max(...ys), span = hi - lo || 1;
  const x = i => P + (W - 2 * P) * i / (pts.length - 1), y = v => H - P - (H - 2 * P) * (v - lo) / span;
  const d = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.c).toFixed(1)}`).join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Цена по дням">
    <line x1="${P}" x2="${W - P}" y1="${y(hi)}" y2="${y(hi)}" stroke="var(--line)"/><line x1="${P}" x2="${W - P}" y1="${y(lo)}" y2="${y(lo)}" stroke="var(--line)"/>
    <text x="${P}" y="${y(hi) - 6}" fill="var(--muted)" font-size="12">${nf(hi, 2)}</text>
    <text x="${P}" y="${y(lo) + 16}" fill="var(--muted)" font-size="12">${nf(lo, 2)}</text>
    <path d="${d}" fill="none" stroke="var(--accent)" stroke-width="2"/>
    <circle cx="${x(pts.length - 1)}" cy="${y(ys[ys.length - 1])}" r="4" fill="var(--accent)"/>
    <text x="${W - P}" y="${H - 6}" fill="var(--muted)" font-size="12" text-anchor="end">${dmy(pts[pts.length - 1].t)}</text>
    <text x="${P}" y="${H - 6}" fill="var(--muted)" font-size="12">${dmy(pts[0].t)}</text></svg>`;
}

function gauge(title, s) {
  const r = s.r ?? 0, cx = 110, cy = 100, R = 80;
  const pt = (v, rad) => { const th = Math.PI * (1 - (v + 1) / 2); return [cx + rad * Math.cos(th), cy - rad * Math.sin(th)]; };
  const arc = (a, b, color) => { const [x1, y1] = pt(a, R), [x2, y2] = pt(b, R);
    return `<path d="M${x1.toFixed(1)},${y1.toFixed(1)} A${R},${R} 0 0 1 ${x2.toFixed(1)},${y2.toFixed(1)}" stroke="${color}" stroke-width="16" fill="none"/>`; };
  const [nx, ny] = pt(Math.max(-1, Math.min(1, r)), R - 14);
  return `<div class="gauge"><h4>${title}</h4>
    <svg viewBox="0 0 220 118" role="img" aria-label="${esc(title)}: ${esc(s.label)}">
      ${arc(-1, -0.5, "var(--down)")}${arc(-0.5, -0.1, "color-mix(in srgb, var(--down) 55%, var(--surface))")}
      ${arc(-0.1, 0.1, "var(--surface2)")}${arc(0.1, 0.5, "color-mix(in srgb, var(--up) 55%, var(--surface))")}${arc(0.5, 1, "var(--up)")}
      <text x="22" y="114" font-size="10" fill="var(--muted)">Продавать</text><text x="198" y="114" font-size="10" fill="var(--muted)" text-anchor="end">Покупать</text>
      ${s.r == null ? "" : `<line x1="${cx}" y1="${cy}" x2="${nx.toFixed(1)}" y2="${ny.toFixed(1)}" stroke="var(--fg)" stroke-width="3" stroke-linecap="round"/>`}
      <circle cx="${cx}" cy="${cy}" r="5" fill="var(--fg)"/></svg>
    <div class="rec ${s.rating === "buy" || s.rating === "strong_buy" ? "up" : s.rating === "sell" || s.rating === "strong_sell" ? "down" : ""}">${esc(s.label)}</div>
    <div class="cnt">Покупать: (${s.buy}) Нейтрально: (${s.neutral}) Продавать: (${s.sell})${s.na ? ` · н/д: ${s.na}` : ""}</div></div>`;
}

function taBlock(c) {
  const ta = c.ta || {};
  const strip = `<div class="tfstrip" role="group" aria-label="Таймфрейм">${c.overview.map(o =>
    `<button data-tf="${o.tf}" aria-pressed="${o.tf === S.tf}"><b>${esc(o.label)}</b><span class="dot ${o.rating || ""}" title="${esc(o.text)}"></span></button>`).join("")}</div>`;
  let body;
  if (ta.status === "na") body = `<div class="panel"><p>${esc(ta.message)}</p></div>`;
  else if (ta.status !== "ok") body = `<div class="panel"><p>Свечей для этого таймфрейма пока нет. ${ta.warning ? esc(ta.warning) : "Проверьте ключи провайдеров на вкладке «Состояние»."}</p></div>`;
  else {
    const s = ta.summary;
    const rows = (title, items) => `<tr class="sec"><td colspan="3">${title}</td></tr>` + items.map(it =>
      `<tr><td>${esc(it.name)}</td><td class="v">${val4(it.value)}</td><td class="s-${it.signal}">${SIG_RU[it.signal]}</td></tr>`).join("");
    const atr = ta.reference.atr, hl = ta.reference.highs_lows;
    body = `${ta.warning ? `<div class="banner" style="margin-top:10px">${esc(ta.warning)}</div>` : ""}
      <div class="muted" style="font-size:12.5px;margin-top:10px">По закрытой свече до ${esc(mskTime(ta.candle_close_time))} МСК · свечей: ${ta.candles}
        ${ta.live ? ` · формирующаяся свеча <span class="pill warn">live</span> ${fmtPrice(ta.live.close)} в расчёт не входит` : ""}</div>
      <div class="gauges">${gauge("Скользящие средние", s.mas)}${gauge("Осцилляторы", s.oscillators)}${gauge("Резюме", s.total)}</div>
      <div class="group"><h2><span>Индикаторы</span><span>время свечи: ${esc(mskTime(ta.candle_time))}</span></h2>
        <div class="tablewrap"><table class="ind"><tr><th>Индикатор</th><th style="text-align:right">Значение</th><th>Сигнал</th></tr>
        ${rows("Осцилляторы (12)", ta.oscillators)}${rows("Скользящие средние (15)", ta.mas)}</table></div></div>
      <div class="panel"><h3>Справочно (не голосуют)</h3><dl class="kv">
        <dt>ATR (14)</dt><dd>${val4(atr.value)} · ${atr.pct != null ? nf(atr.pct) + "%" : "н/д"}</dd>
        <dt>Волатильность</dt><dd>${atr.label ? (atr.label === "Норма" ? "Норма" : `<span class="pill warn">${atr.label}</span>`) : "н/д"}</dd>
        <dt>Положение в диапазоне 14 свечей</dt><dd>${hl.position_pct != null ? nf(hl.position_pct, 1) + "%" : "н/д"}</dd>
        <dt>До максимума ${hl.high != null ? "(" + fmtPrice(hl.high) + ")" : ""}</dt><dd>${hl.to_high_pct != null ? nf(hl.to_high_pct) + "%" : "н/д"}</dd>
        <dt>До минимума ${hl.low != null ? "(" + fmtPrice(hl.low) + ")" : ""}</dt><dd>${hl.to_low_pct != null ? nf(hl.to_low_pct) + "%" : "н/д"}</dd></dl></div>`;
  }
  return `<div class="group"><h2><span>Технический анализ</span></h2>${strip}${body}</div>`;
}

/* ---------- алерты ---------- */
async function viewAlerts() {
  const list = await api("/api/alerts");
  const TYPE = { PRICE: "Цена", INDICATOR: "Индикатор", SIGNAL_CHANGE: "Смена сигнала" };
  main.innerHTML = `<div class="group"><h2><span>${list.length} алертов · уведомления в Telegram</span>
      <button class="txtbtn" data-newalert="PRICE">+ Новый алерт</button></h2>
    <div class="list">${list.map(a => `<div class="row">
      <a href="#inst-${a.instrument_id}" style="color:inherit;text-decoration:none;min-width:0;display:grid;gap:1px">
        <span class="tick">${esc(a.code)} <span class="pill">${TYPE[a.type]}</span></span>
        <span class="sub">${esc(a.description)}</span></a>
      <div class="r"><button class="btn small ${a.is_active ? "" : "ghost"}" data-toggle="${a.id}" data-on="${a.is_active ? 0 : 1}">${a.is_active ? "Вкл" : "Выкл"}</button></div>
      <div class="meta muted" style="font-size:12px">${a.repeat ? "повторяемый" : "однократный"}
        ${a.last_fired_at ? ` · сработал ${esc(mskTime(a.last_fired_at))}` : ""}
        <button class="txtbtn" data-editalert="${a.id}" style="margin-left:auto">Изменить</button>
        <button class="txtbtn" data-delalert="${a.id}" style="color:var(--down)">Удалить</button></div></div>`).join("") ||
      `<div class="empty">Алертов нет. Создайте первый: цена, значение индикатора или смена рекомендации.
        <button class="btn" data-newalert="PRICE">Создать алерт</button></div>`}</div></div>`;
  S.alerts = list;
}

/* ---------- состояние и настройки ---------- */
async function viewStatus() {
  const [h, st] = await Promise.all([api("/api/health"), api("/api/settings")]);
  const prov = h.providers.map(p => `<tr><td>${esc(p.name)}</td>
    <td class="v">${p.age_min != null ? Math.round(p.age_min) + " мин" : "—"}</td>
    <td class="v">${p.left_minute}/${p.per_min}${p.per_day ? ` · ${p.left_day}/${p.per_day} сут` : ""}</td>
    <td style="white-space:normal;min-width:160px" class="${p.last_error ? "s-sell" : ""}">${p.last_error ? esc(p.last_error).slice(0, 140) : p.last_ok ? "OK " + esc(mskTime(p.last_ok)) : "—"}</td></tr>`).join("");
  const key = (ok, name) => `<span class="pill ${ok ? "buy" : "sell"}">${name}: ${ok ? "задан" : "нет"}</span>`;
  const STATUS = { sent: "доставлено", failed: "не доставлено", retry: "повтор", muted: "пауза" };
  main.innerHTML = `
    <div class="panel"><h3>Сервер</h3><dl class="kv">
      <dt>Запущен</dt><dd>${esc(mskTime(h.started_at))}</dd><dt>Последний цикл опроса</dt><dd>${esc(mskTime(h.last_cycle))}</dd>
      <dt>Торги MOEX / Nasdaq</dt><dd>${h.trading.MOEX ? "идут" : "нет"} / ${h.trading.NASDAQ ? "идут" : "нет"}</dd>
      <dt>Активных алертов</dt><dd>${h.alerts_active}</dd><dt>Расчётов индикаторов</dt><dd>${h.ta_calculations}</dd></dl>
      <div class="btnrow">${key(h.keys.finnhub, "Finnhub")}${key(h.keys.twelvedata, "Twelve Data")}${key(h.keys.telegram, "Telegram")}</div>
      <div class="btnrow"><button class="btn small" data-act="cycle">Обновить котировки сейчас</button>
        <button class="btn small ghost" data-act="testTg">Тест Telegram</button></div>
      ${h.cycle_errors.length ? `<div class="log">${h.cycle_errors.map(e => `<div class="e">${esc(e)}</div>`).join("")}</div>` : ""}</div>
    <div class="group"><h2><span>Провайдеры</span></h2><div class="tablewrap"><table class="ind">
      <tr><th>Источник</th><th style="text-align:right">Возраст</th><th style="text-align:right">Лимит</th><th>Статус</th></tr>${prov}</table></div></div>
    <div class="panel"><h3>Настройки</h3><div class="formgrid">
      <label class="f" for="thIn">Порог дисбаланса, %<input class="i" id="thIn" type="number" min="1" step="1" value="${esc(st.allocation_threshold)}"></label>
      <label class="f" for="expIn">Напоминание об экспирации, дней<input class="i" id="expIn" type="number" min="1" step="1" value="${esc(st.expiry_notify_days)}"></label></div>
      <div class="btnrow"><button class="btn small" data-act="saveSettings">Сохранить</button><button class="btn small ghost" data-act="logout">Сменить токен</button></div>
      <p id="setMsg"></p></div>
    <div class="group"><h2><span>Журнал уведомлений</span></h2><div class="log">${h.notifications.map(n =>
      `<div class="${n.status === "failed" ? "e" : ""}">${esc(mskTime(n.created_at))} · ${STATUS[n.status] || n.status} · ${esc(n.text.replace(/<[^>]+>/g, "")).slice(0, 160)}</div>`).join("") || "Пусто"}</div></div>
    <div class="group"><h2><span>Журнал сервера</span></h2><div class="log">${h.log.map(l =>
      `<div class="${l.level === "error" ? "e" : ""}">${esc(mskTime(l.ts))} [${esc(l.source)}] ${esc(l.message)}</div>`).join("") || "Пусто"}</div></div>`;
}

/* ---------- нижние листы (формы) ---------- */
function openSheet(html) { $("#sheet").innerHTML = html; $("#sheet").hidden = false; $("#sheetBg").hidden = false; }
function closeSheet() { $("#sheet").hidden = true; $("#sheetBg").hidden = true; $("#sheet").innerHTML = ""; }
$("#sheetBg").addEventListener("click", closeSheet);

function searchField(placeholder = "Тикер, название или ISIN") {
  return `<label class="f" for="sq">Бумага<input class="i" id="sq" type="search" placeholder="${placeholder}" autocomplete="off" autocapitalize="characters"></label>
    <div id="sres" class="results" hidden></div><div id="schosen"></div>`;
}
let searchTimer = null, chosen = null;
function bindSearch(onChoose) {
  chosen = null;
  const inp = $("#sq");
  inp.addEventListener("input", () => {
    clearTimeout(searchTimer);
    const q = inp.value.trim();
    if (q.length < 2) { $("#sres").hidden = true; return; }
    searchTimer = setTimeout(async () => {
      try {
        const res = await api("/api/search?q=" + encodeURIComponent(q));
        $("#sres").hidden = false;
        $("#sres").innerHTML = res.map((r, k) => `<button data-k="${k}"><span><b>${esc(r.code)}</b> <span class="badge ${r.exchange === "NASDAQ" ? "us" : ""}">${r.exchange}</span>
          <span class="muted" style="font-size:12px">${CLASS_RU[r.asset_class]}</span></span>
          <span class="muted" style="font-size:12.5px">${esc(r.name || "")}${r.isin ? " · " + esc(r.isin) : ""}</span></button>`).join("") ||
          `<div class="muted" style="padding:10px 12px">Ничего не найдено</div>`;
        $("#sres").onclick = e => { const b = e.target.closest("button[data-k]"); if (!b) return;
          chosen = res[+b.dataset.k]; $("#sres").hidden = true; inp.value = chosen.code;
          $("#schosen").innerHTML = `<div class="chosen"><b>${esc(chosen.code)}</b> · ${esc(chosen.name || "")} · ${CLASS_RU[chosen.asset_class]}</div>`;
          onChoose && onChoose(chosen); };
      } catch (e) { $("#sres").hidden = false; $("#sres").innerHTML = `<div class="err" style="padding:10px 12px">${esc(e.message)}</div>`; }
    }, 350);
  });
}

function priceLabel(c) {
  return c === "bond_ru" ? "Средняя чистая цена входа, % номинала" : c === "futures" ? "Средняя цена входа, пункты" : "Средняя цена входа за 1 бумагу";
}
function qtyLabel(c) {
  return c === "equity_ru" || c === "currency" ? "Количество, лотов" : c === "futures" ? "Контрактов (минус — шорт)" : "Количество, шт.";
}

function sheetPosition(pos) {
  const edit = !!pos;
  openSheet(`<h3>${edit ? "Позиция " + esc(pos.code) : "Новая позиция"}</h3>
    ${edit ? `<div class="chosen">${esc(pos.name || "")} · ${CLASS_RU[pos.asset_class]}</div>` : searchField()}
    <div class="formgrid">
      <label class="f" for="pq"><span id="pqL">${qtyLabel(pos?.asset_class)}</span><input class="i" id="pq" type="number" step="1" inputmode="numeric" value="${edit ? pos.quantity : ""}"></label>
      <label class="f" for="pp"><span id="ppL">${priceLabel(pos?.asset_class)}</span><input class="i" id="pp" type="number" step="any" inputmode="decimal" value="${edit ? pos.avg_price : ""}"></label></div>
    <label class="f" for="pd">Дата открытия (необязательно)<input class="i" id="pd" type="date" value="${edit && pos.opened_at ? esc(pos.opened_at) : ""}"></label>
    <div class="err" id="perr"></div>
    <div class="btnrow"><button class="btn" data-act="savePos" data-id="${edit ? pos.id : ""}">Сохранить</button>
      ${edit ? `<button class="btn danger" data-act="delPos" data-id="${pos.id}">Удалить позицию</button>` : ""}
      <button class="btn ghost" data-act="close">Отмена</button></div>`);
  if (!edit) bindSearch(c => { $("#pqL").textContent = qtyLabel(c.asset_class); $("#ppL").textContent = priceLabel(c.asset_class); });
}

async function sheetAlert(type, preset, existing) {
  if (!S.options) S.options = await api("/api/alert-options");
  const o = S.options, a = existing || {};
  type = a.type || type;
  const opt = (obj, sel) => Object.entries(obj).map(([k, v]) => `<option value="${k}" ${k === sel ? "selected" : ""}>${esc(v)}</option>`).join("");
  const targets = (a.target_rating || "").split(",").filter(Boolean);
  openSheet(`<h3>${existing ? "Изменить алерт" : "Новый алерт"}</h3>
    ${preset || existing ? `<div class="chosen"><b>${esc((preset || a).code)}</b> ${esc((preset || a).name || "")}</div>` : searchField()}
    <div class="seg" role="group" aria-label="Тип">${[["PRICE", "Цена"], ["INDICATOR", "Индикатор"], ["SIGNAL_CHANGE", "Смена сигнала"]].map(([k, t]) =>
      `<button data-atype="${k}" aria-pressed="${k === type}">${t}</button>`).join("")}</div>
    <div id="aform" data-type="${type}">
      <div class="formgrid" data-for="INDICATOR SIGNAL_CHANGE"><label class="f" for="atf">Таймфрейм<select class="i" id="atf">${opt(o.timeframes, a.timeframe || "1d")}</select></label>
        <label class="f" for="aind" data-for="INDICATOR">Индикатор<select class="i" id="aind">${opt(o.indicators, a.indicator || "rsi")}</select></label>
        <label class="f" for="acat" data-for="SIGNAL_CHANGE">Оценка<select class="i" id="acat">${opt({ total: "Резюме", mas: "Скользящие средние", oscillators: "Осцилляторы" }, a.category || "total")}</select></label></div>
      <div class="formgrid" data-for="PRICE INDICATOR" style="margin-top:10px"><label class="f" for="aop">Условие<select class="i" id="aop">${opt({ ">": "выше (>)", "<": "ниже (<)" }, a.operator || ">")}</select></label>
        <label class="f" for="ath">Уровень<input class="i" id="ath" type="number" step="any" value="${a.threshold ?? ""}"></label></div>
      <div data-for="SIGNAL_CHANGE" style="margin-top:10px"><div class="f muted" style="font-size:13px">Сообщить при переходе в (ничего не выбрано — при любой смене):</div>
        <div class="btnrow" style="margin-top:6px">${Object.entries(o.ratings).map(([k, v]) =>
          `<label class="chip"><input type="checkbox" class="atg" value="${k}" ${targets.includes(k) ? "checked" : ""}> ${esc(v)}</label>`).join("")}</div>
        <label class="f" for="ak" style="margin-top:10px">Подтверждение, закрытых свечей подряд<input class="i" id="ak" type="number" min="1" step="1" placeholder="по умолчанию: 2 внутри дня, 1 для дня и старше" value="${a.confirm_bars ?? ""}"></label></div>
      <div class="formgrid" style="margin-top:10px"><label class="f" for="arep">Режим<select class="i" id="arep">${opt({ 0: "Однократный", 1: "Повторяемый" }, String(a.repeat ? 1 : 0))}</select></label>
        <label class="f" for="acd">Пауза между срабатываниями, мин<input class="i" id="acd" type="number" min="1" step="1" placeholder="60 / 1440" value="${a.cooldown_min ?? ""}"></label></div></div>
    <div class="err" id="aerr"></div>
    <div class="btnrow"><button class="btn" data-act="saveAlert" data-id="${existing ? a.id : ""}" data-inst="${preset ? preset.id : a.instrument_id || ""}">Сохранить</button>
      <button class="btn ghost" data-act="close">Отмена</button></div>`);
  applyAlertType(type);
  if (!preset && !existing) bindSearch();
}
function applyAlertType(t) {
  $("#aform").dataset.type = t;
  document.querySelectorAll("[data-atype]").forEach(b => b.setAttribute("aria-pressed", b.dataset.atype === t));
  document.querySelectorAll("#aform [data-for]").forEach(el => { el.hidden = !el.dataset.for.split(" ").includes(t); });
}

/* ---------- обработчики ---------- */
document.addEventListener("click", async e => {
  const b = e.target.closest("button"); if (!b) return;
  const d = b.dataset;
  try {
    if (d.go) { location.hash = d.go === "portfolio" ? (S.pid ? "portfolio-" + S.pid : "portfolio") : d.go; return; }
    if (b.id === "backBtn") { history.length > 1 ? history.back() : (location.hash = "portfolio"); return; }
    if (b.id === "refreshBtn") { load(); return; }
    if (d.pid) { S.pid = +d.pid; store.set("pid", S.pid); location.hash = "portfolio-" + S.pid; load(); return; }
    if (d.tf) { S.tf = d.tf; load(); return; }
    if (d.edit) { const v = store.get("c:/api/portfolios/" + S.pid, null); const p = v && v.data.positions.find(x => x.id === +d.edit); if (p) sheetPosition(p); return; }
    if (d.atype) { applyAlertType(d.atype); return; }
    if (d.newalert) {
      let preset = null;
      if (S.route === "inst") { const c = store.get(`c:/api/instruments/${S.instId}?tf=${S.tf}`, null); if (c) preset = c.data.instrument; }
      await sheetAlert(d.newalert, preset); return;
    }
    if (d.editalert) { const a = (S.alerts || []).find(x => x.id === +d.editalert); if (a) await sheetAlert(a.type, null, a); return; }
    if (d.toggle) { await api("/api/alerts/" + d.toggle, { method: "PATCH", body: { is_active: d.on === "1" } }); load(); return; }
    if (d.delalert) { b.textContent = "Точно удалить?"; if (b.dataset.confirm) { await api("/api/alerts/" + d.delalert, { method: "DELETE" }); load(); } b.dataset.confirm = 1; return; }
    switch (d.act) {
      case "reload": load(); break;
      case "close": closeSheet(); break;
      case "saveToken": {
        const t = $("#tokenIn").value.trim();
        if (!/^[\x21-\x7e]+$/.test(t)) { alertBanner("Токен должен состоять из латинских букв и цифр, без пробелов и кириллицы"); break; }
        store.set("token", t); load(); break;
      }
      case "logout": store.set("token", ""); viewToken(); break;
      case "newPortfolio":
        openSheet(`<h3>Новый портфель</h3><label class="f" for="pfn">Название<input class="i" id="pfn" placeholder="Например, Долгосрок"></label>
          <div class="err" id="pferr"></div><div class="btnrow"><button class="btn" data-act="savePf">Создать</button><button class="btn ghost" data-act="close">Отмена</button></div>`);
        break;
      case "savePf": { const r = await api("/api/portfolios", { method: "POST", body: { name: $("#pfn").value } }); S.pid = r.id; closeSheet(); location.hash = "portfolio-" + r.id; load(); break; }
      case "pfMenu": {
        const v = store.get("c:/api/portfolios/" + S.pid, null);
        openSheet(`<h3>Портфель</h3><label class="f" for="pfr">Название<input class="i" id="pfr" value="${esc(v ? v.data.portfolio.name : "")}"></label>
          <div class="btnrow"><button class="btn" data-act="renamePf">Переименовать</button><button class="btn danger" data-act="delPf">Удалить портфель</button>
          <button class="btn ghost" data-act="close">Отмена</button></div><p class="muted" id="pfmsg"></p>`); break;
      }
      case "renamePf": await api("/api/portfolios/" + S.pid, { method: "PATCH", body: { name: $("#pfr").value } }); closeSheet(); load(); break;
      case "delPf":
        if (!b.dataset.confirm) { b.dataset.confirm = 1; b.textContent = "Удалить вместе с позициями?"; break; }
        await api("/api/portfolios/" + S.pid, { method: "DELETE" }); S.pid = null; closeSheet(); location.hash = "portfolio"; load(); break;
      case "addPos": sheetPosition(null); break;
      case "savePos": {
        $("#perr").textContent = "";
        const body = { quantity: $("#pq").value, avg_price: $("#pp").value, opened_at: $("#pd").value || null };
        if (!body.quantity || !body.avg_price) { $("#perr").textContent = "Заполните количество и среднюю цену входа"; break; }
        b.disabled = true;
        try {
          if (d.id) await api("/api/positions/" + d.id, { method: "PATCH", body });
          else {
            if (!chosen) { $("#perr").textContent = "Выберите бумагу из списка"; break; }
            await api("/api/positions", { method: "POST", body: { ...body, portfolio_id: S.pid, exchange: chosen.exchange, code: chosen.code } });
          }
          closeSheet(); load();
        } catch (err) { $("#perr").textContent = err.message; } finally { b.disabled = false; }
        break;
      }
      case "delPos":
        if (!b.dataset.confirm) { b.dataset.confirm = 1; b.textContent = "Точно удалить?"; break; }
        await api("/api/positions/" + d.id, { method: "DELETE" }); closeSheet(); load(); break;
      case "saveAlert": {
        $("#aerr").textContent = "";
        let instId = +d.inst || null;
        try {
          if (!instId) {
            if (!chosen) { $("#aerr").textContent = "Выберите бумагу из списка"; break; }
            instId = (await api("/api/instruments", { method: "POST", body: { exchange: chosen.exchange, code: chosen.code } })).id;
          }
          const t = $("#aform").dataset.type;
          const num = id => $(id).value === "" ? null : Number($(id).value);
          const body = { type: t, instrument_id: instId, timeframe: $("#atf").value, indicator: $("#aind").value,
            operator: $("#aop").value, threshold: num("#ath"), category: $("#acat").value,
            target_rating: [...document.querySelectorAll(".atg:checked")].map(x => x.value).join(",") || null,
            confirm_bars: num("#ak"), cooldown_min: num("#acd"), repeat: $("#arep").value === "1", is_active: true };
          if (d.id) await api("/api/alerts/" + d.id, { method: "PUT", body }); else await api("/api/alerts", { method: "POST", body });
          closeSheet(); if (S.route === "alerts") load(); else location.hash = "alerts";
        } catch (err) { $("#aerr").textContent = err.message; }
        break;
      }
      case "cycle": b.disabled = true; b.textContent = "Обновляю…"; await api("/api/refresh", { method: "POST" }); load(); break;
      case "testTg": { const r = await api("/api/test-telegram", { method: "POST" }); b.textContent = r.status === "sent" ? "Отправлено" : "Не доставлено: см. журнал"; break; }
      case "saveSettings":
        await api("/api/settings", { method: "PATCH", body: { allocation_threshold: $("#thIn").value, expiry_notify_days: $("#expIn").value } });
        $("#setMsg").textContent = "Сохранено"; break;
    }
  } catch (err) {
    if (err instanceof AuthError) { closeSheet(); viewToken(); return; }
    const box = $("#sheet").hidden ? null : ($("#perr") || $("#aerr") || $("#pferr"));
    if (box) box.textContent = err.message; else alertBanner(err.message);
  }
});
function alertBanner(msg) { $("#banner").innerHTML = `<div class="banner">${esc(msg)}</div>`; }

/* ---------- запуск ---------- */
Object.assign(S, parseHash());
if (S.route === "portfolio" && S.pid != null) store.set("pid", S.pid);
load();
setInterval(() => { if (!document.hidden && $("#sheet").hidden) load(); }, 300000);   // D-02: раз в 300 с
document.addEventListener("visibilitychange", () => { if (!document.hidden && S.lastLoad && Date.now() - S.lastLoad > 60000 && $("#sheet").hidden) load(); });
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
