from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from enum import Enum


class TradeDirection(Enum):
    """Trade direction for credit spreads"""
    BULLISH = "bullish"  # Put credit spread
    BEARISH = "bearish"  # Call credit spread


@dataclass
class OptionLeg:
    """Single option leg"""
    strike: float
    option_type: str  # 'call' or 'put'
    action: str  # 'buy' or 'sell'
    price: float
    quantity: int = 1
    symbol: Optional[str] = None
    
    def __repr__(self) -> str:
        return f"{self.action.upper()} {self.quantity} {self.option_type.upper()} @ ${self.strike}"


@dataclass
class CreditSpread:
    """Credit spread position (vertical spread)"""
    direction: TradeDirection
    short_leg: OptionLeg
    long_leg: OptionLeg
    credit_received: float
    entry_time: datetime
    expiration: datetime
    underlying_price_at_entry: Optional[float] = None
    
    @property
    def spread_width(self) -> float:
        """Calculate width of spread"""
        return abs(self.short_leg.strike - self.long_leg.strike)
    
    @property
    def max_profit(self) -> float:
        """Calculate maximum profit per contract"""
        return self.credit_received * 100
    
    @property
    def max_risk(self) -> float:
        """Calculate maximum risk per contract"""
        return (self.spread_width - self.credit_received) * 100
    
    @property
    def breakeven(self) -> float:
        """Calculate breakeven price"""
        if self.direction == TradeDirection.BULLISH:
            return self.short_leg.strike - self.credit_received
        else:
            return self.short_leg.strike + self.credit_received
    
    @property
    def risk_reward_ratio(self) -> float:
        """Calculate risk/reward ratio"""
        if self.max_profit == 0:
            return 0
        return self.max_risk / self.max_profit
    
    def profit_at_price(self, underlying_price: float) -> float:
        """Calculate P&L at a given underlying price"""
        if self.direction == TradeDirection.BULLISH:
            # Put credit spread
            if underlying_price >= self.short_leg.strike:
                return self.max_profit
            elif underlying_price <= self.long_leg.strike:
                return -self.max_risk
            else:
                loss = (self.short_leg.strike - underlying_price) * 100
                return self.max_profit - loss
        else:
            # Call credit spread
            if underlying_price <= self.short_leg.strike:
                return self.max_profit
            elif underlying_price >= self.long_leg.strike:
                return -self.max_risk
            else:
                loss = (underlying_price - self.short_leg.strike) * 100
                return self.max_profit - loss
    
    def __repr__(self) -> str:
        return (f"CreditSpread({self.direction.value.upper()}, "
                f"${self.short_leg.strike}/{self.long_leg.strike}, "
                f"Credit=${self.credit_received:.2f})")