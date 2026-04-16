import asyncio
import logging
from omega_system.engines.state_engine import StateEngine
from omega_system.adapters.mt5_client import MT5Client
from omega_system.core.types import OrderStatus

logger = logging.getLogger("ReconEngine")

class ReconciliationEngine:
    def __init__(self, state_engine: StateEngine, mt5_client: MT5Client):
        self.state = state_engine
        self.mt5 = mt5_client

    async def start_worker(self):
        """Runs in the background, auditing the system every 5 seconds."""
        logger.info("[ReconEngine] Self-healing loop active. Scanning every 5s...")
        while True:
            await asyncio.sleep(5.0)
            await self._reconcile()

    async def _reconcile(self):
        # 1. Get what Python THINKS is happening
        internal_state = await self.state.get_snapshot()
        
        # 2. Get what the Broker KNOWS is happening
        live_positions = await self.mt5.get_all_active_symbols() # Assume this returns a list of symbols like ['AUDUSD', 'NZDUSD']
        
        for b_id, basket in internal_state.items():
            if basket.status == OrderStatus.OPEN:
                leg_a = basket.legs["PRIMARY"].symbol
                leg_b = basket.legs["HEDGE"].symbol
                
                has_a = leg_a in live_positions
                has_b = leg_b in live_positions

                # SCENARIO 1: Half-open trade (Catastrophic Risk)
                if has_a and not has_b:
                    logger.critical(f"[RECON] DESYNC: {b_id} missing HEDGE leg ({leg_b})! Emergency Close {leg_a}!")
                    await self.mt5.close_position_by_symbol(leg_a)
                    await self.state.transition_basket_state(b_id, OrderStatus.FAILED, "Recon: Missing Hedge Leg")
                
                elif has_b and not has_a:
                    logger.critical(f"[RECON] DESYNC: {b_id} missing PRIMARY leg ({leg_a})! Emergency Close {leg_b}!")
                    await self.mt5.close_position_by_symbol(leg_b)
                    await self.state.transition_basket_state(b_id, OrderStatus.FAILED, "Recon: Missing Primary Leg")
                
                # SCENARIO 2: Both missing (Broker hit Take-Profit / Stop-Loss)
                elif not has_a and not has_b:
                    logger.info(f"[RECON] Basket {b_id} closed at broker level. Updating internal state.")
                    await self.state.transition_basket_state(b_id, OrderStatus.CLOSED)
