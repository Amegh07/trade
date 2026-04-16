import sqlite3
import json
import os
from omega_system.config.settings import settings

class StateRepo:
    def __init__(self):
        os.makedirs(os.path.dirname(settings.CONTROL_DB_PATH), exist_ok=True)
        
    def write_snapshot(self, state: dict) -> None:
        conn = sqlite3.connect(settings.CONTROL_DB_PATH, timeout=5.0)
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS state_snapshots (id INTEGER PRIMARY KEY, state_json TEXT, timestamp REAL)")
        cur.execute("INSERT INTO state_snapshots (state_json, timestamp) VALUES (?, strftime('%s','now'))", (json.dumps(state),))
        conn.commit()
        conn.close()
        
    def write_override(self, key: str, value: str) -> None:
        conn = sqlite3.connect(settings.CONTROL_DB_PATH, timeout=5.0)
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS control_state (key TEXT PRIMARY KEY, value TEXT)")
        cur.execute("REPLACE INTO control_state (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
        conn.close()

    def fetch_override(self, key: str) -> str:
        try:
            conn = sqlite3.connect(f"file:{settings.CONTROL_DB_PATH}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute("SELECT value FROM control_state WHERE key=?", (key,))
            row = cur.fetchone()
            conn.close()
            return row[0] if row else ""
        except:
            return ""

state_repo = StateRepo()
