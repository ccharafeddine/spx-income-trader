from typing import List, Optional
from datetime import datetime
import logging
import uuid
import pytz

from ..models.trade import Trade, TradeStatus
from ..models.spread import CreditSpread, TradeDirection
from ..models.bar import Bar
from .strategy import SPXIncomeStrategy
from ..brokers.base import BrokerInterface

logger = logging.getLogger(__name__)


class PositionManager:
    """
    Manages open positions and exit logic
    
    Responsibilities:
    - Track open trades
    - Monitor for exit conditions
    - Execute exits
    - Update P&L
    """
    
    def __init__(
        self,
        broker: BrokerInterface,
        strategy: SPXIncomeStrategy,
        db_manager
    ):
        self.broker = broker
        self.strategy = strategy
        self.db = db_manager
        
        self.open_trades: List[Trade] = []
        self.tz = pytz.timezone("America/New_York")
        
        logger.info("PositionManager initialized")
    
    def enter_trade(
        self,
        spread: CreditSpread,
        setup_bar: Bar,
        quantity: int
    ) -> Optional[Trade]:
        """
        Enter a new trade
        
        Args:
            spread: CreditSpread to trade
            setup_bar: Bar that triggered the setup
            quantity: Number of contracts
            
        Returns:
            Trade object if successful, None otherwise
        """
        try:
            logger.info("=" * 60)
            logger.info("ENTERING TRADE")
            logger.info("=" * 60)
            
            # Place order
            order_id = self.broker.place_spread_order(spread, quantity)
            
            # Wait briefly for fill
            import time
            time.sleep(2)
            
            # Check order status
            order_status = self.broker.get_order_status(order_id)
            
            if order_status['status'] != 'filled':
                logger.error(f"Order not filled: {order_status['status']}")
                return None
            
            # Create trade record
            trade = Trade(
                id=str(uuid.uuid4()),
                spread=spread,
                status=TradeStatus.ACTIVE,
                setup_bar=setup_bar,
                entry_price=order_status['fill_price'],
                entry_time=datetime.now(self.tz),
                entry_order_id=order_id,
                quantity=quantity
            )
            
            # Add to open trades
            self.open_trades.append(trade)
            
            # Save to database
            self.db.save_trade(trade)
            
            logger.info(f"✓ Trade entered successfully: {trade.id}")
            logger.info(f"  Entry price: ${trade.entry_price:.2f}")
            logger.info(f"  Max profit: ${spread.max_profit * quantity:.2f}")
            logger.info(f"  Max risk: ${spread.max_risk * quantity:.2f}")
            
            return trade
            
        except Exception as e:
            logger.error(f"Failed to enter trade: {e}", exc_info=True)
            return None
    
    def monitor_positions(self):
        """Monitor all open positions for exit conditions"""
        if not self.open_trades:
            return
        
        logger.debug(f"Monitoring {len(self.open_trades)} open positions")
        
        current_time = datetime.now(self.tz)
        
        for trade in self.open_trades.copy():
            if trade.status != TradeStatus.ACTIVE:
                continue
            
            try:
                # Get current spread value
                current_value = self.broker.get_position_value(trade.spread)
                
                # Update P&L
                trade.update_pnl(current_value)
                
                logger.debug(f"Trade {trade.id[:8]}: Current value=${current_value:.2f}, "
                           f"P&L=${trade.pnl:.2f} ({trade.pnl_percent:.1f}%)")
                
                # Check exit conditions
                should_exit, reason = self.strategy.should_exit(
                    trade,
                    current_value,
                    current_time
                )
                
                if should_exit:
                    self._exit_trade(trade, reason)
                
            except Exception as e:
                logger.error(f"Error monitoring trade {trade.id}: {e}")
    
    def _exit_trade(self, trade: Trade, reason: str):
        """Execute trade exit"""
        try:
            logger.info("=" * 60)
            logger.info(f"EXITING TRADE: {reason}")
            logger.info("=" * 60)
            
            # Get current value for exit
            current_value = self.broker.get_position_value(trade.spread)
            
            # If expiration, no need to close
            if "expiration" in reason.lower():
                underlying_price = self.broker.get_current_price("SPX")
                final_pnl = trade.spread.profit_at_price(underlying_price) * trade.quantity
                
                trade.close(
                    exit_price=0.0,
                    exit_time=datetime.now(self.tz),
                    reason=reason
                )
                trade.pnl = final_pnl
                
                logger.info(f"Trade expired: Final P&L ${final_pnl:.2f}")
                
            else:
                # Close position
                limit_price = min(current_value + 0.10, 0.50)
                
                order_id = self.broker.close_spread(
                    trade.spread,
                    trade.quantity,
                    limit_price
                )
                
                # Wait for fill
                import time
                time.sleep(2)
                
                order_status = self.broker.get_order_status(order_id)
                exit_price = order_status['fill_price']
                
                trade.close(
                    exit_price=exit_price,
                    exit_time=datetime.now(self.tz),
                    reason=reason
                )
                trade.exit_order_id = order_id
                
                logger.info(f"✓ Trade closed at ${exit_price:.2f}")
            
            logger.info(f"  P&L: ${trade.pnl:.2f}")
            logger.info(f"  Duration: {trade.duration:.1f} hours")
            
            # Remove from open trades
            self.open_trades.remove(trade)
            
            # Update database
            self.db.save_trade(trade)
            self.db.update_daily_stats(trade.entry_time.date())
            
        except Exception as e:
            logger.error(f"Failed to exit trade: {e}", exc_info=True)
    
    def has_open_position(self) -> bool:
        """Check if there are any open positions"""
        return len(self.open_trades) > 0
    
    def get_open_trades(self) -> List[Trade]:
        """Get all open trades"""
        return self.open_trades.copy()
    
    def close_all_positions(self, reason: str = "Manual close"):
        """Close all open positions"""
        logger.warning(f"Closing all positions: {reason}")
        
        for trade in self.open_trades.copy():
            self._exit_trade(trade, reason)