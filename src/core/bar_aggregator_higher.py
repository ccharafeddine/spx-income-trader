"""
Higher-timeframe bar aggregator for 1h and 4h chart views.

NOT used by any trading strategy. Aggregates completed Bar objects from
lower timeframes (30m bars feed 1h, 1h bars feed 4h) for display purposes.

Keeps at most max_bars bars in memory and auto-evicts older bars.
"""

from typing import Optional, List
from datetime import datetime, timedelta, time as dtime
import logging
import pytz

from ..models.bar import Bar

logger = logging.getLogger(__name__)

MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)


class BarAggregatorHigherTF:
    """Aggregates completed bars into higher-timeframe display bars."""

    def __init__(self, interval_minutes: int, max_bars: int):
        self.interval_minutes = interval_minutes
        self.max_bars = max_bars

        # Current bar being built
        self.current_bar_start: Optional[datetime] = None
        self.current_bar_end: Optional[datetime] = None
        self.open_price: Optional[float] = None
        self.high_price: Optional[float] = None
        self.low_price: Optional[float] = None
        self.close_price: Optional[float] = None
        self.tick_count: int = 0

        # Completed bars (capped at max_bars)
        self.completed_bars: List[Bar] = []

        self.tz = pytz.timezone("America/New_York")

    def _align_to_boundary(self, timestamp: datetime) -> datetime:
        """Align a timestamp to the start of its interval boundary."""
        minutes_since_midnight = timestamp.hour * 60 + timestamp.minute
        interval_number = minutes_since_midnight // self.interval_minutes
        start_minutes = interval_number * self.interval_minutes
        start_hour = start_minutes // 60
        start_minute = start_minutes % 60
        return timestamp.replace(
            hour=start_hour, minute=start_minute, second=0, microsecond=0
        )

    def add_bar(self, bar: Bar) -> Optional[Bar]:
        """Add a completed bar from a lower timeframe.

        Returns a completed higher-TF bar when a boundary is crossed,
        or None if the bar was merged into the current building bar.

        No market hours filter here -- input bars are already filtered
        by the lower-timeframe aggregator. Aligned boundaries may
        precede 09:30 (e.g. 09:00 for 1h, 08:00 for 4h).
        """
        ts = bar.timestamp
        if ts.tzinfo is None:
            ts = self.tz.localize(ts)
        else:
            ts = ts.astimezone(self.tz)

        boundary_start = self._align_to_boundary(ts)
        boundary_end = boundary_start + timedelta(minutes=self.interval_minutes)

        # Initialize first bar
        if self.current_bar_start is None:
            self._start_new_bar_from(boundary_start, boundary_end, bar)
            return None

        # Check if this bar crosses into a new interval
        if boundary_start >= self.current_bar_end:
            completed = self._complete_bar()
            self._start_new_bar_from(boundary_start, boundary_end, bar)
            return completed

        # Merge into current bar
        self.high_price = max(self.high_price, bar.high)
        self.low_price = min(self.low_price, bar.low)
        self.close_price = bar.close
        self.tick_count += 1
        return None

    def _start_new_bar_from(self, boundary_start: datetime, boundary_end: datetime, bar: Bar):
        """Start a new building bar from a lower-TF bar."""
        self.current_bar_start = boundary_start
        self.current_bar_end = boundary_end
        self.open_price = bar.open
        self.high_price = bar.high
        self.low_price = bar.low
        self.close_price = bar.close
        self.tick_count = 1

    def _complete_bar(self) -> Bar:
        """Complete and store current bar, evicting oldest if over max_bars."""
        bar = Bar(
            timestamp=self.current_bar_start,
            open=self.open_price,
            high=self.high_price,
            low=self.low_price,
            close=self.close_price,
            volume=self.tick_count,
        )
        self.completed_bars.append(bar)

        if len(self.completed_bars) > self.max_bars:
            self.completed_bars = self.completed_bars[-self.max_bars:]

        return bar

    def backfill(self, bars: List[Bar]) -> int:
        """Seed the aggregator with historical bars at this timeframe.

        Bars should be sorted oldest-first. Only bars whose interval has
        fully elapsed (end <= now) are added. Bars clearly outside the
        trading session are skipped. Returns the number of bars injected.
        """
        if not bars:
            return 0

        now = datetime.now(self.tz)
        count = 0

        # Relaxed open: aligned boundaries can precede 09:30
        # (e.g., 09:00 for 1h, 08:00 for 4h)
        open_mins = MARKET_OPEN.hour * 60 + MARKET_OPEN.minute
        boundary_mins = (open_mins // self.interval_minutes) * self.interval_minutes
        earliest = dtime(boundary_mins // 60, boundary_mins % 60)

        for bar in bars:
            ts = bar.timestamp
            if ts.tzinfo is None:
                ts = self.tz.localize(ts)
            else:
                ts = ts.astimezone(self.tz)

            t = ts.time()
            if t < earliest or t >= MARKET_CLOSE:
                continue

            bar_end = ts + timedelta(minutes=self.interval_minutes)
            if bar_end > now:
                continue

            self.completed_bars.append(Bar(
                timestamp=ts,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=getattr(bar, 'volume', 0),
            ))
            count += 1

        if len(self.completed_bars) > self.max_bars:
            self.completed_bars = self.completed_bars[-self.max_bars:]

        # Set current_bar_end so next live bar starts correctly
        if self.completed_bars:
            last = self.completed_bars[-1]
            last_ts = last.timestamp
            if last_ts.tzinfo is None:
                last_ts = self.tz.localize(last_ts)
            self.current_bar_end = last_ts + timedelta(minutes=self.interval_minutes)

        logger.info(f"Backfilled {count} {self.interval_minutes}-min bars from history")
        return count

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
