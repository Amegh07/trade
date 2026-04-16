import asyncio
import logging
import random
import MetaTrader5 as mt5
from typing import Tuple, List
from omega_system.core.types import Signal, OrderStatus
from omega_system.engines.state_engine import StateEngine
from omega_system.core.message_bus import MessageBus
from omega_system.adapters.mt5_client import MT5Client
from omega_system.config.settings import settings

logger = logging.getLogger("ExecutionEngine")

class ExecutionEngine:
    def __init__(self, state_engine: StateEngine, message_bus: MessageBus, mt5_client: MT5Client):
        self.state = state_engine
        self.bus = message_bus
        self.mt5 = mt5_client
        self.spread_threshold = settings.MAX_SPREAD_POINTS
        self.vwap_threshold = settings.VWAP_LOT_THRESHOLD

    async def start_worker(self):
        """Continuously listens for approved trades."""
        logger.info("[ExecutionEngine] Smart VWAP Router Armed and listening...")
        while True:
            signal: Signal = await self.bus.approved_signal_queue.get()
            await self._execute_atomic_basket(signal)
            self.bus.approved_signal_queue.task_done()

    def _check_spread(self, symbol: str, dynamic_threshold: float) -> bool:
        """Fetches live spread in points, enforcing pre-trade liquidity gates."""
        tick = mt5.symbol_info_tick(symbol)
        info = mt5.symbol_info(symbol)
        if not tick or not info:
            logger.error(f"[SPREAD GUARD] Could not fetch tick/info for {symbol}")
            return False
            
        spread_points = (tick.ask - tick.bid) / info.point
        if spread_points > dynamic_threshold:
            logger.warning(f"[SPREAD GUARD] {symbol} spread {spread_points:.1f} exceeds max {dynamic_threshold:.1f} pts.")
            return False
            
        return True

    async def _execute_smart_order(self, symbol: str, action: str, total_lot: float) -> Tuple[float, List[int], float]:
        """
        Executes a trade using VWAP Slicing logic.
        Returns (average_fill_price, list_of_tickets, total_filled_lot).
        """
        slices = []
        if total_lot <= self.vwap_threshold:
            slices.append(total_lot)
        else:
            remaining = total_lot
            while remaining >= 0.01:
                chunk = round(min(1.0, remaining), 2)
                slices.append(chunk)
                remaining -= chunk

        tickets = []
        filled_lot = 0.0

        for idx, chunk in enumerate(slices):
            logger.info(f"[_execute_smart_order] {symbol} Route {idx+1}/{len(slices)} -> {chunk} lots")
            ticket = await self.mt5.market_order(symbol, action, chunk)
            
            if not ticket:
                logger.error(f"[_execute_smart_order] {symbol} slice {idx+1} FAILED. Liquidity dried.")
                break
                
            tickets.append(ticket)
            filled_lot += chunk
            
            if idx < len(slices) - 1:
                # Absorb liquidity dynamically
                await asyncio.sleep(random.uniform(0.1, 0.5))
                
        # Pseudo average fill calculation (A real implementation would log actual Deal prices natively)
        avg_fill = 1.0 
        return avg_fill, tickets, filled_lot

    async def _rollback_tickets(self, symbol: str, tickets: List[int]) -> bool:
        """Helper to cleanly close out a list of partially filled VWAP slice tickets."""
        success = True
        for tk in tickets:
            res = await self.mt5.close_position_by_ticket(symbol, tk)
            if not res:
                success = False
        return success

    async def _execute_atomic_basket(self, sig: Signal):
        """The core atomic sequence: Pre-Check -> Register -> Leg A -> Leg B -> Rollback."""
        b_id = sig.basket_id
        
        # --- L2 REGIME DRIVEN SLICING ---
        current_spread_thresh = float(self.spread_threshold)
        current_target_lot = sig.target_lot
        
        if sig.regime == "VOLATILE":
            current_target_lot = round(current_target_lot * 0.3, 2)
            current_spread_thresh *= 0.8
            logger.info(f"[{b_id}] Splitting size to {current_target_lot} and tightening spread to {current_spread_thresh:.1f} (CHAOS).")
        elif sig.regime == "DEAD":
            logger.warning(f"[{b_id}] Execution blocked: Regime is DEAD. Slippage risk too high.")
            return
        elif sig.regime == "TRENDING":
            logger.warning(f"[{b_id}] Execution blocked: Mean Reversion strategy blocked in TRENDING regimes.")
            return

        if current_target_lot < 0.01:
            logger.warning(f"[{b_id}] Sliced target lot below broker minimum. Aborting.")
            return
            
        # PRE-TRADE SPREAD CHECK
        spread_a_ok = await asyncio.to_thread(self._check_spread, sig.asset_a, current_spread_thresh)
        spread_b_ok = await asyncio.to_thread(self._check_spread, sig.asset_b, current_spread_thresh)
        
        if not spread_a_ok or not spread_b_ok:
            logger.warning(f"[{b_id}] Spread check failed safely. Aborting execution.")
            return
        
        # STEP 1: Reserve state as PENDING (Failsafe)
        registered = await self.state.register_pending_basket(
            b_id, sig.asset_a, current_target_lot, sig.asset_b, current_target_lot * sig.beta, sig.confidence, sig.regime
        )
        if not registered:
            return 

        try:
            # STEP 2: Execute PRIMARY Leg A
            logger.info(f"[{b_id}] Executing Leg A: {sig.asset_a} ({current_target_lot} Lots)")
            avg_price_a, leg_a_tickets, filled_a = await self._execute_smart_order(sig.asset_a, "ENTER", current_target_lot)
            
            # Partial Fill Resolution
            if filled_a < current_target_lot:
                logger.error(f"[{b_id}] Leg A PARTIAL FILL ({filled_a}/{current_target_lot}). Initiating immediate rollback.")
                if leg_a_tickets:
                    await self._rollback_tickets(sig.asset_a, leg_a_tickets)
                await self.state.transition_basket_state(b_id, OrderStatus.FAILED, "Leg A Partial Fill Rollback")
                return
            
            # Store master ticket
            await self.state.update_leg_fill(b_id, "PRIMARY", leg_a_tickets[0], filled_a) 

            # STEP 3: Execute HEDGE Leg B
            target_b = round(sig.target_lot * sig.beta, 2)
            logger.info(f"[{b_id}] Executing Leg B: {sig.asset_b} ({target_b} Lots)")
            avg_price_b, leg_b_tickets, filled_b = await self._execute_smart_order(sig.asset_b, "ENTER", target_b)

            if filled_b < target_b:
                logger.critical(f"[{b_id}] Leg B FAILED or PARTIAL ({filled_b}/{target_b}). Initiating ATOMIC ROLLBACK.")
                
                # ROLLBACK LEG B PARTIALS
                if leg_b_tickets:
                    await self._rollback_tickets(sig.asset_b, leg_b_tickets)
                    
                # ROLLBACK LEG A
                rollback_success = await self._rollback_tickets(sig.asset_a, leg_a_tickets)
                if not rollback_success:
                    logger.error(f"[{b_id}] ⚠️ FATAL ROLLBACK FAILURE! Unhedged exposure exists on {sig.asset_a}!")
                
                await self.state.transition_basket_state(b_id, OrderStatus.FAILED, "Leg B failed, basket rolled back")
                return

            await self.state.update_leg_fill(b_id, "HEDGE", leg_b_tickets[0], filled_b)

            # STEP 4: Both legs succeeded. Mark OPEN.
            await self.state.transition_basket_state(b_id, OrderStatus.OPEN)
            logger.info(f"[{b_id}] Basket execution complete. Fully hedged via Smart Routing.")

        except Exception as e:
            logger.critical(f"[{b_id}] Fatal execution exception: {e}")
            await self.state.transition_basket_state(b_id, OrderStatus.FAILED, str(e))
