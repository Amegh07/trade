from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    MT5_LOGIN: int
    MT5_PASSWORD: str
    MT5_SERVER: str
    
    MAX_CONCURRENT: int = 8
    KELLY_MAX: float = 0.25
    ROLLOVER_START_HOUR: int = 23    # NY time
    ROLLOVER_END_HOUR: int = 0
    SPREAD_VARIANCE_MAX: float = 5.0 # 5x average = veto
    
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

settings = Settings()
