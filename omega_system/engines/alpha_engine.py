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
        self.z_entry = 2.0
        self.z_exit = 0.5

    async def listen_for_params(self):
        while True:
            from omega_system.core.types import ParamUpdate
            update: ParamUpdate = await self.bus.param_update_queue.get()
            self.z_entry = update.z_entry
            self.z_exit = update.z_exit
            logger.warning(f"[SHADOW AI] DNA Reprogrammed. New Z-Entry: {self.z_entry:.2f} | Z-Exit: {self.z_exit:.2f}")
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
        
        loop = asyncio.get_running_loop()
        
        # Trigger periodic Cointegration math on an interval (Simulated M15 formation proxy)
        if (loop.time() - self.last_calc_time) > 5.0:
            self.last_calc_time = loop.time()
            
            # Utilizing asyncio.to_thread prevents the statsmodels OLS from blocking the event loop
            sig = await asyncio.to_thread(self._sync_coint, "EURUSD", "GBPUSD")
            if sig:
                await self.bus.publish_signal(sig)

    def _sync_coint(self, asset_a: str, asset_b: str) -> Signal:
        """Isolated mathematical processor evaluating historical pricing."""
        q_a = list(self.ticks[asset_a])
        q_b = list(self.ticks[asset_b])
        
        if len(q_a) < 100 or len(q_b) < 100:
            return None
            
        df_a = pd.DataFrame([vars(t) for t in q_a])
        df_b = pd.DataFrame([vars(t) for t in q_b])
        
        # Inner merge aligns time epochs precisely
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
            
            # Fetch derived algorithmic regime from the tick ingestion tail
            current_regime = self.ticks[asset_a][-1].regime if len(self.ticks[asset_a]) > 0 else "UNKNOWN"
            
            # Emit Signal if beyond execution thresholds
            if abs(z_score) >= self.z_entry:
                logger.info(f"[AlphaEngine] Z-SCORE BREACH ({z_score:.2f}) on {asset_a}/{asset_b} [Regime: {current_regime}]")
                return Signal(
                    basket_id=f"BSKT_{asset_a}_{asset_b}",
                    asset_a=asset_a,
                    asset_b=asset_b,
                    action="ENTER",
                    beta=float(beta),
                    confidence=abs(z_score),  # Proxy for Expected Value to pass Risk limits
                    regime=current_regime
                )
        except Exception as e:
            logger.debug(f"[AlphaEngine] Math constraint failure: {e}")
            
        return None
