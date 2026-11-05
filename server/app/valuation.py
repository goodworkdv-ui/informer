"""Стоимость позиций и нереализованный P&L (разделы 3 и 4 ТЗ). Чистые функции — их проверяют тесты."""
from __future__ import annotations


def units(inst: dict, quantity: int) -> float:
    """Количество бумаг: акции MOEX и валюта — в лотах, остальное — штуки/контракты (P-03)."""
    if inst["asset_class"] in ("equity_ru", "currency"):
        return quantity * (inst.get("lot_size") or 1)
    return float(quantity)


def position_metrics(inst: dict, quantity: int, avg_price: float, quote: dict | None) -> dict:
    """Возвращает стоимость, P&L (в валюте и %) и вспомогательные поля. Без котировки — None."""
    cls = inst["asset_class"]
    price = quote.get("last") if quote else None
    out = {"price": price, "value": None, "pnl": None, "pnl_pct": None, "cost": None}
    if cls == "bond_ru":
        face = inst.get("face_value") or 1000.0
        aci = (quote or {}).get("accrued_interest") or 0.0
        out["face_value"] = face
        out["accrued_interest"] = aci
        out["cost"] = quantity * avg_price * face / 100
        if price is not None:
            clean_rub = price * face / 100
            out["clean_price_rub"] = clean_rub
            out["full_price_rub"] = clean_rub + aci                  # I-01
            out["value"] = quantity * (clean_rub + aci)              # I-03
            out["pnl"] = quantity * (price - avg_price) * face / 100  # P-05/P-06: по чистой цене, без НКД
    elif cls == "futures":
        step, step_price = inst.get("min_step"), inst.get("step_price")
        out["cost"] = None  # для фьючерса «вложено» не определено: показываем ГО справочно
        if price is not None and step and step_price:
            # 4.2: P&L = N × (P − Pср) / шаг цены × стоимость шага
            out["pnl"] = quantity * (price - avg_price) / step * step_price
            out["notional"] = abs(quantity) * price / step * step_price
            out["value"] = out["pnl"]  # в итог портфеля фьючерс входит результатом, а не номиналом
            out["margin"] = abs(quantity) * (inst.get("initial_margin") or 0) or None
    else:
        u = units(inst, quantity)
        out["units"] = u
        out["cost"] = u * avg_price
        if price is not None:
            out["value"] = u * price                                  # I-20, I-21
            out["pnl"] = u * (price - avg_price)
    if out["pnl"] is not None:
        base = out["cost"] if out["cost"] else (abs(quantity) * avg_price / inst["min_step"] * inst["step_price"]
                                                if cls == "futures" and inst.get("min_step") and inst.get("step_price") else None)
        out["pnl_pct"] = (100 * out["pnl"] / abs(base)) if base else None
    return out


def portfolio_totals(rows: list[dict], threshold: float) -> dict:
    """Итоги отдельно по USD и RUB без кросс-конвертации (P-08) и доли позиций (P-09)."""
    totals = {}
    for cur in ("USD", "RUB"):
        part = [r for r in rows if r["currency"] == cur]
        value = sum(r["value"] or 0 for r in part)
        pnl = sum(r["pnl"] or 0 for r in part)
        cost = sum(r["cost"] or 0 for r in part if r["cost"])
        totals[cur] = {"value": value, "pnl": pnl, "pnl_pct": (100 * pnl / cost) if cost else None,
                       "count": len(part), "missing": sum(1 for r in part if r["value"] is None)}
    warnings = []
    for r in rows:
        tot = totals[r["currency"]]["value"]
        # доля считается по стоимости; фьючерсы входят результатом, поэтому для них доля не считается
        if r["asset_class"] != "futures" and tot and r["value"] is not None:
            r["share_pct"] = 100 * r["value"] / tot
            if r["share_pct"] > threshold:
                warnings.append(f"{r['code']}: доля {r['share_pct']:.1f}% выше порога {threshold:g}%")
        else:
            r["share_pct"] = None
    return {"totals": totals, "imbalance": warnings}
