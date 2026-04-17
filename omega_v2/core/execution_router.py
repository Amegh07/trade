import asyncio
import logging
import MetaTrader5 as mt5
from config import settings

logger = logging.getLogger("ExecutionRouter")

class ExecutionRouter:
    def __init__(self, signal_queue: asyncio.Queue):
        self.queue = signal_queue

        # ── Session-level risk state ────────────────────────────────────────────
        # Captured on first account_info call; used for drawdown gate.
        self._session_equity_start: float | None = None

        # Counts consecutive losing baskets. Reset to 0 on any winning basket.
        # Only incremented once per basket (on the HEDGE leg close).
        self._consecutive_losses: int = 0

        # Stores the account equity snapshot taken at the start of each basket
        # close sequence (PRIMARY leg). Consumed when the HEDGE leg settles.
        # Key: basket_id str — value: equity float
        self._pre_close_equity: dict = {}

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

        # 1. C4 FIX: Guard account_info against None (MT5 disconnect)
        account_info = await loop.run_in_executor(None, mt5.account_info)
        if account_info is None:
            logger.error("MT5 account_info returned None — connection lost. Skipping order.")
            return

        # Capture session equity baseline on first successful call
        if self._session_equity_start is None:
            self._session_equity_start = account_info.equity
            logger.info(f"[Risk] Session equity baseline set: {self._session_equity_start:.2f}")

        # ── Session drawdown gate ───────────────────────────────────────────────
        # Applies to ALL actions (entries AND exits) — a blown session must halt everything
        # except the user can still ManualClose via MT5 terminal.
        session_dd = (self._session_equity_start - account_info.equity) / self._session_equity_start
        if session_dd >= settings.MAX_SESSION_DRAWDOWN:
            logger.critical(
                f"[Risk] SESSION DRAWDOWN LIMIT BREACHED: {session_dd:.2%} >= "
                f"{settings.MAX_SESSION_DRAWDOWN:.2%} ({account_info.equity:.2f} equity). "
                f"All orders rejected."
            )
            return

        # ── Consecutive losses gate ─────────────────────────────────────────────
        # Only blocks new ENTRIES. CLOSE signals always pass through so existing
        # positions can still be exited cleanly.
        if sig.action != "CLOSE" and self._consecutive_losses >= settings.MAX_CONSECUTIVE_LOSSES:
            logger.critical(
                f"[Risk] {self._consecutive_losses} consecutive losing baskets. "
                f"New entries blocked until manual reset or a winning basket."
            )
            return

        # ── Margin floor ────────────────────────────────────────────────────────
        if account_info.margin_free < 200.0:
            logger.critical(
                f"[Risk] Margin floor breached ({account_info.margin_free:.2f} free). Rejecting order."
            )
            return

        # 2. Symbol visibility check
        symbol_info = await loop.run_in_executor(None, mt5.symbol_info, sig.symbol)
        if not symbol_info or not symbol_info.visible:
            logger.error(f"{sig.symbol} not visible in Market Watch.")
            return

        # 3. CLOSE action — snapshot equity on PRIMARY so we can measure basket PnL on HEDGE
        if sig.action == "CLOSE":
            if getattr(sig, 'role', '') == "PRIMARY":
                self._pre_close_equity[sig.basket_id] = account_info.equity
            await self._close_positions(sig, loop)
            return

        # 4. ENTER action — open a new position
        order_type = mt5.ORDER_TYPE_BUY if "LONG" in sig.action else mt5.ORDER_TYPE_SELL

        # C3 FIX: wrap symbol_info_tick in run_in_executor — never call MT5 directly on event loop
        tick = await loop.run_in_executor(None, mt5.symbol_info_tick, sig.symbol)
        if tick is None:
            logger.error(f"Could not fetch tick for {sig.symbol}. Skipping order.")
            return
        price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid

        # Dynamic volume using Kelly fraction from account equity
        kelly_volume = round(max(0.01, account_info.equity * settings.KELLY_MAX / 100_000), 2)

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sig.symbol,
            "volume": kelly_volume,
            "type": order_type,
            "price": price,
            "deviation": 10,
            "magic": 999,
            "comment": sig.basket_id,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        result = await loop.run_in_executor(None, mt5.order_send, request)
        if result is None:
            logger.error(f"order_send returned None for {sig.symbol} — {mt5.last_error()}")
        elif result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"Order failed for {sig.symbol}: {result.comment} [retcode={result.retcode}]")
        else:
            logger.info(f"Order executed: {sig.symbol} {sig.action} | {kelly_volume} lots @ {price}")

    async def _close_positions(self, sig, loop):
        """Close all open positions for the given symbol belonging to this basket.

        BUG-V2-04 FIX:
          - Primary filter: comment == basket_id AND magic == 999
          - Fallback filter: magic == 999 ONLY (NOT all positions on symbol)
            The original code fell back to closing ALL positions on the symbol if the
            comment was stripped by the broker. On a live account this would close
            positions opened by other strategies or manual trades.
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
                f"[CLOSE] Basket comment not matched for {sig.symbol}. "
                f"Falling back to {len(basket_positions)} magic=999 position(s)."
            )

        closed_count = 0
        for pos in basket_positions:
            close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
            tick = await loop.run_in_executor(None, mt5.symbol_info_tick, sig.symbol)
            if tick is None:
                logger.error(f"[CLOSE] Could not fetch tick for {sig.symbol}. Skipping ticket {pos.ticket}.")
                continue
            price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask

            close_request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": sig.symbol,
                "volume": pos.volume,
                "type": close_type,
                "position": pos.ticket,
                "price": price,
                "deviation": 10,
                "magic": 999,
                "comment": f"close_{sig.basket_id}",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            result = await loop.run_in_executor(None, mt5.order_send, close_request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                err = result.comment if result else mt5.last_error()
                logger.error(f"[CLOSE] Failed to close {sig.symbol} ticket {pos.ticket}: {err}")
            else:
                logger.info(f"[CLOSE] Closed {sig.symbol} ticket {pos.ticket} @ {price}")
                closed_count += 1

        # ── Consecutive loss tracking ────────────────────────────────────────────
        # Only runs on the HEDGE leg (last signal in the basket close sequence).
        # At this point both PRIMARY and HEDGE legs have been closed, so the full
        # basket PnL is reflected in account equity.
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
                    f"[Risk] No pre-close equity snapshot for basket {sig.basket_id}. "
                    f"PnL tracking skipped (likely recovered from crash)."
                )
