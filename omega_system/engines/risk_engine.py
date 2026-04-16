import asyncio
import logging
from omega_system.core.types import Signal
from omega_system.core.message_bus import MessageBus
from omega_system.engines.state_engine import StateEngine

logger = logging.getLogger("RiskEngine")

class RiskEngine:
    def __init__(self, message_bus: MessageBus, state_engine: StateEngine):
        from omega_system.config.settings import settings
        self.bus = message_bus
        self.state = state_engine
        
        # Failsafe Limits driven by universe configuration
        self.max_daily_loss = settings.MAX_DAILY_LOSS_USD  # Centralized hard stop
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

        # RULE 2: The Mathematical Edge (CRITICAL-5 FIX) + L2 Regime Overrides
        REGIME_MULTIPLIER = {
            "RANGING": 1.0,  # Full StatArb resonance
            "TRENDING": 0.0, # StatArb fails in pure trends, hard veto
            "VOLATILE": 0.3, # Chop zone whip-saw risk, slash sizing by 70%
            "DEAD": 0.0,     # Slippage hazard inside flat variance, hard veto
            "UNKNOWN": 0.0
        }
        
        reg_mult = REGIME_MULTIPLIER.get(sig.regime, 0.0)
        final_risk = sig.confidence * reg_mult

        if final_risk <= 0.0:
            logger.warning(f"[{b_id}] BLOCKED: Regime is {sig.regime}. Signal discarded.")
            return

        # Sizing algorithm: Convert expected edge array into fractional Lot output limits
        # Here we translate the static 1.0 lot constraint to conform to the regime multiplier
        sig.target_lot = max(0.01, round(1.0 * reg_mult, 2))
        
        # RULE 3: Exposure Limits
        snapshot = await self.state.get_snapshot()
        if b_id in snapshot:
            logger.warning(f"[{b_id}] BLOCKED: Basket already active in State Engine.")
            return

        # If it survives the gauntlet, pass it to the Muscle.
        logger.info(f"[{b_id}] APPROVED: Signal passed all risk parameters.")
        await self.bus.publish_approved_trade(sig)
