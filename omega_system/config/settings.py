import os

class Settings:
    MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "5.0"))
    MAX_RISK_PCT = float(os.getenv("MAX_RISK_PCT", "2.0"))
    DB_PATH = os.getenv("OMEGA_DB_PATH", "logs/trades.db")
    CONTROL_DB_PATH = os.getenv("OMEGA_CONTROL_DB_PATH", "logs/control.db")
    GHOST_MODE = os.getenv("GHOST_MODE", "False") == "True"

settings = Settings()
