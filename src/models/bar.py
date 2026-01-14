from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from enum import Enum


class BarType(Enum):
    """Types of bars based on pulse bar analysis"""
    BULLISH_PULSE = "bullish_pulse"
    BEARISH_PULSE = "bearish_pulse"
    NEUTRAL = "neutral"


@dataclass
class Bar:
    """Represents a 30-minute price bar"""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: Optional[int] = None
    
    @property
    def range(self) -> float:
        """Calculate bar range (high - low)"""
        return self.high - self.low
    
    @property
    def is_bullish(self) -> bool:
        """Check if bar closed higher than it opened"""
        return self.close > self.open
    
    @property
    def is_bearish(self) -> bool:
        """Check if bar closed lower than it opened"""
        return self.close < self.open
    
    @property
    def body_size(self) -> float:
        """Calculate body size (abs difference between open and close)"""
        return abs(self.close - self.open)
    
    def close_percentage_in_range(self) -> float:
        """
        Calculate where close is as percentage from low (0%) to high (100%)
        Returns: 0.0 to 100.0
        """
        if self.range == 0:
            return 50.0
        return ((self.close - self.low) / self.range) * 100.0
    
    def __repr__(self) -> str:
        return (f"Bar(time={self.timestamp.strftime('%H:%M')}, "
                f"O={self.open:.2f}, H={self.high:.2f}, "
                f"L={self.low:.2f}, C={self.close:.2f})")