import asyncio
import logging
import MetaTrader5 as mt5
from omega_system.core.types import Tick
from omega_system.core.message_bus import MessageBus
from omega_system.adapters.mt5_client import MT5Client

logger = logging.getLogger("DataEngine")

class DataEngine:
    def __init__(self, mt5_client: MT5Client, message_bus: MessageBus):
        self.mt5 = mt5_client
        self.bus = message_bus
        # Tracking primary pairs for statistical arbitrage
        self.symbols = ["EURUSD", "GBPUSD", "USDJPY", "USDCHF"]

    async def start_worker(self):
        logger.info("[DataEngine] Commencing async tick polling sequence...")
        while True:
            for sym in self.symbols:
                tick_info = mt5.symbol_info_tick(sym)
                if tick_info:
                    tick_dt = Tick(
                        symbol=sym,
                        bid=tick_info.bid,
                        ask=tick_info.ask,
                        last=tick_info.last,
                        volume=tick_info.volume_real,
                        time=float(tick_info.time)
                    )
                    await self.bus.tick_queue.put(tick_dt)
            
            # Polling limiter preventing MT5 terminal CPU locks
            await asyncio.sleep(0.01)
