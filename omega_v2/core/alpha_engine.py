"""
AlphaEngine — Institutional Analysis Pipeline

Signal generation pipeline (sequential, per-pair):

  1. Auto-Scanner        Force symbol into Market Watch; check SYMBOL_TRADE_MODE_FULL
  2. Rate Fetch          512 M5 OHLC candles, inner-join aligned
  3. Correlation Guard   Pearson >= MIN_CORRELATION (default 0.6)
  4. Regime Guard        Lag-1 autocorrelation of spread returns < 0 = RANGE
  5. ATR Calculation     14-period EWM ATR for dynamic position sizing
  6. Chronos Forecast    Zero-shot probabilistic spread forecast (next 10 M5 bars)
  7. Edge Pre-Computation Dual-leg round-trip cost vs. predicted divergence
  8. Groq Meta-Layer     LLaMA 3 70B regime sanity check (advisory only)
  9. Signal Emission     Enriched Signal -> queue -> ExecutionRouter

The core math (steps 1–7) is always authoritative.
Groq (step 8) is advisory: it can veto only when confidence >= GROQ_VETO_CONFIDENCE
AND the math edge_multiple < GROQ_VETO_OVERRIDE_MULTIPLE.
"""

import asyncio
import functools
import logging
import time
import numpy as np
import pandas as pd
import torch
import MetaTrader5 as mt5
from chronos import BaseChronosPipeline
from config import settings
from utils.risk_gates import risk_gates
from utils.groq_meta import build_client, classify_regime

logger = logging.getLogger("AlphaEngine")

MASTER_PAIRS = [
    ("EURUSD", "GBPUSD"),  # London/NY Core
    ("AUDUSD", "NZDUSD"),  # Pacific Core
    ("AUDJPY", "NZDJPY"),  # Asian Core
    ("EURJPY", "GBPJPY"),  # High Volatility Cross
    ("USDCAD", "AUDUSD"),  # Commodity Cross
    ("USDCHF", "EURCHF"),  # Safe Haven Cross
    ("AUDCAD", "NZDCAD"),  # Commodity Cross 2
]

# ── Entry thresholds ──────────────────────────────────────────────────────────
#
# SIGNAL_THRESHOLD is a NOISE FLOOR ONLY.
# The real entry gate is in ExecutionRouter:
#   actual entry requires: divergence >= MIN_COST_MULTIPLE x round-trip broker cost
#
# 0.15% filters sub-noise Chronos outputs while passing real candidates
# for the ExecutionRouter's Dynamic Spread Filter to validate.
SIGNAL_THRESHOLD = 0.0015   # 0.15% minimum signal strength (noise floor)
EXIT_THRESHOLD   = 0.25    # 25% spread reversion from entry triggers close
MAX_BASKET_AGE_H = 24.0    # Time stop: force-close after 24 hours
ATR_PERIODS      = 14      # Standard ATR lookback window


def _calc_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
              period: int = ATR_PERIODS) -> float:
    """14-period EWM Average True Range on M5 OHLC data.

    True Range = max(H-L, |H-prev_C|, |L-prev_C|)
    Returns the EWM ATR of the final aligned candle.
    Used by ExecutionRouter for volatility-scaled position sizing:
      High ATR (volatile)  -> smaller position (more risk per pip)
      Low  ATR (quiet)     -> larger position (stable, cleaner signal)
    """
    if len(closes) < period + 1:
        return 0.0
    h = pd.Series(highs)
    l = pd.Series(lows)
    c = pd.Series(closes)
    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    return float(tr.ewm(span=period, adjust=False).mean().iloc[-1])


class Signal:
    """Trade signal carrying enriched quality metadata for ExecutionRouter."""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        # Entry signals carry: symbol, action, basket_id, role,
        #   divergence, atr, correlation, round_trip_cost, edge_multiple
        # Close signals carry: symbol, action, basket_id, role


class AlphaEngine:
    def __init__(self, signal_queue: asyncio.Queue):
        self.queue = signal_queue
        self.active_pairs: dict = {}

        # Groq meta-layer — None if GROQ_API_KEY not set or groq SDK missing
        self._groq = build_client(settings.GROQ_API_KEY)
        if self._groq:
            logger.info(f"[Groq] Meta-layer enabled (model: {settings.GROQ_MODEL})")
        else:
            logger.info("Groq meta-layer disabled — math-only mode.")

        logger.info("Loading Chronos-Bolt-tiny model to GPU/CPU...")
        self.pipeline = BaseChronosPipeline.from_pretrained(
            "amazon/chronos-bolt-tiny",
            device_map="cuda" if torch.cuda.is_available() else "cpu",
            dtype=torch.bfloat16,
        )
        logger.info("Model loaded successfully.")

    async def _sync_open_positions(self):
        """Recover open baskets from MT5 on startup to prevent re-entry."""
        loop = asyncio.get_running_loop()
        positions = await loop.run_in_executor(None, mt5.positions_get)
        if not positions:
            return

        recovered = 0
        for pos in positions:
            c = pos.comment or ""
            if c.startswith("BSKT_") and pos.magic == 999:
                parts = c.split("_")
                if len(parts) >= 4:
                    pair_key = (parts[1], parts[2])
                    if pair_key not in self.active_pairs:
                        self.active_pairs[pair_key] = {
                            "basket_id":    c,
                            "entry_spread": 0.0,   # unknown after restart
                            "entry_time":   float(pos.time),
                        }
                        recovered += 1

        if recovered:
            logger.info(f"[Recovery] Restored {recovered} active basket(s) from MT5.")

    async def run(self):
        """
        Defensive Auto-Scanner loop.

        Per-cycle for each pair:
          1. Force both symbols into MT5 Market Watch (symbol_select)
          2. Check broker trade_mode == SYMBOL_TRADE_MODE_FULL for BOTH legs
          3. Skip any pair where either leg is unavailable or non-tradable
          4. Run the full analysis pipeline on confirmed-open pairs only
        """
        await self._sync_open_positions()
        loop = asyncio.get_running_loop()
        cycle = 0

        while True:
            # ── Rollover safety pause ────────────────────────────────────────
            if risk_gates.is_rollover_window():
                logger.warning("[PAUSE] Rollover window active. Pausing Alpha Engine 60s.")
                await asyncio.sleep(60)
                continue

            cycle += 1
            open_count = 0
            skip_reasons: list[str] = []

            for asset_a, asset_b in MASTER_PAIRS:
                pair_id = f"{asset_a}/{asset_b}"

                # Step 1: Force symbols into Market Watch
                # Without this, new symbols (EURUSD, GBPUSD...) are 'not visible'
                # and all orders fail immediately with MT5 error 10004.
                await loop.run_in_executor(None, mt5.symbol_select, asset_a, True)
                await loop.run_in_executor(None, mt5.symbol_select, asset_b, True)

                # Step 2: Verify both legs are fully tradable
                info_a = await loop.run_in_executor(None, mt5.symbol_info, asset_a)
                info_b = await loop.run_in_executor(None, mt5.symbol_info, asset_b)

                if not info_a or not info_b:
                    reason = f"{pair_id}: symbol_info unavailable"
                    logger.warning(f"[SKIP] {reason}")
                    skip_reasons.append(reason)
                    continue

                if info_a.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
                    reason = f"{pair_id}: {asset_a} not in FULL trade mode (mode={info_a.trade_mode})"
                    logger.debug(f"[SKIP] {reason}")
                    skip_reasons.append(reason)
                    continue

                if info_b.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
                    reason = f"{pair_id}: {asset_b} not in FULL trade mode (mode={info_b.trade_mode})"
                    logger.debug(f"[SKIP] {reason}")
                    skip_reasons.append(reason)
                    continue

                open_count += 1
                await self._process_pair(asset_a, asset_b)

            # Cycle summary — shows exactly what was evaluated and what was skipped
            logger.info(
                f"[Cycle {cycle}] Evaluated {open_count}/{len(MASTER_PAIRS)} "
                f"active markets. "
                + (f"Skipped: {len(skip_reasons)} pairs." if skip_reasons else "All pairs open.")
            )
            await asyncio.sleep(60)

    async def _process_pair(self, asset_a: str, asset_b: str):
        """Catch-all wrapper — no exception in a single pair evaluation exits the loop."""
        loop = asyncio.get_running_loop()
        try:
            await self._evaluate_pair(asset_a, asset_b, loop)
        except Exception:
            logger.exception(f"[{asset_a}/{asset_b}] Unhandled exception in pair evaluation.")

    async def _evaluate_pair(self, asset_a: str, asset_b: str, loop):
        """
        Full institutional analysis pipeline for one pair.

        Skip reasons are logged with precise context so profitability issues
        can be debugged from the log without needing a debugger.
        """
        pair_id = f"{asset_a}/{asset_b}"

        # ── Rate Fetch ───────────────────────────────────────────────────────
        rates_a = await loop.run_in_executor(
            None, mt5.copy_rates_from_pos, asset_a, mt5.TIMEFRAME_M5, 0, 512)
        rates_b = await loop.run_in_executor(
            None, mt5.copy_rates_from_pos, asset_b, mt5.TIMEFRAME_M5, 0, 512)

        n_a = len(rates_a) if rates_a is not None else 0
        n_b = len(rates_b) if rates_b is not None else 0
        if n_a < 512 or n_b < 512:
            logger.warning(
                f"[{pair_id}] [SKIP: INSUFFICIENT DATA] "
                f"Need 512 candles, got a={n_a}, b={n_b}."
            )
            return

        # Align on shared M5 timestamps; include OHLC for ATR calculation
        df_a = pd.DataFrame({
            'time':   rates_a['time'],
            'a':      rates_a['close'],
            'high_a': rates_a['high'],
            'low_a':  rates_a['low'],
        })
        df_b = pd.DataFrame({
            'time':   rates_b['time'],
            'b':      rates_b['close'],
            'high_b': rates_b['high'],
            'low_b':  rates_b['low'],
        })
        df = pd.merge(df_a, df_b, on='time', how='inner')
        if len(df) < 100:
            logger.warning(
                f"[{pair_id}] [SKIP: ALIGNMENT] Only {len(df)} shared candles. "
                f"Timestamp gap between legs."
            )
            return

        # ── FILTER 1: Correlation Guard ──────────────────────────────────────
        # Pearson correlation < MIN_CORRELATION means the pair has decoupled.
        # Stat-arb spread-reversion assumptions are invalid — false signals spike.
        corr = float(np.corrcoef(df['a'].values, df['b'].values)[0, 1])
        if corr < 0.4:
            logger.info(
                f"[{pair_id}] [SKIP: LOW CORRELATION] "
                f"corr={corr:.3f} < 0.4. "
                f"Pair has decoupled. Stat-arb assumptions broken."
            )
            return

        spread = df['a'] - df['b']
        current_spread = float(spread.iloc[-1])

        # ── FILTER 2: Regime Guard ───────────────────────────────────────────
        # Lag-1 autocorrelation of spread returns:
        #   AC1 < 0 -> RANGE (mean-reverting): each move partially reverses -> ENTER OK
        #   AC1 >= 0 -> TREND (momentum): moves reinforce each other -> SKIP
        # Prevents averaging into a directional macro event (rate hike, data release).
        spread_returns = spread.diff().dropna()
        ac1 = float(spread_returns.autocorr(lag=1)) if len(spread_returns) > 10 else 0.0
        regime = "RANGE" if ac1 < 0 else "TREND"
        if regime == "TREND":
            logger.info(
                f"[{pair_id}] [SKIP: TREND REGIME] "
                f"AC1={ac1:.4f} >= 0. Spread is trending directionally. "
                f"Mean-reversion strategy contraindicated."
            )
            return

        # ── ATR Calculation ──────────────────────────────────────────────────
        atr_a = _calc_atr(df['high_a'].values, df['low_a'].values, df['a'].values)
        atr_b = _calc_atr(df['high_b'].values, df['low_b'].values, df['b'].values)

        # ── Zero-Shot Chronos Forecast ────────────────────────────────────────
        # Chronos-Bolt predicts next 10 M5 bars of the spread time series.
        # We use the median quantile of the first bar as the directional signal.
        # pipeline.predict() is blocking CPU/GPU — offloaded to thread executor.
        context    = torch.tensor(spread.values, dtype=torch.float32)
        predict_fn = functools.partial(self.pipeline.predict, context, prediction_length=10)
        forecast   = await loop.run_in_executor(None, predict_fn)
        median_idx = forecast.shape[1] // 2
        future_spread = float(forecast[0, median_idx].cpu().numpy()[0])

        # ── Edge Pre-Computation (dual-leg cost model) ────────────────────────
        # Compute the full round-trip execution cost for BOTH legs here, in
        # AlphaEngine where we have access to both symbols simultaneously.
        # This is passed to ExecutionRouter via Signal so it doesn't need to
        # fetch ticks for the hedge leg (it only processes one symbol at a time).
        tick_a = await loop.run_in_executor(None, mt5.symbol_info_tick, asset_a)
        tick_b = await loop.run_in_executor(None, mt5.symbol_info_tick, asset_b)

        round_trip_cost = 0.0
        cost_a_pct = cost_b_pct = 0.0
        if tick_a and tick_b and tick_a.ask > 0 and tick_b.ask > 0:
            cost_a_pct      = (tick_a.ask - tick_a.bid) / tick_a.ask   # one-way %
            cost_b_pct      = (tick_b.ask - tick_b.bid) / tick_b.ask   # one-way %
            slippage_est    = (cost_a_pct + cost_b_pct) * 0.5           # 50% of spread = conservative slippage
            round_trip_cost = (cost_a_pct + cost_b_pct + slippage_est) * 2.0  # entry + exit

        divergence = 0.0
        if current_spread != 0:
            divergence = (future_spread - current_spread) / abs(current_spread)

        edge_multiple = abs(divergence) / max(round_trip_cost, 1e-9)

        # ── EXIT CHECK (open basket) ─────────────────────────────────────────
        pair_key = (asset_a, asset_b)
        if pair_key in self.active_pairs:
            basket    = self.active_pairs[pair_key]
            age_hours = (time.time() - basket["entry_time"]) / 3600.0
            time_stop = age_hours >= MAX_BASKET_AGE_H

            entry_spread = basket["entry_spread"]
            if abs(entry_spread) > 1e-9:
                predicted_reversion = (abs(entry_spread) - abs(future_spread)) / abs(entry_spread)
                spread_converged    = predicted_reversion >= EXIT_THRESHOLD
            else:
                predicted_reversion, spread_converged = 0.0, False

            logger.info(
                f"[{pair_id}] BASKET OPEN | "
                f"age={age_hours:.1f}h | entry={entry_spread:.5f} | "
                f"current={current_spread:.5f} | future={future_spread:.5f} | "
                f"reversion={predicted_reversion:.1%} | corr={corr:.3f} | "
                f"regime={regime} | time_stop={time_stop}"
            )

            if spread_converged or time_stop:
                reason = "TIME_STOP" if time_stop else "MEAN_REVERSION"
                logger.info(
                    f"[{pair_id}] EXIT -> {reason} | "
                    f"age={age_hours:.1f}h | future={future_spread:.5f}"
                )
                await self.queue.put(Signal(
                    symbol=asset_a, action="CLOSE",
                    basket_id=basket["basket_id"], role="PRIMARY"))
                await self.queue.put(Signal(
                    symbol=asset_b, action="CLOSE",
                    basket_id=basket["basket_id"], role="HEDGE"))
                del self.active_pairs[pair_key]
            return

        # ── ENTRY CANDIDATE ──────────────────────────────────────────────────
        # Broker spread gate (variance + absolute limits)
        info_a = await loop.run_in_executor(None, mt5.symbol_info, asset_a)
        info_b = await loop.run_in_executor(None, mt5.symbol_info, asset_b)

        for sym, info in [(asset_a, info_a), (asset_b, info_b)]:
            if info and risk_gates.is_spread_blown(sym, info.spread):
                logger.warning(
                    f"[{pair_id}] [SKIP: SPREAD VARIANCE] "
                    f"{sym} spread={info.spread} pts is {settings.SPREAD_VARIANCE_MAX}x "
                    f"above moving average. Broker spread anomaly."
                )
                return
            if info and risk_gates.is_spread_absolute_blown(sym, info.spread):
                logger.warning(
                    f"[{pair_id}] [SKIP: SPREAD LIMIT] "
                    f"{sym} spread={info.spread} pts exceeds MAX_ALLOWED_SPREAD="
                    f"{settings.MAX_ALLOWED_SPREAD}. Hard limit hit."
                )
                return

        # Noise floor — purely directional check before Groq call
        action_a = action_b = None
        if abs(divergence) > SIGNAL_THRESHOLD:
            if divergence > 0:
                action_a, action_b = "ENTER_SHORT", "ENTER_LONG"
            else:
                action_a, action_b = "ENTER_LONG", "ENTER_SHORT"

        if not action_a:
            logger.info(
                f"[{pair_id}] No signal | "
                f"current={current_spread:.5f} | future={future_spread:.5f} | "
                f"divergence={divergence:.4%} (floor +/-{SIGNAL_THRESHOLD:.1%}) | "
                f"corr={corr:.3f} | regime={regime} | "
                f"round_trip_cost={round_trip_cost:.4%} | edge={edge_multiple:.2f}x"
            )
            return

        # ── GROQ META-LAYER (advisory) ────────────────────────────────────────
        # Only called AFTER all mathematical filters have passed, so Groq is
        # never a bottleneck on pairs that get filtered anyway.
        # Timeout: 5s hard cap. Failure mode: APPROVED (math-only fallback).
        if self._groq:
            groq_metrics = {
                'correlation':        corr,
                'ac1':                ac1,
                'atr_a':              atr_a,
                'atr_b':              atr_b,
                'current_spread':     current_spread,
                'divergence_pct':     abs(divergence) * 100,
                'round_trip_cost_pct': round_trip_cost * 100,
                'edge_multiple':      edge_multiple,
            }
            verdict = await classify_regime(pair_id, groq_metrics, self._groq)

            if verdict.available:
                if not verdict.trade_permitted and verdict.confidence >= settings.GROQ_VETO_CONFIDENCE:
                    # Groq veto: log concerns and skip — UNLESS math edge is very strong
                    if edge_multiple < settings.GROQ_VETO_OVERRIDE_MULTIPLE:
                        logger.warning(
                            f"[{pair_id}] [Groq VETO] "
                            f"(confidence={verdict.confidence:.0%} | regime={verdict.regime}). "
                            f"Reason: {verdict.reasoning}. "
                            f"Concerns: {verdict.concerns}. "
                            f"edge_multiple={edge_multiple:.2f}x < "
                            f"override threshold {settings.GROQ_VETO_OVERRIDE_MULTIPLE}x. "
                            f"[SKIP: GROQ VETO]"
                        )
                        return
                    else:
                        logger.warning(
                            f"[{pair_id}] [Groq VETO OVERRIDDEN] — "
                            f"edge_multiple={edge_multiple:.2f}x >= "
                            f"{settings.GROQ_VETO_OVERRIDE_MULTIPLE}x override threshold. "
                            f"Groq concerns noted: {verdict.concerns}"
                        )
                elif verdict.concerns:
                    logger.info(
                        f"[{pair_id}] [Groq APPROVED] "
                        f"(regime={verdict.regime} | conf={verdict.confidence:.0%}). "
                        f"Advisory concerns: {verdict.concerns}. {verdict.reasoning}"
                    )
                else:
                    logger.info(
                        f"[{pair_id}] [Groq APPROVED] "
                        f"(regime={verdict.regime} | conf={verdict.confidence:.0%}). "
                        f"{verdict.reasoning}"
                    )
            # If verdict.available is False: Groq was down. Math decides -> proceed.

        # ── SIGNAL EMISSION ──────────────────────────────────────────────────
        b_id = f"BSKT_{asset_a}_{asset_b}_{int(time.time())}"

        # Enrich signals with quality metadata for ExecutionRouter's:
        #   - Dynamic Spread Filter (divergence, round_trip_cost, edge_multiple)
        #   - ATR-based position sizing (atr)
        #   - Observability (correlation)
        common = dict(
            basket_id=b_id,
            divergence=abs(divergence),
            round_trip_cost=round_trip_cost,
            edge_multiple=edge_multiple,
            correlation=corr,
        )
        await self.queue.put(Signal(
            symbol=asset_a, action=action_a, role="PRIMARY", atr=atr_a, **common))
        await self.queue.put(Signal(
            symbol=asset_b, action=action_b, role="HEDGE",   atr=atr_b, **common))

        self.active_pairs[pair_key] = {
            "basket_id":    b_id,
            "entry_spread": current_spread,
            "entry_time":   time.time(),
        }
        logger.info(
            f"[SIGNAL] [{pair_id}] -> {action_a}/{action_b} | "
            f"basket={b_id} | spread={current_spread:.5f} | "
            f"divergence={divergence:.4%} | corr={corr:.3f} | regime={regime} | "
            f"atr_a={atr_a:.5f} | atr_b={atr_b:.5f} | "
            f"round_trip_cost={round_trip_cost:.4%} | edge={edge_multiple:.2f}x"
        )
