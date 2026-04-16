import asyncio
import logging
import collections
import numpy as np
from omega_system.core.types import Tick
from omega_system.core.message_bus import MessageBus

logger = logging.getLogger("MarketStateEngine")

class MarketStateEngine:
    def __init__(self, message_bus: MessageBus):
        self.bus = message_bus
        self.history = collections.defaultdict(lambda: collections.deque(maxlen=50))

    async def start_worker(self):
        logger.info("[MarketStateEngine] Regime detection heuristics online. Sniffing flow.")
        while True:
            tick: Tick = await self.bus.raw_tick_queue.get()
            await self._classify_regime(tick)
            self.bus.raw_tick_queue.task_done()

    async def _classify_regime(self, tick: Tick):
        mid = (tick.bid + tick.ask) / 2.0
        self.history[tick.symbol].append(mid)
        
        prices = list(self.history[tick.symbol])
        
        # Default fallback
        regime = "DEAD"
        
        if len(prices) >= 20: 
            std_dev = np.std(prices)
            mean_price = np.mean(prices)
            var_pct = std_dev / mean_price
            
            # Simple heuristic constants
            if var_pct > 0.005:  # High volatility / Chop
                regime = "VOLATILE"
            elif var_pct < 0.0002: # Extremely tight variance
                regime = "DEAD"
            else:
                # Check for trend vs range via delta mapping over window
                delta = prices[-1] - prices[0]
                if abs(delta) > (std_dev * 1.5): # Drifting directionally outside 1.5 bands
                    regime = "TRENDING"
                else: # Returning consistently
                    regime = "RANGING"

        tick.regime = regime
        
        # Forward enriched tick to Alpha pipeline
        await self.bus.regime_tick_queue.put(tick)
