import asyncio
import aiofiles
import sqlite3
import json
import os
import pandas as pd
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Omega Command Center")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
TRADES_DB = os.getenv("OMEGA_DB_PATH", "logs/trades.db")
CONTROL_DB = os.getenv("OMEGA_CONTROL_DB_PATH", "logs/control.db")

def query_db(query, params=(), db_path=None):
    if db_path is None:
        db_path = TRADES_DB
    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(query, params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        print(f"DB Read Error: {e}")
        return []

@app.get("/api/equity")
def get_equity():
    rows = query_db("SELECT timestamp, pnl FROM trades WHERE pnl IS NOT NULL ORDER BY timestamp ASC")
    current_equity = 100000.0
    result = []
    
    if not rows:
        return []
        
    df = pd.DataFrame(rows)
    df['time'] = df['timestamp'].astype(int)
    grouped = df.groupby('time')['pnl'].sum().reset_index()
    
    initial_eq = 100000.0
    for _, row in grouped.iterrows():
        initial_eq += row['pnl']
        result.append({"time": int(row['time']), "value": round(initial_eq, 2)})
        
    return result

@app.get("/api/positions")
def get_positions():
    """Returns open deals directly from the SQLite engine"""
    rows = query_db("SELECT symbol, direction, lot, price, ticket FROM trades WHERE pnl IS NULL ORDER BY timestamp DESC")
    
    positions = []
    for r in rows:
        dir_str = "BUY" if r['direction'] == 1.0 else "SELL"
        positions.append({
            "ticker": r['symbol'],
            "side": dir_str,
            "lot": r['lot'],
            "entry_price": r['price'],
            "ticket": r['ticket']
        })
        
    return positions

@app.get("/api/brain/signals")
def get_signals():
    """Queries the latest signal per symbol from the signals table."""
    rows = query_db('''
        SELECT symbol, regime, z_score, beta, confidence 
        FROM signals 
        WHERE timestamp IN (SELECT max(timestamp) FROM signals GROUP BY symbol)
    ''')
    
    return rows

@app.get("/api/risk")
def get_risk():
    """Calculates live risk metrics from the database."""
    rows = query_db("SELECT pnl FROM trades WHERE pnl IS NOT NULL")
    
    drawdown = 0.0
    if rows:
        df = pd.DataFrame(rows)
        eq = 100000.0 + df['pnl'].cumsum()
        peak = eq.cummax()
        if (peak > 0).all():
            drawdown = ((peak - eq) / peak).max() * 100
    
    acc = query_db("SELECT equity, balance FROM account_state ORDER BY id DESC LIMIT 1")
    if acc and acc[0]['balance'] > 0 and acc[0]['balance'] > acc[0]['equity']:
        live_dd = ((acc[0]['balance'] - acc[0]['equity']) / acc[0]['balance']) * 100
        drawdown = max(drawdown, live_dd)
    
    open_trades = query_db("SELECT sum(lot) as total_lot FROM trades WHERE pnl IS NULL")
    exposure = open_trades[0]['total_lot'] if open_trades and open_trades[0]['total_lot'] else 0.0
    
    state = query_db("SELECT value FROM control_state WHERE key='correlation_penalty'", db_path=CONTROL_DB)
    penalty = float(state[0]['value']) if state else 0.85 
    
    return {
        "drawdown_pct": round(drawdown, 2),
        "total_exposure": round(exposure, 2),
        "correlation_penalty": penalty
    }

# Control Commands Pydantic Model
class OverrideCommand(BaseModel):
    manual_override: bool
    ghost_mode: bool
    max_risk_pct: float

@app.post("/api/control/override")
def control_override(cmd: OverrideCommand):
    try:
        conn = sqlite3.connect(CONTROL_DB, timeout=5.0)
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS control_state (key TEXT PRIMARY KEY, value TEXT)")
        cur.execute("REPLACE INTO control_state (key, value) VALUES (?, ?)", ("manual_override", str(cmd.manual_override)))
        cur.execute("REPLACE INTO control_state (key, value) VALUES (?, ?)", ("ghost_mode", str(cmd.ghost_mode)))
        cur.execute("REPLACE INTO control_state (key, value) VALUES (?, ?)", ("max_risk_pct", str(cmd.max_risk_pct)))
        conn.commit()
        conn.close()
        return {"status": "SUCCESS"}
    except Exception as e:
        return {"status": "ERROR", "msg": str(e)}

@app.get("/api/stats")
def get_stats():
    """Returns essential system performance statistics."""
    # This is a placeholder for future strategy-level analysis (Win Rate, CAGR, etc.)
    # For now, it returns counts from the trades table.
    try:
        total = query_db("SELECT count(*) as count FROM trades WHERE pnl IS NOT NULL")
        return {
            "total_trades": total[0]['count'] if total else 0,
            "system_uptime": "LIVE",
            "version": "1.2.0-OMEGA"
        }
    except:
        return {"total_trades": 0, "system_uptime": "ERROR"}

@app.post("/api/control/kill")
def emergency_kill():
    try:
        conn = sqlite3.connect(CONTROL_DB, timeout=5.0)
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS control_state (key TEXT PRIMARY KEY, value TEXT)")
        cur.execute("REPLACE INTO control_state (key, value) VALUES (?, ?)", ("EMERGENCY_STOP", "True"))
        conn.commit()
        conn.close()
        return {"status": "KILLED"}
    except Exception as e:
        return {"status": "ERROR"}

# Multiplexed JSON WebSocket
@app.websocket("/ws/stream")
async def websocket_stream(websocket: WebSocket):
    await websocket.accept()
    log_path = "logs/trading.log"
    
    # 1. Log Tailing Task
    async def tail_logs():
        try:
            if not os.path.exists(log_path):
                os.makedirs(os.path.dirname(log_path), exist_ok=True)
                with open(log_path, 'a') as f:
                    f.write("--- LOG STREAM INITIALIZED ---\n")
            
            async with aiofiles.open(log_path, mode="r", encoding="utf-8", errors='replace') as f:
                await f.seek(0, os.SEEK_END)
                while True:
                    line = await f.readline()
                    if not line:
                        await asyncio.sleep(0.5)
                        continue
                    await websocket.send_text(json.dumps({
                        "type": "LOG",
                        "message": line.strip()
                    }))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"Log Tailer Error: {e}")

    # 2. Brain Polling Task
    async def broadcast_brain():
        try:
            while True:
                await asyncio.sleep(2.0)
                signals = get_signals()
                await websocket.send_text(json.dumps({
                    "type": "BRAIN_TICK",
                    "data": signals
                }))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"Brain Broadcaster Error: {e}")

    try:
        # Run both tasks concurrently. gather will raise if one task fails or is cancelled.
        await asyncio.gather(tail_logs(), broadcast_brain())
    except Exception as e:
        print(f"WS Main Exception: {e}")
    finally:
        try:
            await websocket.close()
        except:
            pass
