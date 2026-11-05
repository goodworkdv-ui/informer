"""SQLite: схема из раздела 11 ТЗ и небольшие помощники."""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Iterable

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, sort INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS instrument (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL,              -- secid MOEX или тикер США
  isin TEXT, name TEXT,
  asset_class TEXT NOT NULL,       -- equity_us / equity_ru / bond_ru / currency / futures
  exchange TEXT NOT NULL,          -- NASDAQ / MOEX
  currency TEXT NOT NULL,          -- USD / RUB
  engine TEXT, market TEXT, board TEXT,
  lot_size REAL DEFAULT 1, face_value REAL, maturity_date TEXT, offer_date TEXT,
  coupon_rate REAL, next_coupon TEXT, coupon_value REAL,
  min_step REAL, step_price REAL, expiry_date TEXT, initial_margin REAL,
  spec_updated TEXT,
  UNIQUE(exchange, code)
);
CREATE TABLE IF NOT EXISTS position (
  id INTEGER PRIMARY KEY,
  portfolio_id INTEGER NOT NULL REFERENCES portfolio(id) ON DELETE CASCADE,
  instrument_id INTEGER NOT NULL REFERENCES instrument(id),
  quantity INTEGER NOT NULL,
  avg_price REAL NOT NULL,
  opened_at TEXT
);
CREATE TABLE IF NOT EXISTS quote (
  instrument_id INTEGER PRIMARY KEY REFERENCES instrument(id),
  ts TEXT,               -- время данных (UTC ISO)
  fetched_at TEXT,       -- когда получено приложением (UTC ISO)
  last REAL, prev REAL, bid REAL, ask REAL, volume REAL,
  accrued_interest REAL, yield REAL, data_delay TEXT
);
CREATE TABLE IF NOT EXISTS candle (
  instrument_id INTEGER NOT NULL, timeframe TEXT NOT NULL, open_time_utc TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL,
  is_final INTEGER DEFAULT 0, source TEXT,
  PRIMARY KEY (instrument_id, timeframe, open_time_utc)
);
CREATE TABLE IF NOT EXISTS candle_sync (
  instrument_id INTEGER NOT NULL, timeframe TEXT NOT NULL, synced_at TEXT,
  PRIMARY KEY (instrument_id, timeframe)
);
CREATE TABLE IF NOT EXISTS bond_coupon (
  instrument_id INTEGER NOT NULL, date TEXT NOT NULL, amount REAL, type TEXT NOT NULL,
  PRIMARY KEY (instrument_id, date, type)
);
CREATE TABLE IF NOT EXISTS indicator_cache (
  instrument_id INTEGER NOT NULL, timeframe TEXT NOT NULL,
  candle_time TEXT, is_final INTEGER, data_as_of TEXT, computed_at TEXT,
  payload TEXT,
  PRIMARY KEY (instrument_id, timeframe)
);
CREATE TABLE IF NOT EXISTS corporate_event (
  id INTEGER PRIMARY KEY, instrument_id INTEGER, event_type TEXT, event_date TEXT,
  amount REAL, description TEXT
);
CREATE TABLE IF NOT EXISTS economic_event (
  id INTEGER PRIMARY KEY, country TEXT, name TEXT, event_date TEXT, forecast TEXT, actual TEXT
);
CREATE TABLE IF NOT EXISTS news_item (
  id INTEGER PRIMARY KEY, instrument_id INTEGER, title TEXT, summary TEXT, url TEXT UNIQUE,
  source TEXT, published_at TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS alert (
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL,                 -- PRICE / INDICATOR / SIGNAL_CHANGE
  instrument_id INTEGER NOT NULL REFERENCES instrument(id),
  timeframe TEXT, indicator TEXT, operator TEXT, threshold REAL,
  category TEXT, target_rating TEXT,
  confirm_bars INTEGER, cooldown_min INTEGER,
  repeat INTEGER DEFAULT 0, is_active INTEGER DEFAULT 1,
  last_state TEXT, last_fired_at TEXT, last_candle TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS notification_log (
  id INTEGER PRIMARY KEY, alert_id INTEGER, text TEXT, status TEXT, attempts INTEGER DEFAULT 0,
  created_at TEXT, delivered_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS app_log (id INTEGER PRIMARY KEY, ts TEXT, level TEXT, source TEXT, message TEXT);
"""

_local = threading.local()


def connect(path=None) -> sqlite3.Connection:
    p = path or config.DB_PATH
    if p != ":memory:":
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), check_same_thread=False, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


_con: sqlite3.Connection | None = None
_lock = threading.RLock()


def get() -> sqlite3.Connection:
    global _con
    if _con is None:
        _con = connect()
    return _con


def use(con: sqlite3.Connection) -> None:
    """Подменить соединение (тесты)."""
    global _con
    _con = con


def q(sql: str, args: Iterable[Any] = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in get().execute(sql, tuple(args)).fetchall()]


def q1(sql: str, args: Iterable[Any] = ()) -> dict | None:
    r = q(sql, args)
    return r[0] if r else None


def x(sql: str, args: Iterable[Any] = ()) -> int:
    with _lock:
        cur = get().execute(sql, tuple(args))
        return cur.lastrowid


def xmany(sql: str, rows: Iterable[Iterable[Any]]) -> None:
    with _lock:
        con = get()
        con.execute("BEGIN")
        try:
            con.executemany(sql, [tuple(r) for r in rows])
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise


def get_setting(key: str):
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    if r is None:
        return config.DEFAULT_SETTINGS.get(key)
    return json.loads(r["value"])


def set_setting(key: str, value) -> None:
    x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
      (key, json.dumps(value, ensure_ascii=False)))


def all_settings() -> dict:
    out = dict(config.DEFAULT_SETTINGS)
    for r in q("SELECT key,value FROM settings"):
        out[r["key"]] = json.loads(r["value"])
    return out


def log(level: str, source: str, message: str) -> None:
    from .timeutil import now_iso
    import logging
    logging.getLogger("informer").log(logging.ERROR if level == "error" else logging.INFO, "[%s] %s", source, message)
    try:
        x("INSERT INTO app_log(ts,level,source,message) VALUES(?,?,?,?)", (now_iso(), level, source, message[:1000]))
        x("DELETE FROM app_log WHERE id < (SELECT MAX(id) - 2000 FROM app_log)")
    except Exception:
        pass
