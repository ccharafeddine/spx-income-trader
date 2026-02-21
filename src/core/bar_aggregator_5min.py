"""
Lightweight 5-minute bar aggregator for display purposes only.

NOT used by any trading strategy. Feeds from the same tick stream as the
30-min BarBuilder but stores bars separately for the chart timeframe toggle.

Keeps at most MAX_BARS bars in memory (2 hours of 5-min bars = 24 bars)
and auto-evicts older bars.
"""

from typing import Optional, List
from datetime import datetime, timedelta, time as dtime
import logging
import pytz

from ..models.bar import Bar

logger = logging.getLogger(__name__)

MAX_BARS = 24  # 2 hours of 5-min bars
INTERVAL_MINUTES = 5
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)


class BarAggregator5Min:
    """Aggregates ticks into 5-minute display bars with auto-eviction."""

    def __init__(self):
        self.interval_minutes = INTERVAL_MINUTES

        # Current bar being built
        self.current_bar_start: Optional[datetime] = None
        self.current_bar_end: Optional[datetime] = None
        self.open_price: Optional[float] = None
        self.high_price: Optional[float] = None
        self.low_price: Optional[float] = None
        self.close_price: Optional[float] = None
        self.tick_count: int = 0

        # Completed bars (capped at MAX_BARS)
        self.completed_bars: List[Bar] = []

        self.tz = pytz.timezone("America/New_York")

    def add_price(self, timestamp: datetime, price: float) -> Optional[Bar]:
        """Add a price tick. Returns completed Bar if a bar boundary is crossed."""
        if timestamp.tzinfo is None:
            timestamp = self.tz.localize(timestamp)
        else:
            timestamp = timestamp.astimezone(self.tz)

        # Skip ticks outside market hours
        t = timestamp.time()
        if t < MARKET_OPEN or t >= MARKET_CLOSE:
            return None

        # Initialize first bar
        if self.current_bar_start is None:
            self._start_new_bar(timestamp, price)
            return None

        # Check if we need to complete current bar and start new one
        if timestamp >= self.current_bar_end:
            completed_bar = self._complete_bar()
            self._start_new_bar(timestamp, price)
            return completed_bar

        # Update current bar
        self._update_current_bar(price)
        return None

    def _start_new_bar(self, timestamp: datetime, price: float):
        """Start a new bar aligned to 5-minute boundary."""
        minutes_since_midnight = timestamp.hour * 60 + timestamp.minute
        interval_number = minutes_since_midnight // self.interval_minutes
        start_minutes = interval_number * self.interval_minutes
        start_hour = start_minutes // 60
        start_minute = start_minutes % 60

        self.current_bar_start = timestamp.replace(
            hour=start_hour, minute=start_minute, second=0, microsecond=0
        )
        self.current_bar_end = self.current_bar_start + timedelta(minutes=self.interval_minutes)

        self.open_price = price
        self.high_price = price
        self.low_price = price
        self.close_price = price
        self.tick_count = 1

    def _update_current_bar(self, price: float):
        """Update current bar with new tick."""
        self.high_price = max(self.high_price, price)
        self.low_price = min(self.low_price, price)
        self.close_price = price
        self.tick_count += 1

    def _complete_bar(self) -> Bar:
        """Complete and store current bar, evicting oldest if over MAX_BARS."""
        bar = Bar(
            timestamp=self.current_bar_start,
            open=self.open_price,
            high=self.high_price,
            low=self.low_price,
            close=self.close_price,
            volume=self.tick_count,
        )
        self.completed_bars.append(bar)

        # Auto-evict oldest bars beyond limit
        if len(self.completed_bars) > MAX_BARS:
            self.completed_bars = self.completed_bars[-MAX_BARS:]

        return bar

    def get_bars(self, count: int = None) -> List[Bar]:
        """Get completed bars (most recent last)."""
        if count is None:
            return self.completed_bars.copy()
        return self.completed_bars[-count:] if count <= len(self.completed_bars) else self.completed_bars.copy()

    def get_current_bar_info(self) -> Optional[dict]:
        """Get info about the bar currently being built (for UI)."""
        if self.current_bar_start is None:
            return None
        return {
            'start': self.current_bar_start.strftime('%H:%M'),
            'open': self.open_price,
            'high': self.high_price,
            'low': self.low_price,
            'close': self.close_price,
            'ticks': self.tick_count,
        }

    def reset(self):
        """Reset aggregator for new trading day."""
        self.current_bar_start = None
        self.current_bar_end = None
        self.open_price = None
        self.high_price = None
        self.low_price = None
        self.close_price = None
        self.tick_count = 0
        self.completed_bars = []
