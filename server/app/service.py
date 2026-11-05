"""Бизнес-логика: портфели, карточка бумаги, коллектор (планировщик), команды бота."""
from __future__ import annotations

import asyncio
import html
from datetime import date, datetime, timedelta

from . import alerts, candles, config, db, indicators, instruments, telegram, valuation
from .providers import moex, us
from .timeutil import (MSK, TIMEFRAMES, fmt_local, is_trading_now, now, now_iso, parse_iso,
                       trading_days_between)

STATE = {"started_at": None, "last_cycle": None, "last_cycle_errors": [], "provider_alarm_at": None}


# ---------------- Портфели ----------------

def _age_min(ts: str | None) -> float | None:
    return None if not ts else round((now() - parse_iso(ts)).total_seconds() / 60, 1)


def daily_rating(inst: dict) -> dict | None:
    """P-11: сводная рекомендация дневного ТФ из кэша (без обращения к провайдеру)."""
    if inst["asset_class"] == "bond_ru":
        return None
    c = db.q1("SELECT payload FROM indicator_cache WHERE instrument_id=? AND timeframe='1d'", (inst["id"],))
    if not c:
        return None
    import json
    s = json.loads(c["payload"])["summary"]["total"]
    return {"rating": s["rating"], "label": s["label"]}


def futures_status(inst: dict) -> dict:
    if inst["asset_class"] != "futures" or not inst.get("expiry_date"):
        return {}
    exp = date.fromisoformat(inst["expiry_date"][:10])
    today = now().astimezone(MSK).date()
    left = trading_days_between(today, exp)
    return {"expiry_date": inst["expiry_date"][:10], "expired": exp < today, "trading_days_left": left,
            "liquidity_warning": (not exp < today) and left <= 5}


def position_row(p: dict) -> dict:
    inst = instruments.get(p["instrument_id"])
    qt = instruments.quote(inst["id"])
    m = valuation.position_metrics(inst, p["quantity"], p["avg_price"], qt)
    row = {
        "id": p["id"], "portfolio_id": p["portfolio_id"], "instrument_id": inst["id"],
        "code": inst["code"], "name": inst["name"], "exchange": inst["exchange"], "currency": inst["currency"],
        "asset_class": inst["asset_class"], "quantity": p["quantity"], "avg_price": p["avg_price"],
        "opened_at": p["opened_at"], "lot_size": inst.get("lot_size"),
        "change_pct": (100 * (qt["last"] / qt["prev"] - 1)) if qt and qt.get("last") and qt.get("prev") else None,
        "data_time": qt["ts"] if qt else None, "fetched_at": qt["fetched_at"] if qt else None,
        "age_min": _age_min(qt["fetched_at"]) if qt else None,
        "yield": qt["yield"] if qt else None,
        "rating": daily_rating(inst),
        **m, **futures_status(inst),
    }
    return row


def portfolios() -> list[dict]:
    return db.q("SELECT * FROM portfolio ORDER BY sort, id")


def portfolio_view(pid: int) -> dict:
    pf = db.q1("SELECT * FROM portfolio WHERE id=?", (pid,))
    if not pf:
        raise KeyError("Портфель не найден")
    rows = [position_row(p) for p in db.q("SELECT * FROM position WHERE portfolio_id=? ORDER BY id", (pid,))]
    th = float(db.get_setting("allocation_threshold"))
    tot = valuation.portfolio_totals(rows, th)
    return {"portfolio": pf, "positions": rows, **tot, "threshold": th, "delay_label": config.DATA_DELAY_LABEL}


def validate_position(inst: dict, quantity, avg_price) -> tuple[int, float]:
    """P-03, P-04, P-07: целое количество, шорт только для фьючерсов, цена > 0."""
    try:
        qf = float(quantity)
    except (TypeError, ValueError):
        raise ValueError("Количество должно быть целым числом")
    if qf != int(qf):
        raise ValueError("Количество должно быть целым: дробные бумаги и лоты не поддерживаются")
    q = int(qf)
    if q == 0:
        raise ValueError("Количество не может быть нулём: закрытую позицию удалите")
    if q < 0 and inst["asset_class"] != "futures":
        raise ValueError("Отрицательное количество (шорт) допустимо только для фьючерсов")
    try:
        price = float(avg_price)
    except (TypeError, ValueError):
        raise ValueError("Укажите среднюю цену входа числом")
    if price <= 0:
        raise ValueError("Средняя цена входа должна быть больше нуля")
    return q, price


# ---------------- Карточка бумаги ----------------

async def instrument_card(inst_id: int, tf: str = "1d") -> dict:
    inst = instruments.get(inst_id)
    if not inst:
        raise KeyError("Бумага не найдена")
    qt = instruments.quote(inst_id)
    if not qt:
        await instruments.refresh_quotes([inst])
        qt = instruments.quote(inst_id)
        inst = instruments.get(inst_id)
    card = {"instrument": inst, "quote": qt, "age_min": _age_min(qt["fetched_at"]) if qt else None,
            "delay_label": config.DATA_DELAY_LABEL, **futures_status(inst)}
    if inst["asset_class"] == "bond_ru":
        m = valuation.position_metrics(inst, 1, qt["last"] if qt and qt.get("last") else 100, qt)
        card["bond"] = {"clean_pct": qt["last"] if qt else None, "clean_rub": m.get("clean_price_rub"),
                        "accrued_interest": m.get("accrued_interest"), "full_price_rub": m.get("full_price_rub"),
                        "yield": qt["yield"] if qt else None}
        card["schedule"] = db.q("SELECT date,amount,type FROM bond_coupon WHERE instrument_id=? ORDER BY date",
                                (inst_id,))
        if not card["schedule"]:
            await instruments.refresh_bondization(inst)
            card["schedule"] = db.q("SELECT date,amount,type FROM bond_coupon WHERE instrument_id=? ORDER BY date",
                                    (inst_id,))
        try:
            await candles.sync(inst, "1d")
        except Exception as e:  # noqa: BLE001
            card["warning"] = f"История цены недоступна: {e}"
        card["history"] = candles.price_history(inst, "1d", 260)
        return card
    card["ta"] = await candles.ensure_and_compute(inst, tf)
    card["timeframes"] = [{"key": k, "label": v["label"]} for k, v in TIMEFRAMES.items()]
    card["overview"] = overview(inst)
    return card


def overview(inst: dict) -> list[dict]:
    """Сводные рекомендации по всем 7 ТФ — только из кэша (верхняя панель карточки)."""
    import json
    out = []
    for k, v in TIMEFRAMES.items():
        c = db.q1("SELECT payload, candle_time FROM indicator_cache WHERE instrument_id=? AND timeframe=?",
                  (inst["id"], k))
        if c:
            s = json.loads(c["payload"])["summary"]["total"]
            out.append({"tf": k, "label": v["label"], "rating": s["rating"], "text": s["label"]})
        else:
            out.append({"tf": k, "label": v["label"], "rating": None, "text": "—"})
    return out


async def ta_for(inst_id: int, tf: str) -> dict | None:
    inst = instruments.get(inst_id)
    if not inst or inst["asset_class"] == "bond_ru":
        return None
    return await candles.ensure_and_compute(inst, tf)


# ---------------- Коллектор ----------------

async def cycle() -> None:
    errors: list[str] = []
    insts, alert_ids = instruments.watched()

    def due(i):
        qt = instruments.quote(i["id"])
        if not qt:
            return True
        if is_trading_now(i["exchange"]):
            return True
        # вне сессии — раз в 3 часа, чтобы забрать итоговую цену и НКД
        return (now() - parse_iso(qt["fetched_at"])).total_seconds() > 3 * 3600

    res = await instruments.refresh_quotes([i for i in insts if due(i)], alert_ids)
    errors += res["errors"]
    await alerts.check_price_alerts(telegram.notify)

    # P-11: дневная рекомендация по бумагам портфелей
    for i in db.q("SELECT DISTINCT i.* FROM instrument i JOIN position p ON p.instrument_id=i.id "
                  "WHERE i.asset_class!='bond_ru'"):
        r = await candles.ensure_and_compute(i, "1d")
        if r.get("warning"):
            errors.append(r["warning"])
    await alerts.check_candle_alerts(ta_for, telegram.notify)
    await expiry_reminders()
    backup_if_due()
    STATE["last_cycle"], STATE["last_cycle_errors"] = now_iso(), errors[:50]
    if errors:
        last = STATE["provider_alarm_at"]
        if not last or (now() - parse_iso(last)).total_seconds() > 3 * 3600:
            STATE["provider_alarm_at"] = now_iso()
            await telegram.notify("⚠️ Ошибки провайдеров данных:\n" + html.escape("\n".join(errors[:5])) +
                                  "\nПоказываются последние данные из кэша.")


async def expiry_reminders() -> None:
    """I-13: за N дней до экспирации — уведомление (раз в день по каждому контракту)."""
    n = int(db.get_setting("expiry_notify_days"))
    today = now().astimezone(MSK).date()
    for i in db.q("SELECT DISTINCT i.* FROM instrument i JOIN position p ON p.instrument_id=i.id "
                  "WHERE i.asset_class='futures' AND i.expiry_date IS NOT NULL"):
        exp = date.fromisoformat(i["expiry_date"][:10])
        days = (exp - today).days
        key = f"expiry_notified:{i['id']}:{today.isoformat()}"
        if 0 <= days <= n and not db.get_setting(key):
            db.set_setting(key, True)
            await telegram.notify(f"⏳ Экспирация <b>{html.escape(i['code'])}</b> {exp:%d.%m.%Y}: осталось {days} дн. "
                                  f"Позиция в портфеле, проверьте перенос.")


def backup_if_due() -> None:
    """Ежедневная копия БД после 03:00 МСК; хранится 30 последних (раздел 9, «Хранение»)."""
    import os
    import sqlite3
    from pathlib import Path
    loc = now().astimezone(MSK)
    key = "backup_done:" + loc.date().isoformat()
    if loc.hour < 3 or db.get_setting(key):
        return
    folder = Path(os.environ.get("BACKUP_DIR") or (config.BASE_DIR / "backups"))
    try:
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"informer-{loc:%Y-%m-%d}.db"
        dst = sqlite3.connect(str(target))
        with db._lock:
            db.get().backup(dst)
        dst.close()
        for old in sorted(folder.glob("informer-*.db"))[:-30]:
            old.unlink()
        db.set_setting(key, True)
        db.log("info", "backup", f"Резервная копия: {target}")
    except Exception as e:  # noqa: BLE001
        db.log("error", "backup", f"Не удалось сделать копию: {e}")


async def collector_loop() -> None:
    STATE["started_at"] = now_iso()
    await asyncio.sleep(3)
    while True:
        t0 = now()
        try:
            await cycle()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            db.log("error", "collector", f"Цикл опроса: {e}")
        spent = (now() - t0).total_seconds()
        await asyncio.sleep(max(10, config.QUOTE_INTERVAL_S - spent))


def health() -> dict:
    """Страница «Состояние»: возраст данных, лимиты, очередь алертов, журнал."""
    prov = []
    for lim, ex in ((moex.limiter, "MOEX"), (us.finnhub_limiter, "NASDAQ")):
        last_q = db.q1("SELECT MAX(q.fetched_at) AS t FROM quote q JOIN instrument i ON i.id=q.instrument_id "
                       "WHERE i.exchange=?", (ex,))
        prov.append({**lim.status(), "last_quote": last_q["t"] if last_q else None,
                     "age_min": _age_min(last_q["t"]) if last_q and last_q["t"] else None})
    prov.append(us.td_limiter.status())
    return {
        "started_at": STATE["started_at"], "last_cycle": STATE["last_cycle"], "cycle_errors": STATE["last_cycle_errors"],
        "providers": prov,
        "keys": {"finnhub": bool(config.FINNHUB_KEY), "twelvedata": bool(config.TWELVEDATA_KEY),
                 "telegram": telegram.enabled()},
        "alerts_active": db.q1("SELECT COUNT(*) n FROM alert WHERE is_active=1")["n"],
        "ta_calculations": candles.CALC_COUNTER["count"],
        "notifications": db.q("SELECT * FROM notification_log ORDER BY id DESC LIMIT 30"),
        "log": db.q("SELECT * FROM app_log ORDER BY id DESC LIMIT 50"),
        "trading": {"MOEX": is_trading_now("MOEX"), "NASDAQ": is_trading_now("NASDAQ")},
    }


# ---------------- Команды бота (A-10) ----------------

def _money(v, cur):
    if v is None:
        return "—"
    s = f"{v:,.2f}".replace(",", " ")
    return f"{s} {'$' if cur == 'USD' else '₽'}"


async def bot_command(text: str) -> str:
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]
    if cmd in ("/start", "/help"):
        return ("Команды:\n/portfolio — сводка USD/RUB\n/pos — позиции\n/ta TICKER [ТФ] — оценки ТА "
                "(ТФ: 1m 10m 1h 1d 1w 1M 1Q)\n/alerts — алерты\n/alerts on|off ID — включить/выключить\n"
                "/mute [часы] — пауза уведомлений (без аргумента — снять паузу)")
    if cmd == "/portfolio":
        lines = []
        for pf in portfolios():
            v = portfolio_view(pf["id"])
            lines.append(f"<b>{html.escape(pf['name'])}</b>")
            for cur in ("USD", "RUB"):
                t = v["totals"][cur]
                if t["count"]:
                    pct = f" ({t['pnl_pct']:+.2f}%)" if t["pnl_pct"] is not None else ""
                    lines.append(f"  Итого {cur}: {_money(t['value'], cur)}, P&L {_money(t['pnl'], cur)}{pct}")
            for w in v["imbalance"]:
                lines.append("  ⚠️ " + html.escape(w))
        return "\n".join(lines) or "Портфелей пока нет."
    if cmd == "/pos":
        lines = []
        for pf in portfolios():
            v = portfolio_view(pf["id"])
            lines.append(f"<b>{html.escape(pf['name'])}</b>")
            for r in v["positions"]:
                rec = r["rating"]["label"] if r.get("rating") else (f"YTM {r['yield']:.2f}%" if r.get("yield") else "")
                pnl = _money(r["pnl"], r["currency"])
                lines.append(f"  {html.escape(r['code'])} ×{r['quantity']}: {_money(r['value'], r['currency'])}, "
                             f"P&L {pnl} {rec}")
        return "\n".join(lines) or "Позиций нет."
    if cmd == "/ta":
        if not args:
            return "Укажите тикер: /ta SBER 1d"
        inst = instruments.find_code(args[0])
        if not inst:
            return "Бумага не найдена среди добавленных. Добавьте её в портфель или алерт в веб-интерфейсе."
        tf = args[1] if len(args) > 1 and args[1] in TIMEFRAMES else "1d"
        ta = await ta_for(inst["id"], tf)
        if not ta or ta.get("status") != "ok":
            return (ta or {}).get("message") or "Нет данных для расчёта."
        s = ta["summary"]
        out = [f"<b>{html.escape(inst['code'])}</b>, {TIMEFRAMES[tf]['label']}, свеча до {fmt_local(ta['candle_close_time'])} МСК"]
        for key, title in (("mas", "Скользящие"), ("oscillators", "Осцилляторы"), ("total", "Резюме")):
            x = s[key]
            out.append(f"{title}: <b>{x['label']}</b> — Покупать: ({x['buy']}) Нейтрально: ({x['neutral']}) "
                       f"Продавать: ({x['sell']})")
        out.append(config.DATA_DELAY_LABEL)
        return "\n".join(out)
    if cmd == "/alerts":
        if len(args) == 2 and args[0] in ("on", "off") and args[1].isdigit():
            db.x("UPDATE alert SET is_active=? WHERE id=?", (1 if args[0] == "on" else 0, int(args[1])))
            return f"Алерт {args[1]} {'включён' if args[0] == 'on' else 'выключен'}."
        rows = db.q("SELECT a.*, i.code FROM alert a JOIN instrument i ON i.id=a.instrument_id ORDER BY a.id")
        if not rows:
            return "Алертов нет."
        return "\n".join(f"{'🟢' if a['is_active'] else '⚪️'} {a['id']}. {html.escape(a['code'])}: {alerts.describe(a)}"
                         for a in rows) + "\n\n/alerts on|off ID"
    if cmd == "/mute":
        if not args:
            db.set_setting("muted_until", None)
            return "Пауза снята, уведомления включены."
        try:
            hours = float(args[0])
        except ValueError:
            return "Пример: /mute 3 — пауза на 3 часа"
        until = now() + timedelta(hours=hours)
        db.set_setting("muted_until", until.strftime("%Y-%m-%dT%H:%M:%SZ"))
        return f"Уведомления на паузе до {fmt_local(db.get_setting('muted_until'))} МСК."
    if cmd in ("/calendar", "/news"):
        return "Календарь и новости появятся в релизе R2."
    return "Неизвестная команда. /help — список команд."


__all__ = ["cycle", "collector_loop", "health", "bot_command", "portfolio_view", "instrument_card",
           "validate_position", "indicators", "datetime"]
