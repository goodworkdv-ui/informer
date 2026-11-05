"""MOEX ISS (раздел 10.3). Бесплатный доступ, задержка до 15 минут.

Пути и поля сверены с документацией ISS (iss.moex.com/iss/reference). На этапе 1 их нужно
проверить на живых ответах: при ошибке разбора пишется запись в журнал и владелец видит её
на странице «Состояние».
"""
from __future__ import annotations

from datetime import datetime, timedelta

import httpx

from ..timeutil import MSK, UTC
from .ratelimit import RateLimiter

ISS = "https://iss.moex.com/iss"
limiter = RateLimiter("MOEX ISS", per_min=120)

GROUP_TO_CLASS = {
    "stock_shares": "equity_ru", "stock_dr": "equity_ru", "stock_ppif": "equity_ru", "stock_etf": "equity_ru",
    "stock_bonds": "bond_ru", "futures_forts": "futures", "currency_selt": "currency",
}

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=20, headers={"User-Agent": "portfolio-informer/4.0"})
    return _client


def rows(block: dict | None) -> list[dict]:
    if not block:
        return []
    cols = block["columns"]
    return [dict(zip(cols, r)) for r in block["data"]]


async def get(path: str, params: dict | None = None) -> dict:
    await limiter.acquire()
    p = {"iss.meta": "off", **(params or {})}
    try:
        r = await client().get(ISS + path, params=p)
        r.raise_for_status()
        limiter.last_ok = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return r.json()
    except Exception as e:  # noqa: BLE001
        limiter.last_error = f"{path}: {e}"
        raise


async def search(q: str, limit: int = 20) -> list[dict]:
    j = await get("/securities.json", {"q": q, "limit": limit, "is_trading": 1})
    out = []
    for r in rows(j.get("securities")):
        cls = GROUP_TO_CLASS.get(r.get("group"))
        if not cls:
            continue
        out.append({
            "exchange": "MOEX", "code": r["secid"], "name": r.get("shortname") or r.get("name"),
            "fullname": r.get("name"), "isin": r.get("isin"), "asset_class": cls,
            "board": r.get("primary_boardid"),
        })
    return out


async def spec(secid: str) -> dict:
    """Спецификация бумаги: рынок, режим торгов и справочные поля."""
    j = await get(f"/securities/{secid}.json", {"iss.only": "description,boards"})
    desc = {r["name"]: r["value"] for r in rows(j.get("description"))}
    boards = rows(j.get("boards"))
    primary = next((b for b in boards if b.get("is_primary") == 1), boards[0] if boards else None)
    if not primary:
        raise ValueError(f"MOEX: нет режима торгов для {secid}")
    group = desc.get("GROUP") or primary.get("market")
    cls = GROUP_TO_CLASS.get(group) or {
        "shares": "equity_ru", "bonds": "bond_ru", "forts": "futures", "selt": "currency"}.get(primary["market"])
    if not cls:
        raise ValueError(f"MOEX: тип бумаги {secid} ({group}) не поддерживается")
    return {
        "exchange": "MOEX", "code": secid, "isin": desc.get("ISIN"),
        "name": desc.get("SHORTNAME") or desc.get("NAME") or secid,
        "asset_class": cls, "currency": "RUB",
        "engine": primary["engine"], "market": primary["market"], "board": primary["boardid"],
    }


def _num(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _data_time(md: dict) -> str | None:
    sys = md.get("SYSTIME")
    upd = md.get("UPDATETIME") or md.get("TIME")
    try:
        if sys and upd:
            dt = datetime.strptime(sys[:10] + " " + upd, "%Y-%m-%d %H:%M:%S")
        elif sys:
            dt = datetime.strptime(sys, "%Y-%m-%d %H:%M:%S")
        else:
            return None
        return dt.replace(tzinfo=MSK).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


async def quotes(engine: str, market: str, board: str, secids: list[str]) -> dict[str, dict]:
    """Котировки и справочные поля по нескольким бумагам одного режима торгов — один запрос."""
    j = await get(f"/engines/{engine}/markets/{market}/boards/{board}/securities.json",
                  {"securities": ",".join(secids), "iss.only": "securities,marketdata"})
    sec = {r["SECID"]: r for r in rows(j.get("securities"))}
    out = {}
    for md in rows(j.get("marketdata")):
        s = sec.get(md["SECID"], {})
        prev = _num(s.get("PREVSETTLEPRICE")) if market == "forts" else None
        prev = prev if prev is not None else _num(s.get("PREVPRICE"))
        last = _num(md.get("LAST")) or _num(md.get("LCURRENTPRICE")) or _num(md.get("MARKETPRICE"))
        stale = last is None
        if last is None:
            last = _num(md.get("SETTLEPRICE")) or prev
        out[md["SECID"]] = {
            "last": last, "prev": prev, "stale": stale,
            "bid": _num(md.get("BID")), "ask": _num(md.get("OFFER")),
            "volume": _num(md.get("VOLTODAY")),
            "accrued_interest": _num(s.get("ACCRUEDINT")),
            "yield": _num(md.get("YIELD")),
            "ts": _data_time(md),
            "spec": {
                "lot_size": _num(s.get("LOTSIZE")) or _num(s.get("LOTVOLUME")) or 1,
                "face_value": _num(s.get("FACEVALUE")),
                "maturity_date": s.get("MATDATE"),
                "offer_date": s.get("OFFERDATE"),
                "coupon_rate": _num(s.get("COUPONPERCENT")),
                "coupon_value": _num(s.get("COUPONVALUE")),
                "next_coupon": s.get("NEXTCOUPON"),
                "min_step": _num(s.get("MINSTEP")),
                "step_price": _num(s.get("STEPPRICE")),
                "expiry_date": s.get("LASTTRADEDATE") or s.get("LASTDELDATE"),
                "initial_margin": _num(s.get("INITIALMARGIN")),
                "name": s.get("SHORTNAME") or s.get("SECNAME"),
                "isin": s.get("ISIN"),
            },
        }
    return out


async def candles(engine: str, market: str, board: str, secid: str, interval: int,
                  since: datetime | None) -> list[dict]:
    """Свечи ISS (до 500 за страницу, листаем через start). Время ISS — московское."""
    params = {"interval": interval}
    if since is not None:
        params["from"] = since.astimezone(MSK).strftime("%Y-%m-%d %H:%M:%S")
    else:
        back = {1: 2, 10: 10, 60: 45, 24: 400, 7: 2000, 31: 8000, 4: 20000}[interval]
        params["from"] = (datetime.now(MSK) - timedelta(days=back)).strftime("%Y-%m-%d")
    out, start = [], 0
    path = f"/engines/{engine}/markets/{market}/boards/{board}/securities/{secid}/candles.json"
    while True:
        j = await get(path, {**params, "start": start})
        page = rows(j.get("candles"))
        for r in page:
            dt = datetime.strptime(r["begin"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK)
            out.append({"t": dt.astimezone(UTC), "o": r["open"], "h": r["high"], "l": r["low"],
                        "c": r["close"], "v": r.get("volume") or 0})
        if len(page) < 500 or start > 20000:
            break
        start += len(page)
    return out


async def bondization(secid: str) -> list[dict]:
    """График выплат: купоны, амортизация, погашение, оферты (I-05)."""
    j = await get(f"/securities/{secid}/bondization.json",
                  {"iss.only": "coupons,amortizations,offers", "limit": "unlimited"})
    out = []
    for r in rows(j.get("coupons")):
        out.append({"date": r.get("coupondate"), "amount": _num(r.get("value_rub")) or _num(r.get("value")), "type": "coupon"})
    for r in rows(j.get("amortizations")):
        t = "redemption" if r.get("data_source") == "maturity" else "amortization"
        out.append({"date": r.get("amortdate"), "amount": _num(r.get("value_rub")) or _num(r.get("value")), "type": t})
    for r in rows(j.get("offers")):
        out.append({"date": r.get("offerdate"), "amount": _num(r.get("value")), "type": "offer"})
    return [o for o in out if o["date"]]
