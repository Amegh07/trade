from dataclasses import dataclass, field
from typing import Dict, Optional, Any
from enum import Enum
import time

class OrderStatus(Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FAILED = "FAILED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"

@dataclass
class Tick:
    symbol: str
    bid: float
    ask: float
    last: float
    volume: float
    regime: str = "UNKNOWN"

@dataclass
class CandleSnapshot:
    symbol: str
    timeframe: str
    rates: Any

@dataclass
class ParamUpdate:
    regime_params: dict

@dataclass
class RegimeMultiplierUpdate:
    regime: str
    multiplier: float

@dataclass
class Signal:
    basket_id: str
    asset_a: str
    asset_b: str
    action: str  # "ENTER" or "CLOSE"
    beta: float
    confidence: float
    regime: str = "UNKNOWN"
    target_lot: float = 1.0
    timestamp: float = field(default_factory=time.time)

@dataclass
class LegState:
    symbol: str
    role: str  # "PRIMARY" or "HEDGE"
    target_lot: float
    filled_lot: float = 0.0
    ticket: Optional[int] = None
    status: OrderStatus = OrderStatus.PENDING

@dataclass
class BasketState:
    basket_id: str
    status: OrderStatus
    legs: Dict[str, LegState]
    entry_time: float
    z_score_entry: float
    error_msg: Optional[str] = None
    regime: str = "UNKNOWN"
