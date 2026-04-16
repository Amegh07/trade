import logging
import asyncio
from typing import Dict, Optional
from omega_system.core.types import BasketState, LegState, OrderStatus

logger = logging.getLogger("StateEngine")

class StateEngine:
    def __init__(self):
        # THE SINGLE SOURCE OF TRUTH
        # Dictionary mapping basket_id -> BasketState
        from omega_system.db.state_repo import StateRepository
        self._active_positions: Dict[str, BasketState] = {}
        self._lock = asyncio.Lock()  # Ensures async-safe updates
        import time
        self._last_tick_time = time.time()
        self.repo = StateRepository()

    async def boot_restore(self):
        """Called by the main supervisor block upon boot to recover crashed states"""
        recovered = await self.repo.load_snapshot()
        self._active_positions = recovered
        logger.info(f"[StateEngine] Central memory reconstituted ({len(recovered)} positions imported).")

    async def update_tick_time(self) -> None:
        import time
        self._last_tick_time = time.time()

    async def get_last_tick_time(self) -> float:
        return self._last_tick_time

    async def register_pending_basket(self, basket_id: str, sym_a: str, lot_a: float, sym_b: str, lot_b: float, z_score: float, regime: str = "UNKNOWN") -> bool:
        """Reserves the state BEFORE execution begins."""
        async with self._lock:
            if basket_id in self._active_positions:
                logger.error(f"[State] Basket {basket_id} already exists. Rejecting registration.")
                return False
            
            self._active_positions[basket_id] = BasketState(
                basket_id=basket_id,
                status=OrderStatus.PENDING,
                entry_time=time.time(), # Initially set as memory registration time
                z_score_entry=z_score,
                regime=regime,
                legs={
                    "PRIMARY": LegState(symbol=sym_a, role="PRIMARY", target_lot=lot_a),
                    "HEDGE": LegState(symbol=sym_b, role="HEDGE", target_lot=lot_b)
                }
            )
            logger.info(f"[State] Registered PENDING basket: {basket_id}")
            return True

    async def update_leg_fill(self, basket_id: str, role: str, ticket: int, filled_lot: float) -> None:
        """Atomic update for a specific leg's execution result."""
        async with self._lock:
            basket = self._active_positions.get(basket_id)
            if not basket:
                logger.error(f"[State] Cannot update leg for unknown basket {basket_id}")
                return

            leg = basket.legs.get(role)
            if leg:
                leg.ticket = ticket
                leg.filled_lot = filled_lot
                leg.status = OrderStatus.OPEN
                logger.debug(f"[State] Updated {role} leg for {basket_id} (Ticket: {ticket})")
                asyncio.create_task(self.repo.save_snapshot(self._active_positions.copy()))

    async def transition_basket_state(self, basket_id: str, new_status: OrderStatus, error_msg: str = None) -> None:
        """Moves the whole basket state (e.g., PENDING -> OPEN, or OPEN -> FAILED)."""
        async with self._lock:
            basket = self._active_positions.get(basket_id)
            if not basket:
                return

            basket.status = new_status
            if error_msg:
                basket.error_msg = error_msg
            
            import time
            if new_status == OrderStatus.OPEN:
                basket.entry_time = time.time()

            logger.info(f"[State] Basket {basket_id} transitioned to {new_status.name}")

            # Memory cleanup: If closed or failed and fully processed, we can pop it
            # (In a real system, you might move this to a 'history' dict instead of deleting)
            if new_status in [OrderStatus.CLOSED, OrderStatus.FAILED]:
                logger.info(f"[State] Archiving basket {basket_id} from active memory.")
                self._active_positions.pop(basket_id, None)

            # Mirror exact state mapping dynamically post-transaction 
            asyncio.create_task(self.repo.save_snapshot(self._active_positions.copy()))

    async def get_snapshot(self) -> Dict[str, BasketState]:
        """Returns a read-only copy of the current truth for other engines to read."""
        async with self._lock:
            # Shallow copy of the dict to prevent iteration errors during reads
            return self._active_positions.copy()

    async def _garbage_collector_loop(self):
        """Asynchronous system cleaner isolating stuck MT5 trades."""
        import time
        from omega_system.core.types import OrderStatus
        
        logger.info("[StateEngine API] Memory GC Pipeline Armed (120s PENDING limit).")
        while True:
            await asyncio.sleep(60.0)
            async with self._lock:
                current = time.time()
                for b_id, basket in list(self._active_positions.items()):
                    if basket.status == OrderStatus.PENDING:
                        dur = current - basket.entry_time
                        if dur > 120.0:
                            logger.critical(f"[State GC] Basket {b_id} orphaned for {dur:.1f}s. Trashing state.")
                            basket.status = OrderStatus.FAILED
                            basket.error_msg = "Timeout in PENDING state"
                            self._active_positions.pop(b_id, None)

state_engine = StateEngine()
