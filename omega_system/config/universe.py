# TRADABLE_PAIRS matrix isolates asset universes from trading loops.
# Provides base normalization values for dynamically scoped calculations.

TRADABLE_PAIRS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", 
    "NZDUSD", "USDCAD", "EURGBP", "EURJPY", "GBPJPY", 
    "AUDJPY", "EURAUD", "XAUUSD", "XAGUSD", "US30"
]

SYMBOL_METADATA = {
    "EURUSD": {"pip_size": 0.0001, "min_lot": 0.01},
    "GBPUSD": {"pip_size": 0.0001, "min_lot": 0.01},
    "USDJPY": {"pip_size": 0.01, "min_lot": 0.01},
    "USDCHF": {"pip_size": 0.0001, "min_lot": 0.01},
    "AUDUSD": {"pip_size": 0.0001, "min_lot": 0.01},
    "NZDUSD": {"pip_size": 0.0001, "min_lot": 0.01},
    "USDCAD": {"pip_size": 0.0001, "min_lot": 0.01},
    "EURGBP": {"pip_size": 0.0001, "min_lot": 0.01},
    "EURJPY": {"pip_size": 0.01, "min_lot": 0.01},
    "GBPJPY": {"pip_size": 0.01, "min_lot": 0.01},
    "AUDJPY": {"pip_size": 0.01, "min_lot": 0.01},
    "EURAUD": {"pip_size": 0.0001, "min_lot": 0.01},
    "XAUUSD": {"pip_size": 0.01, "min_lot": 0.01},
    "XAGUSD": {"pip_size": 0.01, "min_lot": 0.01},
    "US30": {"pip_size": 1.0, "min_lot": 0.1},
}
