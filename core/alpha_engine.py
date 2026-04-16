"""
core/alpha_engine.py  —  Omega Architecture: Process 2 (Alpha Engine)
═══════════════════════════════════════════════════════════════════════
Architectural Rewrite: Delta-Neutral Statistical Arbitrage (Pairs Trading)
"""

import time
import logging
import sqlite3
import uuid
import queue
import multiprocessing as mp
import numpy as np
import statsmodels.api as sm
import pandas as pd
import statsmodels.tsa.stattools as ts
from multiprocessing.shared_memory import SharedMemory
import datetime
import collections
from typing import NamedTuple, Dict, List, Tuple

from core.data_feeder import FIELDS_PER_SYMBOL, BYTES_PER_SYMBOL

# ── Signal wire type ─────────────────────────────────────────────────────────
class Signal(NamedTuple):
    symbol:       str
    signal:       float     # +1.0 = BUY, -1.0 = SELL, 0.0 = FLAT
    confidence:   float     # [0.0 - 1.0] (1 - p-value)
    regime:       str       # "COINTEGRATED" | "DIVERGING"
    action:       str       # "ENTER_LONG" | "ENTER_SHORT" | "CLOSE"
    atr:          float
    z_score:      float     # Current Spread Z-Score
    ofi_ok:       bool      
    timestamp:    float
    # STATARB EXTENSIONS
    basket_id:    str       # Links multiple legs of a single trade
    beta:         float     # Hedge Ratio
    leg_role:     str       # 'PRIMARY' | 'HEDGE'
    # ARCHITECT UPGRADE: Execution & Safety Metadata
    hedge_symbol: str       # To assist router with margin checks
    hedge_beta:   float     # Beta at signal time
    exit_reason:  str       # "MEAN_REVERSION" | "Z_HARD_STOP" | "TIME_STOP" | "COINT_BREAKDOWN"

# ── Constants ─────────────────────────────────────────────────────────────────
BUFFER_SIZE         = 600    # Need > 500 for cointegration check
CANDLE_SEED_MINIMUM = 500   
ZSCORE_WINDOW       = 50
COINT_P_LIMIT       = 0.05
CANDLE_SEC          = 300   
EVAL_INTERVAL       = 10.0  # Statistical tests take more time
MT5_INIT_RETRIES    = 3
MT5_INIT_BACKOFF    = 2.0
Z_HARD_STOP         = 4.5
TIME_STOP_PERIODS   = 96    # Redefined: 24 hours on M15 (architect ruling)

# Predefined tradable pairs — Economically tethered, high correlation spreads
TRADABLE_PAIRS = [
    # --- THE COMMODITY BLOC (Highly Tethered) ---
    ("AUDUSD", "NZDUSD"),  # The classic Oceanic spread
    ("AUDJPY", "NZDJPY"),  # Oceanic yield vs safe haven
    ("USDCAD", "AUDUSD"),  # Oil vs Metals (Inverted beta)
    
    # --- THE EUROPEAN CORE ---
    ("EURUSD", "GBPUSD"),  # Eurozone vs UK
    ("EURGBP", "EURCHF"),  # European cross-flows
    ("EURJPY", "GBPJPY"),  # European yield vs Japanese yield
    
    # --- THE SAFE HAVENS ---
    ("USDCHF", "USDJPY"),  # Swiss Franc vs Japanese Yen
    ("EURCHF", "USDCHF"),  # Euro vs Dollar (Swiss anchored)

    # --- THE PRECIOUS METALS (If your broker offers them) ---
    ("XAUUSD", "XAGUSD"),  # Gold vs Silver (The ultimate stat-arb pair)
    ("XAUEUR", "XAGEUR"),  # Gold vs Silver (Euro priced)
]

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
# STATISTICAL ARBITRAGE UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def _calculate_spread_stats(y: np.ndarray, x: np.ndarray, logger: logging.Logger = None) -> Tuple[float, float, float, np.ndarray]:
    """
    Performs Cointegration test & OLS to find Beta and Z-Score.
    Returns: (p_value, beta, current_z, spread_series) or (None, None, None, None) on error.
    
    MATH GUARD: Validates array fitness before statistical operations.
    - Checks variance > 1e-8 to detect flat/degenerate arrays from ffill() on missing data.
    - Catches statsmodels LinAlgError/ValueError and returns None tuple to skip pair.
    """
    if logger is None:
        logger = _make_logger()
    
    try:
        # ── MATH GUARD 1: Validate input arrays for degeneracy ──────────
        y_var = np.var(y)
        x_var = np.var(x)
        if y_var < 1e-8 or x_var < 1e-8:
            logger.warning(f"[Math Guard] Degenerate/flat arrays detected (y_var={y_var:.2e}, x_var={x_var:.2e}). Skipping statistical evaluation.")
            return None, None, None, None
        
        # 1. Cointegration Test
        _, p_value, _ = coint(y, x)
        
        # 2. OLS for Hedge Ratio (Beta)
        # y = beta * x + constant
        x_const = sm.add_constant(x)
        model = sm.OLS(y, x_const).fit()
        beta = model.params[1]
        constant = model.params[0]
        
        # Validate outputs are finite
        if not (np.isfinite(p_value) and np.isfinite(beta) and np.isfinite(constant)):
            logger.warning(f"[Math Guard] Non-finite statistics detected (p={p_value}, beta={beta}). Skipping pair.")
            return None, None, None, None
        
        # 3. Calculate Spread
        spread = y - (beta * x + constant)
        
        # 4. Z-Score calculation
        if len(spread) < ZSCORE_WINDOW:
            return p_value, beta, 0.0, spread
            
        window = spread[-ZSCORE_WINDOW:]
        mu, sigma = np.mean(window), np.std(window)
        
        if sigma < 1e-12:
            return p_value, beta, 0.0, spread
            
        z_score = (spread[-1] - mu) / sigma
        if not np.isfinite(z_score):
            logger.warning(f"[Math Guard] Non-finite z-score detected ({z_score}). Returning 0.0.")
            z_score = 0.0
        
        return p_value, beta, z_score, spread
    
    except (np.linalg.LinAlgError, ValueError) as exc:
        logger.warning(f"[Math Guard] Statsmodels error: {type(exc).__name__}: {exc}. Skipping pair.")
        return None, None, None, None
    except Exception as exc:
        logger.error(f"[Math Guard] Unexpected error in cointegration/OLS: {exc}", exc_info=False)
        return None, None, None, None

def _vectorized_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1: return 0.0
    tr = np.maximum(highs[1:] - lows[1:], np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])))
    return float(np.mean(tr[-period:]))

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
    live_params:  mp.Array,
    shm_lock     = None,
):
    import MetaTrader5 as mt5

    logger = _make_logger()
    logger.info(f"AlphaEngine PID={mp.current_process().pid} | StatArb Mode")

    if not _connect_mt5(mt5_login, mt5_password, mt5_server, logger):
        return

    feeder_ready.wait(timeout=30)
    try:
        shm = SharedMemory(name=shm_name)
    except FileNotFoundError:
        logger.critical(f"SharedMemory '{shm_name}' not found — exiting.")
        mt5.shutdown()
        return

    ticks_np = np.ndarray(shape=(len(symbols), FIELDS_PER_SYMBOL), dtype=np.float64, buffer=shm.buf)

    # Historical Buffers
    closes     = {s: np.zeros(BUFFER_SIZE, dtype=np.float64) for s in symbols}
    highs      = {s: np.zeros(BUFFER_SIZE, dtype=np.float64) for s in symbols}
    lows       = {s: np.zeros(BUFFER_SIZE, dtype=np.float64) for s in symbols}
    times      = {s: np.zeros(BUFFER_SIZE, dtype=np.float64) for s in symbols}
    buf_ptr    = {s: 0     for s in symbols}  
    buf_count  = {s: 0     for s in symbols}  
    candle_ts  = {s: 0.0   for s in symbols}
    spread_history  = {s: collections.deque(maxlen=20) for s in symbols}  

    logger.info("Seeding candle buffers for pairs...")
    for sym in symbols:
        rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M5, 0, BUFFER_SIZE)
        if rates is not None and len(rates) > 0:
            n = len(rates)
            closes[sym][:n] = rates['close']
            highs[sym][:n]  = rates['high']
            lows[sym][:n]   = rates['low']
            times[sym][:n]  = rates['time'].astype(np.float64)
            buf_ptr[sym]    = n % BUFFER_SIZE
            buf_count[sym]  = n
            candle_ts[sym]  = float(rates['time'][-1])

    # Tracking open baskets for exit logic
    # active_baskets: basket_id -> {"start_time": float, "asset_a": str, "asset_b": str}
    active_baskets = {} 

    logger.info("Syncing existing positions from MT5...")
    try:
        current_pos = mt5.positions_get()
        if current_pos:
            for pos in current_pos:
                # Recover basket_id from comment if possible
                b_id = pos.comment if pos.comment.startswith("BSKT_") else None
                if b_id:
                    if b_id not in active_baskets:
                        pos_dt = datetime.datetime.fromtimestamp(pos.time, tz=datetime.timezone.utc)
                        now_dt = datetime.datetime.now(tz=datetime.timezone.utc)
                        age_seconds = (now_dt - pos_dt).total_seconds()
                        
                        active_baskets[b_id] = {
                            "start_time": time.time() - age_seconds,  # Normalize to local epoch
                            "symbols": set()
                        }
                    active_baskets[b_id]["symbols"].add(pos.symbol)
            logger.info(f"Recovered {len(active_baskets)} active baskets from MT5.")
    except Exception as exc:
        logger.warning(f"Failed to sync positions on startup: {exc}")

    logger.info("Entering StatArb Decision Loop.")

    while not stop_event.is_set():
        t_start = time.perf_counter()

        # 0. Sync Dynamic Thresholds
        with live_params.get_lock():
            z_entry_dynamic = live_params[0]
            z_exit_dynamic  = live_params[1]

        # 1. Tick Ingestion & Candle Building
        for sym in symbols:
            idx = symbol_index[sym]
            
            # ── ATOMIC READ WITH SHM_LOCK ──────────────────
            if shm_lock: shm_lock.acquire()
            try:
                bid = ticks_np[idx, 0]
                ask = ticks_np[idx, 1]
                vol = ticks_np[idx, 3]
                tick_time = ticks_np[idx, 4]
            finally:
                if shm_lock: shm_lock.release()
            
            if bid == 0.0: continue
            
            mid = (bid + ask) / 2.0
            if candle_ts[sym] == 0.0: candle_ts[sym] = tick_time - (tick_time % CANDLE_SEC)

            if tick_time - candle_ts[sym] >= CANDLE_SEC:
                buf_ptr[sym] = (buf_ptr[sym] + 1) % BUFFER_SIZE
                buf_count[sym] = min(buf_count[sym] + 1, BUFFER_SIZE)
                closes[sym][buf_ptr[sym]] = mid
                highs[sym][buf_ptr[sym]] = mid
                lows[sym][buf_ptr[sym]] = mid
                times[sym][buf_ptr[sym]] = tick_time - (tick_time % CANDLE_SEC)
                candle_ts[sym] = tick_time - (tick_time % CANDLE_SEC)
            else:
                p = buf_ptr[sym]
                closes[sym][p] = mid
                if mid > highs[sym][p]: highs[sym][p] = mid
                if mid < lows[sym][p]: lows[sym][p] = mid

        # 2. Pair Evaluation
        for asset_a, asset_b in TRADABLE_PAIRS:
            if buf_count[asset_a] < CANDLE_SEED_MINIMUM or buf_count[asset_b] < CANDLE_SEED_MINIMUM:
                continue

            # ── ARCHITECT UPGRADE: Precision Timestamp Alignment ──
            ptr_a, ptr_b = buf_ptr[asset_a], buf_ptr[asset_b]
            idx_a = np.roll(np.arange(BUFFER_SIZE), -ptr_a - 1)
            idx_b = np.roll(np.arange(BUFFER_SIZE), -ptr_b - 1)

            # Use only valid history segments
            start_a = BUFFER_SIZE - buf_count[asset_a]
            start_b = BUFFER_SIZE - buf_count[asset_b]

            # ── ARCHITECT UPDATE: State-Aware Logic ──
            # Find if this pair already has an active basket
            existing_b_id = next((k for k, v in active_baskets.items() 
                                  if asset_a in v["symbols"] and asset_b in v["symbols"]), None)

            # Action logic initialized
            action_a, action_b = None, None
            signal_a, signal_b = 0.0, 0.0
            exit_reason = ""

            df_a = pd.DataFrame({
                'time': times[asset_a][idx_a][start_a:],
                'y':    closes[asset_a][idx_a][start_a:]
            })
            df_b = pd.DataFrame({
                'time': times[asset_b][idx_b][start_b:],
                'x':    closes[asset_b][idx_b][start_b:]
            })

            # Merge inner
            df_merged = pd.merge(df_a, df_b, on='time', how='inner').sort_values('time')
            df_merged = df_merged.dropna()

            y = df_merged['y'].values
            x = df_merged['x'].values

            if len(y) < CANDLE_SEED_MINIMUM: continue

            # Cointegration & Z-Score
            p_val, beta, z_score, _ = _calculate_spread_stats(y, x, logger)
            
            # ── MATH GUARD CHECK: Skip if stats failed ───────────────────
            if p_val is None:
                continue
            
            # Confidence & Regime
            confidence = max(0.0, 1.0 - p_val)
            is_coint = p_val <= COINT_P_LIMIT
            
            if is_coint:
                # ENTRY LOGIC: Only if not already open
                if not existing_b_id:
                    if z_score > z_entry_dynamic:
                        # Short Spread: Sell A, Buy B
                        action_a, action_b = "ENTER_SHORT", "ENTER_LONG"
                        signal_a, signal_b = -1.0, 1.0
                    elif z_score < -z_entry_dynamic:
                        # Long Spread: Buy A, Sell B
                        action_a, action_b = "ENTER_LONG", "ENTER_SHORT"
                        signal_a, signal_b = 1.0, -1.0
                
                # EXIT LOGIC: Only if already open
                if existing_b_id:
                    # 1. Mean Reversion
                    if abs(z_score) < z_exit_dynamic: 
                        action_a, action_b = "CLOSE", "CLOSE"
                        signal_a, signal_b = 0.0, 0.0
                        exit_reason = "MEAN_REVERSION"
                    
                    # 2. CATASTROPHIC STOP: Z-Score Hard Stop
                    elif abs(z_score) >= Z_HARD_STOP:
                        action_a, action_b = "CLOSE", "CLOSE"
                        signal_a, signal_b = 0.0, 0.0
                        exit_reason = "Z_HARD_STOP"
                        logger.warning(
                            f"WARNING — [Catastrophic Z-Score Stop] {asset_a}/{asset_b} "
                            f"deviated beyond {Z_HARD_STOP} sigma ({z_score:.2f}). Liquidating."
                        )

                    # 3. CATASTROPHIC STOP: Time Stop (24 Hours)
                    else:
                        now = time.time()
                        meta = active_baskets[existing_b_id]
                        age_hours = (now - meta["start_time"]) / 3600.0
                        if age_hours >= 24.0:
                            action_a, action_b = "CLOSE", "CLOSE"
                            signal_a, signal_b = 0.0, 0.0
                            exit_reason = "TIME_STOP"
                            logger.warning(
                                f"WARNING — [Time Stop] Basket {existing_b_id} failed to revert within 24 hours "
                                f"({age_hours:.1f}h). Liquidating to free margin."
                            )
                            # Let the close signal go through, but we'll clean up active_baskets when we emit
            else:
                # ── ARCHITECT UPGRADE: Breakdown State Check ──
                if existing_b_id:
                    action_a, action_b = "CLOSE", "CLOSE"
                    signal_a, signal_b = 0.0, 0.0
                    exit_reason = "COINT_BREAKDOWN"
                    logger.warning(f"WARNING — [Cointegration Breakdown] {asset_a}/{asset_b} Diverging. Liquidating.")

            if action_a:
                # Use existing_b_id if it exists (CLOSE), otherwise generate new (ENTER)
                b_id = existing_b_id if existing_b_id else f"BSKT_{asset_a}_{asset_b}_{int(time.time()/60)}"
                atr_a = _vectorized_atr(highs[asset_a][idx_a][start_a:], lows[asset_a][idx_a][start_a:], closes[asset_a][idx_a][start_a:])
                atr_b = _vectorized_atr(highs[asset_b][idx_b][start_b:], lows[asset_b][idx_b][start_b:], closes[asset_b][idx_b][start_b:])
                
                # Leg A
                sig_a = Signal(
                    symbol=asset_a, signal=signal_a, confidence=confidence, regime="COINTEGRATED" if is_coint else "DIVERGING",
                    action=action_a, atr=atr_a, z_score=z_score, ofi_ok=True, timestamp=time.time(),
                    basket_id=b_id, beta=1.0, leg_role="PRIMARY",
                    hedge_symbol=asset_b, hedge_beta=beta, exit_reason=exit_reason
                )
                # Leg B
                sig_b = Signal(
                    symbol=asset_b, signal=signal_b, confidence=confidence, regime="COINTEGRATED" if is_coint else "DIVERGING",
                    action=action_b, atr=atr_b, z_score=z_score, ofi_ok=True, timestamp=time.time(),
                    basket_id=b_id, beta=beta, leg_role="HEDGE",
                    hedge_symbol=asset_a, hedge_beta=1.0, exit_reason=exit_reason
                )
                
                try:
                    signal_queue.put(sig_a, timeout=1.0)
                    signal_queue.put(sig_b, timeout=1.0)
                    
                    # ── ARCHITECT UPDATE: Atomic State Cleanup ──
                    if action_a == "CLOSE":
                        active_baskets.pop(existing_b_id, None)
                    else:
                        # New Entry
                        active_baskets[b_id] = {"start_time": time.time(), "symbols": {asset_a, asset_b}}

                    if action_a != "CLOSE":
                        logger.info(f"[{asset_a}-{asset_b}] Pair Alert: Z={z_score:.2f} | p={p_val:.4f} | Beta={beta:.2f}")
                except queue.Full:
                    logger.error(f"Signal queue FULL: basket {b_id} signals {asset_a}/{asset_b} dropped. ExecRouter backlog detected.")
                    # IMPORTANT: Only pop the tracked state on queue failure if it was an ENTER action.
                    # If.it was a CLOSE action, we WANT to retain it so AlphaEngine keeps trying to close the position.
                    if action_a != "CLOSE":
                        active_baskets.pop(b_id, None)
                except Exception as exc:
                    logger.error(f"Signal queue error: {exc}")

            # ── Dashboard Signals Pipeline ───────────────────────────────────
            try:
                conn = sqlite3.connect("file:logs/trades.db", uri=True, timeout=1.0)
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO signals (timestamp, symbol, regime, z_score, beta, confidence)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (time.time(), f"{asset_a}/{asset_b}", "COINT" if is_coint else "DIV", z_score, beta, confidence))
                conn.commit()
                conn.close()
            except Exception:
                pass

        # 3. DB Logging & Interval Control
        elapsed = time.perf_counter() - t_start
        if elapsed < EVAL_INTERVAL:
            time.sleep(EVAL_INTERVAL - elapsed)

    shm.close()
    mt5.shutdown()
    logger.info("AlphaEngine exited cleanly.")
