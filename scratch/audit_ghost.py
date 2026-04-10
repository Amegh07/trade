import sqlite3
import pandas as pd

def audit_ghost_recovery():
    db_path = 'logs/trades.db'
    conn = sqlite3.connect(db_path)
    
    # 1. Check the Active Ghost Trade
    print("\n--- 🛰️ ACTIVE GHOST TRACKING ---")
    try:
        # Note: the user's script assumes 'trades' table with 'is_ghost' which may not map to our 'ghost_history' 
        # But we will use the exact script given for fidelity, and if it fails, I'll adapt it to our actual schema.
        active_query = "SELECT symbol, action, entry_price, tp, sl, timestamp FROM trades WHERE is_ghost = 1 AND status = 'OPEN'"
        active_df = pd.read_sql_query(active_query, conn)
        
        if active_df.empty:
            print("No active ghost trades. Either it hit TP/SL or failed to open.")
        else:
            print(active_df)
    except Exception as e:
        print(f"Error reading 'trades' table: {e}")

    # 2. Calculate the "Ghost Profit Factor" for Re-entry
    print("\n--- 🛡️ RECOVERY METRICS (Target PF > 1.2) ---")
    try:
        metrics_query = """
            SELECT 
                SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) as gross_profit,
                SUM(CASE WHEN pnl < 0 THEN ABS(pnl) ELSE 0 END) as gross_loss,
                COUNT(*) as total_ghost_trades
            FROM ghost_history 
            ORDER BY timestamp DESC LIMIT 5
        """
        metrics = pd.read_sql_query(metrics_query, conn).iloc[0]
        
        gp = metrics['gross_profit'] or 0
        gl = metrics['gross_loss'] or 0
        pf = (gp / gl) if gl > 0 else (gp if gp > 0 else 0)
        
        print(f"Total Closed Ghosts: {metrics['total_ghost_trades']}")
        print(f"Ghost Profit Factor: {pf:.2f}")
        
        if pf >= 1.2:
            print("STATUS: ✅ ALPHA RESTORED. System ready for Stage 1 (25% Volume).")
        else:
            print(f"STATUS: ⏳ HEALING. Need more wins to reach 1.2 threshold.")
    except Exception as e:
        print(f"Error calculating metrics: {e}")

    conn.close()

if __name__ == "__main__":
    audit_ghost_recovery()
