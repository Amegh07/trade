import asyncio
import aiofiles
import sqlite3
import pandas as pd
from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import os
import time

app = FastAPI(title="Omega Architecture Dashboard")

# Allow CORS for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
async def serve_index():
    return FileResponse("index.html")

@app.get("/api/equity")
def get_equity():
    try:
        conn = sqlite3.connect('file:logs/trades.db?mode=ro', uri=True)
        # Fetch trades that have completed (pnl is not null)
        query = "SELECT timestamp, pnl FROM trades WHERE pnl IS NOT NULL ORDER BY timestamp ASC"
        df = pd.read_sql_query(query, conn)
        conn.close()
    except sqlite3.OperationalError:
        # DB might not exist yet
        return [{"time": int(time.time()), "value": 100000}]

    current_equity = 100000.0
    result = []
    
    if df.empty:
        return [{"time": int(time.time()), "value": current_equity}]
        
    # Group by timestamp (convert to int seconds) to prevent duplicate times causing chart errors
    df['time'] = df['timestamp'].astype(int)
    grouped = df.groupby('time')['pnl'].sum().reset_index()
    
    for _, row in grouped.iterrows():
        current_equity += row['pnl']
        result.append({
            "time": int(row['time']),
            "value": round(current_equity, 2)
        })
        
    return result

@app.get("/api/stats")
def get_stats():
    try:
        conn = sqlite3.connect('file:logs/trades.db?mode=ro', uri=True)
        df = pd.read_sql_query("SELECT pnl FROM trades WHERE pnl IS NOT NULL", conn)
        conn.close()
    except sqlite3.OperationalError:
        return {"total_trades": 0, "win_rate": 0, "drawdown": 0, "current_equity": 100000}

    total_trades = len(df)
    if total_trades == 0:
        return {"total_trades": 0, "win_rate": 0, "drawdown": 0, "current_equity": 100000}
        
    wins = len(df[df['pnl'] > 0])
    win_rate = round((wins / total_trades) * 100, 2)
    
    eq = 100000.0 + df['pnl'].cumsum()
    current_equity = round(eq.iloc[-1], 2)
    
    peak = eq.cummax()
    # Fix for division by zero if peak somehow <= 0
    drawdown = ((peak - eq) / peak).max() * 100 if (peak > 0).all() else 0
    
    return {
        "total_trades": total_trades,
        "win_rate": win_rate,
        "drawdown": round(drawdown, 2),
        "current_equity": current_equity
    }

@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await websocket.accept()
    log_path = "logs/trading.log"
    
    # Create file if it doesn't exist to prevent crash
    if not os.path.exists(log_path):
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        open(log_path, 'a').close()

    try:
        # Preload the last 50 lines to populate the terminal
        with open(log_path, "r", encoding="utf-8", errors='replace') as f:
            lines = f.readlines()
            for line in lines[-50:]:
                await websocket.send_text(line.strip())
                
        # Tail the file asynchronously
        async with aiofiles.open(log_path, mode="r", encoding="utf-8", errors='replace') as f:
            await f.seek(0, os.SEEK_END)
            while True:
                line = await f.readline()
                if not line:
                    await asyncio.sleep(0.1)
                    continue
                await websocket.send_text(line.strip())
    except Exception as e:
        print(f"WebSocket Error: {e}")
        try:
            await websocket.send_text(f"SERVER ERROR: {e}")
        except:
            pass
