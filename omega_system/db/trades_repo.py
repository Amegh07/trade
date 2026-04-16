import sqlite3
import os
from omega_system.config.settings import settings

class TradesRepo:
    def __init__(self):
        os.makedirs(os.path.dirname(settings.DB_PATH), exist_ok=True)
        # Initialize tables if necessary
        self.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                ticket INTEGER PRIMARY KEY,
                symbol TEXT,
                volume REAL,
                price_open REAL,
                price_close REAL,
                pnl REAL,
                timestamp REAL,
                type INTEGER,
                basket_id TEXT,
                regime TEXT,
                z_entry_used REAL,
                execution_latency_ms REAL
            )
        """)

    def execute(self, query: str, params: tuple = ()) -> None:
        conn = sqlite3.connect(settings.DB_PATH, timeout=5.0)
        cur = conn.cursor()
        cur.execute(query, params)
        conn.commit()
        conn.close()
        
    def fetch(self, query: str, params: tuple = ()) -> list:
        conn = sqlite3.connect(f"file:{settings.DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    async def insert_trade(self, basket_id: str, regime: str, z_entry: float, latency: float, symbol: str, pnl: float = 0.0):
        """Asynchronously dumps physical execution telemetry onto the permanent ledger for the RL ShadowEngine."""
        import asyncio
        import time
        loop = asyncio.get_event_loop()
        def _write():
            query = """
                INSERT INTO trades (
                    basket_id, symbol, regime, z_entry_used, execution_latency_ms, pnl, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """
            params = (basket_id, symbol, regime, z_entry, latency, pnl, time.time())
            self.execute(query, params)
        await loop.run_in_executor(None, _write)

trades_repo = TradesRepo()
