"""Подмена провайдеров синтетическими данными: для тестов API и демонстрации без сети (python -m tests.demo)."""
from datetime import datetime, timedelta

import numpy as np

from app.providers import moex, us
from app.timeutil import MSK, NY, UTC

SPECS = {
    "SBER": {"asset_class": "equity_ru", "engine": "stock", "market": "shares", "board": "TQBR", "name": "Сбербанк",
             "isin": "RU0009029540", "last": 305.2, "prev": 301.0, "lot_size": 10},
    "SU26238RMFS4": {"asset_class": "bond_ru", "engine": "stock", "market": "bonds", "board": "TQOB", "name": "ОФЗ 26238",
                     "isin": "RU000A1038V6", "last": 58.4, "prev": 58.1, "face_value": 1000.0, "aci": 20.15,
                     "yield": 15.2, "coupon_rate": 7.1, "coupon_value": 35.4, "next_coupon": "2026-12-03",
                     "maturity": "2041-05-15"},
    "SiZ6": {"asset_class": "futures", "engine": "futures", "market": "forts", "board": "RFUD", "name": "Si-12.26",
             "last": 82540, "prev": 82110, "min_step": 1.0, "step_price": 1.0, "margin": 11500.0,
             "expiry": (datetime.now(MSK).date() + timedelta(days=4)).isoformat()},
}
PRICES_US = {"AAPL": (231.4, 228.9), "NVDA": (181.2, 184.0), "TSLA": (402.0, 395.0)}


def candles_for(seed: str, n: int, start_price: float, step: timedelta, end: datetime) -> list[dict]:
    rng = np.random.default_rng(abs(hash(seed)) % 2**32)
    c = start_price * np.exp(np.cumsum(rng.normal(0.0004, 0.012, n)))
    out = []
    for i in range(n):
        t = end - step * (n - 1 - i)
        o = c[i - 1] if i else c[0]
        out.append({"t": t, "o": float(o), "h": float(max(o, c[i]) * 1.004), "l": float(min(o, c[i]) * 0.996),
                    "c": float(c[i]), "v": float(rng.integers(1000, 9000))})
    return out


def install():
    async def m_search(q, limit=20):
        return [{"exchange": "MOEX", "code": k, "name": v["name"], "fullname": v["name"], "isin": v.get("isin"),
                 "asset_class": v["asset_class"], "board": v["board"]}
                for k, v in SPECS.items() if q.lower() in (k + v["name"]).lower()]

    async def m_spec(secid):
        v = SPECS[secid]
        return {"exchange": "MOEX", "code": secid, "isin": v.get("isin"), "name": v["name"], "asset_class": v["asset_class"],
                "currency": "RUB", "engine": v["engine"], "market": v["market"], "board": v["board"]}

    async def m_quotes(engine, market, board, secids):
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        out = {}
        for s in secids:
            v = SPECS[s]
            out[s] = {"last": v["last"], "prev": v["prev"], "bid": None, "ask": None, "volume": 1000, "stale": False,
                      "accrued_interest": v.get("aci"), "yield": v.get("yield"), "ts": now,
                      "spec": {"lot_size": v.get("lot_size", 1), "face_value": v.get("face_value"),
                               "maturity_date": v.get("maturity"), "coupon_rate": v.get("coupon_rate"),
                               "coupon_value": v.get("coupon_value"), "next_coupon": v.get("next_coupon"),
                               "min_step": v.get("min_step"), "step_price": v.get("step_price"),
                               "expiry_date": v.get("expiry"), "initial_margin": v.get("margin"),
                               "name": v["name"], "isin": v.get("isin"), "offer_date": None}}
        return out

    async def m_candles(engine, market, board, secid, interval, since):
        step = {1: timedelta(minutes=1), 10: timedelta(minutes=10), 60: timedelta(hours=1), 24: timedelta(days=1),
                7: timedelta(weeks=1), 31: timedelta(days=30), 4: timedelta(days=91)}[interval]
        n = {31: 120, 4: 40}.get(interval, 320)
        end = datetime.now(UTC).replace(second=0, microsecond=0)
        if interval == 24:
            end = datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        return candles_for(secid + str(interval), n, SPECS[secid]["last"] * 0.8, step, end)

    async def m_bondization(secid):
        return [{"date": f"{y}-{m:02d}-15", "amount": 35.4, "type": "coupon"} for y in range(2026, 2030) for m in (5, 11)] + \
               [{"date": "2041-05-15", "amount": 1000.0, "type": "redemption"}]

    async def u_quote(t):
        last, prev = PRICES_US.get(t, (100.0, 99.0))
        return {"last": last, "prev": prev, "bid": None, "ask": None, "volume": None, "accrued_interest": None,
                "yield": None, "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "stale": False}

    async def u_candles(ticker, interval, since):
        step = {"1min": timedelta(minutes=1), "1h": timedelta(hours=1), "1day": timedelta(days=1),
                "1week": timedelta(weeks=1), "1month": timedelta(days=30)}[interval]
        end = datetime.now(NY).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC) - timedelta(days=1)
        return candles_for(ticker + interval, 300 if interval != "1month" else 100,
                           PRICES_US.get(ticker, (100, 0))[0] * 0.7, step, end)

    moex.search, moex.spec, moex.quotes, moex.candles, moex.bondization = m_search, m_spec, m_quotes, m_candles, m_bondization
    us.quote, us.candles = u_quote, u_candles
