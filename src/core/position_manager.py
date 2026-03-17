from typing import List, Optional, Dict, TYPE_CHECKING
from datetime import datetime, timedelta
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

# Cooldown period after a DB write failure — no new trades during this window.
DB_FAILURE_COOLDOWN_SECONDS = 300  # 5 minutes


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
        self.recently_closed_trades: List[Dict] = []  # Rich data for notifications
        self.tz = pytz.timezone("America/New_York")

        # Daily market cache (set once per day by TradingBot)
        self._day_open: Optional[float] = None
        self._prev_close: Optional[float] = None

        # DB failure cooldown — blocks new entries for DB_FAILURE_COOLDOWN_SECONDS
        # after a write failure.  Replaces the old permanent _db_write_failed flag
        # so the bot can recover automatically without a restart.
        self._db_failure_cooldown_until: Optional[datetime] = None
        self._db_alert_sent: bool = False  # dedup Discord DB failure alerts

        if pdt_tracker:
            logger.info("PositionManager initialized with PDT protection")
        else:
            logger.info("PositionManager initialized (no PDT protection)")
    
    def set_daily_cache(self, day_open: float, prev_close: float):
        """Cache the day's open and previous close prices (called once per day by TradingBot)."""
        self._day_open = day_open
        self._prev_close = prev_close
        logger.info(f"Daily cache set: day_open=${day_open:,.2f}, prev_close=${prev_close:,.2f}")

    def _enter_db_cooldown(self):
        """Enter a cooldown period after a DB write failure.

        During cooldown, no new trades are allowed.  The bot keeps running
        (monitoring existing positions, processing exits) but won't attempt
        new entries that would require DB writes.
        """
        self._db_failure_cooldown_until = (
            datetime.now(self.tz) + timedelta(seconds=DB_FAILURE_COOLDOWN_SECONDS)
        )
        # Keep legacy flag for any external code that checks it
        self._db_write_failed = True
        logger.critical(
            f"DB failure cooldown active for {DB_FAILURE_COOLDOWN_SECONDS}s -- "
            f"no new trades until {self._db_failure_cooldown_until.strftime('%H:%M:%S')} ET"
        )

    def _is_db_cooldown_active(self) -> bool:
        """Check whether the DB failure cooldown is still active."""
        if self._db_failure_cooldown_until is None:
            return False
        now = datetime.now(self.tz)
        if now >= self._db_failure_cooldown_until:
            # Cooldown expired — clear it
            logger.info("DB failure cooldown expired -- new entries allowed")
            self._db_failure_cooldown_until = None
            self._db_write_failed = False
            self._db_alert_sent = False
            return False
        return True

    def _send_db_failure_alert(self, trade, error):
        """Log a critical DB write failure (log-only, no Discord notification).

        Discord alerts for DB failures and watchdog restarts are disabled to
        avoid notification spam.  The error is always logged at CRITICAL level.
        """
        if self._db_alert_sent:
            logger.warning(
                f"DB failure alert suppressed (already logged this cooldown): {error}"
            )
            return
        self._db_alert_sent = True
        logger.critical(
            f"DATABASE WRITE FAILED - Trade {trade.id} is LIVE on broker but "
            f"the database write failed after all retries. "
            f"Direction: {trade.spread.direction.value}, "
            f"Strikes: {trade.spread.short_leg.strike}/{trade.spread.long_leg.strike}, "
            f"Error: {error}"
        )

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
            # Block new entries during DB failure cooldown
            if self._is_db_cooldown_active():
                remaining = (self._db_failure_cooldown_until - datetime.now(self.tz)).total_seconds()
                logger.warning(
                    f"BLOCKED: DB failure cooldown active ({remaining:.0f}s remaining). "
                    f"No new trades until {self._db_failure_cooldown_until.strftime('%H:%M:%S')} ET."
                )
                return None

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
                        'min_bar_range_threshold': self.strategy.pulse_detector.min_bar_range_points,
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
                    f"LOW CREDIT: ${credit_quality['credit_received']:.2f} - "
                    f"simulated chain may not reflect live pricing"
                )

            # ── Write PENDING record BEFORE placing broker order ──
            # This ensures the DB knows about the trade even if the post-fill
            # update fails, preventing orphaned live positions.
            pending_trade_id = str(uuid.uuid4())
            pending_trade = Trade(
                id=pending_trade_id,
                spread=spread,
                status=TradeStatus.PENDING,
                setup_bar=setup_bar,
                entry_price=spread.credit_received,  # Estimated; updated on fill
                entry_time=datetime.now(self.tz),
                quantity=quantity,
            )
            pending_trade._strategy_type = strategy_type
            try:
                self.db.save_trade_with_retry(
                    pending_trade,
                    context={'strategy_type': strategy_type},
                )
                logger.info(f"Pending trade {pending_trade_id} written to DB")
            except Exception as db_err:
                logger.critical(
                    f"Cannot write pending trade to DB: {db_err}. "
                    f"Aborting entry to prevent unrecorded live trade."
                )
                self._enter_db_cooldown()
                return None

            # Place order
            order_id = self.broker.place_spread_order(spread, quantity, metadata=bar_metadata)

            # Wait briefly for fill
            import time
            time.sleep(2)

            # Check order status
            order_status = self.broker.get_order_status(order_id)

            if order_status['status'] != 'filled':
                logger.error(f"Order not filled: {order_status['status']}")
                # Clean up the pending record
                try:
                    pending_trade.status = TradeStatus.CANCELLED
                    pending_trade.exit_price = 0.0
                    pending_trade.exit_reason = f"Order not filled: {order_status['status']}"
                    pending_trade.pnl = 0.0
                    pending_trade.pnl_percent = 0.0
                    self.db.update_trade_close(pending_trade)
                except Exception:
                    pass
                return None

            # Track actual filled quantity
            filled_quantity = order_status.get('filled_quantity', quantity)
            if filled_quantity == 0:
                logger.error(
                    f"ZERO FILL: requested {quantity} contracts, "
                    f"got 0. Order {order_id} did not execute."
                )
                return None
            actual_quantity = filled_quantity if filled_quantity else quantity
            if actual_quantity != quantity:
                logger.warning(
                    f"Partial fill: requested {quantity}, filled {actual_quantity}. "
                    f"Proceeding with {actual_quantity} contracts."
                )

            # Create trade record (reusing the pending ID so DB row is updated)
            actual_fill = order_status['fill_price']
            trade = Trade(
                id=pending_trade_id,
                spread=spread,
                status=TradeStatus.ACTIVE,
                setup_bar=setup_bar,
                entry_price=actual_fill,
                entry_time=self._parse_fill_time(order_status.get('fill_time')) or datetime.now(self.tz),
                entry_order_id=order_id,
                quantity=actual_quantity
            )
            trade._strategy_type = strategy_type  # Carried through to exit notifications

            # Update credit_received with actual fill so P&L cap and
            # dashboard calculations use the real execution price
            original_credit = spread.credit_received
            spread.credit_received = actual_fill
            if abs(actual_fill - original_credit) > 0.001:
                logger.info(
                    f"  Credit updated: theoretical=${original_credit:.4f} -> "
                    f"actual=${actual_fill:.4f}"
                )

            # Log per-leg fill details if available
            if order_status.get('leg_fills'):
                for lf in order_status['leg_fills']:
                    logger.info(
                        f"  Entry leg {lf['leg_id']}: {lf['instruction']} "
                        f"${lf['price']:.4f} x{lf['quantity']} "
                        f"(fees=${lf['fees']:.4f})"
                    )

            # Add to open trades
            self.open_trades.append(trade)

            # Build entry context for database analytics
            entry_context = {'strategy_type': strategy_type}
            try:
                from src.data.vix_provider import classify_vix
                spx_price = spread.underlying_price_at_entry
                entry_context['spx_at_entry'] = round(spx_price, 2)
                vix_price = self._get_vix_price()
                entry_context['vix_at_entry'] = vix_price
                entry_context['vix_regime'] = classify_vix(vix_price)
                if self._day_open is not None:
                    entry_context['day_open'] = round(self._day_open, 2)
                    entry_context['intraday_move_at_entry'] = round(
                        ((spx_price - self._day_open) / self._day_open) * 100, 4
                    )
                if self._day_open is not None and self._prev_close is not None and self._prev_close > 0:
                    entry_context['gap_pct'] = round(
                        ((self._day_open - self._prev_close) / self._prev_close) * 100, 4
                    )
                # Day of week and entry time bucket
                entry_dt = trade.entry_time
                entry_context['day_of_week'] = entry_dt.weekday()  # 0=Mon
                entry_context['day_of_week_name'] = entry_dt.strftime('%A')
                try:
                    from src.data.sma_provider import classify_entry_time_bucket
                    entry_context['entry_time_bucket'] = classify_entry_time_bucket(entry_dt)
                except Exception:
                    pass

                # SMA distances and prior-day range
                try:
                    from src.data.sma_provider import (
                        get_sma50, get_sma200, get_prior_day_range,
                        compute_sma_distance, classify_vs_prior_range,
                    )
                    sma50 = get_sma50()
                    sma200 = get_sma200()
                    if sma50 is not None:
                        entry_context['sma50'] = sma50
                        pts, pct = compute_sma_distance(spx_price, sma50)
                        entry_context['spx_vs_sma50'] = pts
                        entry_context['spx_vs_sma50_pct'] = pct
                    if sma200 is not None:
                        entry_context['sma200'] = sma200
                        pts, pct = compute_sma_distance(spx_price, sma200)
                        entry_context['spx_vs_sma200'] = pts
                        entry_context['spx_vs_sma200_pct'] = pct
                    prior_high, prior_low = get_prior_day_range()
                    if prior_high is not None:
                        entry_context['prior_day_high'] = prior_high
                        entry_context['prior_day_low'] = prior_low
                        entry_context['spx_vs_prior_range'] = classify_vs_prior_range(
                            spx_price, prior_high, prior_low
                        )
                except Exception as e_sma:
                    logger.debug(f"SMA/range lookup failed: {e_sma}")

                # Economic calendar: tag trade with same-day events
                try:
                    from src.data.economic_calendar import get_today_events, format_events_short
                    import json as _json
                    today_events = get_today_events()
                    if today_events:
                        event_tags = [e['event'] for e in today_events]
                        entry_context['economic_events'] = _json.dumps(event_tags)
                        logger.info(f"  Economic events today: {format_events_short(today_events)}")
                except Exception as e_cal:
                    logger.debug(f"Economic calendar lookup failed: {e_cal}")

                # Slippage: actual fill vs theoretical mid-price
                theoretical_mid = getattr(spread, 'theoretical_mid_credit', None)
                actual_fill = order_status['fill_price']
                if theoretical_mid is not None:
                    slippage = round(actual_fill - theoretical_mid, 4)
                    slippage_pct = round((slippage / theoretical_mid) * 100, 2) if theoretical_mid != 0 else 0.0
                    entry_context['theoretical_credit'] = round(theoretical_mid, 4)
                    entry_context['actual_credit'] = round(actual_fill, 4)
                    entry_context['slippage'] = slippage
                    entry_context['slippage_pct'] = slippage_pct
                    logger.info(f"  Slippage: ${slippage:+.4f} ({slippage_pct:+.2f}%) [mid=${theoretical_mid:.4f}, fill=${actual_fill:.4f}]")

                logger.debug(f"Entry context: {entry_context}")
            except Exception as e:
                logger.warning(f"Failed to build entry context: {e}")

            # Update the pending DB record with fill data (retry with backoff).
            # The trade is already live on the broker (order filled above)
            # AND has a pending record in the DB from before order placement.
            try:
                self.db.save_trade_with_retry(trade, context=entry_context)
            except Exception as db_err:
                logger.critical(
                    f"DATABASE WRITE FAILED for live trade {trade.id}: {db_err}. "
                    f"Trade is LIVE on broker (pending record exists). "
                    f"Entering cooldown -- no new trades for {DB_FAILURE_COOLDOWN_SECONDS}s."
                )
                self._enter_db_cooldown()
                # Send Discord alert so the operator knows immediately
                self._send_db_failure_alert(trade, db_err)
                # Trade stays in open_trades so it can still be monitored/closed
                return trade

            logger.info(f"Trade entered successfully: {trade.id}")
            logger.info(f"  Entry price: ${trade.entry_price:.2f}")
            logger.info(f"  Max profit: ${spread.max_profit * quantity:.2f}")
            logger.info(f"  Max risk: ${spread.max_risk * quantity:.2f}")

            return trade

        except Exception as e:
            logger.error(f"Failed to enter trade: {e}", exc_info=True)
            return None
        except BaseException as e:
            # Catch SystemExit / KeyboardInterrupt that bypass except Exception.
            # The trade may already be in open_trades (line 208), so log the
            # failure clearly to aid post-mortem diagnosis.
            try:
                logger.critical(
                    f"CRITICAL: BaseException during enter_trade: "
                    f"{type(e).__name__}: {e}"
                )
            except Exception:
                pass  # logging itself may be broken
            raise  # re-raise so the caller can handle shutdown

    def _sync_external_closes(self):
        """Remove trades that were closed externally (e.g. dashboard manual close).

        Checks the DB status for each in-memory open trade. If the DB says
        'closed', the trade was closed outside the bot — remove it from
        the in-memory list so the bot doesn't try to manage or close it again.
        """
        try:
            for trade in self.open_trades.copy():
                row = self.db.get_trade(trade.id)
                if row and row.get('status', '').lower() == 'closed':
                    logger.info(
                        f"Trade {trade.id[:8]} was closed externally "
                        f"(reason: {row.get('exit_reason', 'unknown')}). "
                        f"Removing from active monitoring."
                    )
                    self.open_trades.remove(trade)
                    # Track for portfolio risk updates
                    pnl = row.get('pnl', 0) or 0
                    self.recently_closed.append({'id': trade.id, 'pnl': pnl})
        except Exception as e:
            logger.debug(f"External close sync check failed (non-fatal): {e}")

    def monitor_positions(self) -> float:
        """Monitor all open positions for exit conditions.

        Returns:
            Total realized P&L from trades closed this cycle.
        """
        if not self.open_trades:
            return 0.0

        # Detect positions closed externally (e.g. dashboard manual close)
        self._sync_external_closes()

        if not self.open_trades:
            return 0.0

        logger.debug(f"Monitoring {len(self.open_trades)} open positions")
        realized_pnl = 0.0

        current_time = datetime.now(self.tz)

        for trade in self.open_trades.copy():
            if trade.status != TradeStatus.ACTIVE:
                continue

            # Skip trades that already have a close order pending
            if getattr(trade, '_close_pending', False):
                logger.debug(f"Trade {trade.id[:8]}: close already pending, skipping")
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
                # Track close retries for escalation
                close_retries = getattr(trade, '_close_retries', 0)

                # Close position - cap at spread width (max possible value)
                # Widen limit price after repeated failures to improve fill odds
                max_debit = trade.spread.spread_width
                price_cushion = 0.10 + (0.05 * min(close_retries, 10))
                limit_price = min(current_value + price_cushion, max_debit)

                if close_retries > 0:
                    logger.warning(
                        f"Close retry #{close_retries} for trade {trade.id[:8]}, "
                        f"limit ${limit_price:.2f} (cushion ${price_cushion:.2f})"
                    )
                if close_retries >= 10:
                    logger.critical(
                        f"ALERT: Trade {trade.id[:8]} has failed to close after "
                        f"{close_retries} attempts. Manual review required."
                    )

                # Mark close pending so monitor_positions() won't re-trigger
                trade._close_pending = True

                order_id = self.broker.close_spread(
                    trade.spread,
                    trade.quantity,
                    limit_price
                )

                if not order_id:
                    trade._close_retries = close_retries + 1
                    logger.error(
                        f"Close order failed for trade {trade.id[:8]} "
                        f"(attempt {trade._close_retries}), will retry next cycle"
                    )
                    trade._close_pending = False
                    return

                # Wait for fill
                import time
                time.sleep(2)

                order_status = self.broker.get_order_status(order_id)
                if order_status['status'] != 'filled':
                    trade._close_retries = close_retries + 1
                    logger.warning(
                        f"Close order {order_id} not filled (status: {order_status['status']}), "
                        f"attempt {trade._close_retries}, will retry next cycle"
                    )
                    trade._close_pending = False
                    return

                exit_price = order_status['fill_price']

                # Log per-leg fill details if available
                if order_status.get('leg_fills'):
                    for lf in order_status['leg_fills']:
                        logger.info(
                            f"  Exit leg {lf['leg_id']}: {lf['instruction']} "
                            f"${lf['price']:.4f} x{lf['quantity']} "
                            f"(fees=${lf['fees']:.4f})"
                        )

                trade.close(
                    exit_price=exit_price,
                    exit_time=self._parse_fill_time(order_status.get('fill_time')) or datetime.now(self.tz),
                    reason=reason
                )
                trade.exit_order_id = order_id

                logger.info(f"Trade closed at ${exit_price:.2f}")
            
            logger.info(f"  P&L: ${trade.pnl:.2f}")
            logger.info(f"  Duration: {trade.duration:.1f} hours")
            
            # Remove from open trades
            self.open_trades.remove(trade)

            # Track for portfolio risk updates
            self.recently_closed.append({'id': trade.id, 'pnl': trade.pnl or 0.0})

            # Rich data for notifications
            max_risk = trade.spread.max_risk * trade.quantity if trade.spread.max_risk else 0
            max_profit = trade.spread.max_profit * trade.quantity if trade.spread.max_profit else 0
            duration = ''
            if trade.entry_time and trade.exit_time:
                dur = trade.exit_time - trade.entry_time
                hours, remainder = divmod(int(dur.total_seconds()), 3600)
                minutes = remainder // 60
                duration = f"{hours}h {minutes}m" if hours else f"{minutes}m"
            self.recently_closed_trades.append({
                'direction': trade.spread.direction.value,
                'pnl': trade.pnl or 0.0,
                'pnl_pct': ((trade.pnl or 0) / max_risk * 100) if max_risk else 0,
                'max_profit_pct': ((trade.pnl or 0) / max_profit * 100) if max_profit else 0,
                'reason': reason,
                'strikes': f"{trade.spread.short_leg.strike}/{trade.spread.long_leg.strike}",
                'short_strike': trade.spread.short_leg.strike,
                'long_strike': trade.spread.long_leg.strike,
                'duration': duration,
                'strategy': getattr(trade, '_strategy_type', 'unknown'),
            })

            # Update database (use targeted UPDATE to preserve entry context)
            # Retry with backoff — DB lock during close is recoverable and must
            # not crash the bot or lose the exit record.
            import time as _time_mod
            _close_saved = False
            for _attempt in range(3):
                try:
                    self.db.update_trade_close(trade)
                    _close_saved = True
                    break
                except Exception as _db_close_err:
                    if _attempt < 2:
                        _delay = (0.1, 0.5)[min(_attempt, 1)]
                        logger.warning(
                            f"update_trade_close attempt {_attempt + 1}/3 failed: "
                            f"{_db_close_err}. Retrying in {_delay}s..."
                        )
                        _time_mod.sleep(_delay)
                    else:
                        logger.critical(
                            f"update_trade_close FAILED for trade {trade.id} after 3 attempts: "
                            f"{_db_close_err}. Trade closed in memory but DB not updated."
                        )
                        self._enter_db_cooldown()

            if _close_saved:
                try:
                    self.db.update_daily_stats(trade.entry_time.date())
                except Exception as _stats_err:
                    logger.warning(f"update_daily_stats failed (non-fatal): {_stats_err}")

            # Build and save exit context for analytics
            try:
                exit_context: Dict = {}
                spx_at_exit = self.broker.get_current_price("SPX")
                exit_context['spx_at_exit'] = round(spx_at_exit, 2)

                # VIX at exit for P&L attribution
                vix_at_exit = self._get_vix_price()
                if vix_at_exit is not None:
                    exit_context['vix_at_exit'] = round(vix_at_exit, 2)

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

            # Capture commissions/fees from broker for this trade
            self._capture_trade_fees(trade)

            # Reconcile P&L from Schwab transaction history (background thread)
            self._reconcile_broker_pnl(trade)

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
    
    def _parse_fill_time(self, fill_time_str):
        """Parse Schwab execution timestamp into a timezone-aware datetime.

        Schwab returns ISO 8601 timestamps (e.g., '2026-03-12T14:50:23+0000').
        Returns None if parsing fails so caller can fall back to local clock.
        """
        if not fill_time_str:
            return None
        try:
            from dateutil import parser as dt_parser
            dt = dt_parser.isoparse(fill_time_str)
            # Convert to our configured timezone
            return dt.astimezone(self.tz)
        except Exception as e:
            logger.warning(f"Could not parse Schwab fill time '{fill_time_str}': {e}")
            return None

    def _capture_trade_fees(self, trade):
        """Capture commissions/fees from broker for entry and exit orders.

        Queries the broker for fee details on each order and writes the
        combined total to the trade's commissions field.  Failures are
        logged but never block trade processing.
        """
        if not hasattr(self.broker, 'get_order_fees'):
            return
        try:
            total_fees = 0.0
            if trade.entry_order_id:
                total_fees += self.broker.get_order_fees(trade.entry_order_id)
            if trade.exit_order_id:
                total_fees += self.broker.get_order_fees(trade.exit_order_id)
            if total_fees > 0:
                self.db.update_trade_commissions(trade.id, total_fees)
                logger.info(f"Commissions captured for {trade.id[:8]}: ${total_fees:.2f}")
        except Exception as e:
            logger.warning(f"Failed to capture fees for trade {trade.id[:8]}: {e}")

    def _reconcile_broker_pnl(self, trade):
        """Spawn background thread to reconcile P&L from Schwab transactions.

        After a short delay (to let Schwab settle), fetches today's transactions,
        matches the 4 legs by order ID, and overwrites the price-based P&L with
        the summed netAmount (which includes all fees).  Falls back to the
        price-based value if matching fails.
        """
        if not hasattr(self.broker, 'get_transactions_for_date'):
            return
        import threading
        thread = threading.Thread(
            target=self._reconcile_broker_pnl_worker,
            args=(
                trade.id,
                trade.entry_order_id,
                trade.exit_order_id,
                trade.pnl,
            ),
            daemon=True,
        )
        thread.start()

    def _reconcile_broker_pnl_worker(self, trade_id, entry_order_id, exit_order_id, gross_pnl):
        """Worker: fetch Schwab transactions, compute broker P&L, update DB.

        For normal closes (exit_order_id exists): expects 4 legs (2 entry + 2 exit),
        sums all netAmount fields to get broker P&L.

        For expirations (no exit_order_id): expects 2 entry legs only, computes
        broker entry credit from those, then derives P&L using the known
        cash-settlement value (SPX close vs strikes).
        """
        import time as _t
        _t.sleep(8)  # wait for Schwab to settle transactions

        today = datetime.now(self.tz).date()
        is_expiration = exit_order_id is None

        order_ids = set()
        if entry_order_id:
            order_ids.add(str(entry_order_id))
        if exit_order_id:
            order_ids.add(str(exit_order_id))

        if not order_ids:
            logger.warning(
                f"Broker P&L skip for {trade_id[:8]}: no order IDs available"
            )
            return

        # For expirations we only need 2 entry legs; for normal closes we need 4
        expected_legs = 2 if is_expiration else 4

        matched = []
        for attempt in range(2):
            try:
                transactions = self.broker.get_transactions_for_date(today)
            except Exception as e:
                logger.warning(f"Broker P&L fetch failed for {trade_id[:8]}: {e}")
                transactions = []

            matched = [t for t in transactions if t['order_id'] in order_ids]

            if len(matched) >= expected_legs:
                break

            if attempt == 0:
                logger.warning(
                    f"Broker P&L for {trade_id[:8]}: found {len(matched)}/{expected_legs} legs, "
                    f"retrying in 15s..."
                )
                _t.sleep(15)

        if len(matched) < expected_legs:
            logger.warning(
                f"Broker P&L reconciliation failed for {trade_id[:8]}: "
                f"only {len(matched)} of {expected_legs} expected legs matched. "
                f"Keeping price-based P&L (${gross_pnl:.2f})"
            )
            # Still store gross_pnl for reference even on failure
            try:
                self.db.update_trade_broker_pnl(
                    trade_id, gross_pnl, gross_pnl, 0.0
                )
            except Exception:
                pass
            return

        if is_expiration:
            # For expirations: entry legs give us the broker net credit.
            # Settlement is deterministic for cash-settled SPX options.
            broker_entry_credit = round(sum(t['net_amount'] for t in matched), 2)

            # Read trade details from DB to compute settlement value
            try:
                import sqlite3
                conn = sqlite3.connect(str(self.db.db_path))
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT direction, short_strike, long_strike, quantity, "
                    "spx_at_exit FROM trades WHERE id = ?",
                    (trade_id,)
                ).fetchone()
                conn.close()
            except Exception as e:
                logger.warning(f"Failed to read trade for expiration reconciliation: {e}")
                self.db.update_trade_broker_pnl(trade_id, gross_pnl, gross_pnl, 0.0)
                return

            if not row or row['spx_at_exit'] is None:
                logger.warning(
                    f"Broker P&L for {trade_id[:8]}: missing trade data for "
                    f"expiration settlement calc"
                )
                self.db.update_trade_broker_pnl(trade_id, gross_pnl, gross_pnl, 0.0)
                return

            spx_close = row['spx_at_exit']
            direction = row['direction']
            short_strike = row['short_strike']
            long_strike = row['long_strike']
            quantity = row['quantity']

            # Compute settlement cost (cash-settled, no exit fees for SPX expirations)
            settlement_per_share = self._profit_at_price(
                direction, short_strike, long_strike, 0, spx_close
            )
            # settlement_per_share is profit with credit=0, so it's the settlement
            # value relative to zero. Negative means the spread settled ITM (loss).
            settlement_cost = round(settlement_per_share * quantity, 2)

            # Broker P&L = entry credit (net of fees) + settlement value
            broker_pnl = round(broker_entry_credit + settlement_cost, 2)
            # Entry commissions = price-based credit - broker credit
            # Price-based credit comes from: gross_pnl - settlement_cost
            price_based_credit = round((gross_pnl or 0) - settlement_cost, 2)
            commissions = round(price_based_credit - broker_entry_credit, 2)

            logger.info(
                f"Expiration reconciliation for {trade_id[:8]}: "
                f"broker entry credit=${broker_entry_credit:.2f}, "
                f"settlement=${settlement_cost:.2f}, "
                f"broker P&L=${broker_pnl:.2f} (entry fees=${commissions:.2f})"
            )
        else:
            # Normal close: sum all 4 legs
            broker_pnl = round(sum(t['net_amount'] for t in matched), 2)
            commissions = round((gross_pnl or 0) - broker_pnl, 2)

        try:
            self.db.update_trade_broker_pnl(
                trade_id, broker_pnl, gross_pnl, commissions
            )
            logger.info(
                f"Broker P&L reconciled for {trade_id[:8]}: "
                f"${broker_pnl:.2f} (gross ${gross_pnl:.2f}, "
                f"fees ${commissions:.2f})"
            )
        except Exception as e:
            logger.warning(
                f"Failed to write broker P&L for {trade_id[:8]}: {e}"
            )

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

            # Calculate duration from entry_time to exit_time
            entry_raw = row.get('entry_time')
            duration = ''
            if entry_raw:
                try:
                    entry_dt = datetime.fromisoformat(str(entry_raw))
                    dur = exit_time - entry_dt
                    hours, remainder = divmod(int(dur.total_seconds()), 3600)
                    minutes = remainder // 60
                    duration = f"{hours}h {minutes}m" if hours else f"{minutes}m"
                except (ValueError, TypeError):
                    pass

            # Queue notification with clear startup reconciliation label
            self.recently_closed_trades.append({
                'direction': direction,
                'pnl': pnl,
                'pnl_pct': pnl_pct,
                'reason': exit_reason,
                'strikes': f"{short_strike}/{long_strike}",
                'duration': duration,
                'is_reconciliation': True,
            })

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