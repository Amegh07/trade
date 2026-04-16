import asyncio
import logging
import MetaTrader5 as mt5

logger = logging.getLogger("ExecutionRouter")

class ExecutionRouter:
    def __init__(self, signal_queue: asyncio.Queue):
        self.queue = signal_queue

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
        loop = asyncio.get_running_loop()  # H2 FIX: use get_running_loop()

        # 1. C4 FIX: Guard account_info against None (MT5 disconnect)
        account_info = await loop.run_in_executor(None, mt5.account_info)
        if account_info is None:
            logger.error("MT5 account_info returned None — connection lost. Skipping order.")
            return
        if account_info.margin_free < 200.0:
            logger.critical(f"Margin Collapse Imminent ({account_info.margin_free:.2f} free). Rejecting order.")
            return

        # 2. Symbol visibility check
        symbol_info = await loop.run_in_executor(None, mt5.symbol_info, sig.symbol)
        if not symbol_info or not symbol_info.visible:
            logger.error(f"{sig.symbol} not visible in Market Watch.")
            return

        # 3. CLOSE action — find and close existing positions for this basket
        if sig.action == "CLOSE":
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
        from config import settings
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
        """Close all open positions for the given symbol belonging to this basket."""
        positions = await loop.run_in_executor(None, mt5.positions_get, sig.symbol)
        if not positions:
            logger.warning(f"[CLOSE] No open positions found for {sig.symbol}.")
            return

        # Filter to only positions belonging to this basket
        basket_positions = [p for p in positions if p.comment == sig.basket_id]
        if not basket_positions:
            # Fall back: close all positions on this symbol if basket comment not found
            basket_positions = list(positions)

        for pos in basket_positions:
            close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
            tick = await loop.run_in_executor(None, mt5.symbol_info_tick, sig.symbol)
            if tick is None:
                logger.error(f"[CLOSE] Could not fetch tick for {sig.symbol}.")
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
