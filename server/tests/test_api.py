"""Сквозная проверка API на синтетических данных провайдеров."""
import pytest
from fastapi.testclient import TestClient

from app import config, db
from tests import fake


@pytest.fixture()
def client(monkeypatch):
    db.use(db.connect(":memory:"))
    fake.install()
    monkeypatch.setattr(config, "APP_TOKEN", "secret")
    monkeypatch.setattr(config, "TELEGRAM_TOKEN", "")
    from app.main import app
    app.state.no_background = True
    with TestClient(app) as c:
        c.headers["X-Token"] = "secret"
        yield c


def test_auth_required(client):
    r = client.get("/api/portfolios", headers={"X-Token": "wrong"})
    assert r.status_code == 401


def test_portfolio_flow(client):
    pid = client.post("/api/portfolios", json={"name": "Основной"}).json()["id"]
    assert client.post("/api/positions", json={"portfolio_id": pid, "exchange": "MOEX", "code": "SBER",
                                               "quantity": 5, "avg_price": 290}).status_code == 200
    assert client.post("/api/positions", json={"portfolio_id": pid, "exchange": "MOEX", "code": "SU26238RMFS4",
                                               "quantity": 10, "avg_price": 57.0}).status_code == 200
    assert client.post("/api/positions", json={"portfolio_id": pid, "exchange": "MOEX", "code": "SiZ6",
                                               "quantity": -2, "avg_price": 82000}).status_code == 200
    assert client.post("/api/positions", json={"portfolio_id": pid, "exchange": "NASDAQ", "code": "AAPL",
                                               "quantity": 3, "avg_price": 200}).status_code == 200
    # шорт акций запрещён, дробное количество — тоже
    r = client.post("/api/positions", json={"portfolio_id": pid, "exchange": "MOEX", "code": "SBER", "quantity": -1, "avg_price": 1})
    assert r.status_code == 400 and "фьючерс" in r.json()["detail"]
    r = client.post("/api/positions", json={"portfolio_id": pid, "exchange": "NASDAQ", "code": "AAPL", "quantity": 1.5, "avg_price": 1})
    assert r.status_code == 400

    v = client.get(f"/api/portfolios/{pid}").json()
    pos = {p["code"]: p for p in v["positions"]}
    assert pos["SBER"]["value"] == pytest.approx(5 * 10 * 305.2)
    assert pos["SU26238RMFS4"]["value"] == pytest.approx(10 * (584.0 + 20.15))
    assert pos["SiZ6"]["pnl"] == pytest.approx(-2 * 540)
    assert pos["SiZ6"]["liquidity_warning"] is True
    assert pos["AAPL"]["currency"] == "USD"
    assert v["totals"]["USD"]["value"] == pytest.approx(3 * 231.4)
    assert v["totals"]["RUB"]["count"] == 3

    # цикл коллектора считает дневной ТА по бумагам портфеля (P-11)
    assert client.post("/api/refresh").status_code == 200
    v = client.get(f"/api/portfolios/{pid}").json()
    pos = {p["code"]: p for p in v["positions"]}
    assert pos["SBER"]["rating"]["rating"] in ("strong_buy", "buy", "neutral", "sell", "strong_sell")
    assert pos["SU26238RMFS4"]["rating"] is None

    inst = pos["SBER"]["instrument_id"]
    card = client.get(f"/api/instruments/{inst}?tf=1h").json()
    assert card["ta"]["status"] == "ok"
    assert len(card["ta"]["oscillators"]) == 12 and len(card["ta"]["mas"]) == 15
    # повторный запрос того же ТФ берётся из кэша
    from app import candles
    n = candles.CALC_COUNTER["count"]
    client.get(f"/api/instruments/{inst}?tf=1h")
    assert candles.CALC_COUNTER["count"] == n

    bond = client.get(f"/api/instruments/{pos['SU26238RMFS4']['instrument_id']}").json()
    assert bond["bond"]["full_price_rub"] == pytest.approx(604.15)
    assert any(s["type"] == "redemption" for s in bond["schedule"])

    aapl = client.get(f"/api/instruments/{pos['AAPL']['instrument_id']}?tf=1m").json()
    assert aapl["ta"]["status"] == "na"   # 1 мин США без интрадей-алерта — н/д


def test_alerts(client):
    pid = client.post("/api/portfolios", json={"name": "П"}).json()["id"]
    inst = client.post("/api/positions", json={"portfolio_id": pid, "exchange": "NASDAQ", "code": "TSLA",
                                               "quantity": 1, "avg_price": 300}).json()["instrument_id"]
    assert client.post("/api/alerts", json={"type": "PRICE", "instrument_id": inst, "operator": ">", "threshold": 150}).status_code == 200
    assert client.post("/api/alerts", json={"type": "INDICATOR", "instrument_id": inst, "timeframe": "1d",
                                            "indicator": "rsi", "operator": "<", "threshold": 30}).status_code == 200
    assert client.post("/api/alerts", json={"type": "SIGNAL_CHANGE", "instrument_id": inst, "timeframe": "1d",
                                            "category": "total", "target_rating": "buy,strong_buy"}).status_code == 200
    bad = client.post("/api/alerts", json={"type": "INDICATOR", "instrument_id": inst, "timeframe": "1d", "indicator": "rsi"})
    assert bad.status_code == 400
    client.post("/api/refresh")
    rows = client.get("/api/alerts").json()
    assert len(rows) == 3 and all(r["last_state"] is not None for r in rows)
    h = client.get("/api/health").json()
    assert h["alerts_active"] == 3
