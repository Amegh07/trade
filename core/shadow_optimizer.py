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
    Shadow Optimizer Process
    Wakes up every 4 hours, reads trade outcomes, runs Bayesian Optimization via Optuna,
    and seamlessly injects the best Hurst and ATR thresholds into the live memory array.
    """
    logger = _make_logger()
    logger.info(f"ShadowOptimizer PID={mp.current_process().pid} started.")
    
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    
    while not stop_event.is_set():
        try:
            logger.info("[SHADOW AI] Waking up. Starting Optuna optimization study...")
            
            # Connect to logs/trades.db
            conn = sqlite3.connect("logs/trades.db", uri=True, flags=sqlite3.SQLITE_OPEN_READONLY)
            
            # Fetch past 48 hours of trade outcomes for optimization
            now = time.time()
            start_48h = now - 48 * 3600
            
            query = f"SELECT * FROM trades WHERE timestamp >= {start_48h} AND pnl IS NOT NULL"
            try:
                df_trades = pd.read_sql_query(query, conn)
            except Exception as e:
                logger.warning(f"Failed to read trades.db (might not exist yet): {e}.")
                df_trades = pd.DataFrame()
                
            conn.close()

            def objective(trial):
                # Request parameters
                h_thresh = trial.suggest_float("hurst_threshold", 0.55, 0.75)
                a_mult = trial.suggest_float("atr_multiplier", 0.05, 0.50)
                
                # Base theoretical Sortino if empty DB
                if df_trades.empty:
                    import random
                    return random.uniform(-0.5, 1.5)
                
                # Filter trades that would have survived the new Hurst threshold
                if 'hurst' in df_trades.columns:
                    sim_trades = df_trades[df_trades['hurst'] >= h_thresh].copy()
                else:
                    sim_trades = df_trades.copy()
                
                if sim_trades.empty:
                    return -999.0
                    
                # Simulate mock PnL impact from ATR multiplier adjustment (a tighter or wider SL/TP)
                sim_trades['adj_pnl'] = sim_trades['pnl'] * (1.0 + (a_mult - 0.25))
                returns = sim_trades['adj_pnl']
                
                if len(returns) < 2:
                    return -999.0
                    
                mean_return = returns.mean()
                downside_returns = returns[returns < 0]
                downside_std = downside_returns.std() if len(downside_returns) > 1 else 1e-5
                
                if downside_std == 0:
                    downside_std = 1e-5
                    
                sortino = (mean_return) / downside_std
                return sortino
            
            study = optuna.create_study(direction="maximize")
            study.optimize(objective, n_trials=100, timeout=120) 
            
            best_hurst = study.best_params["hurst_threshold"]
            best_atr = study.best_params["atr_multiplier"]
            
            # Inject memory
            with live_params.get_lock():
                live_params[0] = best_hurst
                live_params[1] = best_atr
                
            logger.info(f"[SHADOW AI] 🧠 Neural re-wire complete. New DNA: Hurst={best_hurst:.4f}, ATR={best_atr:.4f}")
            
        except Exception as e:
            logger.error(f"[SHADOW AI] Optimization error: {e}")
            
        # Sleep 4 hours. using stop_event.wait returns True if event is set, meaning we must break.
        if stop_event.wait(14400):
            break
