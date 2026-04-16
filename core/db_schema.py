import sqlite3
import os

def initialize_database(db_path='logs/trades.db'):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Original trades table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL,
        symbol TEXT,
        direction REAL,
        lot REAL,
        price REAL,
        ticket INTEGER,
        tp REAL,
        sl REAL,
        kelly REAL,
        z_score REAL,
        pnl REAL,
        basket_id TEXT,
        leg_role TEXT
    )
    ''')

    # New signals tracking table for Omega dashboard
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL,
        symbol TEXT,
        regime TEXT,
        z_score REAL,
        beta REAL,
        confidence REAL
    )
    ''')

    # New Control state table for UI Overrides
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS control_state (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    ''')

    # New Account State table for FastAPI bridging
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS account_state (
        id INTEGER PRIMARY KEY DEFAULT 1,
        equity REAL,
        balance REAL,
        margin_free REAL,
        timestamp REAL
    )
    ''')

    conn.commit()
    conn.close()
    print("Database schema successfully generated/verified.")

if __name__ == "__main__":
    initialize_database()
