import asyncio
import logging
import numpy as np
import MetaTrader5 as mt5
from datetime import datetime
from omega_system.core.types import Signal, RegimeMultiplierUpdate
from omega_system.core.message_bus import MessageBus
from omega_system.engines.state_engine import StateEngine

logger = logging.getLogger("RiskEngine")

class RiskEngine:
    def __init__(self, message_bus: MessageBus, state_engine: StateEngine):
        from omega_system.config.settings import settings
        self.bus = message_bus
        self.state = state_engine
        
        # Failsafe Limits driven by universe configuration
        self.max_daily_loss = settings.MAX_DAILY_LOSS_USD
        self.current_daily_pnl = 0.0   

        # AI-Driven Dynamic Multipliers
        self.regime_multiplier = {
            "TRENDING": 1.25,  # Aggressive directional mapping
            "RANGING": 1.0,    # Standard operating baseline
            "VOLATILE": 0.35,  # Defensive spread widening compensation
            "DEAD": 0.0,       # Complete Kill-switch
            "UNKNOWN": 0.0
        }
        
        # Base exposure allocation (Standard lot definition per execution sequence)
        self.base_lot = 1.0
        
        # Baseline target Volatility (0.01% standard deviation norm acting as the scalar center point)
        self.target_volatility = 0.0001

    async def listen_for_multipliers(self):
        while True:
            update: RegimeMultiplierUpdate = await self.bus.multiplier_update_queue.get()
            self.regime_multiplier[update.regime] = update.multiplier
            logger.warning(f"[RiskEngine] Shadow AI mapped new EV parameter: {update.regime} -> {update.multiplier:.2f}x")
            self.bus.multiplier_update_queue.task_done()

    async def start_worker(self):
        """Continuously intercepts raw signals to vet them."""
        logger.info("[RiskEngine] Armed and guarding Volatility Adjusted execution flow...")
        asyncio.create_task(self.listen_for_multipliers())
        
        while True:
            signal: Signal = await self.bus.raw_signal_queue.get()
            await self._evaluate_signal(signal)
            self.bus.raw_signal_queue.task_done()
            
    async def _get_realized_volatility(self, symbol: str) -> float:
        """Physical tick block fetching via MT5 terminal executing purely isolated from main loops."""
        loop = asyncio.get_event_loop()
        
        # Extrapolating 50 deep raw tick arrays manually
        ticks = await loop.run_in_executor(None, lambda: mt5.copy_ticks_from(symbol, datetime.now(), 50, mt5.COPY_TICKS_ALL))
        
        if ticks is None or len(ticks) < 10:
            return self.target_volatility
            
        mids = (ticks['bid'] + ticks['ask']) / 2.0
        returns = np.diff(mids) / mids[:-1]
        vol = np.std(returns)
        
        return float(vol) if vol > 0.0 else self.target_volatility

    async def _margin_safety_gate(self, symbol: str, final_lot: float) -> bool:
        """Isolated evaluation resolving strict true limits checking 1.5x buffer capacity natively."""
        loop = asyncio.get_event_loop()
        acc = await loop.run_in_executor(None, mt5.account_info)
        
        if not acc: 
            return False
            
        # Simulating required order calculating actual symbol parameters
        required_margin = await loop.run_in_executor(
            None, 
            mt5.order_calc_margin, 
            mt5.ORDER_TYPE_BUY, 
            symbol, 
            final_lot, 
            acc.margin_free
        )
        
        if required_margin is None:
            # Fallback estimation 
            required_margin = final_lot * 1000.0
            
        # Calculate strict physical capacity against the 1.5x buffer line
        limit = required_margin * 1.5
        
        if acc.margin_free < limit:
            logger.error(f"[RiskGuard] Margin depleted. Requires {limit:.2f} | Free: {acc.margin_free:.2f}")
            return False
            
        return True

    async def _evaluate_signal(self, sig: Signal):
        b_id = sig.basket_id

        # RULE 1: Global Failsafe (Daily Loss Limit)
        if self.current_daily_pnl <= self.max_daily_loss:
            logger.critical(f"[{b_id}] BLOCKED: Max daily loss breached. System halted.")
            return

        # RULE 2: L3 Override Regimes
        reg_mult = self.regime_multiplier.get(sig.regime, 0.0)
        
        if reg_mult <= 0.0:
            logger.warning(f"[{b_id}] BLOCKED: Regime Multiplier triggers Kill-Switch ({sig.regime}). Discarded.")
            return
            
        # RULE 3: Volatility Scale normalization
        realized_vol = await self._get_realized_volatility(sig.asset_a)
        raw_vol_scalar = self.target_volatility / realized_vol
        
        # Clamp bounds strictly establishing ceiling and floors manually
        vol_scalar = float(np.clip(raw_vol_scalar, 0.5, 1.5))
        
        # RULE 4: Dynamic Capital Allocation Output Mapping
        raw_lot = self.base_lot * reg_mult * vol_scalar
        final_lot = max(0.01, round(raw_lot, 2))
        
        sig.target_lot = final_lot
        
        logger.debug(f"[{b_id}] Sizing parameters - Regime: {reg_mult:.2f}x | Vol_Scalar: {vol_scalar:.2f}x | Final: {final_lot} lots")
        
        # RULE 5: 1.5x Margin Test (Terminal Check)
        margin_safe = await self._margin_safety_gate(sig.asset_a, final_lot)
        if not margin_safe:
            logger.warning(f"[{b_id}] BLOCKED: Margin capacity test failure mapped against {final_lot} lot demand.")
            return

        # RULE 6: Exposure Limits Mapping
        snapshot = await self.state.get_snapshot()
        if b_id in snapshot:
            logger.warning(f"[{b_id}] BLOCKED: Basket natively active in State Registry.")
            return

        # Passed Comprehensive Array Execution Layer
        logger.info(f"[{b_id}] APPROVED: Signal breached logic mapping. Exporting {final_lot} lots down pipeline.")
        await self.bus.publish_approved_trade(sig)
