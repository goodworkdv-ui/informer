"""HTTP API и раздача PWA. Запуск: python -m app.main (или start.bat)."""
from __future__ import annotations

import asyncio
import hmac
import os
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import alerts, config, db, indicators, instruments, service, telegram
from .timeutil import TIMEFRAMES, now_iso

from logging.handlers import RotatingFileHandler

LOG_FILE = config.BASE_DIR / "informer.log"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(),
                              RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")])
WEB = config.BASE_DIR / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.get()
    if not config.APP_TOKEN:
        logging.warning("APP_TOKEN не задан: веб-интерфейс будет без пароля. Задайте его в .env!")
    tasks = []
    if not getattr(app.state, "no_background", False):
        tasks = [asyncio.create_task(service.collector_loop()),
                 asyncio.create_task(telegram.poll_commands(service.bot_command))]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="Портфельный Информер", lifespan=lifespan)


def auth(request: Request):
    if not config.APP_TOKEN:
        return
    tok = request.headers.get("x-token") or request.query_params.get("token") or ""
    if not hmac.compare_digest(tok, config.APP_TOKEN):
        raise HTTPException(401, "Нужен токен доступа (Настройки → Токен)")


@app.exception_handler(ValueError)
async def value_error(_, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(KeyError)
async def key_error(_, exc: KeyError):
    return JSONResponse({"detail": str(exc).strip("'\"")}, status_code=404)


A = [Depends(auth)]


@app.get("/healthz")
def healthz():
    """Для внешнего монитора доступности (без токена, без данных)."""
    return {"ok": True, "time": now_iso(), "last_cycle": service.STATE["last_cycle"]}


# ---------- портфели и позиции ----------

class PortfolioIn(BaseModel):
    name: str


@app.get("/api/portfolios", dependencies=A)
def list_portfolios():
    out = []
    for p in service.portfolios():
        v = service.portfolio_view(p["id"])
        out.append({**p, "totals": v["totals"], "positions": len(v["positions"])})
    return out


@app.post("/api/portfolios", dependencies=A)
def create_portfolio(body: PortfolioIn):
    name = body.name.strip()
    if not name:
        raise ValueError("Введите название портфеля")
    pid = db.x("INSERT INTO portfolio(name, sort) VALUES(?, (SELECT COALESCE(MAX(sort),0)+1 FROM portfolio))", (name,))
    return {"id": pid}


@app.patch("/api/portfolios/{pid}", dependencies=A)
def rename_portfolio(pid: int, body: PortfolioIn):
    db.x("UPDATE portfolio SET name=? WHERE id=?", (body.name.strip(), pid))
    return {"ok": True}


@app.delete("/api/portfolios/{pid}", dependencies=A)
def delete_portfolio(pid: int):
    db.x("DELETE FROM position WHERE portfolio_id=?", (pid,))
    db.x("DELETE FROM portfolio WHERE id=?", (pid,))
    return {"ok": True}


@app.get("/api/portfolios/{pid}", dependencies=A)
def get_portfolio(pid: int):
    return service.portfolio_view(pid)


class PositionIn(BaseModel):
    portfolio_id: int
    exchange: str
    code: str
    quantity: float
    avg_price: float
    opened_at: str | None = None


class PositionPatch(BaseModel):
    quantity: float
    avg_price: float
    opened_at: str | None = None


@app.post("/api/positions", dependencies=A)
async def add_position(body: PositionIn):
    if not db.q1("SELECT 1 FROM portfolio WHERE id=?", (body.portfolio_id,)):
        raise KeyError("Портфель не найден")
    inst = await instruments.ensure(body.exchange.upper(), body.code)
    q, price = service.validate_position(inst, body.quantity, body.avg_price)
    pid = db.x("INSERT INTO position(portfolio_id,instrument_id,quantity,avg_price,opened_at) VALUES(?,?,?,?,?)",
               (body.portfolio_id, inst["id"], q, price, body.opened_at or None))
    return {"id": pid, "instrument_id": inst["id"]}


@app.patch("/api/positions/{pos_id}", dependencies=A)
def edit_position(pos_id: int, body: PositionPatch):
    p = db.q1("SELECT * FROM position WHERE id=?", (pos_id,))
    if not p:
        raise KeyError("Позиция не найдена")
    q, price = service.validate_position(instruments.get(p["instrument_id"]), body.quantity, body.avg_price)
    db.x("UPDATE position SET quantity=?, avg_price=?, opened_at=? WHERE id=?", (q, price, body.opened_at or None, pos_id))
    return {"ok": True}


@app.delete("/api/positions/{pos_id}", dependencies=A)
def delete_position(pos_id: int):
    db.x("DELETE FROM position WHERE id=?", (pos_id,))
    return {"ok": True}


# ---------- инструменты ----------

@app.get("/api/search", dependencies=A)
async def search(q: str):
    return await instruments.search(q)


class InstrumentIn(BaseModel):
    exchange: str
    code: str


@app.post("/api/instruments", dependencies=A)
async def add_instrument(body: InstrumentIn):
    return await instruments.ensure(body.exchange.upper(), body.code)


@app.get("/api/instruments/{inst_id}", dependencies=A)
async def card(inst_id: int, tf: str = "1d"):
    if tf not in TIMEFRAMES:
        raise ValueError("Неизвестный таймфрейм")
    return await service.instrument_card(inst_id, tf)


# ---------- алерты ----------

class AlertIn(BaseModel):
    type: str
    instrument_id: int
    timeframe: str | None = None
    indicator: str | None = None
    operator: str | None = None
    threshold: float | None = None
    category: str | None = None
    target_rating: str | None = None
    confirm_bars: int | None = None
    cooldown_min: int | None = None
    repeat: bool = False
    is_active: bool = True


def _check_alert(a: AlertIn):
    if a.type not in ("PRICE", "INDICATOR", "SIGNAL_CHANGE"):
        raise ValueError("Тип алерта: PRICE, INDICATOR или SIGNAL_CHANGE")
    if not instruments.get(a.instrument_id):
        raise KeyError("Бумага не найдена")
    if a.type in ("PRICE", "INDICATOR"):
        if a.operator not in (">", "<") or a.threshold is None:
            raise ValueError("Укажите условие (выше/ниже) и уровень")
    if a.type in ("INDICATOR", "SIGNAL_CHANGE") and a.timeframe not in TIMEFRAMES:
        raise ValueError("Укажите таймфрейм")
    if a.type == "INDICATOR" and a.indicator not in alerts.INDICATOR_CHOICES:
        raise ValueError("Выберите индикатор")
    if a.type == "SIGNAL_CHANGE" and a.category not in ("mas", "oscillators", "total"):
        raise ValueError("Выберите категорию оценки")
    if a.type == "PRICE":
        a.timeframe = None
    if instruments.get(a.instrument_id)["asset_class"] == "bond_ru" and a.type != "PRICE":
        raise ValueError("Для облигаций технический анализ не выполняется: доступен только ценовой алерт")


@app.get("/api/alerts", dependencies=A)
def list_alerts():
    rows = db.q("SELECT a.*, i.code, i.name, i.currency FROM alert a JOIN instrument i ON i.id=a.instrument_id ORDER BY a.id DESC")
    for r in rows:
        r["description"] = alerts.describe(r)
    return rows


@app.get("/api/alert-options", dependencies=A)
def alert_options():
    return {"indicators": alerts.INDICATOR_CHOICES,
            "ratings": {k: indicators.RATING_RU[k] for k in indicators.RATINGS},
            "timeframes": {k: v["label"] for k, v in TIMEFRAMES.items()}}


@app.post("/api/alerts", dependencies=A)
def create_alert(body: AlertIn):
    _check_alert(body)
    aid = db.x("INSERT INTO alert(type,instrument_id,timeframe,indicator,operator,threshold,category,target_rating,"
               "confirm_bars,cooldown_min,repeat,is_active,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
               (body.type, body.instrument_id, body.timeframe, body.indicator, body.operator, body.threshold,
                body.category, body.target_rating, body.confirm_bars, body.cooldown_min, int(body.repeat),
                int(body.is_active), now_iso()))
    return {"id": aid}


@app.put("/api/alerts/{aid}", dependencies=A)
def update_alert(aid: int, body: AlertIn):
    _check_alert(body)
    db.x("UPDATE alert SET type=?,instrument_id=?,timeframe=?,indicator=?,operator=?,threshold=?,category=?,"
         "target_rating=?,confirm_bars=?,cooldown_min=?,repeat=?,is_active=?,last_state=NULL,last_candle=NULL WHERE id=?",
         (body.type, body.instrument_id, body.timeframe, body.indicator, body.operator, body.threshold, body.category,
          body.target_rating, body.confirm_bars, body.cooldown_min, int(body.repeat), int(body.is_active), aid))
    return {"ok": True}


class Toggle(BaseModel):
    is_active: bool


@app.patch("/api/alerts/{aid}", dependencies=A)
def toggle_alert(aid: int, body: Toggle):
    db.x("UPDATE alert SET is_active=?, last_state=NULL WHERE id=?", (int(body.is_active), aid))
    return {"ok": True}


@app.delete("/api/alerts/{aid}", dependencies=A)
def delete_alert(aid: int):
    db.x("DELETE FROM alert WHERE id=?", (aid,))
    return {"ok": True}


# ---------- настройки и состояние ----------

@app.get("/api/settings", dependencies=A)
def get_settings():
    s = db.all_settings()
    return {k: s.get(k) for k in config.DEFAULT_SETTINGS}


@app.patch("/api/settings", dependencies=A)
def patch_settings(body: dict):
    for k, v in body.items():
        if k not in config.DEFAULT_SETTINGS:
            continue
        if k in ("allocation_threshold", "expiry_notify_days"):
            v = float(v)
            if v <= 0:
                raise ValueError("Значение должно быть больше нуля")
        db.set_setting(k, v)
    return get_settings()


@app.get("/api/health", dependencies=A)
def health():
    return service.health()


@app.post("/api/refresh", dependencies=A)
async def refresh_now():
    await service.cycle()
    return {"ok": True, "last_cycle": service.STATE["last_cycle"], "errors": service.STATE["last_cycle_errors"]}


@app.post("/api/test-telegram", dependencies=A)
async def test_telegram():
    return {"status": await telegram.notify("✅ Тестовое сообщение Портфельного Информера", force=True)}


# ---------- PWA ----------

@app.get("/")
def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
def sw():
    return FileResponse(WEB / "sw.js", media_type="application/javascript", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=WEB), name="static")


def run():
    import socket
    import webbrowser

    import uvicorn
    url = f"http://127.0.0.1:{config.PORT}"
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", config.PORT)) == 0:
            logging.error("Порт %s уже занят: сервер уже запущен или порт занят другой программой. "
                          "Откройте %s или смените PORT в .env", config.PORT, url)
            raise SystemExit(2)
    logging.info("База: %s", config.DB_PATH)
    logging.info("Ключи: Finnhub=%s, Twelve Data=%s, Telegram=%s, токен входа=%s",
                 *("задан" if v else "НЕТ" for v in (config.FINNHUB_KEY, config.TWELVEDATA_KEY,
                                                     config.TELEGRAM_TOKEN and config.TELEGRAM_CHAT_ID, config.APP_TOKEN)))
    logging.info("Сервер запускается: %s  (окно не закрывайте, остановка — Ctrl+C)", url)
    if not os.environ.get("INFORMER_NO_BROWSER"):
        import threading
        threading.Timer(3, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=config.PORT, log_level="info", log_config=None)


if __name__ == "__main__":
    run()
