# TRADABLE_PAIRS matrix isolates asset universes from trading loops.
# Each tuple defines the highly cointegrated primary and hedge leg.

TRADABLE_PAIRS = [
    ("EURUSD", "GBPUSD"),
    ("USDJPY", "USDCHF"),
]
