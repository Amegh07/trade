import asyncio
import functools
import logging
import time
import pandas as pd
import torch
import MetaTrader5 as mt5
from chronos import BaseChronosPipeline
from utils.risk_gates import risk_gates

logger = logging.getLogger("AlphaEngine")

MASTER_PAIRS = [
    ("EURUSD", "GBPUSD"),  # London/NY Core
    ("AUDUSD", "NZDUSD"),  # Pacific Core
    ("AUDJPY", "NZDJPY"),  # Asian Core
    ("EURJPY", "GBPJPY"),  # High Volatility Cross
    ("USDCAD", "AUDUSD"),  # Commodity Cross
]

# SIGNAL_THRESHOLD: 0.1% forecast divergence required to enter.
#
# Chronos-Bolt is a zero-shot probabilistic forecaster for stationary spreads.
# Its median quantile output typically moves 0.05–0.3% per M5 bar relative to
# current value. The previous threshold of 2% was structurally unreachable —
# this caused zero signals across all observed runtime.
SIGNAL_THRESHOLD = 0.001  # 0.1% forecast divergence required to enter
EXIT_THRESHOLD   = 0.25   # 25% reversion from entry spread triggers close
MAX_BASKET_AGE_H = 24.0   # Time stop: close after 24 hours regardless


class Signal:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class AlphaEngine:
    def __init__(self, signal_queue: asyncio.Queue):
        self.queue = signal_queue

        # Position memory — tracks open baskets keyed by (asset_a, asset_b)
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
                            "entry_spread": 0.0,   # Unknown at recovery — time-stop only
                            "entry_time": float(pos.time)
                        }
                        recovered += 1

        if recovered:
            logger.info(f"[Recovery] Recovered {recovered} active baskets from MT5 on startup.")

    async def run(self):
        """Main async loop for generating signals."""
        await self._sync_open_positions()
        cycle = 0

        while True:
            if risk_gates.is_rollover_window():
                logger.warning("Rollover Window Active. Alpha Engine pausing for 60s...")
                await asyncio.sleep(60)
                continue

            cycle += 1
            logger.info(f"[Cycle {cycle}] Evaluating {len(MASTER_PAIRS)} pair(s)...")

            # SEQUENTIAL evaluation — MT5's Python binding uses a COM object that is
            # NOT safe for concurrent cross-thread calls. Running pairs with asyncio.gather
            # would submit multiple MT5 calls simultaneously from different thread-pool
            # workers, risking a crash. Sequential evaluation is safe and correct.
            for asset_a, asset_b in MASTER_PAIRS:
                await self._process_pair(asset_a, asset_b)

            logger.info(f"[Cycle {cycle}] Complete. Sleeping 60s.")
            await asyncio.sleep(60)

    async def _process_pair(self, asset_a: str, asset_b: str):
        """Wrapper that catches and logs all exceptions from pair evaluation."""
        loop = asyncio.get_running_loop()
        try:
            await self._evaluate_pair(asset_a, asset_b, loop)
        except Exception as e:
            logger.exception(f"[{asset_a}/{asset_b}] Unhandled exception in pair evaluation: {e}")

    async def _evaluate_pair(self, asset_a: str, asset_b: str, loop):
        """Core pair evaluation — rate fetch, forecast, entry/exit signal emission."""
        rates_a = await loop.run_in_executor(None, mt5.copy_rates_from_pos, asset_a, mt5.TIMEFRAME_M5, 0, 512)
        rates_b = await loop.run_in_executor(None, mt5.copy_rates_from_pos, asset_b, mt5.TIMEFRAME_M5, 0, 512)

        if rates_a is None or rates_b is None or len(rates_a) < 512 or len(rates_b) < 512:
            logger.warning(
                f"[{asset_a}/{asset_b}] Insufficient rate data "
                f"(a={len(rates_a) if rates_a is not None else 'None'}, "
                f"b={len(rates_b) if rates_b is not None else 'None'}). Skipping."
            )
            return

        # Align data on shared timestamps before computing spread
        df_a = pd.DataFrame({'time': rates_a['time'], 'a': rates_a['close']})
        df_b = pd.DataFrame({'time': rates_b['time'], 'b': rates_b['close']})
        df = pd.merge(df_a, df_b, on='time', how='inner')
        if len(df) < 480:
            logger.warning(f"[{asset_a}/{asset_b}] Insufficient aligned candles ({len(df)}). Skipping.")
            return

        spread = df['a'] - df['b']
        current_spread = float(spread.iloc[-1])

        # Zero-Shot Forecast.
        # Wrapped in run_in_executor: pipeline.predict() is a blocking PyTorch call
        # (~15–60s on CPU). Running it on the event loop would freeze all async
        # scheduling, preventing signal consumption and SIGINT handling.
        # functools.partial is required because run_in_executor cannot pass keyword args.
        context = torch.tensor(spread.values, dtype=torch.float32)
        predict_fn = functools.partial(self.pipeline.predict, context, prediction_length=10)
        forecast = await loop.run_in_executor(None, predict_fn)
        # forecast shape: (batch=1, quantiles, prediction_length) — take median quantile
        n_quantiles = forecast.shape[1]
        median_idx = n_quantiles // 2
        mean_forecast = forecast[0, median_idx].cpu().numpy()
        future_spread = float(mean_forecast[0])

        pair_key = (asset_a, asset_b)

        # ── EXIT LOGIC ─────────────────────────────────────────────────────────
        if pair_key in self.active_pairs:
            basket = self.active_pairs[pair_key]
            age_hours = (time.time() - basket["entry_time"]) / 3600.0
            time_stop = age_hours >= MAX_BASKET_AGE_H

            # Correct mean-reversion exit: has spread reverted toward zero from entry?
            # predicted_reversion = fraction of entry spread magnitude the model predicts
            # will have unwound.  Positive = converging.  Exit when ≥ EXIT_THRESHOLD.
            # Recovery case (entry_spread=0.0): entry unknown — rely on time-stop only.
            entry_spread = basket["entry_spread"]
            if abs(entry_spread) > 1e-9:
                predicted_reversion = (abs(entry_spread) - abs(future_spread)) / abs(entry_spread)
                spread_converged = predicted_reversion >= EXIT_THRESHOLD
            else:
                spread_converged = False

            logger.info(
                f"[{asset_a}/{asset_b}] BASKET OPEN | age={age_hours:.1f}h | "
                f"entry={entry_spread:.5f} | current={current_spread:.5f} | "
                f"future={future_spread:.5f} | "
                f"reversion={'N/A' if abs(entry_spread) <= 1e-9 else f'{predicted_reversion:.1%}'} | "
                f"time_stop={time_stop}"
            )

            if spread_converged or time_stop:
                reason = "TIME_STOP" if time_stop else "MEAN_REVERSION"
                logger.info(
                    f"[{asset_a}/{asset_b}] EXIT → {reason} | "
                    f"age={age_hours:.1f}h | entry={entry_spread:.5f} | future={future_spread:.5f}"
                )
                sig_a = Signal(symbol=asset_a, action="CLOSE", basket_id=basket["basket_id"], role="PRIMARY")
                sig_b = Signal(symbol=asset_b, action="CLOSE", basket_id=basket["basket_id"], role="HEDGE")
                await self.queue.put(sig_a)
                await self.queue.put(sig_b)
                del self.active_pairs[pair_key]
            return  # Never attempt fresh entry while an existing basket is open

        # ── ENTRY LOGIC ────────────────────────────────────────────────────────
        # symbol_info awaited via run_in_executor; both legs checked for spread gates
        info_a = await loop.run_in_executor(None, mt5.symbol_info, asset_a)
        info_b = await loop.run_in_executor(None, mt5.symbol_info, asset_b)

        if info_a and risk_gates.is_spread_blown(asset_a, info_a.spread):
            logger.warning(f"[{asset_a}/{asset_b}] Variance spread blown on {asset_a}. Vetoing.")
            return
        if info_b and risk_gates.is_spread_blown(asset_b, info_b.spread):
            logger.warning(f"[{asset_a}/{asset_b}] Variance spread blown on {asset_b}. Vetoing.")
            return
        if info_a and risk_gates.is_spread_absolute_blown(asset_a, info_a.spread):
            logger.warning(f"[{asset_a}/{asset_b}] Absolute spread limit exceeded on {asset_a}. Vetoing.")
            return
        if info_b and risk_gates.is_spread_absolute_blown(asset_b, info_b.spread):
            logger.warning(f"[{asset_a}/{asset_b}] Absolute spread limit exceeded on {asset_b}. Vetoing.")
            return

        action_a = action_b = None
        divergence = 0.0
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

            # Register basket immediately to prevent re-entry on next tick
            self.active_pairs[pair_key] = {
                "basket_id": b_id,
                "entry_spread": current_spread,
                "entry_time": time.time()
            }
            logger.info(
                f"🚀 [{asset_a}/{asset_b}] SIGNAL → {action_a}/{action_b} | "
                f"basket={b_id} | spread={current_spread:.5f} | divergence={divergence:.4%}"
            )
        else:
            # Every evaluation now logged at INFO so the engine is never silent
            logger.info(
                f"[{asset_a}/{asset_b}] No signal | "
                f"current={current_spread:.5f} | future={future_spread:.5f} | "
                f"divergence={divergence:.4%} (threshold ±{SIGNAL_THRESHOLD:.1%})"
            )
