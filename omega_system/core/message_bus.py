import asyncio
import logging
from typing import Any

logger = logging.getLogger("MessageBus")

class MessageBus:
    def __init__(self):
        # Async queues for inter-engine communication
        self.tick_queue = asyncio.Queue()            # DataEngine -> AlphaEngine / RiskEngine
        self.raw_signal_queue = asyncio.Queue()      # AlphaEngine -> RiskEngine
        self.approved_signal_queue = asyncio.Queue() # RiskEngine -> ExecutionEngine
        
    async def publish_signal(self, signal: Any) -> None:
        """Alpha Engine uses this to broadcast a signal."""
        try:
            # Replaces the blocking put_nowait that caused CRITICAL-2
            await self.raw_signal_queue.put(signal)
            logger.debug(f"[Bus] Raw signal published for basket: {getattr(signal, 'basket_id', 'UNKNOWN')}")
        except Exception as e:
            logger.error(f"[Bus] Failed to publish signal: {e}")

    async def publish_approved_trade(self, signal: Any) -> None:
        """Risk Engine uses this to pass vetted trades to the Execution Engine."""
        await self.approved_signal_queue.put(signal)
        logger.info(f"[Bus] Trade APPROVED and routed to Execution: {getattr(signal, 'basket_id', 'UNKNOWN')}")
