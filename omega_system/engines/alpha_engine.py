import asyncio
import logging
import collections
import pandas as pd
import numpy as np
import statsmodels.api as sm
from statsmodels.tsa.stattools import coint
from omega_system.core.types import Tick, Signal
from omega_system.core.message_bus import MessageBus

logger = logging.getLogger("AlphaEngine")

class AlphaEngine:
    def __init__(self, message_bus: MessageBus):
        self.bus = message_bus
        self.ticks = collections.defaultdict(lambda: collections.deque(maxlen=1000))
        self.last_calc_time = 0.0
        
        # DNA Control (Dynamic Regime Aware Arrays)
        self.default_params = {"z_entry": 2.1, "z_exit": 0.5}
        self.regime_params = {}

    async def listen_for_params(self):
        smoothing_factor = 0.3 # 30% new DNA, 70% old DNA
        from omega_system.core.types import ParamUpdate
        while True:
            update: ParamUpdate = await self.bus.param_update_queue.get()
            
            for regime, new_dna in update.regime_params.items():
                if regime not in self.regime_params:
                    self.regime_params[regime] = self.default_params.copy()
                    
                old_entry = self.regime_params[regime]["z_entry"]
                old_exit = self.regime_params[regime]["z_exit"]
                
                # EMA Smoothing Layer
                smoothed_z_entry = ((1.0 - smoothing_factor) * old_entry) + (smoothing_factor * new_dna["z_entry"])
                smoothed_z_exit = ((1.0 - smoothing_factor) * old_exit) + (smoothing_factor * new_dna["z_exit"])
                
                # Hard Safety bounds mapping
                self.regime_params[regime]["z_entry"] = float(np.clip(smoothed_z_entry, 2.0, 5.0))
                self.regime_params[regime]["z_exit"] = float(np.clip(smoothed_z_exit, 0.0, 1.0))
                
                logger.warning(f"🧬 [{regime}] DNA EVOLUTION Smoothed Z-Entry: {self.regime_params[regime]['z_entry']:.2f} | Z-Exit: {self.regime_params[regime]['z_exit']:.2f}")
                
            self.bus.param_update_queue.task_done()

    async def start_worker(self):
        logger.info("[AlphaEngine] Statistical Arbitrage Coprocessor online...")
        asyncio.create_task(self.listen_for_params())
        while True:
            tick: Tick = await self.bus.regime_tick_queue.get()
            await self._process_tick(tick)
            self.bus.regime_tick_queue.task_done()

    async def _process_tick(self, tick: Tick):
        self.ticks[tick.symbol].append(tick)
        
        # Log market data seamlessly for historical simulations
        from omega_system.db.market_data_repo import market_data_repo
        # Throttle SQLite writes slightly by checking memory array length dynamically if needed
        # We write every tick here to map highly granular regimes natively.
        await market_data_repo.save_candle(tick.symbol, tick.last, tick.regime)
        
        loop = asyncio.get_running_loop()
        
        if (loop.time() - self.last_calc_time) > 5.0:
            self.last_calc_time = loop.time()
            
            sig = await asyncio.to_thread(self._sync_coint, "EURUSD", "GBPUSD")
            if sig:
                await self.bus.publish_signal(sig)

    def _sync_coint(self, asset_a: str, asset_b: str) -> Signal:
        q_a = list(self.ticks[asset_a])
        q_b = list(self.ticks[asset_b])
        
        if len(q_a) < 100 or len(q_b) < 100:
            return None
            
        df_a = pd.DataFrame([vars(t) for t in q_a])
        df_b = pd.DataFrame([vars(t) for t in q_b])
        
        df_merged = pd.merge(df_a, df_b, on='time', how='inner', suffixes=('_a', '_b'))
        if len(df_merged) < 100:
            return None
            
        y = df_merged['bid_a'].values
        x = df_merged['bid_b'].values
        
        try:
            _, p_value, _ = coint(y, x)
            x_const = sm.add_constant(x)
            model = sm.OLS(y, x_const).fit()
            beta = model.params[1]
            constant = model.params[0]
            
            spread = y - (beta * x + constant)
            mu, sigma = np.mean(spread[-20:]), np.std(spread[-20:])
            
            if sigma < 1e-12: return None
            z_score = (spread[-1] - mu) / sigma
            
            current_regime = self.ticks[asset_a][-1].regime if len(self.ticks[asset_a]) > 0 else "UNKNOWN"
            
            # Dynamically fetch Regime-Specific Threshold DNA
            current_z_entry = self.regime_params.get(current_regime, self.default_params)['z_entry']
            
            if abs(z_score) >= current_z_entry:
                logger.info(f"[AlphaEngine] Z-SCORE BREACH ({z_score:.2f}) on {asset_a}/{asset_b} [Regime: {current_regime}]")
                return Signal(
                    basket_id=f"BSKT_{asset_a}_{asset_b}",
                    asset_a=asset_a,
                    asset_b=asset_b,
                    action="ENTER",
                    beta=float(beta),
                    confidence=abs(z_score),
                    regime=current_regime
                )
        except Exception as e:
            logger.debug(f"[AlphaEngine] Math constraint failure: {e}")
            
        return None
