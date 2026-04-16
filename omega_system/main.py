import asyncio
import logging
import signal
import sys

from omega_system.adapters.mt5_client import MT5Client
from omega_system.core.message_bus import MessageBus
from omega_system.engines.state_engine import StateEngine
from omega_system.engines.data_engine import DataEngine
from omega_system.engines.alpha_engine import AlphaEngine
from omega_system.engines.market_state_engine import MarketStateEngine
from omega_system.engines.risk_engine import RiskEngine
from omega_system.engines.execution_engine import ExecutionEngine
from omega_system.engines.recon_engine import ReconciliationEngine
from omega_system.engines.watchdog_engine import WatchdogEngine
from omega_system.engines.shadow_engine import ShadowEngine

# 8. Logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("Supervisor")

async def main():
    logger.info("Initializing Omega System V2...")

    # 1. Graceful Exit Hook
    def signal_handler(sig, frame):
        logger.info('System termination signal received. Exiting safely...')
        asyncio.create_task(shutdown())
        
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 2. Initialization
    mt5 = MT5Client()
    await mt5.initialize()
    bus = MessageBus()
    state = StateEngine()
    
    # 3. Serialized Memory Rescue Sequence (Avoid Amnesia)
    await state.boot_restore()

    # 4. Dependency Injection
    data_engine = DataEngine(mt5, bus, state)
    market_state_engine = MarketStateEngine(bus)
    alpha_engine = AlphaEngine(bus)
    risk_engine = RiskEngine(bus, state)
    exec_engine = ExecutionEngine(state, bus, mt5)
    recon_engine = ReconciliationEngine(state, mt5)
    watchdog_engine = WatchdogEngine(mt5, state, bus)
    shadow_engine = ShadowEngine(bus)

    # Mount UI WS Server
    from omega_system.adapters.api_server import APIServer, app
    import uvicorn
    api_server = APIServer(state, bus)
    app.state_engine = state
    app.message_bus = bus
    app.server_logic = api_server
    
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="warning")
    server = uvicorn.Server(config)

    # 5. The Async Loop
    logger.info("Starting up microservice cluster...")
    tasks = [
        asyncio.create_task(data_engine.start_worker(), name="DataTask"),
        asyncio.create_task(market_state_engine.start_worker(), name="MarketStateTask"),
        asyncio.create_task(alpha_engine.start_worker(), name="AlphaTask"),
        asyncio.create_task(risk_engine.start_worker(), name="RiskTask"),
        asyncio.create_task(exec_engine.start_worker(), name="ExecTask"),
        asyncio.create_task(recon_engine.start_worker(), name="ReconTask"),
        asyncio.create_task(watchdog_engine.start_worker(), name="WatchdogTask"),
        asyncio.create_task(shadow_engine.start_worker(), name="ShadowTask"),
        asyncio.create_task(state._garbage_collector_loop(), name="StateGCTask"),
        asyncio.create_task(api_server.stream_loop(), name="APIStreamTask"),
        asyncio.create_task(server.serve(), name="UvicornServerTask")
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
