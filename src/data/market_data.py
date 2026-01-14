import logging
from typing import Optional
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)


class MarketDataFeed:
    """
    Manages market data feed
    
    In production, this would connect to real-time data provider
    For now, it polls the broker for current prices
    """
    
    def __init__(self, broker):
        self.broker = broker
        self.tz = pytz.timezone("America/New_York")
        self.last_price: Optional[float] = None
        self.last_update: Optional[datetime] = None
    
    def get_current_price(self, symbol: str = "SPX") -> Optional[float]:
        """Get current price for symbol"""
        try:
            price = self.broker.get_current_price(symbol)
            self.last_price = price
            self.last_update = datetime.now(self.tz)
            return price
        except Exception as e:
            logger.error(f"Failed to get price for {symbol}: {e}")
            return None
    
    def is_data_fresh(self, max_age_seconds: int = 60) -> bool:
        """Check if data is fresh"""
        if not self.last_update:
            return False
        
        age = (datetime.now(self.tz) - self.last_update).total_seconds()
        return age <= max_age_seconds