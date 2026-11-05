"""Время, торговые сессии и сетка таймфреймов (D-06, раздел 5.2)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import config

UTC = timezone.utc
MSK = ZoneInfo("Europe/Moscow")
NY = ZoneInfo("America/New_York")

# Ключ ТФ -> (подпись, интервал MOEX ISS, интервал Twelve Data или None, если строится агрегацией)
TIMEFRAMES: dict[str, dict] = {
    "1m":  {"label": "1 мин",  "moex": 1,  "td": "1min",   "intraday": True},
    "10m": {"label": "10 мин", "moex": 10, "td": None,     "intraday": True,  "agg_from": "1m",  "agg_n": 10},
    "1h":  {"label": "1 час",  "moex": 60, "td": "1h",     "intraday": True},
    "1d":  {"label": "1 день", "moex": 24, "td": "1day",   "intraday": False},
    "1w":  {"label": "1 нед",  "moex": 7,  "td": "1week",  "intraday": False},
    "1M":  {"label": "1 мес",  "moex": 31, "td": "1month", "intraday": False},
    "1Q":  {"label": "1 кв",   "moex": 4,  "td": None,     "intraday": False, "agg_from": "1M"},
}
TF_ORDER = list(TIMEFRAMES)


def now() -> datetime:
    return datetime.now(UTC)


def now_iso() -> str:
    return now().strftime("%Y-%m-%dT%H:%M:%SZ")


def to_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def exchange_tz(exchange: str) -> ZoneInfo:
    return NY if exchange == "NASDAQ" else MSK


def _add_months(d: datetime, n: int) -> datetime:
    m = d.month - 1 + n
    return d.replace(year=d.year + m // 12, month=m % 12 + 1, day=1)


def period_end(open_utc: datetime, tf: str, exchange: str) -> datetime:
    """Момент, после которого свеча с началом open_utc считается закрытой."""
    if tf == "1m":
        return open_utc + timedelta(minutes=1)
    if tf == "10m":
        return open_utc + timedelta(minutes=10)
    if tf == "1h":
        return open_utc + timedelta(hours=1)
    tz = exchange_tz(exchange)
    loc = open_utc.astimezone(tz)
    start = loc.replace(hour=0, minute=0, second=0, microsecond=0)
    if tf == "1d":
        if exchange == "NASDAQ":
            close_t = time(13, 0) if start.date().isoformat() in config.HOLIDAYS.get("nasdaq_short_days", []) else time(16, 0)
            return datetime.combine(start.date(), close_t, tz).astimezone(UTC)
        return (start + timedelta(days=1)).astimezone(UTC)
    if tf == "1w":
        monday = start - timedelta(days=start.weekday())
        return (monday + timedelta(days=7)).astimezone(UTC)
    if tf == "1M":
        return _add_months(start, 1).astimezone(UTC)
    if tf == "1Q":
        q0 = start.replace(month=(start.month - 1) // 3 * 3 + 1, day=1)
        return _add_months(q0, 3).astimezone(UTC)
    raise ValueError(tf)


def quarter_start(dt_local: datetime) -> datetime:
    return dt_local.replace(month=(dt_local.month - 1) // 3 * 3 + 1, day=1, hour=0, minute=0, second=0, microsecond=0)


def is_trading_now(exchange: str, at: datetime | None = None) -> bool:
    at = at or now()
    if exchange == "NASDAQ":
        loc = at.astimezone(NY)
        if loc.weekday() >= 5 or loc.date().isoformat() in config.HOLIDAYS.get("nasdaq", []):
            return False
        close = time(13, 0) if loc.date().isoformat() in config.HOLIDAYS.get("nasdaq_short_days", []) else time(16, 0)
        # +20 мин после закрытия, чтобы забрать финальную котировку с задержкой
        return time(9, 30) <= loc.time() <= (datetime.combine(loc.date(), close) + timedelta(minutes=20)).time()
    loc = at.astimezone(MSK)
    if loc.date().isoformat() in config.HOLIDAYS.get("moex", []):
        return False
    if loc.weekday() >= 5:  # торги выходного дня
        return time(9, 50) <= loc.time() <= time(19, 20)
    return time(6, 50) <= loc.time() <= time(23, 59)


def trading_days_between(d0: date, d1: date, exchange: str = "MOEX") -> int:
    """Число торговых дней в (d0, d1]."""
    hol = set(config.HOLIDAYS.get("nasdaq" if exchange == "NASDAQ" else "moex", []))
    n, d = 0, d0
    while d < d1:
        d += timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() not in hol:
            n += 1
    return n


def fmt_local(iso: str | None, tz: str = "Europe/Moscow", with_date: bool = True) -> str:
    if not iso:
        return "—"
    dt = parse_iso(iso).astimezone(ZoneInfo(tz))
    return dt.strftime("%d.%m %H:%M" if with_date else "%H:%M")
