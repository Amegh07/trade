import asyncio
import logging
import os
import signal
import time
import MetaTrader5 as mt5
from omega_system.adapters.mt5_client import MT5Client
from omega_system.engines.state_engine import StateEngine
from omega_system.core.message_bus import MessageBus
from omega_system.core.types import OrderStatus

logger = logging.getLogger("WatchdogEngine")

class WatchdogEngine:
    def __init__(self, mt5_client: MT5Client, state_engine: StateEngine, message_bus: MessageBus):
        from omega_system.config.settings import settings
        self.mt5 = mt5_client
        self.state = state_engine
        self.bus = message_bus
        self.timeout_sec = settings.WATCHDOG_TIMEOUT_SEC

    async def start_worker(self):
        logger.info("[WatchdogEngine] L0 Failsafe Armed. Heartbeat array initialized.")
        while True:
            await asyncio.sleep(5.0)
            await self._run_diagnostics()

    async def _run_diagnostics(self):
        # 1. Connection Check (Is the pipeline cut?)
        info = mt5.terminal_info()
        if info is None or not info.connected:
            logger.critical("[Watchdog] ⚠️ MT5 Disconnected! Terminal response void.")
            recovered = await self._attempt_recovery()
            if not recovered:
                await self._emergency_shutdown("MT5 Terminal Connection Extinguished")
                return
                
        # 2. Data Feed Stagnation Checks (Are we flying blind?)
        last_tick = await self.state.get_last_tick_time()
        stagnation = time.time() - last_tick
        
        # Dead silence triggers a halt mapping centralized parameters
        if stagnation > self.timeout_sec:
            logger.critical(f"[Watchdog] ⚠️ Data feed frozen for {stagnation:.1f}s! Silent Deadlock detected.")
            await self._emergency_shutdown(f"Data Feed Stagnation (Frozen for {stagnation:.1f}s)")

    async def _attempt_recovery(self) -> bool:
        """Attemps broker re-link up to 3 times."""
        for i in range(3):
            logger.warning(f"[Watchdog] Re-initialization node strike {i+1}/3...")
            try:
                res = await self.mt5.initialize()
                if res: 
                    logger.info("[Watchdog] MT5 Comms re-established.")
                    return True
            except TypeError:
                # Sync fallback handling just in case the execution client passes it natively
                res = self.mt5.initialize()
                if res: 
                    logger.info("[Watchdog] MT5 Comms re-established.")
                    return True
                
            await asyncio.sleep(2.0)
        return False

    async def _emergency_shutdown(self, reason: str):
        """The final abort button."""
        logger.critical(f"=======================================")
        logger.critical(f"🚨   EMERGENCY SHUTDOWN INITIATED   🚨")
        logger.critical(f"    REASON: {reason}")
        logger.critical(f"=======================================")
        
        # First order of business: Flatten the active positions immediately
        snapshot = await self.state.get_snapshot()
        for b_id, basket in snapshot.items():
            if basket.status == OrderStatus.OPEN:
                logger.critical(f"[Watchdog] Flattening active basket {b_id} instantly...")
                for role, leg in basket.legs.items():
                    if leg.ticket:
                        # Targeted closure
                        await self.mt5.close_position_by_ticket(leg.symbol, leg.ticket)
                        logger.critical(f"    -> Hard-closed Ticket: {leg.ticket}")
                    else:
                        # Sweep closure
                        await self.mt5.close_position_by_symbol(leg.symbol)
                        logger.critical(f"    -> Swept Symbol: {leg.symbol}")
        
        logger.critical("[Watchdog] Book flattened. Severing the supervisor loop.")
        
        # Initiate a surgical OS level SIGTERM to shut the python thread completely via standard system protocols.
        # Ensure that external managers like PM2 or Systemd can restart it automatically.
        os.kill(os.getpid(), signal.SIGTERM)
