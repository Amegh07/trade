import asyncio
import logging
import sqlite3
import pandas as pd
import numpy as np
import optuna
from omega_system.core.message_bus import MessageBus
from omega_system.core.types import ParamUpdate
from omega_system.config.settings import settings

logger = logging.getLogger("ShadowEngine")

class ShadowEngine:
    def __init__(self, message_bus: MessageBus):
        self.bus = message_bus
        self.retrain_timer = settings.SHADOW_AI_RETRAIN_INTERVAL_SEC
        optuna.logging.set_verbosity(optuna.logging.WARNING)

    async def start_worker(self):
        logger.info("[ShadowEngine] L3 Regime-Aware Optuna Matrix Armed.")
        while True:
            await asyncio.sleep(self.retrain_timer)
            await self._run_adaptation()

    async def _run_adaptation(self):
        logger.info("[ShadowEngine] Initiating Regime-Isolated Full Strategy Simulation...")
        regime_params = await asyncio.to_thread(self._optimize)
        
        if regime_params:
            update = ParamUpdate(regime_params=regime_params)
            await self.bus.param_update_queue.put(update)
            logger.info(f"[ShadowEngine] Dispatching optimal regime matrix: {list(regime_params.keys())}")

    def _get_historical_market_data(self):
        try:
            conn = sqlite3.connect(f"file:{settings.DB_PATH}?mode=ro", uri=True)
            query = """
                SELECT timestamp, symbol, close, regime 
                FROM market_data 
                WHERE symbol IN ('EURUSD', 'GBPUSD') 
                ORDER BY timestamp DESC 
                LIMIT 10000
            """
            df = pd.read_sql_query(query, conn)
            conn.close()
            return df
        except Exception as e:
            return pd.DataFrame()

    def _optimize(self):
        df = self._get_historical_market_data()
        if df is None or df.empty:
            logger.debug("[ShadowEngine] Sand-box devoid of data. Optimization bypassed.")
            return None
            
        regimes = df['regime'].dropna().unique()
        final_params = {}
        
        for reg in regimes:
            if reg == "UNKNOWN" or reg == "DEAD": continue
            
            reg_df = df[df['regime'] == reg].copy()
            
            # Pivot table to map correlated arrays against chronological timeline
            df_pivot = reg_df.pivot(index='timestamp', columns='symbol', values='close').sort_index()
            df_pivot = df_pivot.ffill().dropna()
            
            if 'EURUSD' not in df_pivot or 'GBPUSD' not in df_pivot:
                continue
                
            prices_a = df_pivot['EURUSD'].values
            prices_b = df_pivot['GBPUSD'].values
            
            if len(prices_a) < 50:
                continue
                
            spread = prices_a - prices_b 
            
            window = 20
            rolling_mean = pd.Series(spread).rolling(window).mean().values
            rolling_std = pd.Series(spread).rolling(window).std().values
            
            z_scores = (spread - rolling_mean) / (rolling_std + 1e-9)

            def objective(trial):
                z_entry = trial.suggest_float("z_entry", 2.0, 4.5)
                z_exit = trial.suggest_float("z_exit", 0.0, 0.8)
                
                entry_long = z_scores <= -z_entry
                entry_short = z_scores >= z_entry
                exit_signal = np.abs(z_scores) <= z_exit
                
                returns = []
                current_state = 0
                entry_price = 0.0
                spread_penalty = 0.0002 
                
                for i in range(window, len(z_scores)):
                    if current_state != 0 and exit_signal[i]:
                        pn = (spread[i] - entry_price) * current_state - spread_penalty
                        returns.append(pn)
                        current_state = 0
                    elif current_state == 0:
                        if entry_long[i]:
                            current_state = 1
                            entry_price = spread[i]
                        elif entry_short[i]:
                            current_state = -1
                            entry_price = spread[i]
                            
                if len(returns) < 5:
                    return -1.0 # Penalized starvation
                    
                ret_arr = np.array(returns)
                mean_ret = np.mean(ret_arr)
                std_ret = np.std(ret_arr) + 1e-6
                sharpe = (mean_ret / std_ret) * np.sqrt(len(ret_arr))
                
                cumul = np.cumsum(ret_arr)
                running_max = np.maximum.accumulate(cumul)
                drawdown = running_max - cumul
                max_drawdown = np.max(drawdown) if len(drawdown) > 0 else 0.001
                
                score = sharpe - (max_drawdown * 0.2)
                return score
                
            study = optuna.create_study(direction="maximize")
            study.optimize(objective, n_trials=50)
            
            final_params[reg] = {
                "z_entry": study.best_params["z_entry"],
                "z_exit": study.best_params["z_exit"]
            }
            
        return final_params if final_params else None
