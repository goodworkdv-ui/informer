"""Свечи: хранение, инкрементальная докачка (D-04), агрегация ТФ (5.2), расчёт ТА с кэшем (D-03, T-08)."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

from . import db, indicators
from .providers import moex, us
from .timeutil import (MSK, NY, TIMEFRAMES, UTC, now, now_iso, parse_iso, period_end, quarter_start, to_iso)

# Как часто разрешено докачивать свечи для ТФ, если они уже есть (сек).
SYNC_TTL = {"1m": 60, "10m": 300, "1h": 600, "1d": 1800, "1w": 3600 * 6, "1M": 3600 * 12, "1Q": 3600 * 24}
CALC_COUNTER = {"count": 0}          # счётчик расчётов для проверки «один раз на пару»
_locks: dict[tuple, asyncio.Lock] = {}


class NotAvailable(Exception):
    """ТФ недоступен для бумаги (например, 1 мин США без интрадей-алерта)."""


def load(instrument_id: int, tf: str, limit: int = 600) -> list[dict]:
    rows = db.q("SELECT * FROM candle WHERE instrument_id=? AND timeframe=? ORDER BY open_time_utc DESC LIMIT ?",
                (instrument_id, tf, limit))
    rows.reverse()
    return rows


def _store(instrument_id: int, tf: str, cs: list[dict], exchange: str, source: str) -> None:
    t_now = now()
    db.xmany(
        "INSERT INTO candle(instrument_id,timeframe,open_time_utc,open,high,low,close,volume,is_final,source) "
        "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(instrument_id,timeframe,open_time_utc) DO UPDATE SET "
        "open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume,"
        "is_final=excluded.is_final,source=excluded.source",
        [(instrument_id, tf, to_iso(c["t"]), c["o"], c["h"], c["l"], c["c"], c["v"],
          1 if period_end(c["t"], tf, exchange) <= t_now else 0, source) for c in cs])


def aggregate_10m(one_min: list[dict]) -> list[dict]:
    """10-минутные свечи из минутных (США)."""
    buckets: dict[datetime, dict] = {}
    for c in one_min:
        t = c["t"]
        b = t.replace(minute=t.minute - t.minute % 10, second=0, microsecond=0)
        if b not in buckets:
            buckets[b] = {"t": b, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"], "v": c["v"]}
        else:
            x = buckets[b]
            x["h"], x["l"], x["c"], x["v"] = max(x["h"], c["h"]), min(x["l"], c["l"]), c["c"], x["v"] + c["v"]
    return [buckets[k] for k in sorted(buckets)]


def aggregate_quarter(monthly: list[dict], tz) -> list[dict]:
    """Квартальные свечи из месячных по календарным кварталам (США)."""
    buckets: dict[datetime, dict] = {}
    for c in monthly:
        loc = c["t"].astimezone(tz)
        q = quarter_start(loc).astimezone(UTC)
        if q not in buckets:
            buckets[q] = {"t": q, "o": c["o"], "h": c["h"], "l": c["l"], "c": c["c"], "v": c["v"]}
        else:
            x = buckets[q]
            x["h"], x["l"], x["c"], x["v"] = max(x["h"], c["h"]), min(x["l"], c["l"]), c["c"], x["v"] + c["v"]
    return [buckets[k] for k in sorted(buckets)]


def _rows_to_c(rows):
    return [{"t": parse_iso(r["open_time_utc"]), "o": r["open"], "h": r["high"], "l": r["low"],
             "c": r["close"], "v": r["volume"]} for r in rows]


def last_us_close() -> datetime:
    """Последнее закрытие основной сессии Nasdaq (+20 мин на задержку данных)."""
    loc = now().astimezone(NY)
    d = loc.date()
    for _ in range(10):
        close = datetime(d.year, d.month, d.day, 16, 20, tzinfo=NY)
        if d.weekday() < 5 and close <= loc:
            return close.astimezone(UTC)
        d -= timedelta(days=1)
    return (loc - timedelta(days=1)).astimezone(UTC)


def us_intraday_allowed(instrument_id: int) -> bool:
    """5.1: 1 и 10 мин по США — только для бумаг с активными внутридневными алертами."""
    return db.q1("SELECT 1 FROM alert WHERE instrument_id=? AND is_active=1 AND timeframe IN ('1m','10m')",
                 (instrument_id,)) is not None


async def sync(inst: dict, tf: str, force: bool = False) -> None:
    """Докачивает свечи ТФ с последней сохранённой (или историю, если её нет)."""
    key = (inst["id"], tf)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        st = db.q1("SELECT synced_at FROM candle_sync WHERE instrument_id=? AND timeframe=?", key)
        if st and not force:
            synced = parse_iso(st["synced_at"])
            if (now() - synced).total_seconds() < SYNC_TTL[tf]:
                return
            # D-03: дневные и старше по США — один раз после закрытия сессии (экономим лимит Twelve Data)
            if inst["exchange"] == "NASDAQ" and not TIMEFRAMES[tf]["intraday"] and synced >= last_us_close():
                return
        if inst["exchange"] == "NASDAQ" and us.td_limiter.day_exhausted():
            raise RuntimeError("Twelve Data: исчерпан суточный лимит, используем кэш")
        last = db.q1("SELECT open_time_utc FROM candle WHERE instrument_id=? AND timeframe=? "
                     "ORDER BY open_time_utc DESC LIMIT 1", key)
        since = parse_iso(last["open_time_utc"]) if last else None
        spec = TIMEFRAMES[tf]

        if inst["exchange"] == "MOEX":
            cs = await moex.candles(inst["engine"], inst["market"], inst["board"], inst["code"], spec["moex"], since)
            _store(inst["id"], tf, cs, "MOEX", "moex_iss")
        else:
            if spec["intraday"] and tf in ("1m", "10m") and not us_intraday_allowed(inst["id"]):
                raise NotAvailable("Внутридневные 1 и 10 мин по США доступны только для бумаг с интрадей-алертами")
            if tf == "10m":
                await sync(inst, "1m", force)
                src = _rows_to_c(load(inst["id"], "1m", 3000))
                _store(inst["id"], "10m", aggregate_10m(src), "NASDAQ", "agg_1m")
            elif tf == "1Q":
                await sync(inst, "1M", force)
                src = _rows_to_c(load(inst["id"], "1M", 1000))
                _store(inst["id"], "1Q", aggregate_quarter(src, NY), "NASDAQ", "agg_1M")
            else:
                cs = await us.candles(inst["code"], spec["td"], since)
                if spec["intraday"]:
                    cs = us.main_session_only(cs)
                _store(inst["id"], tf, cs, "NASDAQ", "twelvedata")
        db.x("INSERT INTO candle_sync(instrument_id,timeframe,synced_at) VALUES(?,?,?) "
             "ON CONFLICT(instrument_id,timeframe) DO UPDATE SET synced_at=excluded.synced_at", (*key, now_iso()))


def refresh_final_flags(instrument_id: int, tf: str, exchange: str) -> None:
    t_now = now()
    for r in db.q("SELECT open_time_utc FROM candle WHERE instrument_id=? AND timeframe=? AND is_final=0",
                  (instrument_id, tf)):
        if period_end(parse_iso(r["open_time_utc"]), tf, exchange) <= t_now:
            db.x("UPDATE candle SET is_final=1 WHERE instrument_id=? AND timeframe=? AND open_time_utc=?",
                 (instrument_id, tf, r["open_time_utc"]))


def technicals(inst: dict, tf: str) -> dict:
    """ТА по закрытым свечам из кэша; пересчёт только при новой закрытой свече (D-03, T-01, T-08)."""
    refresh_final_flags(inst["id"], tf, inst["exchange"])
    rows = load(inst["id"], tf, 600)
    closed = [r for r in rows if r["is_final"]]
    live = next((r for r in reversed(rows) if not r["is_final"]), None)
    if not closed:
        return {"timeframe": tf, "status": "no_data", "live": live}
    last_t = closed[-1]["open_time_utc"]
    cached = db.q1("SELECT * FROM indicator_cache WHERE instrument_id=? AND timeframe=?", (inst["id"], tf))
    if cached and cached["candle_time"] == last_t:
        payload = json.loads(cached["payload"])
    else:
        payload = indicators.compute([r["open"] for r in closed], [r["high"] for r in closed],
                                     [r["low"] for r in closed], [r["close"] for r in closed],
                                     [r["volume"] or 0 for r in closed])
        CALC_COUNTER["count"] += 1
        db.log("info", "ta", f"Расчёт индикаторов {inst['code']} {tf} по свече {last_t} (всего расчётов: {CALC_COUNTER['count']})")
        db.x("INSERT INTO indicator_cache(instrument_id,timeframe,candle_time,is_final,data_as_of,computed_at,payload) "
             "VALUES(?,?,?,?,?,?,?) ON CONFLICT(instrument_id,timeframe) DO UPDATE SET candle_time=excluded.candle_time,"
             "is_final=1,data_as_of=excluded.data_as_of,computed_at=excluded.computed_at,payload=excluded.payload",
             (inst["id"], tf, last_t, 1, last_t, now_iso(), json.dumps(payload)))
    payload.update({"timeframe": tf, "status": "ok", "candle_time": last_t,
                    "candle_close_time": to_iso(period_end(parse_iso(last_t), tf, inst["exchange"])),
                    "live": ({"open_time_utc": live["open_time_utc"], "close": live["close"]} if live else None)})
    return payload


async def ensure_and_compute(inst: dict, tf: str) -> dict:
    """Докачать (если пора) и посчитать. При ошибке провайдера — данные из кэша с пометкой (D-07)."""
    err = None
    try:
        await sync(inst, tf)
    except NotAvailable as e:
        return {"timeframe": tf, "status": "na", "message": str(e)}
    except Exception as e:  # noqa: BLE001
        err = str(e)
        db.log("error", "candles", f"{inst['code']} {tf}: {e}")
    res = technicals(inst, tf)
    if err:
        res["warning"] = "Провайдер недоступен, показаны данные из кэша: " + err
    return res


def price_history(inst: dict, tf: str = "1d", limit: int = 260) -> list[dict]:
    return [{"t": r["open_time_utc"], "c": r["close"]} for r in load(inst["id"], tf, limit)]


__all__ = ["sync", "technicals", "ensure_and_compute", "aggregate_10m", "aggregate_quarter", "NotAvailable",
           "CALC_COUNTER", "MSK", "timedelta"]
