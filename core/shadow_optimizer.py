import time
import sqlite3
import logging
import multiprocessing as mp
import pandas as pd
import numpy as np

def _make_logger():
    log = logging.getLogger("shadow_optimizer")
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter(
            "%(asctime)s [ShadowOptimizer] %(levelname)s — %(message)s"
        ))
        log.addHandler(h)
    log.setLevel(logging.INFO)
    return log

def run_shadow_optimizer(live_params: mp.Array, stop_event: mp.Event):
    """
    Shadow Optimizer Process: StatArb Edition
    Optimizes Spread Z-Score thresholds (Entry/Exit) based on rolling PnL outcomes.
    """
    logger = _make_logger()
    logger.info(f"ShadowOptimizer PID={mp.current_process().pid} started.")
    
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    
    while not stop_event.is_set():
        try:
            logger.info("[SHADOW AI] Waking up. Optimizing StatArb Z-Score thresholds...")
            
            conn = sqlite3.connect('file:logs/trades.db?mode=ro', uri=True)
            now = time.time()
            start_48h = now - 48 * 3600
            
            query = f"SELECT * FROM trades WHERE timestamp >= {start_48h} AND pnl IS NOT NULL"
            try:
                df_trades = pd.read_sql_query(query, conn)
            except Exception as e:
                logger.warning(f"Failed to read trades.db: {e}.")
                df_trades = pd.DataFrame()
                
            conn.close()

            def objective(trial):
                z_entry = trial.suggest_float("z_entry", 2.0, 4.0)
                z_exit  = trial.suggest_float("z_exit", 0.0, 0.5)
                
                if df_trades.empty:
                    return 0.0
                
                # Filter trades that would have been triggered by this Entry Z
                if 'z_score' in df_trades.columns:
                    # Logic: If z_score was >= required threshold, trade happens
                    sim_trades = df_trades[df_trades['z_score'].abs() >= z_entry].copy()
                else:
                    sim_trades = df_trades.copy()
                
                if sim_trades.empty:
                    return -10.0
                    
                returns = sim_trades['pnl']
                if len(returns) < 2: return -5.0
                    
                mean_return = returns.mean()
                downside_returns = returns[returns < 0]
                downside_std = downside_returns.std() if len(downside_returns) > 1 else 1e-5
                
                return (mean_return) / downside_std
            
            study = optuna.create_study(direction="maximize")
            study.optimize(objective, n_trials=50, timeout=60) 
            
            best_entry = study.best_params["z_entry"]
            best_exit  = study.best_params["z_exit"]
            
            # Inject memory [0]=Entry, [1]=Exit
            with live_params.get_lock():
                live_params[0] = best_entry
                live_params[1] = best_exit
                
            logger.info(f"[SHADOW AI] 🧠 DNA Reprogrammed: Z_Entry={best_entry:.2f}, Z_Exit={best_exit:.2f}")
            
        except Exception as e:
            logger.error(f"[SHADOW AI] Optimization error: {e}")
            
        if stop_event.wait(14400): # 4 Hour cycle
            break
            
        # Sleep 4 hours. using stop_event.wait returns True if event is set, meaning we must break.
        if stop_event.wait(14400):
            break
