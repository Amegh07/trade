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

    # Risk gates
    MAX_DAILY_LOSS: float = 0.02          # Kill entries if daily loss exceeds 2% (override via .env)
    MAX_SESSION_DRAWDOWN: float = 0.015   # Kill new entries if session equity down ≥1.5%
    MAX_CONSECUTIVE_LOSSES: int = 3       # Block entries after 3 straight losing baskets

    # Track pair performance to auto-disable toxic pairs
    PAIR_MIN_TRADES: int = 5               # Evaluate pair after N closures
    PAIR_MIN_WIN_RATE: float = 0.30        # Must win at least 30% or will be disabled

    # Institutional quality filters
    MIN_CORRELATION: float = 0.6       # Min Pearson correlation for pair integrity
    MIN_COST_MULTIPLE: float = 3.0     # Divergence must be ≥ N× round-trip broker cost
    ATR_RISK_PCT: float = 0.001        # 0.1% of equity at risk per leg per ATR unit

    # Groq / LLaMA 3 70B Meta-Layer
    # Set GROQ_API_KEY in .env to enable. Leave empty to run in math-only mode.
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "llama-3.3-70b-versatile"
    # Groq must reach this confidence (0–1) before a VETO can block an entry.
    # Below this threshold, Groq concerns are logged but not acted on.
    GROQ_VETO_CONFIDENCE: float = 0.75
    # If the math edge_multiple >= this value, Groq VETO is overridden anyway.
    # Prevents Groq from blocking very strong signals.
    GROQ_VETO_OVERRIDE_MULTIPLE: float = 5.0

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()

