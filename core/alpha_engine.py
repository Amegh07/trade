"""
core/alpha_engine.py  —  Omega Architecture: Process 2 (Alpha Engine)
═══════════════════════════════════════════════════════════════════════
Spawned as an independent OS process (bypasses GIL).

Responsibilities:
  - Waits for DataFeeder's ready_event, then attaches to SharedMemory.
  - Maintains per-symbol circular OHLCV buffers (pre-allocated np.zeros(200))
    updated in-place via a rolling pointer — zero new MT5 candle fetches
    during steady-state operation.
  - Runs ALL mathematics as pure NumPy matrix operations:
      Phase 2: Hurst Exponent (R/S), ATR (Wilder EWM), Z-Score
      Phase 3: Order Flow Imbalance (tick-level bid/ask delta vectors)
  - Publishes typed Signal namedtuples to a multiprocessing.Queue
    consumed by the Execution Router.

Fixes vs. previous version:
  - lows[] buffer: initialised to np.zeros(); first-write guard prevents
    stale 1e18 sentinel contaminating ATR on seeded symbols.
  - CANDLE_SEED_MINIMUM guard: signals are not published until at least
    CANDLE_SEED_MINIMUM bars of real data are in the buffer.
  - MT5 init retry via _connect_mt5() (shared pattern with DataFeeder).
"""

import time
import logging
import multiprocessing as mp
import numpy as np
from collections import deque
from multiprocessing.shared_memory import SharedMemory
from typing import NamedTuple

from core.data_feeder import FIELDS_PER_SYMBOL, BYTES_PER_SYMBOL

# ── Signal wire type ─────────────────────────────────────────────────────────
class Signal(NamedTuple):
    symbol:    str
    direction: float     # +1.0 = BUY, -1.0 = SELL, 0.0 = FLAT
    atr:       float
    hurst:     float
    regime:    str       # "TRENDING" | "RANGING" | "NO_TRADE"
    ofi_ok:    bool      # True = OFI clear, False = OFI vetoed
    timestamp: float     # unix epoch


# ── Constants ─────────────────────────────────────────────────────────────────
BUFFER_SIZE        = 200    # circular candle buffer (pre-allocated)
CANDLE_SEED_MINIMUM = 30   # must have ≥ this many real bars before publishing
HURST_LAGS         = 60    # max R/S lags for Hurst calculation
ATR_PERIOD         = 14    # EWM alpha = 1/14
ZSCORE_PERIOD      = 20    # rolling Z-Score window
OFI_DEPTH          = 100   # tick-level deque maxlen
CANDLE_SEC         = 300   # 5-minute candle duration in seconds
EVAL_INTERVAL      = 5.0   # full signal evaluation every N seconds
MT5_INIT_RETRIES   = 3
MT5_INIT_BACKOFF   = 2.0


# ── Logger ────────────────────────────────────────────────────────────────────
def _make_logger() -> logging.Logger:
    log = logging.getLogger("alpha_engine")
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter(
            "%(asctime)s [AlphaEngine] %(levelname)s — %(message)s"
        ))
        log.addHandler(h)
    log.setLevel(logging.INFO)
    return log


def _connect_mt5(login: int, password: str, server: str, logger: logging.Logger) -> bool:
    import MetaTrader5 as mt5
    for attempt in range(1, MT5_INIT_RETRIES + 1):
        logger.info(f"MT5 init attempt {attempt}/{MT5_INIT_RETRIES}…")
        if mt5.initialize(login=int(login), password=password, server=server):
            logger.info("MT5 connection established.")
            return True
        logger.warning(f"mt5.initialize() failed: {mt5.last_error()}")
        if attempt < MT5_INIT_RETRIES:
            time.sleep(MT5_INIT_BACKOFF * attempt)
    logger.critical(f"MT5 init FAILED after {MT5_INIT_RETRIES} attempts — process exiting.")
    return False


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2: VECTORIZED MATHEMATICS
# ══════════════════════════════════════════════════════════════════════════════

def _vectorized_atr(
    highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = ATR_PERIOD
) -> float:
    """
    Pure-NumPy Wilder ATR.  No Python loops over candles.
    Returns the Wilder EWM ATR scalar for the full buffer.
    """
    if len(closes) < period + 1:
        valid_range = highs - lows
        return float(np.std(valid_range[valid_range > 0])) if np.any(valid_range > 0) else 0.0

    prev_c = closes[:-1]
    curr_h = highs[1:]
    curr_l = lows[1:]

    tr = np.maximum(
        curr_h - curr_l,
        np.maximum(
            np.abs(curr_h - prev_c),
            np.abs(curr_l - prev_c),
        )
    )

    alpha   = 1.0 / period
    weights = (1.0 - alpha) ** np.arange(len(tr) - 1, -1, -1, dtype=np.float64)
    w_sum   = weights.sum()
    return float(np.dot(weights, tr) / w_sum) if w_sum > 0 else 0.0


def _vectorized_hurst(closes: np.ndarray, max_lags: int = HURST_LAGS) -> float:
    """
    Pure-NumPy Hurst Exponent via Rescaled Range (R/S) analysis.
    All reshape / cumsum / std operations are NumPy matrix ops —
    the outer Python loop is over ~58 lag values (trivial cost).
    Returns H clamped to [0.0, 1.0].
    """
    if len(closes) < 4:
        return 0.5

    safe_closes = np.where(closes == 0, 1e-10, closes)
    ts = np.log(safe_closes[1:] / safe_closes[:-1])
    ts = ts[np.isfinite(ts)]
    n  = len(ts)
    if n < 4:
        return 0.5

    max_lag = min(max_lags, n // 2)
    lags    = np.arange(2, max(3, max_lag), dtype=np.int32)

    rs_vals  = []
    lag_vals = []

    for lag in lags:
        m = n // lag
        if m == 0:
            continue
        chunk = ts[:m * lag].reshape(m, lag)   # ← all inner math is matrix ops
        means = chunk.mean(axis=1, keepdims=True)
        dev   = chunk - means
        cum   = dev.cumsum(axis=1)
        r     = cum.max(axis=1) - cum.min(axis=1)
        s     = chunk.std(axis=1)
        s     = np.where(s == 0, 1e-10, s)
        rs_vals.append(float(np.mean(r / s)))
        lag_vals.append(int(lag))

    if len(lag_vals) < 2:
        return 0.5

    log_lags = np.log(np.array(lag_vals, dtype=np.float64))
    log_rs   = np.log(np.array(rs_vals,  dtype=np.float64))
    valid    = np.isfinite(log_lags) & np.isfinite(log_rs)
    if valid.sum() < 2:
        return 0.5

    poly = np.polyfit(log_lags[valid], log_rs[valid], 1)
    return float(np.clip(poly[0], 0.0, 1.0))


def _vectorized_zscore(series: np.ndarray, period: int = ZSCORE_PERIOD) -> float:
    """Vectorised rolling Z-Score on the tail slice."""
    if len(series) < period:
        return 0.0
    window = series[-period:]
    mu, sigma = window.mean(), window.std()
    if sigma < 1e-12:
        return 0.0
    return float((series[-1] - mu) / sigma)


def _regime_from_hurst(h: float) -> str:
    if h > 0.55:
        return "TRENDING"
    elif h < 0.45:
        return "RANGING"
    return "NO_TRADE"


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3: OFI (per-symbol tick deque)
# ══════════════════════════════════════════════════════════════════════════════

def _compute_ofi(tick_deque: deque, signal: float) -> bool:
    """
    Order Flow Imbalance gate — pure NumPy, no Python loops over ticks.
    If a BUY signal faces dominant ask-side absorption (sell pressure > 65%) → VETO.
    If a SELL signal faces dominant bid-side absorption (buy pressure > 65%) → VETO.
    Returns True (CLEAR) or False (VETOED).
    """
    if len(tick_deque) < 10:
        return True   # not enough microstructure data — let through

    ticks = np.array(tick_deque, dtype=np.float64)   # shape (N, 3): bid, ask, vol
    vols  = ticks[:, 2]

    bid_delta = np.diff(ticks[:, 0], prepend=ticks[0, 0])
    ask_delta = np.diff(ticks[:, 1], prepend=ticks[0, 1])

    buyer_mask  = (bid_delta > 0) | (ask_delta < 0)
    seller_mask = (bid_delta < 0) | (ask_delta > 0)

    bid_vol = vols[buyer_mask].sum()
    ask_vol = vols[seller_mask].sum()
    total   = bid_vol + ask_vol

    if total < 1e-10:
        return True

    buy_pressure  = bid_vol / total
    sell_pressure = ask_vol / total

    if signal == 1.0 and sell_pressure > 0.65:
        return False   # sellers dominating micro-tape → VETO BUY
    if signal == -1.0 and buy_pressure > 0.65:
        return False   # buyers dominating micro-tape → VETO SELL
    return True


# ══════════════════════════════════════════════════════════════════════════════
# MAIN PROCESS ENTRY
# ══════════════════════════════════════════════════════════════════════════════

def alpha_engine_process(
    symbols:      list,
    shm_name:     str,
    symbol_index: dict,
    signal_queue: mp.Queue,
    stop_event:   mp.Event,
    mt5_login:    int,
    mt5_password: str,
    mt5_server:   str,
    feeder_ready: mp.Event,
):
    """
    Alpha Engine entry point.

    Reads live ticks from shared memory written by the Data Feeder.
    Applies vectorised mathematics to pre-allocated circular OHLCV buffers.
    Publishes Signal namedtuples to signal_queue for the Execution Router.
    """
    import MetaTrader5 as mt5

    logger = _make_logger()
    logger.info(f"AlphaEngine PID={mp.current_process().pid} | {len(symbols)} symbols")

    # ── Independent MT5 init (needed for initial candle seed + HTF data) ──────
    if not _connect_mt5(mt5_login, mt5_password, mt5_server, logger):
        return
    logger.info("MT5 connection established.")

    # ── Wait for DataFeeder's first tick snapshot ─────────────────────────────
    logger.info("Waiting for DataFeeder first snapshot…")
    feeder_ready.wait(timeout=30)
    logger.info("DataFeeder ready — attaching shared memory.")

    # ── Attach to shared memory ───────────────────────────────────────────────
    try:
        shm = SharedMemory(name=shm_name)
    except FileNotFoundError:
        logger.critical(f"SharedMemory '{shm_name}' not found — exiting.")
        mt5.shutdown()
        return

    ticks_np = np.ndarray(
        shape=(len(symbols), FIELDS_PER_SYMBOL),
        dtype=np.float64,
        buffer=shm.buf,
    )

    # ── Pre-allocate circular OHLCV buffers (PHASE 2: zero-copy) ─────────────
    # FIX: lows initialised to 0.0 (not 1e18) — first-write guard below prevents
    #      stale zeros from corrupting ATR until the slot receives a real tick.
    opens    = {s: np.zeros(BUFFER_SIZE, dtype=np.float64) for s in symbols}
    highs    = {s: np.zeros(BUFFER_SIZE, dtype=np.float64) for s in symbols}
    lows     = {s: np.zeros(BUFFER_SIZE, dtype=np.float64) for s in symbols}
    closes   = {s: np.zeros(BUFFER_SIZE, dtype=np.float64) for s in symbols}
    volumes  = {s: np.zeros(BUFFER_SIZE, dtype=np.float64) for s in symbols}
    buf_ptr      = {s: 0     for s in symbols}  # circular write head
    buf_count    = {s: 0     for s in symbols}  # real bars written (for seed guard)
    buf_filled   = {s: False for s in symbols}  # True once BUFFER_SIZE real bars exist
    candle_ts    = {s: 0.0   for s in symbols}  # epoch of current forming candle

    # ── Seed buffers with initial M5 history from MT5 (once, at startup) ─────
    logger.info("Seeding candle buffers from MT5 history…")
    for sym in symbols:
        rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M5, 0, BUFFER_SIZE)
        if rates is not None and len(rates) > 0:
            n = len(rates)
            opens[sym][:n]   = rates['open']
            highs[sym][:n]   = rates['high']
            lows[sym][:n]    = rates['low']
            closes[sym][:n]  = rates['close']
            volumes[sym][:n] = rates['tick_volume']
            buf_ptr[sym]     = n % BUFFER_SIZE
            buf_count[sym]   = n
            buf_filled[sym]  = n >= BUFFER_SIZE
            candle_ts[sym]   = float(rates['time'][-1])
        else:
            logger.warning(f"Could not seed {sym} — starting from live ticks only.")

    # ── Per-symbol OFI tick deques (PHASE 3) ─────────────────────────────────
    ofi_deques: dict = {s: deque(maxlen=OFI_DEPTH) for s in symbols}

    logger.info("Candle buffers seeded. Entering main loop.")

    while not stop_event.is_set():
        t_start = time.perf_counter()

        # ── TICK INGESTION: update OFI deques and candle buffers ──────────────
        for sym in symbols:
            idx = symbol_index[sym]

            bid       = ticks_np[idx, 0]
            ask       = ticks_np[idx, 1]
            vol       = ticks_np[idx, 3]
            tick_time = ticks_np[idx, 4]

            if bid == 0.0 and ask == 0.0:
                continue   # DataFeeder hasn't written this symbol yet

            mid = (bid + ask) / 2.0

            # Phase 3: feed OFI deque
            ofi_deques[sym].append((bid, ask, vol))

            # Candle aggregation: advance on M5 boundary
            if candle_ts[sym] == 0.0:
                candle_ts[sym] = tick_time - (tick_time % CANDLE_SEC)

            candle_age = tick_time - candle_ts[sym]
            ptr = buf_ptr[sym]

            if candle_age >= CANDLE_SEC:
                # Close current candle → advance circular pointer
                ptr = (ptr + 1) % BUFFER_SIZE
                buf_ptr[sym] = ptr
                buf_count[sym] = min(buf_count[sym] + 1, BUFFER_SIZE)
                if not buf_filled[sym] and buf_count[sym] >= BUFFER_SIZE:
                    buf_filled[sym] = True

                # Open new candle — FIX: initialise all four in one shot
                opens[sym][ptr]   = mid
                highs[sym][ptr]   = mid
                lows[sym][ptr]    = mid    # ← correct: use mid, not 1e18
                closes[sym][ptr]  = mid
                volumes[sym][ptr] = vol
                candle_ts[sym]    = tick_time - (tick_time % CANDLE_SEC)
            else:
                # Update forming candle in-place (zero allocation)
                closes[sym][ptr]  = mid
                if mid > highs[sym][ptr]:
                    highs[sym][ptr] = mid
                # FIX: guard against zero-initialised slot being treated as a real low
                if lows[sym][ptr] == 0.0 or mid < lows[sym][ptr]:
                    lows[sym][ptr] = mid
                volumes[sym][ptr] += vol

        # ── SIGNAL EVALUATION (every EVAL_INTERVAL seconds) ──────────────────
        elapsed = time.perf_counter() - t_start
        if elapsed < EVAL_INTERVAL:
            time.sleep(EVAL_INTERVAL - elapsed)

        for sym in symbols:
            try:
                # CANDLE_SEED_MINIMUM guard — don't publish on cold start
                if buf_count[sym] < CANDLE_SEED_MINIMUM:
                    continue

                # Unroll circular buffer into contiguous time-ordered view
                ptr       = buf_ptr[sym]
                idx_order = np.roll(np.arange(BUFFER_SIZE), -ptr - 1)

                c = closes[sym][idx_order]
                h = highs[sym][idx_order]
                l = lows[sym][idx_order]

                # Strip leading zeros from unseeded slots
                first_real = int(np.argmax(c > 0))
                if first_real > 0:
                    c = c[first_real:]
                    h = h[first_real:]
                    l = l[first_real:]

                if len(c) < CANDLE_SEED_MINIMUM:
                    continue

                # ── Phase 2: Vectorised mathematics ──────────────────────────
                hurst  = _vectorized_hurst(c)
                regime = _regime_from_hurst(hurst)
                atr    = _vectorized_atr(h, l, c)
                zscore = _vectorized_zscore(c)

                # ── Direction logic per regime ────────────────────────────────
                if regime == "NO_TRADE":
                    direction = 0.0
                elif regime == "RANGING":
                    # Mean reversion: short at extreme high z, long at extreme low z
                    if zscore > 2.0:
                        direction = -1.0
                    elif zscore < -2.0:
                        direction = 1.0
                    else:
                        direction = 0.0
                else:  # TRENDING
                    # Momentum: follow the recent directional bias
                    recent    = c[-ZSCORE_PERIOD:]
                    direction = 1.0 if recent[-1] > recent[0] else -1.0

                # ── Phase 3: OFI gate ─────────────────────────────────────────
                ofi_ok = (
                    _compute_ofi(ofi_deques[sym], direction)
                    if direction != 0.0 else True
                )

                sig = Signal(
                    symbol    = sym,
                    direction = direction,
                    atr       = atr,
                    hurst     = hurst,
                    regime    = regime,
                    ofi_ok    = ofi_ok,
                    timestamp = time.time(),
                )

                # Publish only actionable, OFI-cleared signals
                if direction != 0.0 and ofi_ok:
                    try:
                        signal_queue.put_nowait(sig)
                    except Exception:
                        pass   # queue full — drop; next cycle will retry

            except Exception as exc:
                logger.warning(f"[{sym}] evaluation error: {exc}")

    logger.info("Stop event received — releasing resources.")
    shm.close()
    mt5.shutdown()
    logger.info("AlphaEngine exited cleanly.")
