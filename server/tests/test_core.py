"""Тесты формул ТЗ: стоимость и P&L (разделы 3, 4), сигналы и сводная оценка (6), алерты (7), ТФ (5.2)."""
import json
import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app import alerts, candles, indicators as ind, valuation
from app.timeutil import MSK, NY, UTC, period_end


# ---------- раздел 3–4: стоимость и P&L ----------

def test_bond_value_and_pnl():
    inst = {"asset_class": "bond_ru", "face_value": 1000.0}
    m = valuation.position_metrics(inst, 10, 95.0, {"last": 97.5, "accrued_interest": 12.34})
    assert m["clean_price_rub"] == pytest.approx(975.0)
    assert m["full_price_rub"] == pytest.approx(987.34)           # полная = чистая × номинал/100 + НКД
    assert m["value"] == pytest.approx(10 * 987.34)               # I-03
    assert m["pnl"] == pytest.approx(10 * (97.5 - 95.0) * 10)     # по чистой цене, без НКД (P-05)


def test_bond_amortized_face():
    inst = {"asset_class": "bond_ru", "face_value": 500.0}
    m = valuation.position_metrics(inst, 4, 100.0, {"last": 101.0, "accrued_interest": 3.0})
    assert m["value"] == pytest.approx(4 * (101 * 5 + 3))


def test_futures_long_short():
    inst = {"asset_class": "futures", "min_step": 1.0, "step_price": 1.0, "initial_margin": 15000}
    long_ = valuation.position_metrics(inst, 3, 90000, {"last": 90500})
    short = valuation.position_metrics(inst, -2, 90000, {"last": 90500})
    assert long_["pnl"] == pytest.approx(1500)
    assert short["pnl"] == pytest.approx(-1000)   # рост цены при шорте — убыток (сценарий 12)


def test_futures_step_price():
    # Brent: шаг 0,01, стоимость шага ~7,9 ₽
    inst = {"asset_class": "futures", "min_step": 0.01, "step_price": 7.9}
    m = valuation.position_metrics(inst, -1, 70.00, {"last": 69.50})
    assert m["pnl"] == pytest.approx(50 * 7.9)


def test_moex_shares_in_lots():
    inst = {"asset_class": "equity_ru", "lot_size": 10}
    m = valuation.position_metrics(inst, 5, 300.0, {"last": 310.0})
    assert m["value"] == pytest.approx(5 * 10 * 310)
    assert m["pnl"] == pytest.approx(5 * 10 * 10)
    assert m["pnl_pct"] == pytest.approx(100 * 500 / 15000)


def test_totals_separate_currencies_and_imbalance():
    rows = [
        {"code": "AAPL", "currency": "USD", "asset_class": "equity_us", "value": 900.0, "pnl": 100.0, "cost": 800.0},
        {"code": "MSFT", "currency": "USD", "asset_class": "equity_us", "value": 100.0, "pnl": 0.0, "cost": 100.0},
        {"code": "SBER", "currency": "RUB", "asset_class": "equity_ru", "value": 50000.0, "pnl": -500.0, "cost": 50500.0},
    ]
    t = valuation.portfolio_totals(rows, 25)
    assert t["totals"]["USD"]["value"] == 1000 and t["totals"]["RUB"]["value"] == 50000
    assert rows[0]["share_pct"] == pytest.approx(90)
    assert any("AAPL" in w for w in t["imbalance"])
    assert not any("MSFT" in w for w in t["imbalance"])


# ---------- раздел 6: индикаторы ----------

def _series(n=300, seed=1):
    rng = np.random.default_rng(seed)
    c = 100 + np.cumsum(rng.normal(0, 1, n))
    o = c + rng.normal(0, 0.3, n)
    h = np.maximum(o, c) + np.abs(rng.normal(0, 0.5, n))
    l = np.minimum(o, c) - np.abs(rng.normal(0, 0.5, n))
    v = rng.integers(1000, 5000, n).astype(float)
    return o, h, l, c, v


def test_ema_seed_and_sma():
    x = np.arange(1, 21, dtype=float)
    assert ind.sma(x, 5)[4] == pytest.approx(3.0)
    e = ind.ema(x, 5)
    assert e[4] == pytest.approx(3.0)                       # старт с SMA
    assert e[5] == pytest.approx(3.0 + (6 - 3.0) * 2 / 6)


def test_rsi_extremes():
    up = np.arange(1, 40, dtype=float)
    assert ind.rsi(up)[-1] == pytest.approx(100.0)
    assert ind.rsi(up[::-1])[-1] == pytest.approx(0.0)


def test_rsi_wilder_known_value():
    # Пример Уайлдера: 14 изменений, средний рост 3.34/14, среднее падение 1.40/14 -> RSI = 100 - 100/(1 + 3.34/1.40)
    c = np.array([44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
                  45.89, 46.03, 45.61, 46.28, 46.28])
    assert ind.rsi(c, 14)[-1] == pytest.approx(100 - 100 / (1 + 3.34 / 1.40), abs=1e-6)


def test_compute_counts_and_r():
    res = ind.compute(*_series())
    assert len(res["oscillators"]) == 12 and len(res["mas"]) == 15
    for key in ("mas", "oscillators", "total"):
        s = res["summary"][key]
        voting = s["buy"] + s["sell"] + s["neutral"]
        items = res["mas"] if key == "mas" else res["oscillators"] if key == "oscillators" else res["mas"] + res["oscillators"]
        assert voting + s["na"] == len(items)
        assert s["r"] == pytest.approx((s["buy"] - s["sell"]) / voting)
    t, m, o = res["summary"]["total"], res["summary"]["mas"], res["summary"]["oscillators"]
    assert t["buy"] == m["buy"] + o["buy"] and t["sell"] == m["sell"] + o["sell"]


def test_short_history_gives_na():
    o, h, l, c, v = _series(60)
    res = ind.compute(o, h, l, c, v)
    by = {i["key"]: i for i in res["mas"]}
    assert by["ema200"]["signal"] == "na" and by["sma100"]["signal"] == "na"
    assert by["ema20"]["signal"] != "na"
    assert res["summary"]["mas"]["na"] == 4


def test_rating_thresholds_equal_is_neutral():
    assert ind.rating(0.1) == "neutral" and ind.rating(-0.1) == "neutral"
    assert ind.rating(0.11) == "buy" and ind.rating(0.5) == "buy" and ind.rating(0.51) == "strong_buy"
    assert ind.rating(-0.5) == "sell" and ind.rating(-0.51) == "strong_sell"
    assert ind._band(30, 30, 70) == "neutral" and ind._band(29.9, 30, 70) == "buy"


def test_ma_votes_follow_close():
    c = np.linspace(100, 200, 250)
    h, l, o, v = c + 1, c - 1, c, np.full(250, 1000.0)
    res = ind.compute(o, h, l, c, v)
    # устойчивый рост: цена выше всех средних (HMA на прямой линии совпадает с ценой без запаздывания)
    assert all(i["signal"] == "buy" for i in res["mas"] if i["key"] != "hma9")


def test_hma_vwma_kijun_values():
    o, h, l, c, v = _series(100)
    res = ind.compute(o, h, l, c, v)
    by = {i["key"]: i["value"] for i in res["mas"]}
    assert by["vwma20"] == pytest.approx(np.sum(c[-20:] * v[-20:]) / np.sum(v[-20:]))
    assert by["kijun"] == pytest.approx((h[-26:].max() + l[-26:].min()) / 2)
    w = lambda x, n: np.dot(x[-n:], np.arange(1, n + 1)) / np.arange(1, n + 1).sum()
    raw = np.array([2 * w(c[:i + 1], 4) - w(c[:i + 1], 9) for i in range(len(c) - 3, len(c))])
    assert by["hma9"] == pytest.approx(w(raw, 3))


# ---------- раздел 7: алерты ----------

T0 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def test_indicator_alert_transition_and_cooldown():
    a = {"operator": "<", "threshold": 30, "last_state": None, "last_fired_at": None, "cooldown_min": 60, "timeframe": "1h"}
    fire, st = alerts.decide_threshold(a, 25, T0)
    assert not fire and st == "true"          # первое наблюдение не срабатывает
    a["last_state"] = "false"
    fire, st = alerts.decide_threshold(a, 25, T0)
    assert fire and st == "true"              # переход неверно → верно
    a["last_state"], a["last_fired_at"] = "true", T0.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert alerts.decide_threshold(a, 20, T0)[0] is False      # пока условие верно — не повторяется
    a["last_state"] = "false"
    assert alerts.decide_threshold(a, 20, T0 + timedelta(minutes=30))[0] is False   # cooldown
    assert alerts.decide_threshold(a, 20, T0 + timedelta(minutes=61))[0] is True


def test_signal_change_confirmation_k2():
    a = {"timeframe": "1h", "confirm_bars": None, "target_rating": "buy,strong_buy", "last_state": None}
    seq = ["sell", "sell", "buy", "sell", "buy", "buy", "buy"]
    fired = []
    for r in seq:
        f, st = alerts.decide_signal(a, r)
        a["last_state"] = json.dumps(st)
        fired.append(f)
    # sell подтверждается (первое — без уведомления), одиночный buy — «мигание», buy×2 — срабатывание
    assert fired == [False, False, False, False, False, True, False]


def test_signal_change_daily_k1_any_change():
    a = {"timeframe": "1d", "target_rating": None, "last_state": None}
    out = []
    for r in ["neutral", "buy", "buy", "neutral"]:
        f, st = alerts.decide_signal(a, r)
        a["last_state"] = json.dumps(st)
        out.append(f)
    assert out == [False, True, False, True]


# ---------- раздел 5.2: ТФ и агрегация ----------

def test_period_end():
    t = datetime(2026, 11, 3, 7, 0, tzinfo=MSK).astimezone(UTC)
    assert period_end(t, "1d", "MOEX") == datetime(2026, 11, 4, 0, 0, tzinfo=MSK)
    d = datetime(2026, 11, 3, 0, 0, tzinfo=NY).astimezone(UTC)
    assert period_end(d, "1d", "NASDAQ") == datetime(2026, 11, 3, 16, 0, tzinfo=NY)
    q = datetime(2026, 8, 1, tzinfo=MSK).astimezone(UTC)
    assert period_end(q, "1Q", "MOEX") == datetime(2026, 10, 1, tzinfo=MSK)


def test_aggregate_10m_and_quarter():
    base = datetime(2026, 11, 3, 14, 30, tzinfo=UTC)
    one = [{"t": base + timedelta(minutes=i), "o": i, "h": i + 1, "l": i - 1, "c": i + 0.5, "v": 10} for i in range(20)]
    ten = candles.aggregate_10m(one)
    assert len(ten) == 2 and ten[0]["o"] == 0 and ten[0]["c"] == 9.5 and ten[0]["h"] == 10 and ten[0]["v"] == 100
    months = [{"t": datetime(2026, m, 1, tzinfo=NY).astimezone(UTC), "o": m, "h": m + 5, "l": m - 5, "c": m + 1, "v": 1}
              for m in range(1, 7)]
    qs = candles.aggregate_quarter(months, NY)
    assert len(qs) == 2 and qs[0]["o"] == 1 and qs[0]["c"] == 4 and qs[0]["h"] == 8 and qs[1]["l"] == -1


def test_stoch_values_present_and_bounded():
    res = ind.compute(*_series(120))
    by = {i["key"]: i for i in res["oscillators"]}
    for k in ("stoch", "stochrsi", "willr", "uo"):
        assert by[k]["value"] is not None, k
    assert 0 <= by["stoch"]["value"] <= 100 and 0 <= by["stochrsi"]["value"] <= 100
    o, h, l, c, v = _series(120)
    raw = 100 * (c[-16:] - np.array([l[i - 13:i + 1].min() for i in range(len(c) - 16, len(c))])) / \
          np.array([h[i - 13:i + 1].max() - l[i - 13:i + 1].min() for i in range(len(c) - 16, len(c))])
    k = np.convolve(raw, np.ones(3) / 3, "valid")
    assert by["stoch"]["value"] == pytest.approx(k[-1])
