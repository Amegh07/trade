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
        logging.FileHandler("omega.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("Supervisor")


async def main():
    logger.info("Initializing Omega v2...")

    # Boot MT5 synchronously before the event loop takes over
    if not mt5.initialize(login=settings.MT5_LOGIN, password=settings.MT5_PASSWORD, server=settings.MT5_SERVER):
        logger.critical(f"MT5 init failed. Error: {mt5.last_error()}")
        return
    logger.info(f"Connected to MT5 Account: {settings.MT5_LOGIN}")

    signal_queue = asyncio.Queue()
    alpha = AlphaEngine(signal_queue)
    router = ExecutionRouter(signal_queue)

    tasks: list[asyncio.Task] = []

    # ── Task crash handler ───────────────────────────────────────────────────
    # Fires as soon as any task finishes unexpectedly (exception or early return).
    # Logs the crash with full traceback, then cancels the surviving sibling so
    # asyncio.gather unblocks and the finally block can run cleanly.
    #
    # BUG-SELF-02 FIX: The previous approach used asyncio.gather(*tasks, return_exceptions=True).
    # That design HANGS FOREVER if one task crashes while the other (infinite loop) keeps
    # running — gather waits for all tasks to complete but the survivor never ends.
    # The correct approach is done_callbacks: crash is detected and logged immediately,
    # siblings are cancelled, and gather propagates the CancelledError to finally.
    def _on_task_done(task: asyncio.Task) -> None:
        if task.cancelled():
            return              # Normal shutdown — already logged by the CancelledError path
        exc = task.exception()
        if exc is not None:
            logger.critical(
                f"[{task.get_name()}] CRASHED — initiating emergency shutdown.",
                exc_info=exc
            )
            # Cancel every other task so asyncio.gather unblocks
            for t in tasks:
                if t is not task and not t.done():
                    t.cancel()

    tasks.extend([
        asyncio.create_task(alpha.run(), name="AlphaEngine"),
        asyncio.create_task(router.run(), name="ExecutionRouter"),
    ])
    for task in tasks:
        task.add_done_callback(_on_task_done)

    try:
        # No return_exceptions=True — exceptions propagate naturally.
        # If a task crashes, _on_task_done cancels siblings → CancelledError in gather.
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        # Ctrl+C or a task crash → siblings cancelled → lands here
        logger.info("Shutdown signal received. Stopping all tasks.")
    except Exception as e:
        # Safety net: any exception that wasn't caught by tasks themselves
        logger.critical(f"[Supervisor] Unhandled exception: {e!r}", exc_info=e)
    finally:
        # Ensure every task is fully stopped before calling mt5.shutdown()
        for task in tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        mt5.shutdown()
        logger.info("Omega v2 stopped cleanly.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass  # Ctrl+C logged inside main(); suppress Python's default traceback
