import asyncio
import logging
import MetaTrader5 as mt5
from config import settings

logger = logging.getLogger("ExecutionRouter")


def _resolve_filling(symbol_info) -> int:
    """Resolve the correct MT5 filling mode for a given symbol.

    symbol_info.filling_mode is a bitmask of what the broker supports:
      bit 1 (value 2) -> ORDER_FILLING_IOC allowed
      bit 0 (value 1) -> ORDER_FILLING_FOK allowed
      neither set     -> only ORDER_FILLING_RETURN available

    Broker-side retcode 10030 ("Unsupported filling mode") is thrown when the
    order uses a filling type the instrument doesn't support. AUDJPY and many
    JPY crosses only allow FOK or RETURN — not IOC — so a hard-coded IOC was
    the direct cause of the AUDJPY execution failure.
    """
    mode = getattr(symbol_info, 'filling_mode', 0)
    if mode & 2:
        return mt5.ORDER_FILLING_IOC
    if mode & 1:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


class ExecutionRouter:
    def __init__(self, signal_queue: asyncio.Queue):
        self.queue = signal_queue

        # ── Session-level risk state ────────────────────────────────────────────
        self._session_equity_start: float | None = None
        self._daily_equity_start: float | None = None
        self._consecutive_losses: int = 0
        self._pre_close_equity: dict = {}

        import datetime
        self._current_day: datetime.date = datetime.date.today()

        # ── Half-open basket guard ──────────────────────────────────────────────
        # When a PRIMARY entry leg fails, its basket_id is added here.
        # The HEDGE signal (next on the queue for the same basket) is then aborted.
        self._failed_baskets: set[str] = set()

        # ── Pair Performance Tracking ───────────────────────────────────────────
        self._pair_stats: dict = {}   # "EURUSD/GBPUSD": {"trades": 0, "wins": 0}
        self._banned_pairs: set[str] = set()

    async def run(self):
        """Listens for AI signals and executes them."""
        logger.info("Execution Router online.")
        while True:
            signal = await self.queue.get()
            try:
                await self._execute(signal)
            except Exception as e:
                logger.error(f"Execution failed for {getattr(signal, 'symbol', '?')}: {e}")
            finally:
                self.queue.task_done()

    async def _execute(self, sig):
        loop = asyncio.get_running_loop()

        # ── Half-open basket guard — skip HEDGE if PRIMARY entry failed ─────────
        if getattr(sig, 'role', '') == 'HEDGE' and sig.action != 'CLOSE':
            if sig.basket_id in self._failed_baskets:
                logger.error(
                    f"[{sig.basket_id}] PRIMARY leg FAILED — aborting HEDGE on "
                    f"{sig.symbol} to prevent a naked directional position."
                )
                self._failed_baskets.discard(sig.basket_id)
                return

        # Guard account_info against None (MT5 disconnect)
        account_info = await loop.run_in_executor(None, mt5.account_info)
        if account_info is None:
            logger.error("MT5 account_info returned None — connection lost. Skipping order.")
            return

        # Capture session/daily equity baselines
        import datetime
        today = datetime.date.today()
        if self._current_day != today:
            self._current_day = today
            self._daily_equity_start = account_info.equity
            logger.info(f"[Risk] New day, Daily loss limit reset. Baseline: {account_info.equity:.2f}")

        if self._session_equity_start is None:
            self._session_equity_start = account_info.equity
            logger.info(f"[Risk] Session equity baseline set: {self._session_equity_start:.2f}")
        if self._daily_equity_start is None:
            self._daily_equity_start = account_info.equity

        # ── Drawdown gates (Daily + Session) ────────────────────────────────────
        session_dd = (self._session_equity_start - account_info.equity) / self._session_equity_start
        if session_dd >= settings.MAX_SESSION_DRAWDOWN:
            logger.critical(
                f"[Risk] [SKIP: RISK LIMIT HIT] SESSION DRAWDOWN BREACHED: {session_dd:.2%} >= "
                f"{settings.MAX_SESSION_DRAWDOWN:.2%} ({account_info.equity:.2f} equity)."
            )
            return

        daily_dd = (self._daily_equity_start - account_info.equity) / self._daily_equity_start
        if daily_dd >= settings.MAX_DAILY_LOSS:
            logger.critical(
                f"[Risk] [SKIP: RISK LIMIT HIT] DAILY LOSS BREACHED: {daily_dd:.2%} >= "
                f"{settings.MAX_DAILY_LOSS:.2%} ({account_info.equity:.2f} equity)."
            )
            return

        # ── Consecutive losses gate — ENTRY only ────────────────────────────────
        if sig.action != "CLOSE" and self._consecutive_losses >= settings.MAX_CONSECUTIVE_LOSSES:
            logger.critical(
                f"[Risk] [SKIP: RISK LIMIT HIT] {self._consecutive_losses} consecutive losing baskets."
            )
            return

        # ── Margin floor ────────────────────────────────────────────────────────
        if account_info.margin_free < 200.0:
            logger.critical(
                f"[Risk] Margin floor breached ({account_info.margin_free:.2f} free). Rejecting order."
            )
            return

        # Symbol visibility check (also needed to resolve filling mode)
        symbol_info = await loop.run_in_executor(None, mt5.symbol_info, sig.symbol)
        if not symbol_info or not symbol_info.visible:
            logger.error(f"[SKIP: SYMBOL NOT TRADABLE] {sig.symbol} not visible in Market Watch.")
            if getattr(sig, 'role', '') == 'PRIMARY' and sig.action != 'CLOSE':
                self._failed_baskets.add(sig.basket_id)
            return

        # Extract pair ID from basket for performance tracking (BSKT_EURUSD_GBPUSD_...)
        basket_parts = sig.basket_id.split('_')
        pair_id = f"{basket_parts[1]}/{basket_parts[2]}" if len(basket_parts) >= 3 else "UNKNOWN"

        # Auto-disable weak pair check
        if sig.action != "CLOSE" and pair_id in self._banned_pairs:
            logger.warning(f"[{sig.basket_id}] [SKIP: PAIR BANNED] {pair_id} is disabled due to poor performance.")
            if getattr(sig, 'role', '') == 'PRIMARY':
                self._failed_baskets.add(sig.basket_id)
            return

        # ── CLOSE action ────────────────────────────────────────────────────────
        if sig.action == "CLOSE":
            if getattr(sig, 'role', '') == "PRIMARY":
                self._pre_close_equity[sig.basket_id] = account_info.equity
            # Pass symbol_info so _close_positions can resolve filling without
            # an extra MT5 call per position
            await self._close_positions(sig, loop, symbol_info)
            return

        # ── ENTER action ────────────────────────────────────────────────────────
        order_type = mt5.ORDER_TYPE_BUY if "LONG" in sig.action else mt5.ORDER_TYPE_SELL

        # Resolve broker-supported filling mode for this specific symbol
        filling = _resolve_filling(symbol_info)

        tick = await loop.run_in_executor(None, mt5.symbol_info_tick, sig.symbol)
        if tick is None:
            logger.error(f"Could not fetch tick for {sig.symbol}. Skipping order.")
            if getattr(sig, 'role', '') == 'PRIMARY':
                self._failed_baskets.add(sig.basket_id)
            return
        price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid

        # ── Dynamic Spread / Cost Filter (Edge > Cost gate) ──────────────────
        # AlphaEngine pre-computes dual-leg round-trip cost (both symbols + slippage).
        # We do a final spot-check using the live tick and veto if cost spiked since
        # AlphaEngine ran (which can happen if there's a news event between the two).
        #
        # Decision logic:
        #  signal_divergence   = Chronos predicted spread move (%)
        #  round_trip_cost     = (cost_a + cost_b + slippage) x 2 (from AlphaEngine)
        #  spot_round_trip     = live re-check of THIS symbol's bid-ask (sanity check)
        #  effective_cost      = max(round_trip_cost, spot_round_trip) = most conservative
        #  edge_multiple       = divergence / effective_cost
        #  Required edge_multiple >= MIN_COST_MULTIPLE (default 3.0)
        signal_divergence = float(getattr(sig, 'divergence',      0.0))
        pre_round_trip    = float(getattr(sig, 'round_trip_cost', 0.0))
        pre_edge_multiple = float(getattr(sig, 'edge_multiple',   0.0))

        live_bid_ask    = float(tick.ask - tick.bid)
        live_cost_pct   = live_bid_ask / float(tick.ask) if tick.ask > 0 else 0.0
        live_round_trip = live_cost_pct * 2.0

        # Use the larger of the pre-computed cost (both legs) and the live spot check
        effective_cost  = max(pre_round_trip, live_round_trip)
        effective_edge  = signal_divergence / max(effective_cost, 1e-9)
        min_edge        = settings.MIN_COST_MULTIPLE

        # Full edge breakdown — logged before every execution decision
        logger.info(
            f"[{sig.basket_id}] [Edge check] | {sig.symbol} | "
            f"divergence={signal_divergence:.4%} | "
            f"pre_cost={pre_round_trip:.4%} | live_cost={live_round_trip:.4%} | "
            f"effective_cost={effective_cost:.4%} | "
            f"edge={effective_edge:.2f}x (required {min_edge:.1f}x)"
        )

        if effective_edge < min_edge:
            logger.warning(
                f"[{sig.basket_id}] [SKIP: INSUFFICIENT EDGE] "
                f"edge={effective_edge:.2f}x < required {min_edge:.1f}x. "
                f"Expected move ({signal_divergence:.4%}) does not cover "
                f"execution costs ({effective_cost:.4%}). Trade skipped."
            )
            if getattr(sig, 'role', '') == 'PRIMARY':
                self._failed_baskets.add(sig.basket_id)
            return

        # ── ATR-Based Dynamic Position Sizing ─────────────────────────────────
        # Risk ATR_RISK_PCT% of equity per ATR unit of movement on this leg.
        # High ATR -> smaller position (more volatile, more risk per pip).
        # Low ATR  -> larger position (quiet market, cleaner signal).
        # Volume clamped to [0.01, 100] lots.

        signal_atr = float(getattr(sig, 'atr', 0.0))
        if signal_atr > 1e-9:
            risk_budget  = account_info.equity * settings.ATR_RISK_PCT
            dollar_atr   = signal_atr * 100_000          # approx $ moved per lot per ATR
            kelly_volume = float(round(max(0.01, min(risk_budget / dollar_atr, 100.0)), 2))
            logger.debug(
                f"[{sig.basket_id}] ATR sizing: ATR={signal_atr:.5f} | "
                f"risk=${risk_budget:.0f} | volume={kelly_volume} lots"
            )
        else:
            # Fallback: Kelly fraction of equity (no ATR metadata — e.g. recovery sessions)
            kelly_volume = float(round(max(0.01, account_info.equity * settings.KELLY_MAX / 100_000), 2))

        # STRICT TYPE CASTING: MT5's C-extension rejects numpy types and raises
        # (-2, 'Unnamed arguments not allowed'). Every value is cast to a native
        # Python type. comment is clipped to MT5's hard 31-char limit.
        # Lambda wrapper bypasses the asyncio executor argument-unpacking bug
        # where passing a dict positionally fails on some MT5/Windows versions.
        req = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       str(sig.symbol),
            "volume":       float(kelly_volume),
            "type":         int(order_type),
            "price":        float(price),
            "deviation":    int(10),
            "magic":        int(999),
            "comment":      str(sig.basket_id)[:31],
            "type_time":    mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        def _send(): return mt5.order_send(req)
        result = await loop.run_in_executor(None, _send)

        if result is None:
            logger.error(f"order_send returned None for {sig.symbol} — {mt5.last_error()}")
            if getattr(sig, 'role', '') == 'PRIMARY':
                self._failed_baskets.add(sig.basket_id)
        elif result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(
                f"Order FAILED for {sig.symbol}: {result.comment} [retcode={result.retcode}] "
                f"(filling_mode={symbol_info.filling_mode}, used={filling})"
            )
            if getattr(sig, 'role', '') == 'PRIMARY':
                self._failed_baskets.add(sig.basket_id)
        else:
            logger.info(
                f"[OK] {sig.symbol} {sig.action} | {kelly_volume} lots @ {price:.5f} | "
                f"basket={sig.basket_id}"
            )

    async def _close_positions(self, sig, loop, symbol_info):
        """Close all open positions for the given symbol belonging to this basket.

        Primary filter: comment == basket_id AND magic == 999.
        Fallback filter: magic == 999 ONLY — never touches foreign positions.
        """
        positions = await loop.run_in_executor(None, mt5.positions_get, sig.symbol)
        if not positions:
            logger.warning(f"[CLOSE] No open positions found for {sig.symbol}.")
            return

        # Primary: exact basket comment + our magic number
        basket_positions = [p for p in positions if p.comment == sig.basket_id and p.magic == 999]
        if not basket_positions:
            # Fallback: only our magic number — never touch foreign positions
            basket_positions = [p for p in positions if p.magic == 999]
            if not basket_positions:
                logger.error(
                    f"[CLOSE] No positions with magic=999 found for {sig.symbol} "
                    f"(basket: {sig.basket_id}). Aborting close."
                )
                return
            logger.warning(
                f"[CLOSE] Comment not matched for {sig.symbol}. "
                f"Falling back to {len(basket_positions)} magic=999 position(s)."
            )

        filling = _resolve_filling(symbol_info)
        closed_count = 0

        for pos in basket_positions:
            close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
            tick = await loop.run_in_executor(None, mt5.symbol_info_tick, sig.symbol)
            if tick is None:
                logger.error(f"[CLOSE] Could not fetch tick for {sig.symbol}. Skipping ticket {pos.ticket}.")
                continue
            price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask

            close_req = {
                "action":       mt5.TRADE_ACTION_DEAL,
                "symbol":       str(sig.symbol),
                "volume":       float(pos.volume),
                "type":         int(close_type),
                "position":     int(pos.ticket),
                "price":        float(price),
                "deviation":    int(10),
                "magic":        int(999),
                "comment":      f"close_{sig.basket_id}"[:31],
                "type_time":    mt5.ORDER_TIME_GTC,
                "type_filling": filling,
            }
            def _close(): return mt5.order_send(close_req)
            result = await loop.run_in_executor(None, _close)

            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                err = result.comment if result else mt5.last_error()
                logger.error(f"[CLOSE] Failed on {sig.symbol} ticket {pos.ticket}: {err}")
            else:
                logger.info(f"[CLOSE] {sig.symbol} ticket {pos.ticket} closed @ {price:.5f}")
                closed_count += 1
        
        # ── Pair Performance Update (Fires on HEDGE close completion) ───────────
        if getattr(sig, 'role', '') == 'HEDGE' and sig.basket_id in self._pre_close_equity:
            pre_equity = self._pre_close_equity.pop(sig.basket_id)
            account_info_after = await loop.run_in_executor(None, mt5.account_info)
            if account_info_after:
                pnl = account_info_after.equity - pre_equity
                is_win = pnl > 0
                
                # Update consecutive loss sequence
                if is_win:
                    self._consecutive_losses = 0
                else:
                    self._consecutive_losses += 1
                
                # Track in pair analytics
                basket_parts = sig.basket_id.split('_')
                if len(basket_parts) >= 3:
                    pair_id = f"{basket_parts[1]}/{basket_parts[2]}"
                    if pair_id not in self._pair_stats:
                        self._pair_stats[pair_id] = {"trades": 0, "wins": 0}
                    
                    self._pair_stats[pair_id]["trades"] += 1
                    if is_win:
                        self._pair_stats[pair_id]["wins"] += 1
                    
                    trades = self._pair_stats[pair_id]["trades"]
                    win_rate = self._pair_stats[pair_id]["wins"] / trades
                    
                    logger.info(
                        f"[PNL] {pair_id} basket closed: ${pnl:.2f} "
                        f"(Win_Rate: {win_rate:.0%} over {trades} trades)"
                    )
                    
                    # Auto-disable (ban) toxic pairs
                    if trades >= settings.PAIR_MIN_TRADES and win_rate < settings.PAIR_MIN_WIN_RATE:
                        logger.critical(
                            f"[AUTO-BAN] {pair_id} disabled! Win rate "
                            f"{win_rate:.0%} falls below {settings.PAIR_MIN_WIN_RATE:.0%} "
                            f"threshold after {trades} trades."
                        )
                        self._banned_pairs.add(pair_id)

            else:
                logger.debug(
                    f"[Risk] No pre-close equity for basket {sig.basket_id}. "
                    f"PnL tracking skipped (likely recovered from crash)."
                )
