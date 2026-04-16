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
                basket_id TEXT
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

trades_repo = TradesRepo()
