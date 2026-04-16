import asyncio
import logging
import signal
import sys

from omega_system.adapters.mt5_client import MT5Client
from omega_system.core.message_bus import MessageBus
from omega_system.engines.state_engine import StateEngine
from omega_system.engines.data_engine import DataEngine
from omega_system.engines.alpha_engine import AlphaEngine
from omega_system.engines.risk_engine import RiskEngine
from omega_system.engines.execution_engine import ExecutionEngine
from omega_system.engines.recon_engine import ReconciliationEngine

# 8. Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("Supervisor")

async def main():
    logger.info("Initializing Omega System V2 Architecture...")

    # 3. Initialization Sequence
    mt5 = MT5Client()
    
    # Depending on the system, initialize might be async or sync. Assuming async per your prompt's "await mt5.initialize()"
    try:
        await mt5.initialize()
    except TypeError:
        # Fallback if mt5.initialize() turns out to be synchronous in the actual wrapper
        mt5.initialize()
        
    bus = MessageBus()
    state = StateEngine()

    # 4. Dependency Injection
    data_engine = DataEngine(mt5, bus)
    alpha_engine = AlphaEngine(bus)
    risk_engine = RiskEngine(bus, state)
    exec_engine = ExecutionEngine(state, bus, mt5)
    recon_engine = ReconciliationEngine(state, mt5)

    # 5. The Async Loop
    logger.info("Starting up microservice cluster...")
    tasks = [
        asyncio.create_task(data_engine.start_worker(), name="DataTask"),
        asyncio.create_task(alpha_engine.start_worker(), name="AlphaTask"),
        asyncio.create_task(risk_engine.start_worker(), name="RiskTask"),
        asyncio.create_task(exec_engine.start_worker(), name="ExecTask"),
        asyncio.create_task(recon_engine.start_worker(), name="ReconTask")
    ]

    # Signal handler for graceful shutdown
    loop = asyncio.get_running_loop()
    
    def handle_shutdown():
        logger.info("Shutting down Omega System...")
        for task in tasks:
            task.cancel()

    # Windows does not support add_signal_handler natively, so we implement safely
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, handle_shutdown)
            
    # 6. Await all tasks
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Fatal error in execution loop: {e}")
    finally:
        # 7. Graceful Shutdown mt5 cleanup
        logger.info("Clearing MT5 connection...")
        try:
            mt5.shutdown()
        except Exception:
            pass
        logger.info("Shutdown complete.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Fallback for Windows graceful termination
        logger.info("Shutting down Omega System...")
