import asyncio
import logging
from omega_system.core.types import Signal, OrderStatus
from omega_system.engines.state_engine import StateEngine
from omega_system.core.message_bus import MessageBus
from omega_system.adapters.mt5_client import MT5Client  # Your raw MT5 wrapper

logger = logging.getLogger("ExecutionEngine")

class ExecutionEngine:
    def __init__(self, state_engine: StateEngine, message_bus: MessageBus, mt5_client: MT5Client):
        self.state = state_engine
        self.bus = message_bus
        self.mt5 = mt5_client

    async def start_worker(self):
        """Continuously listens for approved trades."""
        logger.info("[ExecutionEngine] Armed and listening for approved signals...")
        while True:
            signal: Signal = await self.bus.approved_signal_queue.get()
            await self._execute_atomic_basket(signal)
            self.bus.approved_signal_queue.task_done()

    async def _execute_atomic_basket(self, sig: Signal):
        """The core atomic sequence: Register -> Leg A -> Leg B -> Rollback on failure."""
        b_id = sig.basket_id
        
        # STEP 1: Reserve state as PENDING (Failsafe)
        registered = await self.state.register_pending_basket(
            b_id, sig.asset_a, 1.0, sig.asset_b, sig.beta, sig.confidence
        )
        if not registered:
            return # State engine rejected it (e.g., already exists)

        try:
            # STEP 2: Execute PRIMARY Leg A
            logger.info(f"[{b_id}] Executing Leg A: {sig.asset_a}")
            leg_a_ticket = await self.mt5.market_order(sig.asset_a, "ENTER", 1.0)
            
            if not leg_a_ticket:
                logger.error(f"[{b_id}] Leg A FAILED. Aborting basket.")
                await self.state.transition_basket_state(b_id, OrderStatus.FAILED, "Leg A rejected by broker")
                return
            
            await self.state.update_leg_fill(b_id, "PRIMARY", leg_a_ticket, 1.0)

            # STEP 3: Execute HEDGE Leg B
            logger.info(f"[{b_id}] Executing Leg B: {sig.asset_b}")
            leg_b_ticket = await self.mt5.market_order(sig.asset_b, "ENTER", sig.beta)

            if not leg_b_ticket:
                logger.critical(f"[{b_id}] Leg B FAILED. Initiating ATOMIC ROLLBACK on Leg A.")
                # ROLLBACK LEG A
                rollback_success = await self.mt5.close_position_by_ticket(sig.asset_a, leg_a_ticket)
                if not rollback_success:
                    logger.error(f"[{b_id}] ⚠️ ROLLBACK FAILED! Unhedged exposure exists on {sig.asset_a}!")
                
                await self.state.transition_basket_state(b_id, OrderStatus.FAILED, "Leg B failed, Leg A rolled back")
                return

            await self.state.update_leg_fill(b_id, "HEDGE", leg_b_ticket, sig.beta)

            # STEP 4: Both legs succeeded. Mark OPEN.
            await self.state.transition_basket_state(b_id, OrderStatus.OPEN)
            logger.info(f"[{b_id}] Basket execution complete. Fully hedged.")

        except Exception as e:
            logger.critical(f"[{b_id}] Fatal execution exception: {e}")
            await self.state.transition_basket_state(b_id, OrderStatus.FAILED, str(e))
