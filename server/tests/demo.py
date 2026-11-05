"""Демо без сети: синтетические котировки и свечи. Запуск: python -m tests.demo  (адрес http://127.0.0.1:8800, токен demo)."""
import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "demo.db")
os.environ["APP_TOKEN"] = "demo"
os.environ["TELEGRAM_TOKEN"] = ""

from app import config, db  # noqa: E402
from tests import fake  # noqa: E402

config.APP_TOKEN = "demo"
config.TELEGRAM_TOKEN = ""
config.DB_PATH = __import__("pathlib").Path(os.environ["DB_PATH"])
fake.install()

if __name__ == "__main__":
    import asyncio
    import uvicorn
    from app import instruments, service
    from app.main import app

    async def seed():
        pid = db.x("INSERT INTO portfolio(name) VALUES('Основной')")
        db.x("INSERT INTO portfolio(name) VALUES('Долгосрок')")
        for ex, code, q, p in [("MOEX", "SBER", 30, 290.0), ("MOEX", "SU26238RMFS4", 40, 57.2), ("MOEX", "SiZ6", -2, 82000),
                               ("NASDAQ", "AAPL", 12, 205.0), ("NASDAQ", "NVDA", 20, 190.0), ("NASDAQ", "TSLA", 2, 410.0)]:
            inst = await instruments.ensure(ex, code)
            db.x("INSERT INTO position(portfolio_id,instrument_id,quantity,avg_price) VALUES(?,?,?,?)", (pid, inst["id"], q, p))
        tsla = instruments.by_code("NASDAQ", "TSLA")
        db.x("INSERT INTO alert(type,instrument_id,operator,threshold,repeat,is_active,created_at) VALUES('PRICE',?,'>',450,0,1,'2026-11-01T10:00:00Z')", (tsla["id"],))
        db.x("INSERT INTO alert(type,instrument_id,timeframe,indicator,operator,threshold,repeat,is_active,created_at) VALUES('INDICATOR',?,'1d','rsi','<',30,1,1,'2026-11-01T10:00:00Z')", (instruments.by_code("MOEX", "SBER")["id"],))
        await service.cycle()

    db.get()
    asyncio.run(seed())
    app.state.no_background = True
    uvicorn.run(app, host="127.0.0.1", port=8800, log_level="warning")
