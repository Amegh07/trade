import os
from dataclasses import dataclass

@dataclass
class SystemConfig:
    MAX_DAILY_LOSS_USD: float = float(os.getenv("MAX_DAILY_LOSS", "-2000.0"))
    MAX_SPREAD_POINTS: int = int(os.getenv("MAX_SPREAD_POINTS", "15"))
    VWAP_LOT_THRESHOLD: float = float(os.getenv("VWAP_LOT_THRESHOLD", "2.0"))
    WATCHDOG_TIMEOUT_SEC: float = float(os.getenv("WATCHDOG_TIMEOUT_SEC", "60.0"))
    SHADOW_AI_RETRAIN_INTERVAL_SEC: float = float(os.getenv("SHADOW_AI_RETRAIN_INTERVAL_SEC", "3600.0"))
    DB_PATH: str = os.getenv("OMEGA_DB_PATH", "logs/trades.db")
    CONTROL_DB_PATH: str = os.getenv("OMEGA_CONTROL_DB_PATH", "logs/control.db")
    GHOST_MODE: bool = os.getenv("GHOST_MODE", "False") == "True"

settings = SystemConfig()
