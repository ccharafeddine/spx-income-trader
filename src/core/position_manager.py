from typing import List, Optional, Dict, TYPE_CHECKING
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
        self.recently_closed: List[Dict] = []  # [{id, pnl}] for portfolio tracking
        self.tz = pytz.timezone("America/New_York")

        # Daily market cache (set once per day by TradingBot)
        self._day_open: Optional[float] = None
        self._prev_close: Optional[float] = None

        if pdt_tracker:
            logger.info("PositionManager initialized with PDT protection")
        else:
            logger.info("PositionManager initialized (no PDT protection)")
    
    def set_daily_cache(self, day_open: float, prev_close: float):
        """Cache the day's open and previous close prices (called once per day by TradingBot)."""
        self._day_open = day_open
        self._prev_close = prev_close
        logger.info(f"Daily cache set: day_open=${day_open:,.2f}, prev_close=${prev_close:,.2f}")

    def _get_vix_price(self) -> Optional[float]:
        """Best-effort VIX price fetch. Returns None on failure."""
        try:
            md = getattr(self.broker, 'market_data', None)
            if md and hasattr(md, 'get_vix_quote'):
                vix_quote = md.get_vix_quote()
                if vix_quote and vix_quote.get('price'):
                    return float(vix_quote['price'])
        except Exception:
            pass

        # Fallback: try yfinance directly
        try:
            import yfinance as yf
            ticker = yf.Ticker('^VIX')
            hist = ticker.history(period='1d', interval='1d')
            if not hist.empty:
                return float(hist['Close'].iloc[-1])
        except Exception:
            pass

        return None

    @staticmethod
    def _classify_day_type(daily_move_pct: float) -> str:
        """Classify the trading day based on intraday move percentage."""
        if daily_move_pct > 0.5:
            return 'trending_up'
        elif daily_move_pct < -0.5:
            return 'trending_down'
        elif -0.2 <= daily_move_pct <= 0.2:
            return 'flat'
        else:
            return 'choppy'

    def enter_trade(
        self,
        spread: CreditSpread,
        setup_bar: Bar,
        quantity: int,
        breakout_time: Optional[datetime] = None,
        strategy_type: str = 'daily_income'
    ) -> Optional[Trade]:
        """
        Enter a new trade

        Args:
            spread: CreditSpread to trade
            setup_bar: Bar that triggered the setup
            quantity: Number of contracts
            breakout_time: When the breakout was confirmed (None if legacy immediate entry)
            strategy_type: Strategy identifier for DB tagging (default: 'daily_income')

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
            
            # Build entry context for database analytics
            entry_context = {'strategy_type': strategy_type}
            try:
                spx_price = spread.underlying_price_at_entry
                entry_context['spx_at_entry'] = round(spx_price, 2)
                entry_context['vix_at_entry'] = self._get_vix_price()
                if self._day_open is not None:
                    entry_context['day_open'] = round(self._day_open, 2)
                    entry_context['intraday_move_at_entry'] = round(
                        ((spx_price - self._day_open) / self._day_open) * 100, 4
                    )
                if self._day_open is not None and self._prev_close is not None and self._prev_close > 0:
                    entry_context['gap_pct'] = round(
                        ((self._day_open - self._prev_close) / self._prev_close) * 100, 4
                    )
                logger.debug(f"Entry context: {entry_context}")
            except Exception as e:
                logger.warning(f"Failed to build entry context: {e}")

            # Save to database
            self.db.save_trade(trade, context=entry_context)

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
            
            # If expiration, calculate P&L from underlying price vs strikes
            if "expiration" in reason.lower():
                underlying_price = self.broker.get_current_price("SPX")
                final_pnl = trade.spread.profit_at_price(underlying_price) * trade.quantity

                # Set exit fields directly (trade.close() calls update_pnl()
                # which uses entry_price-exit_price math, wrong for expirations)
                trade.exit_price = 0.0
                trade.exit_time = datetime.now(self.tz)
                trade.exit_reason = reason
                trade.status = TradeStatus.CLOSED
                trade.pnl = final_pnl
                max_profit = trade.spread.max_profit * trade.quantity
                trade.pnl_percent = (final_pnl / max_profit * 100) if max_profit > 0 else 0.0

                logger.info(f"Trade expired: SPX=${underlying_price:,.2f}, Final P&L ${final_pnl:.2f}")
                
            else:
                # Close position - cap at spread width (max possible value)
                max_debit = trade.spread.spread_width
                limit_price = min(current_value + 0.10, max_debit)

                order_id = self.broker.close_spread(
                    trade.spread,
                    trade.quantity,
                    limit_price
                )

                if not order_id:
                    logger.error(f"Close order failed for trade {trade.id}, will retry next cycle")
                    return

                # Wait for fill
                import time
                time.sleep(2)

                order_status = self.broker.get_order_status(order_id)
                if order_status['status'] != 'filled':
                    logger.warning(
                        f"Close order {order_id} not filled (status: {order_status['status']}), "
                        f"will retry next cycle"
                    )
                    return

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

            # Track for portfolio risk updates
            self.recently_closed.append({'id': trade.id, 'pnl': trade.pnl or 0.0})

            # Update database
            self.db.save_trade(trade)
            self.db.update_daily_stats(trade.entry_time.date())

            # Build and save exit context for analytics
            try:
                exit_context: Dict = {}
                spx_at_exit = self.broker.get_current_price("SPX")
                exit_context['spx_at_exit'] = round(spx_at_exit, 2)

                # Profit captured as percentage of max profit
                max_profit = trade.spread.max_profit * trade.quantity
                if max_profit > 0 and trade.pnl is not None:
                    exit_context['profit_captured_pct'] = round(
                        (trade.pnl / max_profit) * 100, 2
                    )

                # Time in trade
                if trade.entry_time and trade.exit_time:
                    exit_context['time_in_trade_minutes'] = int(
                        (trade.exit_time - trade.entry_time).total_seconds() / 60
                    )

                # Daily move and day type (requires day_open cache)
                if self._day_open is not None and self._day_open > 0:
                    daily_move_pct = ((spx_at_exit - self._day_open) / self._day_open) * 100
                    exit_context['daily_move_pct'] = round(daily_move_pct, 4)
                    exit_context['day_type'] = self._classify_day_type(daily_move_pct)

                self.db.update_trade_exit_context(trade.id, exit_context)
                logger.debug(f"Exit context saved: {exit_context}")
            except Exception as e:
                logger.warning(f"Failed to save exit context: {e}")

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

    def close_trade_by_id(self, trade_id: str, reason: str) -> Optional[float]:
        """Close a specific trade by ID. Used by parallel strategies for custom exits.

        Returns realized P&L if closed, None if not found or close failed.
        """
        trade = next(
            (t for t in self.open_trades if t.id == trade_id and t.status == TradeStatus.ACTIVE),
            None,
        )
        if not trade:
            logger.warning(f"close_trade_by_id: Trade {trade_id} not found or not active")
            return None

        self._exit_trade(trade, reason)

        # _exit_trade removes trade from open_trades on success; check status
        if trade.status == TradeStatus.CLOSED:
            return trade.pnl
        return None

    def resolve_expired_trades(self):
        """Resolve trades still marked 'active' in the DB that have already expired.

        Runs at bot startup to handle trades orphaned when the bot was
        offline at market close.  Uses yfinance to look up the SPX closing
        price on the expiration date and calculates correct P&L.
        """
        # Use raw connection to avoid sqlite3 TIMESTAMP converter choking on
        # timezone-aware strings like '2026-02-03 00:00:00-05:00'
        open_trades = self.db.get_active_trades_raw()
        if not open_trades:
            return

        now = datetime.now(self.tz)
        resolved = 0

        for row in open_trades:
            exp_raw = row.get('expiration')
            if not exp_raw:
                continue

            # Parse expiration (stored as text: "2026-02-03", "2026-02-03 16:00:00",
            # or "2026-02-03 00:00:00-05:00")
            try:
                exp_str = str(exp_raw).split(' ')[0] if ' ' in str(exp_raw) else str(exp_raw)
                # Handle date-only "2026-02-03" format
                exp_date = datetime.strptime(exp_str[:10], '%Y-%m-%d').date()
            except (ValueError, AttributeError):
                continue

            # Only process trades whose expiration date has passed
            if exp_date >= now.date():
                continue

            trade_id = row['id']
            direction = row['direction']
            short_strike = row['short_strike']
            long_strike = row['long_strike']
            credit = row['credit_received']
            quantity = row['quantity']

            # Try to get SPX closing price on expiration date
            spx_close = self._get_historical_close(exp_date)

            if spx_close is not None:
                pnl = self._profit_at_price(
                    direction, short_strike, long_strike, credit, spx_close
                ) * quantity
                source = f"SPX close ${spx_close:,.2f}"
            else:
                # Fallback: assume expired worthless (common for far-OTM 0DTE)
                pnl = credit * 100 * quantity
                source = "assumed OTM (no historical data)"

            max_profit = credit * 100 * quantity
            pnl_pct = (pnl / max_profit * 100) if max_profit > 0 else 0.0

            # Use market close (4 PM ET) on expiration date as exit time
            exit_time = self.tz.localize(
                datetime.combine(exp_date, datetime.strptime('16:00', '%H:%M').time())
            )
            exit_reason = f"Expired (resolved at startup, {source})"

            self.db.close_orphaned_trade(trade_id, pnl, pnl_pct, exit_reason, exit_time)
            self.db.update_daily_stats(exp_date)
            resolved += 1

            logger.info(
                f"Resolved orphaned trade {trade_id[:8]}: {direction} "
                f"{short_strike}/{long_strike}, P&L=${pnl:+.2f} ({pnl_pct:+.1f}%) - {source}"
            )

        if resolved:
            logger.info(f"Resolved {resolved} orphaned expired trade(s)")

    @staticmethod
    def _get_historical_close(trade_date):
        """Get SPX closing price for a given date via yfinance."""
        try:
            import yfinance as yf
            from datetime import timedelta

            start = trade_date
            end = trade_date + timedelta(days=1)
            data = yf.download(
                '%5EGSPC', start=str(start), end=str(end),
                interval='1d', progress=False
            )

            if not data.empty:
                if hasattr(data.columns, 'nlevels') and data.columns.nlevels > 1:
                    data.columns = data.columns.get_level_values(0)
                return float(data['Close'].iloc[-1])
        except Exception as e:
            logger.warning(f"Failed to get historical SPX close for {trade_date}: {e}")
        return None

    @staticmethod
    def _profit_at_price(direction, short_strike, long_strike, credit, underlying_price):
        """Calculate per-contract P&L at a given underlying price (from DB row data)."""
        max_profit = credit * 100
        spread_width = abs(long_strike - short_strike)
        max_risk = (spread_width - credit) * 100

        if direction == 'bullish':
            if underlying_price >= short_strike:
                return max_profit
            elif underlying_price <= long_strike:
                return -max_risk
            else:
                loss = (short_strike - underlying_price) * 100
                return max_profit - loss
        else:
            if underlying_price <= short_strike:
                return max_profit
            elif underlying_price >= long_strike:
                return -max_risk
            else:
                loss = (underlying_price - short_strike) * 100
                return max_profit - loss

    def close_all_positions(self, reason: str = "Manual close"):
        """Close all open positions"""
        logger.warning(f"Closing all positions: {reason}")

        for trade in self.open_trades.copy():
            self._exit_trade(trade, reason)