from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    MT5_LOGIN: int
    MT5_PASSWORD: str
    MT5_SERVER: str

    # Execution
    MAX_CONCURRENT: int = 8
    KELLY_MAX: float = 0.25

    # Timing gates
    ROLLOVER_START_HOUR: int = 23    # NY time
    ROLLOVER_END_HOUR: int = 0

    # Spread gates
    SPREAD_VARIANCE_MAX: float = 5.0  # Veto if current spread is 5× moving average
    MAX_ALLOWED_SPREAD: float = 20.0  # Absolute hard limit in broker points

    # Risk gates (previously orphaned in .env — now active)
    MAX_SESSION_DRAWDOWN: float = 0.015   # Kill new entries if session equity down ≥1.5%
    MAX_CONSECUTIVE_LOSSES: int = 3        # Block entries after 3 straight losing baskets

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
