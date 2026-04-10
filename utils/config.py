import os
from dotenv import load_dotenv

# Load environment variables from the .env file
load_dotenv()

# MetaTrader 5 Configuration
# Fallback to 0 so import never crashes; initialize_mt5() will handle the empty-credentials path gracefully.
MT5_ACCOUNT  = int(os.getenv("MT5_ACCOUNT", "0"))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER   = os.getenv("MT5_SERVER",   "")

# Risk Configuration with reasonable defaults
MAX_RISK_PER_TRADE = float(os.getenv("MAX_RISK_PER_TRADE", "0.005"))
MAX_DAILY_LOSS     = float(os.getenv("MAX_DAILY_LOSS",     "0.05"))
ACCOUNT_BALANCE    = float(os.getenv("ACCOUNT_BALANCE",    "10000.0"))

# Target symbols to monitor if none are specified
SYMBOLS = os.getenv("SYMBOLS", "EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD,USDCHF,NZDUSD").split(",")

# Institutional / Multi-Agent Parameters
ADX_THRESHOLD          = int(os.getenv("ADX_THRESHOLD",          "25"))
MAX_SESSION_DRAWDOWN   = float(os.getenv("MAX_SESSION_DRAWDOWN", "0.015"))
MAX_ALLOWED_SPREAD     = int(os.getenv("MAX_ALLOWED_SPREAD",     "20"))
MAX_CONSECUTIVE_LOSSES = int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3"))
