from typing import List, Optional, TYPE_CHECKING
from datetime import datetime
import logging
import uuid
import pytz

from ..models.trade import Trade, TradeStatus
from ..models.spread import CreditSpread, TradeDirection
from ..models.bar import Bar
from .strategy import SPXIncomeStrategy
from ..brokers.base import BrokerInterface

if TYPE_CHECKING:
    from .pdt_tracker import PDTTracker

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
        db_manager,
        pdt_tracker: Optional['PDTTracker'] = None
    ):
        self.broker = broker
        self.strategy = strategy
        self.db = db_manager
        self.pdt_tracker = pdt_tracker

        self.open_trades: List[Trade] = []
        self.tz = pytz.timezone("America/New_York")

        if pdt_tracker:
            logger.info("PositionManager initialized with PDT protection")
        else:
            logger.info("PositionManager initialized (no PDT protection)")
    
    def enter_trade(
        self,
        spread: CreditSpread,
        setup_bar: Bar,
        quantity: int,
        breakout_time: Optional[datetime] = None
    ) -> Optional[Trade]:
        """
        Enter a new trade

        Args:
            spread: CreditSpread to trade
            setup_bar: Bar that triggered the setup
            quantity: Number of contracts
            breakout_time: When the breakout was confirmed (None if legacy immediate entry)

        Returns:
            Trade object if successful, None otherwise
        """
        try:
            logger.info("=" * 60)
            logger.info("ENTERING TRADE")
            logger.info("=" * 60)

            # Build metadata with pulse bar OHLC for signal logging
            bar_metadata = None
            if setup_bar:
                close_pct = setup_bar.close_percentage_in_range()
                bar_metadata = {
                    'pulse_bar': {
                        'time': setup_bar.timestamp.strftime('%H:%M') if setup_bar.timestamp else None,
                        'open': round(setup_bar.open, 2),
                        'high': round(setup_bar.high, 2),
                        'low': round(setup_bar.low, 2),
                        'close': round(setup_bar.close, 2),
                        'close_position_pct': round(close_pct, 1),
                        'bar_range': round(setup_bar.range, 2),
                    }
                }
                if breakout_time:
                    bar_metadata['setup_bar_time'] = setup_bar.timestamp.isoformat()
                    bar_metadata['breakout_time'] = breakout_time.isoformat()

            # Credit quality classification
            credit_quality = self.strategy.classify_credit_quality(
                credit_received=spread.credit_received,
                current_price=spread.underlying_price_at_entry,
                short_strike=spread.short_leg.strike,
                direction=spread.direction,
            )
            if bar_metadata is None:
                bar_metadata = {}
            bar_metadata['credit_quality'] = credit_quality

            logger.info(
                f"Credit quality: {credit_quality['quality']} "
                f"(${credit_quality['credit_received']:.2f} vs "
                f"{credit_quality['expected_range']} for {credit_quality['moneyness']})"
            )
            if credit_quality['is_simulated_concern']:
                logger.warning(
                    f"LOW CREDIT: ${credit_quality['credit_received']:.2f} — "
                    f"simulated chain may not reflect live pricing"
                )

            # Place order
            order_id = self.broker.place_spread_order(spread, quantity, metadata=bar_metadata)
            
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
    
    def monitor_positions(self) -> float:
        """Monitor all open positions for exit conditions.

        Returns:
            Total realized P&L from trades closed this cycle.
        """
        if not self.open_trades:
            return 0.0

        logger.debug(f"Monitoring {len(self.open_trades)} open positions")
        realized_pnl = 0.0
        
        current_time = datetime.now(self.tz)
        
        for trade in self.open_trades.copy():
            if trade.status != TradeStatus.ACTIVE:
                continue
            
            try:
                # Get current spread value
                current_value = self.broker.get_position_value(trade.spread)
                
                # Update P&L
                trade.update_pnl(current_value)

                # Inject current SPX price for 1pm management check
                try:
                    trade._current_spx_price = self.broker.get_current_price("SPX")
                except Exception:
                    trade._current_spx_price = 0

                logger.debug(f"Trade {trade.id[:8]}: Current value=${current_value:.2f}, "
                           f"P&L=${trade.pnl:.2f} ({trade.pnl_percent:.1f}%)")

                # Check exit conditions
                should_exit, reason = self.strategy.should_exit(
                    trade,
                    current_value,
                    current_time
                )

                if should_exit:
                    # Log 1pm assessment details if that was the trigger
                    if hasattr(trade, '_1pm_assessment') and '1PM CHECK' in reason:
                        a = trade._1pm_assessment
                        logger.info(
                            f"1PM DETAILS: Entry=${a['entry_price']}, "
                            f"Now=${a['current_price']}, Move=${a['move']:+.2f}, "
                            f"Trending={a['is_trending']}, Favorable={a['is_favorable']}, "
                            f"P&L={a.get('pnl_pct', 0):.1f}%, Decision={a['decision']}"
                        )
                        try:
                            self.db.log_event("1pm_management", reason, a)
                        except Exception:
                            pass

                    # PDT protection: check if early close is allowed
                    is_expiration = "expiration" in reason.lower()
                    if not is_expiration and self.pdt_tracker:
                        if not self.pdt_tracker.can_close_early():
                            pdt_status = self.pdt_tracker.get_pdt_status()
                            used = pdt_status['day_trades_used']
                            max_trades = pdt_status['max_day_trades']
                            logger.warning(
                                f"PDT protection: skipping early exit "
                                f"({used}/{max_trades} day trades used in rolling window). "
                                f"Trade will run to expiration."
                            )
                            self.db.log_event("pdt_blocked", "Early exit blocked by PDT", {
                                'trade_id': trade.id,
                                'original_reason': reason,
                                'day_trades_used': used,
                            })
                            continue  # Skip this exit, let trade run to expiration

                    self._exit_trade(trade, reason)
                    if trade.pnl is not None:
                        realized_pnl += trade.pnl
                
            except Exception as e:
                logger.error(f"Error monitoring trade {trade.id}: {e}")

        return realized_pnl

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

            # Record day trade for PDT tracking (if same-day active close)
            if self.pdt_tracker and trade.entry_time and trade.exit_time:
                entry_date = trade.entry_time.date()
                exit_date = trade.exit_time.date()
                self.pdt_tracker.check_and_record_day_trade(
                    trade_id=trade.id,
                    entry_date=entry_date,
                    exit_date=exit_date,
                    exit_reason=reason,
                )

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