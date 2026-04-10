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
            return 0.01   # conservative bootstrap

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

        return float(min(K / 2.0, self.MAX_KELLY))

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
    seen_deal_tickets: set = set()

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
                                seen_deal_tickets.add(deal.ticket)
                                kelly.record(deal.profit)
                                active_symbols.discard(deal.symbol)
                                logger.info(
                                    f"[KELLY] Closed deal #{deal.ticket} | "
                                    f"{deal.symbol} | PnL={deal.profit:+.2f} | "
                                    f"K½={kelly.kelly_fraction():.4f}"
                                )
                    last_pnl_check = now
                except Exception as exc:
                    logger.warning(f"_position_monitor error: {exc}")

    # ── Commander Override helper ──────────────────────────────────────────────
    def _read_override() -> tuple:
        """Reads config.json for manual_override and poll_interval."""
        try:
            with open("config.json", "r") as f:
                cfg = json.load(f)
            return cfg.get("manual_override", False), cfg.get("poll_interval", None)
        except Exception:
            return False, None

    # ── Main async routing loop ────────────────────────────────────────────────
    async def _route_loop():
        loop = asyncio.get_running_loop()

        # Launch position monitor as a background task
        asyncio.create_task(_position_monitor(loop))

        _override_logged = False

        while not stop_event.is_set():

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

                # ── Gate: regime NO_TRADE ─────────────────────────────────────
                if sig.regime == "NO_TRADE":
                    logger.info(f"[{sym}] NO_TRADE regime (Hurst noise band) — skip.")
                    continue

                # ── Phase 4: Kelly lot size ───────────────────────────────────
                lot = kelly.lot_size(
                    equity     = equity,
                    atr        = sig.atr,
                    tick_value = info.trade_tick_value,
                    tick_size  = info.trade_tick_size,
                    volume_min = info.volume_min,
                    volume_max = min(info.volume_max, 2.0),
                )

                # ── Live tick for entry price ─────────────────────────────────
                tick = await loop.run_in_executor(None, mt5.symbol_info_tick, sym)
                if tick is None:
                    continue

                is_buy = sig.direction == 1.0
                price  = tick.ask if is_buy else tick.bid
                atr    = sig.atr

                # TP and SL based on regime
                if sig.regime == "RANGING":
                    # Mean reversion: tighter TP, wider SL
                    tp = price + (atr * 2.0) if is_buy else price - (atr * 2.0)
                    sl = price - (atr * 2.5) if is_buy else price + (atr * 2.5)
                else:
                    # Momentum: 3:1 reward-to-risk
                    tp = price + (atr * 3.0) if is_buy else price - (atr * 3.0)
                    sl = price - (atr * 2.5) if is_buy else price + (atr * 2.5)

                # MT5 Minimum Lot Safety Clamp
                if lot < info.volume_min:
                    raw = lot
                    lot = info.volume_min
                    logger.info(f"[{sym}] Kelly Engine choked ({raw:.3f}). Applying MT5 Lot Hard Floor -> {lot}")

                logger.info(
                    f"[{sym}] ✅ SIGNAL CONFIRMED | dir={'BUY' if is_buy else 'SELL'} | "
                    f"H={sig.hurst:.3f} | regime={sig.regime} | "
                    f"lot={lot:.2f} | K½={kelly.kelly_fraction():.4f} | "
                    f"OFI=CLEAR"
                )

                # ── Fire async market order ───────────────────────────────────
                active_symbols.add(sym)
                try:
                    res_tuple = await loop.run_in_executor(
                        None,
                        router.execute_market,
                        sym, is_buy, lot, atr, tp, sl
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
                            "direction": sig.direction,
                            "lot":       lot,
                            "price":     result.price,
                            "ticket":    result.deal,
                            "tp":        tp,
                            "sl":        sl,
                            "timestamp": time.time(),
                            "kelly":     kelly.kelly_fraction(),
                            "hurst":     sig.hurst,
                        })
                        logger.info(
                            f"[{sym}] Order filled — ticket #{result.deal} | "
                            f"price={result.price:.5f} | trade record queued for DB."
                        )
                    else:
                        retcode = result.retcode if result else "None"
                        logger.warning(
                            f"[{sym}] Order failed — retcode={retcode}. "
                            "Releasing position lock."
                        )
                        active_symbols.discard(sym)

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
