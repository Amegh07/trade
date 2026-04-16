import asyncio
import logging
from omega_system.core.types import Signal
from omega_system.core.message_bus import MessageBus
from omega_system.engines.state_engine import StateEngine

logger = logging.getLogger("RiskEngine")

class RiskEngine:
    def __init__(self, message_bus: MessageBus, state_engine: StateEngine):
        self.bus = message_bus
        self.state = state_engine
        
        # Failsafe Limits
        self.max_daily_loss = -2000.0  # Hard stop limit
        self.current_daily_pnl = 0.0   # Would be synced from DB on boot

    async def start_worker(self):
        """Continuously intercepts raw signals to vet them."""
        logger.info("[RiskEngine] Armed and guarding execution flow...")
        while True:
            signal: Signal = await self.bus.raw_signal_queue.get()
            await self._evaluate_signal(signal)
            self.bus.raw_signal_queue.task_done()

    async def _evaluate_signal(self, sig: Signal):
        b_id = sig.basket_id

        # RULE 1: Global Failsafe (Daily Loss Limit)
        if self.current_daily_pnl <= self.max_daily_loss:
            logger.critical(f"[{b_id}] BLOCKED: Max daily loss breached. System halted.")
            return

        # RULE 2: The Mathematical Edge (CRITICAL-5 FIX)
        # If the expected value (confidence/Kelly) is 0 or negative, DO NOT TRADE.
        if sig.confidence <= 0.0:
            logger.warning(f"[{b_id}] GHOST MODE: Negative edge detected (Kelly <= 0). Signal discarded.")
            return

        # RULE 3: Exposure Limits
        snapshot = await self.state.get_snapshot()
        if b_id in snapshot:
            logger.warning(f"[{b_id}] BLOCKED: Basket already active in State Engine.")
            return

        # If it survives the gauntlet, pass it to the Muscle.
        logger.info(f"[{b_id}] APPROVED: Signal passed all risk parameters.")
        await self.bus.publish_approved_trade(sig)
