"""Справочник инструментов и котировки (P-02, I-11, D-02)."""
from __future__ import annotations

import asyncio

from . import db
from .providers import moex, us
from .timeutil import now_iso

SPEC_FIELDS = ["lot_size", "face_value", "maturity_date", "offer_date", "coupon_rate", "coupon_value",
               "next_coupon", "min_step", "step_price", "expiry_date", "initial_margin"]


async def search(q: str) -> list[dict]:
    q = q.strip()
    if len(q) < 1:
        return []
    res = us.search_local(q, 10)
    try:
        known = {r["code"] for r in res}
        res += [r for r in await us.search_online(q, 8) if r["code"] not in known]
    except Exception as e:  # noqa: BLE001
        db.log("error", "finnhub", f"Поиск «{q}»: {e}")
    try:
        res += await moex.search(q, 25)
    except Exception as e:  # noqa: BLE001
        db.log("error", "moex", f"Поиск «{q}»: {e}")
    return res


def get(instrument_id: int) -> dict | None:
    return db.q1("SELECT * FROM instrument WHERE id=?", (instrument_id,))


def by_code(exchange: str, code: str) -> dict | None:
    return db.q1("SELECT * FROM instrument WHERE exchange=? AND code=?", (exchange, code))


def find_code(code: str) -> dict | None:
    """Поиск по тикеру для бота: сначала среди известных инструментов."""
    return db.q1("SELECT * FROM instrument WHERE upper(code)=upper(?) ORDER BY exchange='NASDAQ' DESC", (code,))


async def ensure(exchange: str, code: str, name: str | None = None) -> dict:
    """Добавить инструмент в справочник (или вернуть существующий) и загрузить спецификацию."""
    inst = by_code(exchange, code)
    if inst:
        return inst
    sp = us.spec(code, name) if exchange == "NASDAQ" else await moex.spec(code)
    db.x("INSERT INTO instrument(code,isin,name,asset_class,exchange,currency,engine,market,board) "
         "VALUES(?,?,?,?,?,?,?,?,?)",
         (sp["code"], sp["isin"], sp["name"], sp["asset_class"], sp["exchange"], sp["currency"],
          sp["engine"], sp["market"], sp["board"]))
    inst = by_code(exchange, code)
    await refresh_quotes([inst])
    if inst["asset_class"] == "bond_ru":
        await refresh_bondization(inst)
    return by_code(exchange, code)


def _save_quote(inst: dict, qd: dict) -> None:
    db.x("INSERT INTO quote(instrument_id,ts,fetched_at,last,prev,bid,ask,volume,accrued_interest,yield,data_delay) "
         "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(instrument_id) DO UPDATE SET ts=excluded.ts,"
         "fetched_at=excluded.fetched_at,last=excluded.last,prev=excluded.prev,bid=excluded.bid,ask=excluded.ask,"
         "volume=excluded.volume,accrued_interest=excluded.accrued_interest,yield=excluded.yield,"
         "data_delay=excluded.data_delay",
         (inst["id"], qd.get("ts"), now_iso(), qd.get("last"), qd.get("prev"), qd.get("bid"), qd.get("ask"),
          qd.get("volume"), qd.get("accrued_interest"), qd.get("yield"), "15m"))
    sp = qd.get("spec")
    if sp:
        sets = {k: sp.get(k) for k in SPEC_FIELDS if sp.get(k) is not None}
        if sp.get("isin") and not inst.get("isin"):
            sets["isin"] = sp["isin"]
        if sets:
            db.x(f"UPDATE instrument SET {', '.join(k + '=?' for k in sets)}, spec_updated=? WHERE id=?",
                 (*sets.values(), now_iso(), inst["id"]))


async def refresh_quotes(insts: list[dict], priority: set[int] | None = None) -> dict:
    """Обновить котировки. MOEX — пачкой по режиму торгов; Finnhub — по очереди, бумаги с алертами первыми."""
    priority = priority or set()
    errors: list[str] = []
    groups: dict[tuple, list[dict]] = {}
    us_list = []
    for i in insts:
        if i["exchange"] == "MOEX":
            groups.setdefault((i["engine"], i["market"], i["board"]), []).append(i)
        else:
            us_list.append(i)

    async def moex_group(key, items):
        for k in range(0, len(items), 50):
            chunk = items[k:k + 50]
            try:
                res = await moex.quotes(*key, [i["code"] for i in chunk])
                for i in chunk:
                    if i["code"] in res:
                        _save_quote(i, res[i["code"]])
            except Exception as e:  # noqa: BLE001
                errors.append(f"MOEX {key[2]}: {e}")

    async def us_all():
        us_list.sort(key=lambda i: i["id"] not in priority)  # D-02: алерты первыми
        for i in us_list:
            try:
                _save_quote(i, await us.quote(i["code"]))
            except Exception as e:  # noqa: BLE001
                errors.append(f"Finnhub {i['code']}: {e}")

    await asyncio.gather(*(moex_group(k, v) for k, v in groups.items()), us_all())
    for e in errors:
        db.log("error", "quotes", e)
    return {"errors": errors}


async def refresh_bondization(inst: dict) -> None:
    try:
        rows = await moex.bondization(inst["code"])
    except Exception as e:  # noqa: BLE001
        db.log("error", "moex", f"График выплат {inst['code']}: {e}")
        return
    db.x("DELETE FROM bond_coupon WHERE instrument_id=?", (inst["id"],))
    db.xmany("INSERT OR REPLACE INTO bond_coupon(instrument_id,date,amount,type) VALUES(?,?,?,?)",
             [(inst["id"], r["date"], r["amount"], r["type"]) for r in rows])


def quote(instrument_id: int) -> dict | None:
    return db.q1("SELECT * FROM quote WHERE instrument_id=?", (instrument_id,))


def watched() -> tuple[list[dict], set[int]]:
    """Бумаги из портфелей и активных алертов (D-02) и множество бумаг с алертами."""
    insts = db.q("SELECT DISTINCT i.* FROM instrument i WHERE i.id IN "
                 "(SELECT instrument_id FROM position UNION SELECT instrument_id FROM alert WHERE is_active=1)")
    alert_ids = {r["instrument_id"] for r in db.q("SELECT instrument_id FROM alert WHERE is_active=1")}
    return insts, alert_ids
