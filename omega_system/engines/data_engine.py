import asyncio
import logging
import MetaTrader5 as mt5
from omega_system.core.types import Tick
from omega_system.core.message_bus import MessageBus
from omega_system.adapters.mt5_client import MT5Client

logger = logging.getLogger("DataEngine")

class DataEngine:
    def __init__(self, mt5_client: MT5Client, message_bus: MessageBus, state_engine=None):
        from omega_system.config.universe import TRADABLE_PAIRS
        self.mt5 = mt5_client
        self.bus = message_bus
        self.state = state_engine
        # Dynamically imported from decentralized config matrix
        self.symbols = TRADABLE_PAIRS

    async def start_worker(self):
        logger.info("[DataEngine] Commencing async tick and concurrent candle polling sequences...")
        
        # Deploy parallel background Candle synchronization
        asyncio.create_task(self._poll_candles(), name="PollCandlesTask")
        
        while True:
            for sym in self.symbols:
                # MT5 Thread-safe execution
                tick_info = await self.mt5.symbol_info_tick(sym)
                if tick_info:
                    tick_dt = Tick(
                        symbol=sym,
                        bid=tick_info.bid,
                        ask=tick_info.ask,
                        last=tick_info.last,
                        time=float(tick_info.time)
                    )
                    await self.bus.raw_tick_queue.put(tick_dt)
                    if self.state:
                        await self.state.update_tick_time()
            
            # Polling limiter preventing MT5 terminal CPU locks
            await asyncio.sleep(0.01)

    async def _poll_candles(self):
        import MetaTrader5 as mt5
        from omega_system.core.types import CandleSnapshot
        
        logger.info("[DataEngine] Background Multi-TF Candle Poller initiated.")
        while True:
            # Re-sync every 60 seconds natively
            await asyncio.sleep(60.0)
            
            tasks = []
            queries = []
            
            # Fire all requests concurrently through thread pools
            for sym in self.symbols:
                # M15 (500 frames)
                tasks.append(self.mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M15, 0, 500))
                queries.append((sym, "M15"))
                # H1 (50 frames)
                tasks.append(self.mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_H1, 0, 50))
                queries.append((sym, "H1"))
                
            results = await asyncio.gather(*tasks)
            
            # Map execution results onto the Queue payload stream
            for (sym, tf), rates in zip(queries, results):
                if rates is not None and len(rates) > 0:
                    snap = CandleSnapshot(symbol=sym, timeframe=tf, rates=rates)
                    await self.bus.candle_queue.put(snap)
