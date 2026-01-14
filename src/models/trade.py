from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from enum import Enum
from .spread import CreditSpread
from .bar import Bar


class TradeStatus(Enum):
    """Trade status"""
    PENDING = "pending"
    ACTIVE = "active"
    CLOSED = "closed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass
class Trade:
    """Complete trade record"""
    id: str
    spread: CreditSpread
    status: TradeStatus
    setup_bar: Bar
    entry_price: float
    entry_time: datetime
    
    # Exit information
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: Optional[str] = None
    
    # P&L
    pnl: Optional[float] = None
    pnl_percent: Optional[float] = None
    
    # Order information
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    
    # Quantity
    quantity: int = 1
    
    # Notes
    notes: Optional[str] = None
    
    @property
    def is_open(self) -> bool:
        """Check if trade is still open"""
        return self.status in [TradeStatus.PENDING, TradeStatus.ACTIVE]
    
    @property
    def is_closed(self) -> bool:
        """Check if trade is closed"""
        return self.status in [TradeStatus.CLOSED, TradeStatus.EXPIRED, TradeStatus.CANCELLED]
    
    @property
    def duration(self) -> Optional[float]:
        """Calculate trade duration in hours"""
        if self.exit_time:
            delta = self.exit_time - self.entry_time
            return delta.total_seconds() / 3600
        return None
    
    @property
    def current_pnl(self) -> Optional[float]:
        """Get current or final P&L"""
        return self.pnl
    
    def update_pnl(self, current_price: float):
        """Update P&L based on current spread price"""
        self.pnl = (self.entry_price - current_price) * 100 * self.quantity
        if self.entry_price > 0:
            self.pnl_percent = (self.pnl / (self.entry_price * 100 * self.quantity)) * 100
    
    def close(self, exit_price: float, exit_time: datetime, reason: str):
        """Close the trade"""
        self.exit_price = exit_price
        self.exit_time = exit_time
        self.exit_reason = reason
        self.status = TradeStatus.CLOSED
        self.update_pnl(exit_price)
    
    def __repr__(self) -> str:
        pnl_str = f", P&L=${self.pnl:.2f}" if self.pnl else ""
        return (f"Trade({self.id[:8]}, {self.status.value}, "
                f"{self.spread.direction.value}{pnl_str})")