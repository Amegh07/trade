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
        # PER-SYMBOL ISOLATION
        self.history = collections.defaultdict(lambda: collections.deque(maxlen=100))
        
        # HYSTERESIS STATE MACHINE
        self.current_regime = {}
        self.regime_candidate = {}
        self.regime_candidate_count = collections.defaultdict(int)
        
        # ELITE CONSTANTS (Universally Normalized Thresholds)
        self.high_vol_threshold = 0.0005     # 0.05% return stdev per tick represents violent chop
        self.trend_threshold = 0.000015      # 0.0015% drift per sequence step marks direction
        self.range_threshold = 0.0002        # Extremely tightly compressed delta marks deadness

    async def start_worker(self):
        logger.info("[MarketStateEngine] Elite Regime Detection Armed. Vectorized Matrix Hysteresis Online.")
        while True:
            tick: Tick = await self.bus.raw_tick_queue.get()
            await self._classify_regime(tick)
            self.bus.raw_tick_queue.task_done()

    async def _classify_regime(self, tick: Tick):
        sym = tick.symbol
        mid = (tick.bid + tick.ask) / 2.0
        self.history[sym].append(mid)
        
        # Init base tracking instances
        if sym not in self.current_regime:
            self.current_regime[sym] = "UNKNOWN"
            self.regime_candidate[sym] = "UNKNOWN"
            
        prices = list(self.history[sym])
        
        if len(prices) < 20: 
            tick.regime = self.current_regime[sym]
            await self.bus.regime_tick_queue.put(tick)
            return
            
        # --- ELITE MATH LOGIC ---
        arr = np.array(prices)
        
        # Normalize relative to starting vector mapping arrays safely around universal 1.0 baselines
        norm_arr = arr / arr[0]
        
        # 1. Delta Return array
        returns = np.diff(norm_arr)
        
        # 2. Extract standard sequence Volatility
        volatility = np.std(returns)
        
        # 3. Extract regression vector Trend Slope directly representing overarching momentum
        x = np.arange(len(norm_arr))
        slope = np.polyfit(x, norm_arr, 1)[0]
        
        # 4. Assess boundary compression 
        compression = np.max(norm_arr) - np.min(norm_arr)
        
        # --- CLASSIFICATION LAYER ---
        if volatility > self.high_vol_threshold:
            candidate = "VOLATILE"
        elif abs(slope) > self.trend_threshold:
            candidate = "TRENDING"
        elif compression < self.range_threshold:
            candidate = "DEAD"
        else:
            candidate = "RANGING"
            
        # --- HYSTERESIS APPLICATION ---
        if candidate == self.current_regime[sym]:
            self.regime_candidate_count[sym] = 0
            self.regime_candidate[sym] = candidate
        else:
            if candidate == self.regime_candidate[sym]:
                self.regime_candidate_count[sym] += 1
            else:
                self.regime_candidate[sym] = candidate
                self.regime_candidate_count[sym] = 1
                
            if self.regime_candidate_count[sym] >= 5:
                logger.debug(f"[MarketStateEngine] Regime Shift Hysteresis Passed for {sym}: {self.current_regime[sym]} -> {candidate}")
                self.current_regime[sym] = candidate
                self.regime_candidate_count[sym] = 0

        # OUTPUT MAP
        tick.regime = self.current_regime[sym]
        await self.bus.regime_tick_queue.put(tick)
