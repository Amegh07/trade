import logging
import asyncio
from typing import Dict, Optional
from omega_system.core.types import BasketState, LegState, OrderStatus

logger = logging.getLogger("StateEngine")

class StateEngine:
    def __init__(self):
        # THE SINGLE SOURCE OF TRUTH
        # Dictionary mapping basket_id -> BasketState
        self._active_positions: Dict[str, BasketState] = {}
        self._lock = asyncio.Lock()  # Ensures async-safe updates

    async def register_pending_basket(self, basket_id: str, sym_a: str, lot_a: float, sym_b: str, lot_b: float, z_score: float) -> bool:
        """Reserves the state BEFORE execution begins."""
        async with self._lock:
            if basket_id in self._active_positions:
                logger.error(f"[State] Basket {basket_id} already exists. Rejecting registration.")
                return False
            
            self._active_positions[basket_id] = BasketState(
                basket_id=basket_id,
                status=OrderStatus.PENDING,
                entry_time=0.0, # Will be set when OPEN
                z_score_entry=z_score,
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

    async def get_snapshot(self) -> Dict[str, BasketState]:
        """Returns a read-only copy of the current truth for other engines to read."""
        async with self._lock:
            # Shallow copy of the dict to prevent iteration errors during reads
            return self._active_positions.copy()

state_engine = StateEngine()
