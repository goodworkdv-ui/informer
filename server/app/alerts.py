"""Движок алертов (раздел 7): PRICE, INDICATOR, SIGNAL_CHANGE.

Логика решений вынесена в чистые функции decide_*: их проверяют тесты.
"""
from __future__ import annotations

import html
import json
from datetime import timedelta

from . import config, db, indicators
from .timeutil import TIMEFRAMES, now, now_iso, parse_iso, fmt_local

OPS = {">": lambda a, b: a > b, "<": lambda a, b: a < b}
OP_RU = {">": "выше", "<": "ниже"}


def default_cooldown(tf: str | None) -> int:
    """A-04: 60 мин для внутридневных ТФ (и цены), 24 ч для дня и старше."""
    if tf is None or TIMEFRAMES.get(tf, {}).get("intraday", True):
        return 60
    return 24 * 60


def default_confirm(tf: str | None) -> int:
    """A-03: K = 2 для внутридневных ТФ, 1 — для дня и старше."""
    return 2 if tf and TIMEFRAMES[tf]["intraday"] else 1


def decide_threshold(alert: dict, value: float | None, at) -> tuple[bool, str]:
    """A-02: срабатывание при переходе «неверно → верно»; повтор — после возврата в «неверно» и cooldown."""
    if value is None:
        return False, alert.get("last_state") or "unknown"
    state = "true" if OPS[alert["operator"]](value, alert["threshold"]) else "false"
    prev = alert.get("last_state")
    fire = state == "true" and prev == "false"
    if prev in (None, "unknown"):
        fire = False  # первое наблюдение только фиксирует состояние, иначе алерт сработает сразу при создании
    if fire and alert.get("last_fired_at"):
        cd = alert.get("cooldown_min") or default_cooldown(alert.get("timeframe"))
        if at - parse_iso(alert["last_fired_at"]) < timedelta(minutes=cd):
            fire = False
    return fire, state


def decide_signal(alert: dict, rating: str) -> tuple[bool, dict]:
    """A-03: новая рекомендация должна продержаться K закрытых свечей подряд."""
    st = json.loads(alert["last_state"]) if alert.get("last_state") and alert["last_state"].startswith("{") else {}
    k = alert.get("confirm_bars") or default_confirm(alert.get("timeframe"))
    if st.get("streak_rating") == rating:
        st["streak"] = st.get("streak", 0) + 1
    else:
        st["streak_rating"], st["streak"] = rating, 1
    fire = False
    if st["streak"] >= k and st.get("confirmed") != rating:
        old = st.get("confirmed")
        st["confirmed"], st["previous"] = rating, old
        if old is not None and rating != "na":
            targets = [t for t in (alert.get("target_rating") or "").split(",") if t]
            fire = not targets or rating in targets
    return fire, st


def _link(inst_id: int) -> str:
    return f'\n<a href="{config.PUBLIC_URL}#inst-{inst_id}">Открыть карточку</a>' if config.PUBLIC_URL else ""


def _fmt(v):
    if v is None:
        return "н/д"
    return f"{v:,.4f}".rstrip("0").rstrip(".").replace(",", " ")


def describe(a: dict) -> str:
    if a["type"] == "PRICE":
        return f"цена {OP_RU[a['operator']]} {_fmt(a['threshold'])}"
    tf = TIMEFRAMES.get(a.get("timeframe") or "", {}).get("label", a.get("timeframe"))
    if a["type"] == "INDICATOR":
        return f"{indicator_name(a['indicator'])} на {tf} {a['operator']} {_fmt(a['threshold'])}"
    cat = {"mas": "Скользящие", "oscillators": "Осцилляторы", "total": "Резюме"}.get(a.get("category"), a.get("category"))
    tg = ", ".join(indicators.RATING_RU.get(t, t) for t in (a.get("target_rating") or "").split(",") if t)
    return f"{cat} на {tf}: " + (f"переход в «{tg}»" if tg else "любая смена рекомендации")


INDICATOR_CHOICES = {
    "close": "Цена закрытия", "rsi": "RSI (14)", "stoch": "Stoch %K", "stochrsi": "Stoch RSI %K",
    "macd": "MACD", "adx": "ADX (14)", "willr": "Williams %R", "cci": "CCI (20)", "mom": "Momentum (10)",
    "uo": "Ultimate Osc.", "roc": "ROC (9)", "bbp": "Bull Bear Power", "ao": "Awesome Osc.",
    "ema20": "EMA (20)", "ema50": "EMA (50)", "ema200": "EMA (200)", "sma50": "SMA (50)", "sma200": "SMA (200)",
    "atr_pct": "ATR, % цены",
}


def indicator_name(key: str) -> str:
    return INDICATOR_CHOICES.get(key, key)


def indicator_value(ta: dict, key: str):
    if key == "close":
        return ta.get("close")
    if key == "atr_pct":
        return ta["reference"]["atr"]["pct"]
    for it in ta["oscillators"] + ta["mas"]:
        if it["key"] == key:
            return it["value"]
    return None


def _save(a: dict, state: str, fired: bool, candle: str | None = None) -> None:
    sets = {"last_state": state}
    if candle is not None:
        sets["last_candle"] = candle
    if fired:
        sets["last_fired_at"] = now_iso()
        if not a.get("repeat"):
            sets["is_active"] = 0  # A-04: однократный алерт выключается после срабатывания
    db.x(f"UPDATE alert SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), a["id"]))


async def check_price_alerts(notify) -> None:
    at = now()
    for a in db.q("SELECT a.*, i.code, i.currency, q.last, q.ts FROM alert a JOIN instrument i ON i.id=a.instrument_id "
                  "LEFT JOIN quote q ON q.instrument_id=a.instrument_id WHERE a.type='PRICE' AND a.is_active=1"):
        fire, state = decide_threshold(a, a["last"], at)
        _save(a, state, fire)
        if fire:
            await notify(
                f"🔔 <b>{html.escape(a['code'])}</b>: {describe(a)}\n"
                f"Цена: {_fmt(a['last'])} {a['currency']}\n"
                f"Время данных: {fmt_local(a['ts'])} МСК, {config.DATA_DELAY_LABEL}" + _link(a["instrument_id"]),
                a["id"])


async def check_candle_alerts(get_ta, notify) -> None:
    """INDICATOR и SIGNAL_CHANGE — по закрытию свечи выбранного ТФ."""
    for a in db.q("SELECT a.*, i.code FROM alert a JOIN instrument i ON i.id=a.instrument_id "
                  "WHERE a.type IN ('INDICATOR','SIGNAL_CHANGE') AND a.is_active=1"):
        ta = await get_ta(a["instrument_id"], a["timeframe"])
        if not ta or ta.get("status") != "ok":
            continue
        candle = ta["candle_time"]
        if a.get("last_candle") == candle:
            continue  # эта закрытая свеча уже обработана
        overdue = (now() - parse_iso(ta["candle_close_time"])) > timedelta(minutes=45)
        mark = " <i>(просрочено: свеча закрылась, пока сервер не работал)</i>" if overdue else ""
        tfl = TIMEFRAMES[a["timeframe"]]["label"]
        if a["type"] == "INDICATOR":
            val = indicator_value(ta, a["indicator"])
            fire, state = decide_threshold(a, val, now())
            _save(a, state, fire, candle)
            if fire:
                await notify(
                    f"📈 <b>{html.escape(a['code'])}</b>: {describe(a)}{mark}\n"
                    f"Значение: {_fmt(val)} (закрытая свеча {tfl}, {fmt_local(ta['candle_close_time'])} МСК)\n"
                    f"{config.DATA_DELAY_LABEL}" + _link(a["instrument_id"]), a["id"])
        else:
            summ = ta["summary"][a["category"]]
            fire, st = decide_signal(a, summ["rating"])
            _save(a, json.dumps(st), fire, candle)
            if fire:
                await notify(
                    f"🧭 <b>{html.escape(a['code'])}</b>: {describe(a)}{mark}\n"
                    f"{indicators.RATING_RU.get(st.get('previous'), '—')} → <b>{summ['label']}</b> "
                    f"(Покупать: {summ['buy']}, Нейтрально: {summ['neutral']}, Продавать: {summ['sell']})\n"
                    f"Свеча {tfl}, {fmt_local(ta['candle_close_time'])} МСК, {config.DATA_DELAY_LABEL}"
                    + _link(a["instrument_id"]), a["id"])
