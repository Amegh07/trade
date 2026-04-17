import asyncio
import logging
import MetaTrader5 as mt5
from config import settings

logger = logging.getLogger("ExecutionRouter")


def _resolve_filling(symbol_info) -> int:
    """Resolve the correct MT5 filling mode for a given symbol.

    symbol_info.filling_mode is a bitmask of what the broker supports:
      bit 1 (value 2) → ORDER_FILLING_IOC allowed
      bit 0 (value 1) → ORDER_FILLING_FOK allowed
      neither set     → only ORDER_FILLING_RETURN available

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
        self._consecutive_losses: int = 0
        self._pre_close_equity: dict = {}

        # ── Half-open basket guard ──────────────────────────────────────────────
        # When a PRIMARY entry leg fails, its basket_id is added here.
        # The HEDGE signal (next on the queue for the same basket) is then aborted.
        # Without this guard, a failed PRIMARY leaves an unhedged HEDGE open,
        # creating a naked directional position — the opposite of stat-arb.
        # The basket_id is removed when the HEDGE is skipped or when a CLOSE fires.
        self._failed_baskets: set[str] = set()

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

        # Capture session equity baseline on first successful call
        if self._session_equity_start is None:
            self._session_equity_start = account_info.equity
            logger.info(f"[Risk] Session equity baseline set: {self._session_equity_start:.2f}")

        # ── Session drawdown gate ───────────────────────────────────────────────
        session_dd = (self._session_equity_start - account_info.equity) / self._session_equity_start
        if session_dd >= settings.MAX_SESSION_DRAWDOWN:
            logger.critical(
                f"[Risk] SESSION DRAWDOWN LIMIT BREACHED: {session_dd:.2%} >= "
                f"{settings.MAX_SESSION_DRAWDOWN:.2%} ({account_info.equity:.2f} equity). "
                f"All orders rejected."
            )
            return

        # ── Consecutive losses gate — ENTRY only ────────────────────────────────
        if sig.action != "CLOSE" and self._consecutive_losses >= settings.MAX_CONSECUTIVE_LOSSES:
            logger.critical(
                f"[Risk] {self._consecutive_losses} consecutive losing baskets. "
                f"New entries blocked until a winning basket resets the counter."
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
            logger.error(f"{sig.symbol} not visible in Market Watch.")
            # If the PRIMARY leg can't even be checked, abort the basket
            if getattr(sig, 'role', '') == 'PRIMARY' and sig.action != 'CLOSE':
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

        # ── Consecutive loss tracking — fires on HEDGE leg after full basket close
        if closed_count > 0 and getattr(sig, 'role', '') == "HEDGE":
            pre_equity = self._pre_close_equity.pop(sig.basket_id, None)
            if pre_equity is not None:
                post_info = await loop.run_in_executor(None, mt5.account_info)
                if post_info is not None:
                    pnl = post_info.equity - pre_equity
                    if pnl < 0:
                        self._consecutive_losses += 1
                        logger.warning(
                            f"[Risk] Basket {sig.basket_id} — LOSS: {pnl:.2f}. "
                            f"Consecutive losses: {self._consecutive_losses}/{settings.MAX_CONSECUTIVE_LOSSES}"
                        )
                    else:
                        self._consecutive_losses = 0
                        logger.info(
                            f"[Risk] Basket {sig.basket_id} — WIN: +{pnl:.2f}. "
                            f"Consecutive loss counter reset to 0."
                        )
            else:
                logger.debug(
                    f"[Risk] No pre-close equity for basket {sig.basket_id}. "
                    f"PnL tracking skipped (likely recovered from crash)."
                )
