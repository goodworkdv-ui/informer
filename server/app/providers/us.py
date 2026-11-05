"""США: Finnhub (котировки, поиск) и Twelve Data (свечи) — разделы 5.1 и 10.3."""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx

from .. import config
from ..timeutil import NY, UTC
from .ratelimit import RateLimiter

finnhub_limiter = RateLimiter("Finnhub", per_min=config.FINNHUB_PER_MIN)
td_limiter = RateLimiter("Twelve Data", per_min=config.TWELVEDATA_PER_MIN, per_day=config.TWELVEDATA_PER_DAY)

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=20)
    return _client


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class ProviderError(Exception):
    pass


async def finnhub(path: str, params: dict) -> dict | list:
    if not config.FINNHUB_KEY:
        raise ProviderError("Не задан FINNHUB_KEY")
    await finnhub_limiter.acquire()
    try:
        r = await client().get("https://finnhub.io/api/v1" + path, params={**params, "token": config.FINNHUB_KEY})
        if r.status_code == 429:
            raise ProviderError("Finnhub: превышен лимит запросов")
        r.raise_for_status()
        finnhub_limiter.last_ok = _stamp()
        return r.json()
    except Exception as e:  # noqa: BLE001
        finnhub_limiter.last_error = f"{path}: {e}"
        raise


async def quote(ticker: str) -> dict:
    j = await finnhub("/quote", {"symbol": ticker})
    if not j or not j.get("c"):
        raise ProviderError(f"Finnhub: нет котировки {ticker}")
    ts = datetime.fromtimestamp(j["t"], UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if j.get("t") else _stamp()
    return {"last": float(j["c"]), "prev": float(j.get("pc") or 0) or None, "bid": None, "ask": None,
            "volume": None, "accrued_interest": None, "yield": None, "ts": ts, "stale": False}


def search_local(q: str, limit: int = 20) -> list[dict]:
    """Поиск по справочнику Nasdaq-100 (P-02, I-22)."""
    ql = q.strip().lower()
    out = []
    for it in config.NASDAQ100["items"]:
        if ql in it["ticker"].lower() or ql in it["name"].lower():
            out.append({"exchange": "NASDAQ", "code": it["ticker"], "name": it["name"], "isin": None,
                        "asset_class": "equity_us"})
    out.sort(key=lambda r: (r["code"].lower() != ql, not r["code"].lower().startswith(ql), r["code"]))
    return out[:limit]


def spec(ticker: str) -> dict:
    it = next((i for i in config.NASDAQ100["items"] if i["ticker"] == ticker.upper()), None)
    if not it:
        raise ProviderError(f"{ticker} нет в справочнике Nasdaq-100")
    return {"exchange": "NASDAQ", "code": it["ticker"], "name": it["name"], "isin": None,
            "asset_class": "equity_us", "currency": "USD", "engine": None, "market": None, "board": None}


async def candles(ticker: str, interval: str, since: datetime | None) -> list[dict]:
    """Свечи Twelve Data /time_series. Время intraday — в часовом поясе биржи (America/New_York)."""
    if not config.TWELVEDATA_KEY:
        raise ProviderError("Не задан TWELVEDATA_KEY")
    if not await td_limiter.acquire():
        raise ProviderError("Twelve Data: исчерпан суточный лимит")
    params = {"symbol": ticker, "interval": interval, "apikey": config.TWELVEDATA_KEY,
              "timezone": "America/New_York", "order": "ASC"}
    if since is not None:
        params["start_date"] = since.astimezone(NY).strftime("%Y-%m-%d %H:%M:%S")
        params["outputsize"] = 5000
    else:
        params["outputsize"] = 300 if interval not in ("1min",) else 1500
    try:
        r = await client().get("https://api.twelvedata.com/time_series", params=params)
        r.raise_for_status()
        j = r.json()
        if j.get("status") == "error":
            raise ProviderError(f"Twelve Data: {j.get('message')}")
        td_limiter.last_ok = _stamp()
    except Exception as e:  # noqa: BLE001
        td_limiter.last_error = f"{ticker} {interval}: {e}"
        raise
    out = []
    for v in j.get("values", []):
        s = v["datetime"]
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S") if len(s) > 10 else datetime.strptime(s, "%Y-%m-%d")
        out.append({"t": dt.replace(tzinfo=NY).astimezone(UTC), "o": float(v["open"]), "h": float(v["high"]),
                    "l": float(v["low"]), "c": float(v["close"]), "v": float(v.get("volume") or 0)})
    out.sort(key=lambda c: c["t"])
    return out


def main_session_only(cs: list[dict]) -> list[dict]:
    """D-08: внутридневные свечи США — только основная сессия 09:30–16:00 ET."""
    res = []
    for c in cs:
        t = c["t"].astimezone(NY).time()
        if (t.hour, t.minute) >= (9, 30) and t.hour < 16:
            res.append(c)
    return res


__all__ = ["quote", "candles", "search_local", "spec", "finnhub_limiter", "td_limiter", "ProviderError",
           "main_session_only", "timedelta"]
