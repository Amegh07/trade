import asyncio
import logging
import MetaTrader5 as mt5
from config import settings
from core.alpha_engine import AlphaEngine
from core.execution_router import ExecutionRouter

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("omega.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("Supervisor")

async def main():
    logger.info("Initializing Omega v2...")

    # 1. Boot MT5 synchronously before event loop takes over
    if not mt5.initialize(login=settings.MT5_LOGIN, password=settings.MT5_PASSWORD, server=settings.MT5_SERVER):
        logger.critical(f"MT5 init failed. Error: {mt5.last_error()}")
        return
    logger.info(f"Connected to MT5 Account: {settings.MT5_LOGIN}")

    # 2. Setup Async Infrastructure
    signal_queue = asyncio.Queue()

    alpha = AlphaEngine(signal_queue)
    router = ExecutionRouter(signal_queue)

    # 3. Run Engines Concurrently with named tasks for clear crash diagnostics
    tasks = [
        asyncio.create_task(alpha.run(), name="AlphaEngine"),
        asyncio.create_task(router.run(), name="ExecutionRouter"),
    ]

    try:
        # BUG-NEW-02 FIX: Use return_exceptions=True so an unhandled crash in either
        # coroutine is captured as a value rather than re-raised, giving us the chance
        # to log a CRITICAL message naming the failed task before shutdown.
        # Without this fix, any non-CancelledError exception silently propagated through
        # asyncio.gather and killed the system with no useful log output.
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for task, result in zip(tasks, results):
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                logger.critical(
                    f"[{task.get_name()}] Crashed with unhandled exception: {result!r}",
                    exc_info=result
                )
    except asyncio.CancelledError:
        logger.info("Tasks cancelled. Shutting down.")
    finally:
        # Cancel any task that is still running (e.g. the survivor when one crashes)
        for task in tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        mt5.shutdown()
        logger.info("MT5 Shutdown complete.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Manual termination received.")
