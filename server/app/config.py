"""Конфигурация: секреты из .env / переменных окружения, остальное — значения по умолчанию.

Секреты (токены, ключи) читаются только из окружения и никогда не пишутся в БД или в git.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "app" / "data"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")

APP_TOKEN = os.environ.get("APP_TOKEN", "")
FINNHUB_KEY = os.environ.get("FINNHUB_KEY", "")
TWELVEDATA_KEY = os.environ.get("TWELVEDATA_KEY", "")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PORT = int(os.environ.get("PORT", "8800"))
DB_PATH = Path(os.environ.get("DB_PATH", "data/informer.db"))
if not DB_PATH.is_absolute():
    DB_PATH = BASE_DIR / DB_PATH
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")  # адрес туннеля, для ссылок в Telegram

# --- Опрос и лимиты (раздел 5, D-02) ---
QUOTE_INTERVAL_S = 300
FINNHUB_PER_MIN = 60
TWELVEDATA_PER_MIN = 8
TWELVEDATA_PER_DAY = 800
MIN_CANDLES = 200          # D-04
DATA_DELAY_LABEL = "задержка до 15 мин"

# --- Технический анализ (раздел 6) ---
TA_THRESHOLDS = {
    "strong": 0.5,   # |R| > 0.5 — «Активно …»
    "weak": 0.1,     # |R| > 0.1 — «Покупать/Продавать»
    "rsi": (30, 70),
    "stoch": (20, 80),
    "stochrsi": (20, 80),
    "adx": 25,
    "willr": (-80, -20),
    "cci": (-100, 100),
    "uo": (30, 70),
}

# --- Значения по умолчанию для настроек владельца (таблица Settings) ---
DEFAULT_SETTINGS = {
    "timezone": "Europe/Moscow",
    "allocation_threshold": 25.0,     # P-09, %
    "expiry_notify_days": 5,          # I-13
    "quiet_hours": None,              # A-07 (R2)
    "muted_until": None,              # /mute
}

# Сессии (D-06). Праздники ведутся вручную в app/data/holidays.json
HOLIDAYS = json.loads((DATA_DIR / "holidays.json").read_text(encoding="utf-8"))
NASDAQ100 = json.loads((DATA_DIR / "nasdaq100.json").read_text(encoding="utf-8"))
