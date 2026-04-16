import sqlite3
import os
import time
from omega_system.config.settings import settings

class MarketDataRepo:
    def __init__(self):
        os.makedirs(os.path.dirname(settings.DB_PATH), exist_ok=True)
        self.execute("""
            CREATE TABLE IF NOT EXISTS market_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                symbol TEXT,
                close REAL,
                regime TEXT
            )
        """)
        self.execute("CREATE INDEX IF NOT EXISTS idx_market_data_stamp ON market_data(timestamp)")

    def execute(self, query: str, params: tuple = ()) -> None:
        conn = sqlite3.connect(settings.DB_PATH, timeout=5.0)
        cur = conn.cursor()
        cur.execute(query, params)
        conn.commit()
        conn.close()

    async def save_candle(self, symbol: str, close: float, regime: str):
        import asyncio
        loop = asyncio.get_event_loop()
        def _write():
            query = """INSERT INTO market_data (timestamp, symbol, close, regime) VALUES (?, ?, ?, ?)"""
            self.execute(query, (time.time(), symbol, close, regime))
        await loop.run_in_executor(None, _write)

market_data_repo = MarketDataRepo()
