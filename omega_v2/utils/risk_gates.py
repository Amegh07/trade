import datetime
import pytz
import numpy as np
from collections import deque
from config import settings

NY_TZ = pytz.timezone("America/New_York")

class RiskGates:
    def __init__(self):
        self.spread_history = {sym: deque(maxlen=20) for sym in [
            "AUDUSD", "NZDUSD", "AUDJPY", "NZDJPY"
        ]}

    def is_rollover_window(self) -> bool:
        """Blocks trading during the 23:55 to 00:05 NY time liquidity vacuum."""
        now = datetime.datetime.now(NY_TZ)
        return (now.hour == settings.ROLLOVER_START_HOUR and now.minute >= 55) or \
               (now.hour == settings.ROLLOVER_END_HOUR and now.minute <= 5)

    def is_spread_blown(self, symbol: str, current_spread: float) -> bool:
        """Vetoes the trade if the current spread is 5x the recent moving average."""
        hist = self.spread_history.get(symbol)
        if hist is None:
            return False
            
        if len(hist) < 10:
            hist.append(current_spread)
            return False
            
        hist.append(current_spread)
        avg = np.mean(list(hist))
        if avg == 0:
            return False
            
        ratio = current_spread / avg
        return ratio > settings.SPREAD_VARIANCE_MAX

risk_gates = RiskGates()
