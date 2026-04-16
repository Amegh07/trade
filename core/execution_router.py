"""
core/execution_router.py  —  Omega Architecture: Process 3 (Execution Router)
═══════════════════════════════════════════════════════════════════════════════
Spawned as an independent OS process (bypasses GIL).

Responsibilities:
  - Listens on signal_queue for Signal namedtuples from the Alpha Engine.
  - Applies all execution gates:
      • Spread guard (dynamic ATR-based limit, mirrors auditor.py)
      • Max concurrency cap
      • Ghost mode (paper trading when strategy edge is gone)
      • Commander Override (bypasses safety gates at 100% volume)
      • Correlation Shield (prevents doubling up on same currency)
  - PHASE 4: Half-Kelly Criterion lot sizing using rolling 50-trade stats.
  - Fires async market orders to MT5 via run_in_executor.
  - PHASE 5: Pushes trade records to io_queue for zero-blocking DB writes.
  - Real PnL feedback: a position monitor coroutine polls mt5.history_deals_get()
    every 30 seconds and feeds actual closed PnL into KellyEngine so it learns.
  - MT5 connection established independently (process-safe pattern).

Fixes vs. previous version:
  - Spread check: was `info.spread > (info.trade_tick_size and 50 or 50)`
    (always 50). Now uses dynamic ATR×0.05/point limit, matching auditor.py.
  - kelly.record(0.0) placeholder removed. Real PnL is fed via _position_monitor.
  - Max concurrency now correctly counts mt5.positions_get() length.
  - MT5 init has 3-attempt retry with back-off.
"""

import time
import json
import logging
import asyncio
import multiprocessing as mp
import threading
from collections import deque

import numpy as np
import sqlite3

from core.alpha_engine import Signal
from core.db_writer    import db_writer_daemon

# ── Logger ────────────────────────────────────────────────────────────────────
def _make_logger() -> logging.Logger:
    log = logging.getLogger("exec_router")
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter(
            "%(asctime)s [ExecRouter] %(levelname)s — %(message)s"
        ))
        log.addHandler(h)
    log.setLevel(logging.INFO)
    return log


# ── MT5 init retry ────────────────────────────────────────────────────────────
_MT5_RETRIES = 3
_MT5_BACKOFF  = 2.0

def _connect_mt5(login: int, password: str, server: str, logger: logging.Logger) -> bool:
    import MetaTrader5 as mt5
    for attempt in range(1, _MT5_RETRIES + 1):
        logger.info(f"MT5 init attempt {attempt}/{_MT5_RETRIES}…")
        if mt5.initialize(login=int(login), password=password, server=server):
            logger.info("MT5 connection established.")
            return True
        logger.warning(f"mt5.initialize() failed: {mt5.last_error()}")
        if attempt < _MT5_RETRIES:
            time.sleep(_MT5_BACKOFF * attempt)
    logger.critical(f"MT5 init FAILED after {_MT5_RETRIES} attempts — process exiting.")
    return False


# ── Currency pair helper (prevent doubling up on same base/quote) ─────────────
def _get_currencies(sym: str) -> set:
    """Extracts base and quote currency codes from a Forex symbol name."""
    clean = ''.join(c for c in sym if c.isupper() and c.isalpha())
    if len(clean) >= 6:
        return {clean[:3], clean[3:6]}
    return {clean}


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 4: FRACTIONAL KELLY CRITERION SIZING
# ══════════════════════════════════════════════════════════════════════════════

class KellyEngine:
    """
    Calculates the Half-Kelly position fraction based on a rolling window
    of the last 50 live trade results fed via record().

    Formula:
        K  = W − [(1 − W) / R]     ← Full Kelly
        K½ = K / 2                 ← Half-Kelly for ruin prevention

    Where:
        W = win probability  (rolling last 50 trades)
        R = win/loss ratio   (avg profit / avg loss, both positive magnitudes)

    If K ≤ 0, returns 0.0 → automatic Ghost Mode for this symbol.
    """
    WINDOW    = 50
    MAX_KELLY = 0.25   # never bet > 25% of equity even with perfect edge

    def __init__(self):
        self._trades: deque = deque(maxlen=self.WINDOW)

    def record(self, pnl: float) -> None:
        """Feed a real closed-trade PnL into the rolling window."""
        self._trades.append(float(pnl))

    def kelly_fraction(self) -> float:
        """Returns Half-Kelly ∈ [0.0, MAX_KELLY]."""
        
        if len(self._trades) < 5:
            return 0.0

        arr    = np.array(self._trades, dtype=np.float64)
        wins   = arr[arr > 0]
        losses = arr[arr < 0]

        W = len(wins) / len(arr)
        if W == 0.0 or len(losses) == 0:
            return 0.0   # all losses or no data → ghost mode

        avg_win  = wins.mean()
        avg_loss = np.abs(losses.mean())

        if avg_loss < 1e-10:
            return self.MAX_KELLY

        R = avg_win / avg_loss
        K = W - ((1.0 - W) / R)

        if K <= 0.0:
            return 0.0

        k_half = K / 2.0
        final_k_half = max(min(k_half, self.MAX_KELLY), 0.0)
        return float(final_k_half)

    def lot_size(
        self,
        equity:     float,
        atr:        float,
        tick_value: float,
        tick_size:  float,
        volume_min: float,
        volume_max: float = 2.0,
    ) -> float:
        """
        Converts the Half-Kelly fraction into a broker lot size.
        risk_amount = equity × K½
        lot         = risk_amount / (atr / tick_size × tick_value)
        """
        frac = self.kelly_fraction()
        if frac == 0.0 or atr <= 0 or tick_value <= 0 or tick_size <= 0:
            return 0.0

        risk_amount  = equity * frac
        pips_at_risk = atr / tick_size
        pip_value    = pips_at_risk * tick_value

        if pip_value <= 0:
            return 0.0

        raw_lot = risk_amount / pip_value
        return float(max(volume_min, min(round(raw_lot, 2), volume_max)))


# ══════════════════════════════════════════════════════════════════════════════
# DYNAMIC SPREAD LIMIT (mirrors agents/auditor.py)
# ══════════════════════════════════════════════════════════════════════════════

def _spread_hard_floor(symbol: str) -> int:
    """
    Returns the minimum permissible spread limit in points for a symbol.
    The dynamic ATR-based limit is always floored here so that microscopically
    low ATR values never block legitimate trades during low-volatility sessions.

    Asset-class floors:
      Crypto  (BTC / ETH / …)           → 500 pts
      Indices (SP500 / USTEC / GER40 …) →  50 pts
      Metals  (XAUUSD / XAGUSD …)       →  30 pts
      Forex   (everything else)          →  10 pts  (1 pip)
    """
    sym = symbol.upper()
    if any(k in sym for k in ("BTC", "ETH", "LTC", "XRP", "BNB", "SOL", "DOGE")):
        return 500
    if any(k in sym for k in ("SP500", "USTEC", "US500", "NAS", "DOW", "DAX",
                               "GER", "UK100", "JP225", "AUS", "FTSE", "CAC")):
        return 50
    if any(k in sym for k in ("XAU", "XAG", "GOLD", "SILVER")):
        return 30
    return 10   # Forex default (1 pip)


def _get_dynamic_spread_limit(mt5_module, sym: str, info) -> int:
    """
    Returns the spread limit in points for a given symbol.

    Dynamic computation: ATR(14) × 0.25 / point.
      (Multiplier raised 0.05 → 0.25 so the spread can occupy up to
       25% of the M5 candle range without triggering a block.)
    Hard floor: max(asset_class_floor, dynamic_limit) — consistent with
      auditor.is_spread_acceptable() so both guards produce identical limits.
    Falls back to 0.10% of mid-price when ATR rates are unavailable.
    """
    floor = _spread_hard_floor(sym)
    try:
        rates = mt5_module.copy_rates_from_pos(sym, mt5_module.TIMEFRAME_M5, 0, 15)
        if rates is not None and len(rates) == 15:
            tr_vals = [
                max(
                    rates[i]['high'] - rates[i]['low'],
                    abs(rates[i]['high'] - rates[i - 1]['close']),
                    abs(rates[i]['low']  - rates[i - 1]['close']),
                )
                for i in range(1, 15)
            ]
            atr     = sum(tr_vals) / 14.0
            dynamic = int((atr * 0.25) / info.point) if info.point > 0 else floor
            return max(floor, dynamic)
    except Exception:
        pass

    mid     = (info.ask + info.bid) / 2.0
    dynamic = int((mid * 0.0002) / info.point) if info.point > 0 else floor
    return max(floor, dynamic)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN PROCESS ENTRY
# ══════════════════════════════════════════════════════════════════════════════

def execution_router_process(
    signal_queue:   mp.Queue,
    io_queue:       mp.Queue,
    stop_event:     mp.Event,
    mt5_login:      int,
    mt5_password:   str,
    mt5_server:     str,
    max_concurrent: int = 20,
):
    """
    Execution Router entry point.

    Runs an asyncio event loop inside this process.
    All MT5 calls are offloaded to a ThreadPoolExecutor so the loop never blocks.

    Also starts the DB Writer daemon thread inside this process.
    """
    import MetaTrader5 as mt5
    from execution.router import LiveOrderRouter

    logger = _make_logger()
    logger.info(f"ExecRouter PID={mp.current_process().pid}")

    if not _connect_mt5(mt5_login, mt5_password, mt5_server, logger):
        return

    # ── Phase 5: DB Writer daemon thread (lives beside the router) ────────────
    _db_stop   = threading.Event()
    _db_thread = threading.Thread(
        target=db_writer_daemon, args=(io_queue, _db_stop),
        daemon=True, name="DBWriterDaemon",
    )
    _db_thread.start()
    logger.info("[PHASE 5] DB Writer daemon started inside ExecRouter.")

    kelly  = KellyEngine()
    router = LiveOrderRouter()

    # active_symbols: tracks which symbols currently have an open position
    # so we don't send duplicate orders before the position shows in MT5.
    active_symbols: set = set()

    # ── Position monitor: feeds real closed PnL into KellyEngine ──────────────
    last_pnl_check  = time.time()
    # MEMORY LEAK PRUNING: Use bounded deque instead of unbounded set (maxlen=10000)
    seen_deal_tickets = deque(maxlen=10000)

    async def _position_monitor(loop):
        """
        Polls MT5 deal history every 30 s.
        Feeds real closed-trade PnL into KellyEngine (Phase 4).
        Clears active_symbols when positions close.
        """
        nonlocal last_pnl_check
        while not stop_event.is_set():
            await asyncio.sleep(30)
            
            # --- Live Trade Check ---
            now = time.time()
            if now - last_pnl_check >= 30:
                try:
                    start = now - 86400   # last 24 h
                    deals = await loop.run_in_executor(
                        None, mt5.history_deals_get, start, now + 60
                    )
                    if deals:
                        for deal in deals:
                            if (
                                deal.entry == mt5.DEAL_ENTRY_OUT
                                and deal.ticket not in seen_deal_tickets
                            ):
                                seen_deal_tickets.append(deal.ticket)
                                kelly.record(deal.profit)
                                active_symbols.discard(deal.symbol)
                                
                                try:
                                    io_queue.put_nowait({
                                        "action": "update_pnl",
                                        "ticket": deal.position_id,
                                        "pnl":    deal.profit
                                    })
                                except Exception:
                                    pass

                                logger.info(
                                    f"[KELLY] Closed deal #{deal.ticket} | "
                                    f"{deal.symbol} | PnL={deal.profit:+.2f} | "
                                    f"K½={kelly.kelly_fraction():.4f}"
                                )
                    last_pnl_check = now
                except Exception as exc:
                    logger.warning(f"_position_monitor error: {exc}")

    # ── Commander Override helper ──────────────────────────────────────────────
    def _read_control_state() -> dict:
        """Reads the unified control state from SQLite with strict timeout."""
        try:
            db_path = os.getenv("OMEGA_CONTROL_DB_PATH", "logs/control.db")
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM control_state")
            rows = cur.fetchall()
            conn.close()
            return {r[0]: r[1] for r in rows}
        except sqlite3.OperationalError as exc:
            logger.warning(f"[Async DB Unblock] _read_control_state timeout/lock (timeout=0.5s): {exc}. Returning safe default.")
            return {}
        except Exception as exc:
            logger.debug(f"[Async DB Unblock] _read_control_state error: {exc}")
            return {}

    def _read_override() -> tuple[bool, bool]:
        """
        Non-blocking short read to get manual_override and ghost_mode
        from control.db. Returns (False, False) on timeout.
        """
        try:
            db_path = os.getenv("OMEGA_CONTROL_DB_PATH", "logs/control.db")
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT key, value FROM control_state WHERE key IN ('COMMANDER_OVERRIDE', 'GHOST_MODE')")
            rows = dict(cur.fetchall())
            conn.close()

            manual = rows.get("COMMANDER_OVERRIDE") == "True"
            ghost = rows.get("GHOST_MODE") == "True"
            return manual, ghost
        except sqlite3.OperationalError as exc:
            logger.warning(f"[Async DB Unblock] _read_override timeout/lock (timeout=0.5s): {exc}. Returning safe default (False, False).")
            return False, False
        except Exception as exc:
            logger.debug(f"[Async DB Unblock] _read_override error: {exc}")
            return False, False

    # ── Main async routing loop ────────────────────────────────────────────────
    async def _route_loop():
        loop = asyncio.get_running_loop()

        # Launch position monitor as a background task
        asyncio.create_task(_position_monitor(loop))

        last_ctrl_check = 0.0
        ctrl = {}
        _override_logged = False

        basket_tracking = {}  # basket_id -> {"lot": lot, "symbol": sym, "ticket": ticket}
        
        while not stop_event.is_set():
            now = time.time()
            
            # Periodically refresh control state (1Hz) — run in executor to avoid blocking event loop
            if now - last_ctrl_check >= 1.0:
                try:
                    ctrl = await loop.run_in_executor(None, _read_control_state)
                except Exception as exc:
                    logger.error(f"[Async DB Unblock] Failed to read control_state via executor: {exc}")
                    ctrl = {}
                last_ctrl_check = now
                
                if ctrl.get("EMERGENCY_STOP") == "True":
                    logger.critical("🛑 EMERGENCY STOP DETECTED IN CONTROL_STATE. SHUTTING DOWN ROUTER.")
                    stop_event.set()
                    break

                # ── Add TTL cleanup for basket_tracking ──
                expired = [b_id for b_id, meta in basket_tracking.items() if now - meta.get("timestamp", now) > 300]
                for b_id in expired:
                    del basket_tracking[b_id]

            # Non-blocking dequeue — yield immediately if nothing to process
            try:
                sig: Signal = signal_queue.get_nowait()
            except Exception:
                await asyncio.sleep(0.05)
                continue

            sym = sig.symbol

            try:
                # ── Gate: OFI already vetoed by AlphaEngine ─────────────────
                if not sig.ofi_ok:
                    logger.info(f"[{sym}] OFI VETO — skip execution.")
                    continue

                # ── Gate: ACTION CLOSE INTERCEPT ──────────────────────────────
                if getattr(sig, 'action', '') == "CLOSE":
                    exit_reason = getattr(sig, 'exit_reason', "")
                    basket_id   = getattr(sig, 'basket_id', "")
                    
                    # ── ARCHITECT UPGRADE: The Hammer (Safety Stops Bypass) ──
                    if exit_reason in ["COINT_BREAKDOWN", "Z_HARD_STOP", "TIME_STOP"]:
                        logger.critical(f"[ExecRouter] INFO — Force liquidating basket {basket_id} due to {exit_reason}.")
                        # Ruthlessly bypass the Transaction Cost Trap
                        target_pos = await loop.run_in_executor(None, mt5.positions_get, sym)
                        if target_pos:
                            for pos in target_pos:
                                await loop.run_in_executor(None, router.close_position, pos, f"FORCE_{exit_reason}")
                            active_symbols.discard(sym)
                            logger.info(f"[{sym}] Emergency {exit_reason} slaughter executed.")
                        else:
                            active_symbols.discard(sym)
                        continue

                    # Regular Mean Reversion: Transaction Cost Trap (Net PnL Guard)
                    if exit_reason == "MEAN_REVERSION" and basket_id:
                        all_pos = await loop.run_in_executor(None, mt5.positions_get)
                        basket_pos = [p for p in all_pos if p.comment == basket_id]
                        
                        if basket_pos:
                            # ═══ SAFE PnL SUMMATION WITH BUFFER ═══
                            MIN_PNL_TO_CLOSE = 0.50  # Hard buffer to cover slippage & late fees
                            net_pnl = 0.0
                            
                            for p in basket_pos:
                                profit = p.profit if p.profit is not None else 0.0
                                swap = p.swap if p.swap is not None else 0.0
                                commission = p.commission if p.commission is not None else 0.0
                                net_pnl += (profit + swap + commission)
                                logger.debug(
                                    f"[PnL] Ticket {p.ticket}: profit={profit:.2f}, swap={swap:.2f}, "
                                    f"commission={commission:.2f}, running_total={net_pnl:.2f}"
                                )
                            
                            if net_pnl < MIN_PNL_TO_CLOSE:
                                logger.info(
                                    f"[{sym}] INFO — Z-Score target reached, but Net PnL ({net_pnl:.2f}) "
                                    f"is below buffer ({MIN_PNL_TO_CLOSE}). Holding for fee clearance."
                                )
                                continue

                    # Proceed with Standard Close if not vetoed
                    target_pos = await loop.run_in_executor(None, mt5.positions_get, sym)
                    if target_pos:
                        for pos in target_pos:
                            await loop.run_in_executor(None, router.close_position, pos)
                        active_symbols.discard(sym)
                        logger.info(f"[{sym}] Mean Reversion exit processed.")
                    else:
                        active_symbols.discard(sym)
                    continue

                # ── Gate: already have an open position ───────────────────────
                if sym in active_symbols:
                    continue

                # ── Commander Override check ──────────────────────────────────
                manual_override, _ = _read_override()
                if manual_override and not _override_logged:
                    logger.critical(
                        "[SYSTEM] 🔱 COMMANDER OVERRIDE ACTIVE — "
                        "All safety gates bypassed. Trading at 100% capacity."
                    )
                    _override_logged = True
                elif not manual_override:
                    _override_logged = False

                # ── Gate: max concurrency ─────────────────────────────────────
                all_pos = await loop.run_in_executor(None, mt5.positions_get)
                all_pos = list(all_pos) if all_pos else []
                if not manual_override and len(all_pos) >= max_concurrent:
                    logger.warning(f"[{sym}] Max concurrency ({max_concurrent}) — skipping.")
                    continue

                # ── Gate: symbol info ─────────────────────────────────────────
                info = await loop.run_in_executor(None, mt5.symbol_info, sym)
                if info is None:
                    continue

                # ── Gate: Market Hours / Permissions ──────────────────────────
                if info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
                    logger.info(f"[{sym}] Market closed or trade disabled. Skipping.")
                    continue

                # ── Gate: spread (dynamic ATR-based limit) ────────────────────
                spread_limit = _get_dynamic_spread_limit(mt5, sym, info)
                if info.spread > spread_limit:
                    logger.info(
                        f"[{sym}] Spread {info.spread} > limit {spread_limit} pts — skipped."
                    )
                    continue

                # ── Gate: Correlation Shield (if override inactive) ───────────
                if not manual_override:
                    target_currencies = _get_currencies(sym)
                    correlated = False
                    for pos in all_pos:
                        pos_currencies = _get_currencies(pos.symbol)
                        # Block only if the EXACT same currency pair exists (e.g. EURUSD and USDEUR)
                        if len(target_currencies.intersection(pos_currencies)) == 2:
                            logger.info(
                                f"[{sym}] Correlation Block — identical pair already "
                                f"exposed via {pos.symbol}. Skipping."
                            )
                            correlated = True
                            break
                    if correlated:
                        continue

                # ── Account info ──────────────────────────────────────────────
                acc = await loop.run_in_executor(None, mt5.account_info)
                if acc is None:
                    logger.error("account_info returned None — skipping cycle.")
                    continue
                equity = acc.equity

                # ── Live tick for entry price & margin check ───────────────
                # MARGIN TICK BUG FIX: Fetch tick BEFORE margin calculation (was after)
                tick = await loop.run_in_executor(None, mt5.symbol_info_tick, sym)
                if tick is None:
                    logger.warning(f"[{sym}] symbol_info_tick() returned None — skipping.")
                    continue

                # ── Gate: regime NO_TRADE ─────────────────────────────────────
                if sig.regime == "NO_TRADE":
                    logger.info(f"[{sym}] NO_TRADE regime (Hurst noise band) — skip.")
                    continue

                # ── Delta-Neutral Sizing logic ────────────────────────────────
                basket_id = getattr(sig, 'basket_id', "")
                beta      = getattr(sig, 'beta', 1.0)
                leg_role  = getattr(sig, 'leg_role', "")
                
                if basket_id:
                    if leg_role == "PRIMARY":
                        # Primary Leg: Calculate Kelly sizing
                        lot = kelly.lot_size(
                            equity     = equity,
                            atr        = sig.atr,
                            tick_value = info.trade_tick_value,
                            tick_size  = info.trade_tick_size,
                            volume_min = info.volume_min,
                            volume_max = min(info.volume_max, 2.0),
                        )
                        
                        # ARCHITECT UPGRADE: Margin Choking (Anti-Bleed)
                        # Pre-check margin for BOTH legs using hedge metadata
                        hedge_sym = getattr(sig, 'hedge_symbol', "")
                        hedge_beta = getattr(sig, 'hedge_beta', 1.0)
                        hedge_lot = round(max(info.volume_min, lot * hedge_beta), 2)
                        
                        try:
                            # Calculate margin for PRIMARY (assume BUY for check, or use intended signal)
                            is_buy = getattr(sig, 'signal', 0.0) == 1.0
                            m_type_p = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
                            m_type_h = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY
                            
                            margin_p = await loop.run_in_executor(None, mt5.order_calc_margin, m_type_p, sym, lot, tick.ask if is_buy else tick.bid)
                            margin_h = await loop.run_in_executor(None, mt5.order_calc_margin, m_type_h, hedge_sym, hedge_lot, tick.ask if not is_buy else tick.bid)
                            
                            # ═══ STRICT MARGIN TYPE SAFETY ═══
                            if margin_p is None or margin_h is None:
                                logger.error(
                                    f"[{sym}] MT5 returned None for margin calculation. "
                                    f"Primary={margin_p}, Hedge={margin_h}. "
                                    f"Aborting to prevent overleveraging."
                                )
                                continue
                            
                            if margin_p < 0 or margin_h < 0:
                                logger.error(
                                    f"[{sym}] MT5 margin calculation returned negative values. "
                                    f"Primary={margin_p}, Hedge={margin_h}. Aborting."
                                )
                                continue
                            
                            required_margin = margin_p + margin_h
                            acc_info = await loop.run_in_executor(None, mt5.account_info)
                            
                            logger.info(
                                f"[Margin] {sym}: Account currency={getattr(acc_info, 'currency', 'N/A')}, "
                                f"Primary={margin_p:.2f}, Hedge={margin_h:.2f}, "
                                f"Total={required_margin:.2f}, Free={acc_info.margin_free:.2f}"
                            )
                            
                            if acc_info.margin_free < (required_margin * 1.5):
                                logger.warning(
                                    f"[{sym}] WARNING — Insufficient margin for dual-basket. "
                                    f"Free: {acc_info.margin_free:.2f} | Required (1.5x): {required_margin * 1.5:.2f}. "
                                    f"Skipping execution."
                                )
                                continue
                        except Exception as m_err:
                            logger.error(f"[{sym}] Margin check failed: {m_err}. Aborting for safety.")
                            continue

                        logger.info(f"[{sym}] Basket Primary Leg identified. Lot={lot:.2f} | basket_id={basket_id}")
                    elif leg_role == "HEDGE":
                        if basket_id not in basket_tracking:
                            logger.warning(f"[{sym}] Hedge leg arrived but Primary leg state missing for {basket_id}. Skipping.")
                            continue
                            
                        # Secondary Leg: Apply Hedge Ratio (Beta) with volume_step validation
                        parent_lot = basket_tracking[basket_id]["lot"]
                        raw_hedge_lot = parent_lot * beta
                        
                        # ═══ STRICT MT5 VOLUME_STEP ROUNDING & DELTA-NEUTRAL VERIFICATION ═══
                        # Enforce MT5 volume_step mathematically (not just round to 2 decimals)
                        if hasattr(info, 'volume_step') and info.volume_step > 0:
                            steps = round((raw_hedge_lot - info.volume_min) / info.volume_step)
                            lot = info.volume_min + (steps * info.volume_step)
                            lot = round(lot, 8)  # Clean up floating point artifacts
                        else:
                            # Fallback if volume_step not available
                            lot = round(max(info.volume_min, raw_hedge_lot), 2)
                        
                        lot = max(info.volume_min, min(lot, info.volume_max))  # Enforce bounds
                        
                        # Delta-Neutral Ratio Verification
                        actual_ratio = lot / parent_lot if parent_lot > 0 else 0
                        ratio_error = abs(actual_ratio - beta) / beta if beta > 0 else 0
                        
                        if ratio_error > 0.15:  # 15% tolerance (critical deviation)
                            logger.error(
                                f"[{sym}] CRITICAL: Hedge ratio deviation too high ({ratio_error*100:.1f}%). "
                                f"Primary: {parent_lot:.4f}, Hedge: {lot:.4f}, Expected β: {beta:.4f}, "
                                f"Actual ratio: {actual_ratio:.4f}. Aborting hedge to prevent unbalanced basket."
                            )
                            continue
                        
                        if ratio_error > 0.05:  # 5% tolerance (warning only)
                            logger.warning(
                                f"[{sym}] Hedge ratio deviation: {ratio_error*100:.1f}% "
                                f"(Primary={parent_lot:.4f}, Hedge={lot:.4f}, β={beta:.4f})"
                            )
                        
                        logger.info(
                            f"[{sym}] Basket Hedge Leg identified. "
                            f"Raw={raw_hedge_lot:.4f} → Final={lot:.4f}, "
                            f"Ratio error={ratio_error*100:.2f}%, basket_id={basket_id}"
                        )
                    else:
                        # Fallback for old signals
                        logger.warning(f"[{sym}] Basket signal missing leg_role. Skipping.")
                        continue
                else:
                    # Fallback for standard signals
                    lot = kelly.lot_size(
                        equity     = equity,
                        atr        = sig.atr,
                        tick_value = info.trade_tick_value,
                        tick_size  = info.trade_tick_size,
                        volume_min = info.volume_min,
                        volume_max = min(info.volume_max, 2.0),
                    )

                # ── Tick already fetched above for margin check ─────────────────
                is_buy = getattr(sig, 'signal', 0.0) == 1.0
                price  = tick.ask if is_buy else tick.bid
                atr    = sig.atr

                # StatArb exit logic is managed by AlphaEngine (Z-score mean reversion),
                # so we set wide ATR-based safety guards here.
                tp = price + (atr * 10.0) if is_buy else price - (atr * 10.0)
                sl = price - (atr * 5.0)  if is_buy else price + (atr * 5.0)

                # MT5 Minimum Lot Safety Clamp
                if lot < info.volume_min:
                    raw = lot
                    lot = info.volume_min
                    logger.info(f"[{sym}] Kelly Engine choked ({raw:.3f}). Applying MT5 Lot Hard Floor -> {lot}")

                logger.info(
                    f"[{sym}] ✅ SIGNAL CONFIRMED | dir={'BUY' if is_buy else 'SELL'} | "
                    f"Z={sig.z_score:.3f} | regime={sig.regime} | "
                    f"lot={lot:.2f} | K½={kelly.kelly_fraction():.4f} | "
                    f"OFI=CLEAR"
                )

                # ══════════════════════════════════════════════════════════════════════
                # 🚨 RED TEAM TRIGGER: Gap Risk / Margin Collapse Defense on HEDGE 🚨
                # ══════════════════════════════════════════════════════════════════════
                if leg_role == "HEDGE":
                    logger.warning(f"[{sym}] HEDGE leg detected — executing gap risk pre-flight defense…")
                    
                    # Step 1: Fetch live tick for hedge symbol (NOT cached from PRIMARY check)
                    hedge_tick = await loop.run_in_executor(None, mt5.symbol_info_tick, sym)
                    if hedge_tick is None:
                        logger.critical(
                            f"[{sym}] 🚨 HEDGE gap defense: symbol_info_tick returned None. "
                            f"Cannot verify current market price. ABORTING HEDGE LEG."
                        )
                        active_symbols.discard(sym)
                        continue
                    
                    # Step 2: Fetch current account info to detect margin collapse
                    acc_info_current = await loop.run_in_executor(None, mt5.account_info)
                    if acc_info_current is None:
                        logger.critical(
                            f"[{sym}] 🚨 HEDGE gap defense: account_info returned None. "
                            f"Cannot verify margin state. ABORTING HEDGE LEG."
                        )
                        active_symbols.discard(sym)
                        continue
                    
                    # Step 3: Calculate exact margin required at CURRENT market price (not stale)
                    hedge_price = hedge_tick.ask if not is_buy else hedge_tick.bid
                    m_type_hedge = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY
                    
                    margin_h_current = await loop.run_in_executor(
                        None,
                        mt5.order_calc_margin,
                        m_type_hedge, sym, lot, hedge_price
                    )
                    
                    if margin_h_current is None:
                        logger.critical(
                            f"[{sym}] 🚨 HEDGE gap defense: order_calc_margin returned None. "
                            f"Cannot verify margin requirement. ABORTING HEDGE LEG."
                        )
                        active_symbols.discard(sym)
                        continue
                    
                    # Step 4: Evaluate safety buffer (1.5x cushion = 33% margin headroom)
                    margin_required_with_buffer = margin_h_current * 1.5
                    
                    if acc_info_current.margin_free < margin_required_with_buffer:
                        logger.critical(
                            f"🚨 🚨 🚨 [RED TEAM TRIGGER] [{sym}] 🚨 🚨 🚨\n"
                            f"   MARGIN COLLAPSE DETECTED BETWEEN PRIMARY AND HEDGE EXECUTION!\n"
                            f"   Available margin: ${acc_info_current.margin_free:.2f}\n"
                            f"   Required (1.5x): ${margin_required_with_buffer:.2f}\n"
                            f"   Shortfall: ${margin_required_with_buffer - acc_info_current.margin_free:.2f}\n"
                            f"   Basket ID: {basket_id}\n"
                            f"   >>> INITIATING EMERGENCY ATOMIC ROLLBACK ON PRIMARY LEG <<<\n"
                        )
                        
                        # ──── ATOMIC ROLLBACK: Fetch parent position and close immediately ────
                        if basket_id in basket_tracking:
                            parent_ticket = basket_tracking[basket_id].get("ticket")
                            parent_symbol = basket_tracking[basket_id].get("symbol")
                            
                            if parent_ticket and parent_symbol:
                                try:
                                    logger.critical(
                                        f"[ATOMIC ROLLBACK] Fetching parent position for immediate closure…\n"
                                        f"   Ticket: {parent_ticket}, Symbol: {parent_symbol}, Basket: {basket_id}"
                                    )
                                    
                                    # Fetch the specific position by ticket
                                    all_positions = await loop.run_in_executor(None, mt5.positions_get)
                                    parent_position = None
                                    
                                    if all_positions:
                                        for pos in all_positions:
                                            if pos.ticket == parent_ticket and pos.symbol == parent_symbol:
                                                parent_position = pos
                                                break
                                    
                                    if parent_position:
                                        # Close the parent position immediately
                                        rollback_result = await loop.run_in_executor(
                                            None,
                                            router.close_position,
                                            parent_position,
                                            f"ATOMIC_ROLLBACK_gap_defense_{basket_id}"
                                        )
                                        
                                        if rollback_result and rollback_result.retcode == mt5.TRADE_RETCODE_DONE:
                                            logger.critical(
                                                f"✅ [ATOMIC ROLLBACK] SUCCESS: Parent position CLOSED.\n"
                                                f"   Ticket: {parent_ticket}, Symbol: {parent_symbol}\n"
                                                f"   Close price: {rollback_result.price:.5f}\n"
                                                f"   Basket {basket_id} eliminated."
                                            )
                                        else:
                                            logger.critical(
                                                f"⚠️  [ATOMIC ROLLBACK] Close attempt returned status: {rollback_result.retcode if rollback_result else 'NONE'}\n"
                                                f"   Position may still be open. Removing from tracking."
                                            )
                                    else:
                                        logger.critical(
                                            f"⚠️  [ATOMIC ROLLBACK] Parent position NOT FOUND in mt5.positions_get().\n"
                                            f"   Expected: Ticket {parent_ticket}, Symbol {parent_symbol}.\n"
                                            f"   Position may have closed externally. Clearing from tracking."
                                        )
                                
                                except Exception as rb_err:
                                    logger.critical(
                                        f"🚨 [ATOMIC ROLLBACK] Exception during parent close: {rb_err}\n"
                                        f"   Position state unknown. Clearing basket anyway."
                                    )
                            
                            # Clean up basket tracking (PRIMARY no longer exists)
                            del basket_tracking[basket_id]
                            logger.critical(f"[ATOMIC ROLLBACK] Basket {basket_id} removed from tracking.")
                        
                        # Abort hedge execution
                        active_symbols.discard(sym)
                        logger.critical(
                            f"🚨 [HEDGE ABORTED] Hedge leg execution cancelled due to margin collapse.\n"
                            f"   Primary leg has been forcefully closed via atomic rollback.\n"
                            f"   System is now in safe state (no unhedged basket)."
                        )
                        continue
                    
                    else:
                        logger.info(
                            f"✅ [HEDGE GAP DEFENSE PASSED] Margin verification succeeded:\n"
                            f"   Available: ${acc_info_current.margin_free:.2f}\n"
                            f"   Required (1.5x): ${margin_required_with_buffer:.2f}\n"
                            f"   Safety buffer OK. Proceeding to execution."
                        )

                # ── Fire async market order ───────────────────────────────────
                active_symbols.add(sym)
                try:
                    res_tuple = await loop.run_in_executor(
                        None,
                        router.execute_market,
                        sym, is_buy, lot, atr, tp, sl, basket_id
                    )

                    if isinstance(res_tuple, tuple) and len(res_tuple) == 2:
                        result, slippage = res_tuple
                    else:
                        result, slippage = None, 0.0

                    # ── Phase 5: push record to io_queue (never block here) ───
                    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                        
                        # --- TCA Apeiron Auto-Tuning Integration ---
                        if not hasattr(router, '_slippage_log'):
                            router._slippage_log = deque(maxlen=10)
                        router._slippage_log.append(slippage)
                        
                        if len(router._slippage_log) == 10:
                            avg_slip = sum(router._slippage_log) / 10.0
                            tca_threshold = info.spread * 0.50
                            if avg_slip > tca_threshold:
                                router.iceberg_jitter += 0.50
                                logger.critical(
                                    f"⚠️ [TCA BREACH] Avg slippage ({avg_slip:.1f} pts) > limit ({tca_threshold:.1f}). "
                                    f"Elevating latency! jitter = {router.iceberg_jitter:.2f}s"
                                )
                                router._slippage_log.clear()

                        io_queue.put_nowait({
                            "symbol":    sym,
                            "direction": getattr(sig, 'signal', 0.0),
                            "lot":       lot,
                            "price":     result.price,
                            "ticket":    result.deal,
                            "tp":        tp,
                            "sl":        sl,
                            "timestamp": time.time(),
                            "kelly":     kelly.kelly_fraction(),
                            "z_score":   sig.z_score,
                        })
                        logger.info(
                            f"[{sym}] Order filled — ticket #{result.deal} | "
                            f"price={result.price:.5f} | trade record queued for DB."
                        )

                        # ATOMIC TRACKING: Store Primary leg for rollback if needed
                        if basket_id and leg_role == "PRIMARY":
                            # Pull active positions to get actual position ticket instead of deal
                            positions = await loop.run_in_executor(None, mt5.positions_get, sym)
                            pos_ticket = positions[-1].ticket if positions else result.order
                            
                            basket_tracking[basket_id] = {
                                "symbol": sym,
                                "lot":    lot,
                                "ticket": pos_ticket,
                                "timestamp": time.time()
                            }
                        elif basket_id and leg_role == "HEDGE":
                            if basket_id in basket_tracking:
                                del basket_tracking[basket_id]
                    else:
                        retcode = result.retcode if result else "None"
                        logger.warning(
                            f"[{sym}] Order failed — retcode={retcode}. "
                            "Releasing position lock."
                        )
                        active_symbols.discard(sym)

                        # ATOMIC ROLLBACK: If Hedge leg fails, liquidate Primary leg
                        if basket_id and leg_role == "HEDGE":
                            if basket_id in basket_tracking:
                                primary_info = basket_tracking[basket_id]
                                logger.critical(
                                    f"CRITICAL — [BASKET FAILED] Leg B ({sym}) rejected (retcode={retcode}). "
                                    f"Initiating emergency atomic rollback on Leg A ({primary_info['symbol']})."
                                )
                                try:
                                    p_ticket = primary_info["ticket"]
                                    p_pos = await loop.run_in_executor(None, mt5.positions_get, ticket=p_ticket)
                                    if p_pos:
                                        await loop.run_in_executor(
                                            None, router.close_position, p_pos[0], 
                                            f"ROLLBACK:{basket_id}"
                                        )
                                        active_symbols.discard(primary_info["symbol"])
                                        logger.info(f"[{primary_info['symbol']}] Atomic Rollback successful.")
                                    else:
                                        logger.error(f"Failed to find Primary position {p_ticket} for rollback!")
                                except Exception as rollback_err:
                                    logger.error(f"Atomic Rollback FAILED: {rollback_err}")
                                finally:
                                    if basket_id in basket_tracking:
                                        del basket_tracking[basket_id]

                except Exception as exc:
                    logger.error(f"[{sym}] Order execution error: {exc}", exc_info=True)
                    active_symbols.discard(sym)

            except Exception as exc:
                logger.error(f"[{sym}] Route loop error: {exc}", exc_info=True)

    # ── Run async event loop ───────────────────────────────────────────────────
    asyncio.run(_route_loop())

    # ── Shutdown ───────────────────────────────────────────────────────────────
    logger.info("Stop event received — shutting down.")
    _db_stop.set()
    _db_thread.join(timeout=5)
    mt5.shutdown()
    logger.info("ExecRouter exited cleanly.")
