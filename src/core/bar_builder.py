from typing import Optional, List
from datetime import datetime, timedelta
import logging
import pytz

from ..models.bar import Bar

logger = logging.getLogger(__name__)


class BarBuilder:
    """
    Builds 30-minute bars from tick/price data
    
    Accumulates prices and creates bars at interval boundaries
    """
    
    def __init__(self, interval_minutes: int = 30):
        self.interval_minutes = interval_minutes
        self.interval_seconds = interval_minutes * 60
        
        # Current bar being built
        self.current_bar_start: Optional[datetime] = None
        self.current_bar_end: Optional[datetime] = None
        self.open_price: Optional[float] = None
        self.high_price: Optional[float] = None
        self.low_price: Optional[float] = None
        self.close_price: Optional[float] = None
        self.tick_count: int = 0
        
        # Completed bars
        self.completed_bars: List[Bar] = []
        
        self.tz = pytz.timezone("America/New_York")
        
        logger.info(f"BarBuilder initialized with {interval_minutes}-minute intervals")
    
    def add_price(self, timestamp: datetime, price: float) -> Optional[Bar]:
        """
        Add a price tick and potentially complete a bar
        
        Args:
            timestamp: Time of price tick
            price: Price value
            
        Returns:
            Bar object if bar completed, None otherwise
        """
        # Ensure timestamp is timezone-aware
        if timestamp.tzinfo is None:
            timestamp = self.tz.localize(timestamp)
        else:
            timestamp = timestamp.astimezone(self.tz)
        
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
        """Start a new bar"""
        # Round down to nearest interval boundary
        minutes_since_midnight = timestamp.hour * 60 + timestamp.minute
        interval_number = minutes_since_midnight // self.interval_minutes
        
        start_minutes = interval_number * self.interval_minutes
        start_hour = start_minutes // 60
        start_minute = start_minutes % 60
        
        self.current_bar_start = timestamp.replace(
            hour=start_hour,
            minute=start_minute,
            second=0,
            microsecond=0
        )
        
        self.current_bar_end = self.current_bar_start + timedelta(minutes=self.interval_minutes)
        
        self.open_price = price
        self.high_price = price
        self.low_price = price
        self.close_price = price
        self.tick_count = 1
        
        logger.debug(f"Started new bar: {self.current_bar_start.strftime('%H:%M')} - "
                    f"{self.current_bar_end.strftime('%H:%M')}, O=${price:.2f}")
    
    def _update_current_bar(self, price: float):
        """Update current bar with new price"""
        self.high_price = max(self.high_price, price)
        self.low_price = min(self.low_price, price)
        self.close_price = price
        self.tick_count += 1
    
    def _complete_bar(self) -> Bar:
        """Complete and return current bar"""
        bar = Bar(
            timestamp=self.current_bar_start,
            open=self.open_price,
            high=self.high_price,
            low=self.low_price,
            close=self.close_price,
            volume=self.tick_count
        )
        
        self.completed_bars.append(bar)
        
        logger.info(f"Completed bar: {bar}")
        logger.debug(f"  Range: ${bar.range:.2f}, Body: ${bar.body_size:.2f}, "
                    f"Close%: {bar.close_percentage_in_range():.1f}%")
        
        return bar
    
    def get_last_bar(self) -> Optional[Bar]:
        """Get the most recently completed bar"""
        if self.completed_bars:
            return self.completed_bars[-1]
        return None
    
    def get_bars(self, count: int = None) -> List[Bar]:
        """
        Get completed bars
        
        Args:
            count: Number of bars to return (most recent first)
                   If None, returns all bars
        """
        if count is None:
            return self.completed_bars.copy()
        else:
            return self.completed_bars[-count:] if count <= len(self.completed_bars) else self.completed_bars.copy()
    
    def reset(self):
        """Reset bar builder"""
        self.current_bar_start = None
        self.current_bar_end = None
        self.open_price = None
        self.high_price = None
        self.low_price = None
        self.close_price = None
        self.tick_count = 0
        self.completed_bars = []
        
        logger.info("BarBuilder reset")