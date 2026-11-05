"""Технический анализ: 27 голосующих индикаторов + 2 справочных (раздел 6 ТЗ).

Формулы повторяют встроенные функции TradingView (Pine): EMA и RMA стартуют со значения SMA
за первые n свечей, RMA — сглаживание Уайлдера (alpha = 1/n). Всё считается только по закрытым
свечам; индикатор без достаточной истории получает статус «н/д» и не голосует (D-05).
"""
from __future__ import annotations

import math

import numpy as np

from .config import TA_THRESHOLDS as TH

BUY, SELL, NEUTRAL, NA = "buy", "sell", "neutral", "na"
SIGNAL_RU = {BUY: "Покупать", SELL: "Продавать", NEUTRAL: "Нейтрально", NA: "н/д"}

RATINGS = ["strong_sell", "sell", "neutral", "buy", "strong_buy"]
RATING_RU = {"strong_buy": "Активно покупать", "buy": "Покупать", "neutral": "Нейтрально",
             "sell": "Продавать", "strong_sell": "Активно продавать", "na": "н/д"}


# ---------- базовые ряды ----------

def sma(x: np.ndarray, n: int) -> np.ndarray:
    """Простая средняя; окно с пустым значением даёт пустое (как ta.sma)."""
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    bad = np.isnan(x)
    cs = np.cumsum(np.insert(np.where(bad, 0.0, x), 0, 0.0))
    cb = np.cumsum(np.insert(bad.astype(int), 0, 0))
    win = (cs[n:] - cs[:-n]) / n
    win[(cb[n:] - cb[:-n]) > 0] = np.nan
    out[n - 1:] = win
    return out


def _seeded(x: np.ndarray, n: int, alpha: float) -> np.ndarray:
    """Рекурсивное сглаживание, старт — SMA первых n непустых значений (как ta.ema / ta.rma)."""
    out = np.full(len(x), np.nan)
    idx = np.where(~np.isnan(x))[0]
    if len(idx) < n:
        return out
    s = idx[0]
    if np.isnan(x[s:s + n]).any():
        return out
    prev = float(np.mean(x[s:s + n]))
    out[s + n - 1] = prev
    for i in range(s + n, len(x)):
        v = x[i]
        if np.isnan(v):
            out[i] = prev
            continue
        prev = alpha * v + (1 - alpha) * prev
        out[i] = prev
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    return _seeded(x, n, 2.0 / (n + 1))


def rma(x: np.ndarray, n: int) -> np.ndarray:
    return _seeded(x, n, 1.0 / n)


def wma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    w = np.arange(1, n + 1, dtype=float)
    for i in range(n - 1, len(x)):
        seg = x[i - n + 1:i + 1]
        if not np.isnan(seg).any():
            out[i] = float(np.dot(seg, w) / w.sum())
    return out


def rolling_max(x, n):
    out = np.full(len(x), np.nan)
    for i in range(n - 1, len(x)):
        out[i] = np.max(x[i - n + 1:i + 1])
    return out


def rolling_min(x, n):
    out = np.full(len(x), np.nan)
    for i in range(n - 1, len(x)):
        out[i] = np.min(x[i - n + 1:i + 1])
    return out


def shift(x, k):
    out = np.full(len(x), np.nan)
    if k < len(x):
        out[k:] = x[:-k]
    return out


def rsi(c, n=14):
    d = np.diff(c, prepend=np.nan)
    up = rma(np.where(np.isnan(d), np.nan, np.maximum(d, 0)), n)
    dn = rma(np.where(np.isnan(d), np.nan, np.maximum(-d, 0)), n)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(dn == 0, 100.0, np.where(up == 0, 0.0, 100 - 100 / (1 + up / dn)))
    r[np.isnan(up) | np.isnan(dn)] = np.nan
    return r


def stoch_raw(src, h, l, n):
    hh, ll = rolling_max(h, n), rolling_min(l, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(hh - ll == 0, 0.0, 100 * (src - ll) / (hh - ll))


def true_range(h, l, c):
    pc = shift(c, 1)
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    tr[0] = h[0] - l[0]
    return tr


def atr(h, l, c, n=14):
    return rma(true_range(h, l, c), n)


def dmi(h, l, c, n=14):
    up = h - shift(h, 1)
    dn = shift(l, 1) - l
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    plus_dm[0] = minus_dm[0] = np.nan
    tr = true_range(h, l, c)
    tr[0] = np.nan
    trur = rma(tr, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus = 100 * rma(plus_dm, n) / trur
        minus = 100 * rma(minus_dm, n) / trur
        s = plus + minus
        dx = 100 * np.abs(plus - minus) / np.where(s == 0, 1, s)
    adx = rma(dx, n)
    return adx, plus, minus


def hma(c, n=9):
    half, sq = int(n / 2), int(math.floor(math.sqrt(n)))
    return wma(2 * wma(c, half) - wma(c, n), sq)


# ---------- сигналы ----------

def _last(a):
    v = a[-1] if len(a) else np.nan
    return None if v is None or (isinstance(v, float) and math.isnan(v)) or np.isnan(v) else float(v)


def _prev(a):
    v = a[-2] if len(a) > 1 else np.nan
    return None if np.isnan(v) else float(v)


def _band(v, lo, hi, inverse=False):
    """Покупать ниже lo, продавать выше hi. Равенство порогу — «Нейтрально»."""
    if v is None:
        return NA
    if v < lo:
        return BUY
    if v > hi:
        return SELL
    return NEUTRAL


def _sign(v):
    if v is None:
        return NA
    return BUY if v > 0 else SELL if v < 0 else NEUTRAL


def _ma_vote(close, v):
    if v is None:
        return NA
    return BUY if close > v else SELL if close < v else NEUTRAL


def compute(o, h, l, c, v) -> dict:
    """Возвращает {'oscillators': [...], 'mas': [...], 'reference': {...}, 'summary': {...}}."""
    o, h, l, c, v = (np.asarray(a, dtype=float) for a in (o, h, l, c, v))
    n = len(c)
    close = float(c[-1]) if n else None
    osc, mas = [], []

    def add(lst, key, name, value, signal, extra=None):
        item = {"key": key, "name": name, "value": value, "signal": signal if value is not None else NA}
        if extra:
            item.update(extra)
        lst.append(item)

    # --- 6.1 осцилляторы (12) ---
    r = rsi(c, 14)
    add(osc, "rsi", "RSI (14)", _last(r), _band(_last(r), *TH["rsi"]))

    k = sma(stoch_raw(c, h, l, 14), 3)
    d = sma(k, 3)
    add(osc, "stoch", "Stoch %K (14, 3, 3)", _last(k), _band(_last(k), *TH["stoch"]), {"d": _last(d)})

    srsi_k = sma(stoch_raw(r, r, r, 14), 3)
    add(osc, "stochrsi", "Stoch RSI Fast (3, 3, 14, 14)", _last(srsi_k), _band(_last(srsi_k), *TH["stochrsi"]))

    macd_line = ema(c, 12) - ema(c, 26)
    signal_line = ema(macd_line, 9)
    m, s = _last(macd_line), _last(signal_line)
    add(osc, "macd", "MACD Level (12, 26)", m if s is not None else None,
        NA if m is None or s is None else BUY if m > s else SELL if m < s else NEUTRAL, {"signal_line": s})

    adx, plus, minus = dmi(h, l, c, 14)
    a, pdi, mdi = _last(adx), _last(plus), _last(minus)
    if a is None or pdi is None or mdi is None:
        sig = NA
    elif a > TH["adx"] and pdi > mdi:
        sig = BUY
    elif a > TH["adx"] and pdi < mdi:
        sig = SELL
    else:
        sig = NEUTRAL
    add(osc, "adx", "ADX (14)", a, sig, {"plus_di": pdi, "minus_di": mdi})

    hh, ll = rolling_max(h, 14), rolling_min(l, 14)
    with np.errstate(divide="ignore", invalid="ignore"):
        wr = np.where(hh - ll == 0, -50.0, -100 * (hh - c) / (hh - ll))
    wr[np.isnan(hh)] = np.nan
    add(osc, "willr", "Williams %R (14)", _last(wr), _band(_last(wr), *TH["willr"]))

    tp = (h + l + c) / 3
    tp_sma = sma(tp, 20)
    md = np.full(n, np.nan)
    for i in range(19, n):
        md[i] = np.mean(np.abs(tp[i - 19:i + 1] - tp_sma[i]))
    with np.errstate(divide="ignore", invalid="ignore"):
        cci = np.where(md == 0, 0.0, (tp - tp_sma) / (0.015 * md))
    cci[np.isnan(md)] = np.nan
    add(osc, "cci", "CCI (20)", _last(cci), _band(_last(cci), *TH["cci"]))

    mom = c - shift(c, 10)
    add(osc, "mom", "Momentum (10)", _last(mom), _sign(_last(mom)))

    pc = shift(c, 1)
    bp = c - np.fmin(l, pc)
    tr = np.fmax(h, pc) - np.fmin(l, pc)
    bp[0] = tr[0] = np.nan

    def _avg(p):
        sb, st = np.full(n, np.nan), np.full(n, np.nan)
        for i in range(p, n):
            sb[i] = np.sum(bp[i - p + 1:i + 1])
            st[i] = np.sum(tr[i - p + 1:i + 1])
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(st == 0, 0.0, sb / st)
    uo = 100 * (4 * _avg(7) + 2 * _avg(14) + _avg(28)) / 7
    add(osc, "uo", "Ultimate Oscillator (7, 14, 28)", _last(uo), _band(_last(uo), *TH["uo"]))

    with np.errstate(divide="ignore", invalid="ignore"):
        roc = 100 * (c - shift(c, 9)) / shift(c, 9)
    add(osc, "roc", "ROC (9)", _last(roc), _sign(_last(roc)))

    e13 = ema(c, 13)
    bull, bear = h - e13, l - e13
    e_now, e_prev = _last(e13), _prev(e13)
    bu, bu_p, be, be_p = _last(bull), _prev(bull), _last(bear), _prev(bear)
    if None in (e_now, e_prev, bu, bu_p, be, be_p):
        sig, val = NA, None
    else:
        val = bu + be
        if e_now > e_prev and be < 0 and be > be_p:
            sig = BUY
        elif e_now < e_prev and bu > 0 and bu < bu_p:
            sig = SELL
        else:
            sig = NEUTRAL
    add(osc, "bbp", "Bull Bear Power (13)", val, sig, {"bull": bu, "bear": be})

    hl2 = (h + l) / 2
    ao = sma(hl2, 5) - sma(hl2, 34)
    add(osc, "ao", "Awesome Oscillator", _last(ao), _sign(_last(ao)))

    # --- 6.2 скользящие (15) ---
    for p in (10, 20, 30, 50, 100, 200):
        val = _last(ema(c, p))
        add(mas, f"ema{p}", f"EMA ({p})", val, _ma_vote(close, val))
    for p in (10, 20, 30, 50, 100, 200):
        val = _last(sma(c, p))
        add(mas, f"sma{p}", f"SMA ({p})", val, _ma_vote(close, val))
    vs = sma(v, 20)
    vw = sma(c * v, 20) / np.where(vs == 0, np.nan, vs)
    add(mas, "vwma20", "VWMA (20)", _last(vw), _ma_vote(close, _last(vw)))
    hm = hma(c, 9)
    add(mas, "hma9", "Hull MA (9)", _last(hm), _ma_vote(close, _last(hm)))
    kijun = (rolling_max(h, 26) + rolling_min(l, 26)) / 2
    add(mas, "kijun", "Ichimoku Base Line (9, 26, 52, 26)", _last(kijun), _ma_vote(close, _last(kijun)))

    # --- 6.3 справочные ---
    at = atr(h, l, c, 14)
    with np.errstate(divide="ignore", invalid="ignore"):
        atr_pct = 100 * at / c
    ap = _last(atr_pct)
    hist = atr_pct[~np.isnan(atr_pct)][-100:]
    med = float(np.median(hist)) if len(hist) else None
    ref = {"atr": {"value": _last(at), "pct": ap, "median_pct": med,
                   "label": None if ap is None or med is None else
                   ("Высокая волатильность" if ap > 1.5 * med else "Норма")}}
    hh14, ll14 = _last(rolling_max(h, 14)), _last(rolling_min(l, 14))
    if hh14 is None or close is None:
        ref["highs_lows"] = {"position_pct": None, "to_high_pct": None, "to_low_pct": None, "high": None, "low": None}
    else:
        rng = hh14 - ll14
        ref["highs_lows"] = {
            "high": hh14, "low": ll14,
            "position_pct": 50.0 if rng == 0 else 100 * (close - ll14) / rng,
            "to_high_pct": 100 * (hh14 - close) / close,
            "to_low_pct": 100 * (close - ll14) / close,
        }

    return {"close": close, "candles": n, "oscillators": osc, "mas": mas, "reference": ref,
            "summary": summarize(osc, mas)}


def rating(r: float | None) -> str:
    if r is None:
        return "na"
    if r > TH["strong"]:
        return "strong_buy"
    if r > TH["weak"]:
        return "buy"
    if r >= -TH["weak"]:
        return "neutral"
    if r >= -TH["strong"]:
        return "sell"
    return "strong_sell"


def _score(items) -> dict:
    b = sum(1 for i in items if i["signal"] == BUY)
    s = sum(1 for i in items if i["signal"] == SELL)
    nn = sum(1 for i in items if i["signal"] == NEUTRAL)
    total = b + s + nn
    r = (b - s) / total if total else None
    return {"buy": b, "sell": s, "neutral": nn, "na": len(items) - total, "r": r,
            "rating": rating(r), "label": RATING_RU[rating(r)]}


def summarize(osc, mas) -> dict:
    """6.4: R = (покупать − продавать) / N голосующих; н/д в N не входит."""
    return {"mas": _score(mas), "oscillators": _score(osc), "total": _score(osc + mas)}
