from typing import Optional
import logging
from ..models.bar import Bar, BarType

logger = logging.getLogger(__name__)


class PulseBarDetector:
    """
    Detects pulse bars according to Production Line Trading rules
    
    A pulse bar is defined as:
    - Bullish: Close > Open AND close in top 10% of bar range
    - Bearish: Close < Open AND close in bottom 10% of bar range
    """
    
    def __init__(self, threshold_percent: float = 10.0):
        """
        Initialize pulse bar detector
        
        Args:
            threshold_percent: Percentage threshold for pulse bar (default 10%)
        """
        self.threshold = threshold_percent
        logger.info(f"PulseBarDetector initialized with {threshold_percent}% threshold")
    
    def analyze_bar(self, bar: Bar) -> BarType:
        """
        Analyze bar to determine if it's a pulse bar
        
        Args:
            bar: Bar object to analyze
            
        Returns:
            BarType indicating if bullish pulse, bearish pulse, or neutral
        """
        if bar.range == 0:
            logger.debug(f"Bar at {bar.timestamp} has zero range, returning NEUTRAL")
            return BarType.NEUTRAL
        
        close_pct = bar.close_percentage_in_range()
        
        # Check for bullish pulse bar
        if bar.is_bullish and close_pct >= (100 - self.threshold):
            logger.info(f"BULLISH PULSE detected at {bar.timestamp}: "
                       f"Close at {close_pct:.1f}% of range")
            return BarType.BULLISH_PULSE
        
        # Check for bearish pulse bar
        elif bar.is_bearish and close_pct <= self.threshold:
            logger.info(f"BEARISH PULSE detected at {bar.timestamp}: "
                       f"Close at {close_pct:.1f}% of range")
            return BarType.BEARISH_PULSE
        
        else:
            logger.debug(f"Neutral bar at {bar.timestamp}: "
                        f"Close at {close_pct:.1f}% of range")
            return BarType.NEUTRAL
    
    def is_pulse_bar(self, bar: Bar) -> bool:
        """
        Quick check if bar qualifies as any pulse bar
        
        Args:
            bar: Bar object to check
            
        Returns:
            True if bar is a pulse bar, False otherwise
        """
        return self.analyze_bar(bar) != BarType.NEUTRAL
    
    def get_direction_from_bar_type(self, bar_type: BarType) -> Optional[str]:
        """
        Convert BarType to trade direction
        
        Args:
            bar_type: BarType enum value
            
        Returns:
            'bullish' or 'bearish' or None
        """
        if bar_type == BarType.BULLISH_PULSE:
            return 'bullish'
        elif bar_type == BarType.BEARISH_PULSE:
            return 'bearish'
        return None