import asyncio
import logging
import MetaTrader5 as mt5

logger = logging.getLogger("ExecutionRouter")

class ExecutionRouter:
    def __init__(self, signal_queue: asyncio.Queue):
        self.queue = signal_queue
        self.loop = asyncio.get_event_loop()

    async def run(self):
        """Listens for AI signals and executes them."""
        logger.info("Execution Router online.")
        while True:
            signal = await self.queue.get()
            try:
                await self._execute(signal)
            except Exception as e:
                logger.error(f"Execution failed: {e}")
            finally:
                self.queue.task_done()

    async def _execute(self, sig):
        # 1. Atomic Margin Check (The Gap Defense)
        account_info = await self.loop.run_in_executor(None, mt5.account_info)
        if account_info.margin_free < 200.0: # Failsafe threshold
            logger.critical("Margin Collapse Imminent. Rejecting order.")
            return

        # 2. Prepare Order
        symbol_info = await self.loop.run_in_executor(None, mt5.symbol_info, sig.symbol)
        if not symbol_info or not symbol_info.visible:
            logger.error(f"{sig.symbol} not visible in Market Watch.")
            return

        order_type = mt5.ORDER_TYPE_BUY if "LONG" in sig.action else mt5.ORDER_TYPE_SELL
        price = mt5.symbol_info_tick(sig.symbol).ask if order_type == mt5.ORDER_TYPE_BUY else mt5.symbol_info_tick(sig.symbol).bid
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sig.symbol,
            "volume": 0.01, # Default minimum size. Expand with your Kelly logic later.
            "type": order_type,
            "price": price,
            "deviation": 10,
            "magic": 999,
            "comment": sig.basket_id,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }

        # 3. Send Order async
        result = await self.loop.run_in_executor(None, mt5.order_send, request)
        if result is None:
            logger.error(f"Order send returned None for {sig.symbol} — market likely closed or symbol unavailable. MT5 error: {mt5.last_error()}")
        elif result.retcode != mt5.TRADE_RETCODE_DONE:
            logger.error(f"Order failed for {sig.symbol}: {result.comment} [retcode={result.retcode}]")
        else:
            logger.info(f"Order executed: {sig.symbol} {sig.action} at {price}")
