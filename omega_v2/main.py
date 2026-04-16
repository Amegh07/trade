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
    
    # 1. Boot MT5 synchronously
    if not mt5.initialize(login=settings.MT5_LOGIN, password=settings.MT5_PASSWORD, server=settings.MT5_SERVER):
        logger.critical(f"MT5 init failed. Error: {mt5.last_error()}")
        return
    logger.info(f"Connected to MT5 Account: {settings.MT5_LOGIN}")

    # 2. Setup Async Infrastructure
    signal_queue = asyncio.Queue()
    
    alpha = AlphaEngine(signal_queue)
    router = ExecutionRouter(signal_queue)

    # 3. Run Engines Concurrently
    tasks = [
        asyncio.create_task(alpha.run()),
        asyncio.create_task(router.run())
    ]

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Tasks cancelled. Shutting down.")
    finally:
        mt5.shutdown()
        logger.info("MT5 Shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Manual termination received.")
