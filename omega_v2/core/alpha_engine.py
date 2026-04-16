import asyncio
import logging
import time
import pandas as pd
import torch
import MetaTrader5 as mt5
from chronos import BaseChronosPipeline
from utils.risk_gates import risk_gates

logger = logging.getLogger("AlphaEngine")

TRADABLE_PAIRS = [
    ("AUDUSD", "NZDUSD"),   # Oceanic Forex spread
    ("AUDJPY", "NZDJPY"),   # Asian Session Engine
]

SIGNAL_THRESHOLD = 0.02   # 2% forecast divergence required to enter
EXIT_THRESHOLD   = 0.005  # 0.5% convergence triggers close
MAX_BASKET_AGE_H = 24.0   # Time stop: close after 24 hours

class Signal:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

class AlphaEngine:
    def __init__(self, signal_queue: asyncio.Queue):
        self.queue = signal_queue

        # C1 FIX: Position memory — tracks open baskets keyed by (asset_a, asset_b)
        # Value: {"basket_id": str, "entry_spread": float, "entry_time": float}
        self.active_pairs: dict = {}

        logger.info("Loading Chronos-Bolt-tiny model to GPU/CPU...")
        self.pipeline = BaseChronosPipeline.from_pretrained(
            "amazon/chronos-bolt-tiny",
            device_map="cuda" if torch.cuda.is_available() else "cpu",
            dtype=torch.bfloat16
        )
        logger.info("Model loaded successfully.")

    async def _sync_open_positions(self):
        """On startup, recover any baskets already open in MT5 to prevent re-entry."""
        loop = asyncio.get_running_loop()
        positions = await loop.run_in_executor(None, mt5.positions_get)
        if not positions:
            return

        recovered = 0
        for pos in positions:
            comment = pos.comment or ""
            if comment.startswith("BSKT_"):
                # Comment format: BSKT_AUDUSD_NZDUSD_1713303600
                parts = comment.split("_")
                if len(parts) >= 4:
                    asset_a = parts[1]
                    asset_b = parts[2]
                    pair_key = (asset_a, asset_b)
                    if pair_key not in self.active_pairs:
                        self.active_pairs[pair_key] = {
                            "basket_id": comment,
                            "entry_spread": 0.0,   # Unknown at recovery — use 0 so exit relies on convergence only
                            "entry_time": float(pos.time)
                        }
                        recovered += 1

        if recovered:
            logger.info(f"[Recovery] Recovered {recovered} active baskets from MT5 on startup.")

    async def run(self):
        """Main async loop for generating signals."""
        await self._sync_open_positions()

        while True:
            if risk_gates.is_rollover_window():
                logger.warning("Rollover Window Active. Alpha Engine pausing for 60s...")
                await asyncio.sleep(60)
                continue

            for asset_a, asset_b in TRADABLE_PAIRS:
                await self._process_pair(asset_a, asset_b)

            await asyncio.sleep(60)

    async def _process_pair(self, asset_a: str, asset_b: str):
        loop = asyncio.get_running_loop()  # H2 FIX: use get_running_loop()

        rates_a = await loop.run_in_executor(None, mt5.copy_rates_from_pos, asset_a, mt5.TIMEFRAME_M5, 0, 512)
        rates_b = await loop.run_in_executor(None, mt5.copy_rates_from_pos, asset_b, mt5.TIMEFRAME_M5, 0, 512)

        if rates_a is None or rates_b is None or len(rates_a) < 512 or len(rates_b) < 512:
            return

        # H3 FIX: Align data on shared timestamps before computing spread
        df_a = pd.DataFrame({'time': rates_a['time'], 'a': rates_a['close']})
        df_b = pd.DataFrame({'time': rates_b['time'], 'b': rates_b['close']})
        df = pd.merge(df_a, df_b, on='time', how='inner')
        if len(df) < 480:
            logger.debug(f"[{asset_a}/{asset_b}] Insufficient aligned candles ({len(df)}). Skipping.")
            return

        spread = df['a'] - df['b']
        current_spread = float(spread.iloc[-1])

        # Zero-Shot Forecast
        context = torch.tensor(spread.values, dtype=torch.float32)
        forecast = self.pipeline.predict(context, prediction_length=10)
        # forecast shape: (batch=1, quantiles, prediction_length) — take median quantile
        n_quantiles = forecast.shape[1]
        median_idx = n_quantiles // 2
        mean_forecast = forecast[0, median_idx].cpu().numpy()
        future_spread = float(mean_forecast[0])

        pair_key = (asset_a, asset_b)

        # ── C2 FIX: EXIT LOGIC ─────────────────────────────────────────────────
        if pair_key in self.active_pairs:
            basket = self.active_pairs[pair_key]
            age_hours = (time.time() - basket["entry_time"]) / 3600.0
            spread_delta = abs(future_spread - current_spread)
            spread_converged = spread_delta < abs(current_spread * EXIT_THRESHOLD)
            time_stop = age_hours >= MAX_BASKET_AGE_H

            if spread_converged or time_stop:
                reason = "TIME_STOP" if time_stop else "MEAN_REVERSION"
                logger.info(f"[{asset_a}/{asset_b}] EXIT signal — reason: {reason} | age: {age_hours:.1f}h")
                sig_a = Signal(symbol=asset_a, action="CLOSE", basket_id=basket["basket_id"], role="PRIMARY")
                sig_b = Signal(symbol=asset_b, action="CLOSE", basket_id=basket["basket_id"], role="HEDGE")
                await self.queue.put(sig_a)
                await self.queue.put(sig_b)
                del self.active_pairs[pair_key]
            return  # Don't attempt entry while a basket is open

        # ── ENTRY LOGIC ────────────────────────────────────────────────────────
        # Check Risk Gates
        info_a = mt5.symbol_info(asset_a)
        if info_a and risk_gates.is_spread_blown(asset_a, info_a.spread):
            logger.warning(f"Spread blown on {asset_a}. Vetoing signal.")
            return

        action_a = action_b = None
        if current_spread != 0:
            divergence = (future_spread - current_spread) / abs(current_spread)
            if divergence > SIGNAL_THRESHOLD:
                action_a, action_b = "ENTER_SHORT", "ENTER_LONG"
            elif divergence < -SIGNAL_THRESHOLD:
                action_a, action_b = "ENTER_LONG", "ENTER_SHORT"

        if action_a:
            b_id = f"BSKT_{asset_a}_{asset_b}_{int(time.time())}"
            sig_a = Signal(symbol=asset_a, action=action_a, basket_id=b_id, role="PRIMARY")
            sig_b = Signal(symbol=asset_b, action=action_b, basket_id=b_id, role="HEDGE")
            await self.queue.put(sig_a)
            await self.queue.put(sig_b)

            # C1 FIX: Register basket so we never re-enter
            self.active_pairs[pair_key] = {
                "basket_id": b_id,
                "entry_spread": current_spread,
                "entry_time": time.time()
            }
            logger.info(f"AI Signal Generated: {b_id} | {action_a} {asset_a} & {action_b} {asset_b}")
