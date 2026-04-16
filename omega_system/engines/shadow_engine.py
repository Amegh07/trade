import asyncio
import logging
import sqlite3
from omega_system.core.message_bus import MessageBus
from omega_system.core.types import ParamUpdate
from omega_system.config.settings import settings

logger = logging.getLogger("ShadowEngine")

class ShadowEngine:
    def __init__(self, message_bus: MessageBus):
        from omega_system.config.settings import settings
        self.bus = message_bus
        self.current_z_entry = 2.0
        self.current_z_exit = 0.5 
        # Clamped Parameters (Guardrails)
        self.min_z_entry = 2.0
        self.max_z_entry = 4.5
        self.retrain_timer = settings.SHADOW_AI_RETRAIN_INTERVAL_SEC

    async def start_worker(self):
        logger.info("[ShadowEngine] L3 Heuristic AI Adaptation Loop armed.")
        while True:
            # Slower cyclic sweep dictated by hyper-parameter registry
            await asyncio.sleep(self.retrain_timer)
            await self._run_adaptation()

    async def _run_adaptation(self):
        logger.info("[ShadowEngine] Accessing historical database for DNA mutation analysis...")
        
        # Offload DB hit to prevent event loop bottlenecks
        trades = await asyncio.to_thread(self._fetch_memory)
        
        if len(trades) < 5:
            logger.debug("[ShadowEngine] Insufficient trades for statistical adaptation.")
            return

        wins = [t for t in trades if t['pnl'] > 0]
        win_rate = len(wins) / len(trades)
        avg_pnl = sum(t['pnl'] for t in trades) / len(trades)
        
        logger.info(f"[ShadowEngine] Target Analysis: Win Rate={win_rate*100:.1f}%, AvgPnL={avg_pnl:.2f}")

        changed = False
        
        # RL Heuristic Optimization Engine
        if win_rate < 0.40:
            logger.warning("[ShadowEngine] Edge Decay detected. Tightening entry threshold.")
            self.current_z_entry = min(self.max_z_entry, self.current_z_entry + 0.2)
            changed = True
            
        elif win_rate > 0.65 and avg_pnl > 0.0:
            logger.info("[ShadowEngine] Alpha abundance detected. Loosening entry constraints.")
            self.current_z_entry = max(self.min_z_entry, self.current_z_entry - 0.1)
            changed = True
            
        if changed:
            logger.warning(f"[ShadowEngine] Emitting DNA Reprogramming Sequence (Z_entry={self.current_z_entry:.2f})...")
            update = ParamUpdate(z_entry=self.current_z_entry, z_exit=self.current_z_exit)
            await self.bus.param_update_queue.put(update)

    def _fetch_memory(self):
        try:
            conn = sqlite3.connect(f"file:{settings.DB_PATH}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT pnl, regime FROM trades ORDER BY timestamp DESC LIMIT 50")
            rows = [dict(r) for r in cur.fetchall()]
            conn.close()
            return rows
        except Exception as e:
            logger.error(f"[ShadowEngine] Failed to access trades.db: {e}")
            return []
