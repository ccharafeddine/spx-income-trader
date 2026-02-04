from abc import ABC, abstractmethod
from typing import Dict, List, Optional
from ..models.spread import CreditSpread


class BrokerInterface(ABC):
    """Abstract base class for broker implementations"""
    
    @abstractmethod
    def get_current_price(self, symbol: str) -> float:
        """Get current market price for symbol"""
        pass
    
    @abstractmethod
    def get_options_chain(self, symbol: str, expiration: str) -> Dict[float, Dict]:
        """
        Get options chain for symbol
        Returns: {strike: {'call_bid': x, 'call_ask': x, 'put_bid': x, 'put_ask': x}}
        """
        pass
    
    @abstractmethod
    def place_spread_order(
        self,
        spread: CreditSpread,
        quantity: int,
        limit_price: Optional[float] = None,
        metadata: Optional[Dict] = None
    ) -> str:
        """
        Place vertical spread order

        Args:
            spread: The credit spread to trade
            quantity: Number of contracts
            limit_price: Optional limit price override
            metadata: Optional extra context (e.g. setup bar OHLC) for signal logging

        Returns: order_id
        """
        pass
    
    @abstractmethod
    def get_order_status(self, order_id: str) -> Dict:
        """
        Get order status and fill information
        Returns: {'status': str, 'fill_price': float, 'filled_quantity': int}
        """
        pass
    
    @abstractmethod
    def close_spread(
        self,
        spread: CreditSpread,
        quantity: int,
        limit_price: float = 0.20
    ) -> str:
        """
        Close existing spread
        Returns: order_id
        """
        pass
    
    @abstractmethod
    def get_position_value(self, spread: CreditSpread) -> float:
        """Get current market value of spread"""
        pass
    
    @abstractmethod
    def get_account_balance(self) -> Dict:
        """
        Get account balance information
        Returns: {'net_account_value': float, 'cash_available': float, 'buying_power': float}
        """
        pass