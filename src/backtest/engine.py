"""
Backtest Engine

Replays historical SPX bars through the existing strategy engine.
Uses the same PulseDetector, BarBuilder, and SPXIncomeStrategy as live
trading, but drives them from a for-loop over historical data instead
of the real-time main loop.
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, date, time, timedelta
from typing import Dict, List, Optional

import pandas as pd
import pytz

from src.models.bar import Bar, BarType
from src.models.spread import CreditSpread, TradeDirection, OptionLeg
from src.models.trade import Trade, TradeStatus
from src.core.pulse_detector import PulseBarDetector
from src.core.bar_builder import BarBuilder
from src.core.strategy import SPXIncomeStrategy
from src.backtest.sim_broker import BacktestBroker
from src.backtest.data_loader import get_trading_days, get_bars_for_day

logger = logging.getLogger(__name__)

ET = pytz.timezone('America/New_York')


@dataclass
class BacktestTrade:
    """Lightweight trade record for backtest results."""
    trade_id: str
    entry_time: datetime
    exit_time: Optional[datetime] = None
    direction: str = ''
    short_strike: float = 0.0
    long_strike: float = 0.0
    credit_received: float = 0.0
    entry_price: float = 0.0
    exit_price: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    exit_reason: str = ''
    quantity: int = 1
    spx_at_entry: float = 0.0
    spx_at_exit: float = 0.0
    vix_at_entry: float = 0.0
    duration_minutes: int = 0
    strategy_type: str = 'daily_income'

    def to_dict(self) -> dict:
        return {
            'trade_id': self.trade_id,
            'entry_time': self.entry_time.isoformat() if self.entry_time else None,
            'exit_time': self.exit_time.isoformat() if self.exit_time else None,
            'direction': self.direction,
            'short_strike': self.short_strike,
            'long_strike': self.long_strike,
            'credit_received': self.credit_received,
            'entry_price': self.entry_price,
            'exit_price': self.exit_price,
            'pnl': self.pnl,
            'pnl_pct': self.pnl_pct,
            'exit_reason': self.exit_reason,
            'quantity': self.quantity,
            'spx_at_entry': self.spx_at_entry,
            'spx_at_exit': self.spx_at_exit,
            'vix_at_entry': self.vix_at_entry,
            'duration_minutes': self.duration_minutes,
            'strategy_type': self.strategy_type,
        }


@dataclass
class DailyResult:
    """Per-day summary."""
    date: date
    trades: int = 0
    pnl: float = 0.0
    spx_open: float = 0.0
    spx_close: float = 0.0
    vix: float = 0.0


class BacktestEngine:
    """
    Replays historical bars through the strategy engine.

    Uses the SAME PulseDetector, BarBuilder, and SPXIncomeStrategy as
    live trading. Only the data source (CSV vs live) and broker (instant
    fills vs real) differ.
    """

    def __init__(
        self,
        bars_df: pd.DataFrame,
        vix_daily: Dict[date, float],
        initial_capital: float = 50000.0,
        pulse_threshold: float = 10.0,
        spread_width: float = 5.0,
        profit_target_pct: float = 80.0,
        min_credit: float = 1.00,
        max_contracts: int = 5,
        slippage: float = 0.02,
        max_daily_loss_pct: float = 2.0,
        progress_callback=None,
    ):
        self.bars_df = bars_df
        self.vix_daily = vix_daily
        self.initial_capital = initial_capital
        self.max_contracts = max_contracts
        self.max_daily_loss_pct = max_daily_loss_pct
        self.progress_callback = progress_callback

        # Initialize broker
        self.broker = BacktestBroker(
            initial_capital=initial_capital,
            slippage=slippage,
        )

        # Initialize strategy (SAME classes as live trading)
        self.strategy = SPXIncomeStrategy(
            pulse_threshold=pulse_threshold,
            spread_width=spread_width,
            profit_target_pct=profit_target_pct,
            min_credit=min_credit,
        )
        self.pulse_detector = PulseBarDetector(pulse_threshold)

        # Results
        self.trades: List[BacktestTrade] = []
        self.daily_results: List[DailyResult] = []
        self.equity_curve: List[Dict] = []  # [{date, equity}]

        # State
        self._active_trade: Optional[Trade] = None
        self._pending_setup = None  # {direction, bar, trigger_price}
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0
        self._circuit_breaker_tripped: bool = False

    def run(self) -> Dict:
        """
        Run the full backtest.

        Returns:
            Dict with trades, daily_results, equity_curve, and summary stats
        """
        trading_days = get_trading_days(self.bars_df)
        total_days = len(trading_days)

        logger.info(
            f"Starting backtest: {total_days} trading days, "
            f"${self.initial_capital:,.0f} capital"
        )

        for day_idx, trading_day in enumerate(trading_days):
            self._process_day(trading_day)

            if self.progress_callback:
                self.progress_callback(day_idx + 1, total_days, trading_day)

        logger.info(
            f"Backtest complete: {len(self.trades)} trades over {total_days} days"
        )

        return {
            'trades': [t.to_dict() for t in self.trades],
            'daily_results': [
                {
                    'date': dr.date.isoformat(),
                    'trades': dr.trades,
                    'pnl': dr.pnl,
                    'spx_open': dr.spx_open,
                    'spx_close': dr.spx_close,
                    'vix': dr.vix,
                }
                for dr in self.daily_results
            ],
            'equity_curve': self.equity_curve,
            'initial_capital': self.initial_capital,
            'final_capital': self.broker.balance,
        }

    def _process_day(self, trading_day: date):
        """Process a single trading day."""
        day_bars = get_bars_for_day(self.bars_df, trading_day)
        if day_bars.empty:
            return

        # Reset daily state
        self._daily_pnl = 0.0
        self._daily_trades = 0
        self._circuit_breaker_tripped = False
        self._pending_setup = None

        # Get VIX for the day (fallback to 15 if not available)
        vix = self.vix_daily.get(trading_day, 15.0)

        # Build Bar objects from DataFrame rows
        bar_builder = BarBuilder(interval_minutes=30)
        bars_for_day: List[Bar] = []

        spx_open = float(day_bars.iloc[0]['open'])
        spx_close = float(day_bars.iloc[-1]['close'])

        max_daily_loss = self.initial_capital * (self.max_daily_loss_pct / 100.0)

        for timestamp, row in day_bars.iterrows():
            bar_dt = timestamp.to_pydatetime()
            if bar_dt.tzinfo is None:
                bar_dt = ET.localize(bar_dt)

            bar = Bar(
                timestamp=bar_dt,
                open=float(row['open']),
                high=float(row['high']),
                low=float(row['low']),
                close=float(row['close']),
                volume=int(row.get('volume', 0)),
            )
            bars_for_day.append(bar)

            # Update broker market state
            self.broker.set_market_state(
                price=bar.close,
                bar_high=bar.high,
                bar_low=bar.low,
                vix=vix,
                dt=bar_dt,
            )

            # Monitor active position
            if self._active_trade:
                self._monitor_position(bar, bar_dt)

            # Circuit breaker check
            if self._daily_pnl <= -max_daily_loss:
                self._circuit_breaker_tripped = True

            if self._circuit_breaker_tripped:
                continue

            # Skip if already traded today (1 trade per day max)
            if self._daily_trades >= 1:
                continue

            # Check breakout trigger on each bar
            if self._pending_setup and not self._active_trade:
                self._check_breakout(bar, bar_dt, vix)

            # Check for new setups (only in setup window: 9:30-11:30)
            bar_time = bar_dt.time()
            if time(9, 30) <= bar_time <= time(11, 30):
                if not self._pending_setup and not self._active_trade:
                    self._check_for_setup(bar, bar_dt, vix)

            # Expire pending setup after 11:30
            if self._pending_setup and bar_time > time(11, 30):
                self._pending_setup = None

        # End of day: expire any active position at settlement
        if self._active_trade:
            self._expire_position(spx_close, trading_day)

        # Record daily result
        daily = DailyResult(
            date=trading_day,
            trades=self._daily_trades,
            pnl=self._daily_pnl,
            spx_open=spx_open,
            spx_close=spx_close,
            vix=vix,
        )
        self.daily_results.append(daily)

        # Equity curve point
        self.equity_curve.append({
            'date': trading_day.isoformat(),
            'equity': self.broker.balance,
            'daily_pnl': self._daily_pnl,
        })

    def _check_for_setup(self, bar: Bar, bar_dt: datetime, vix: float):
        """Check if a completed bar creates a valid pulse setup."""
        direction = self.strategy.evaluate_setup(bar, bar.close)
        if direction is None:
            return

        # Set trigger price: breakout above bar high (bullish) or below low (bearish)
        if direction == TradeDirection.BULLISH:
            trigger_price = bar.high
        else:
            trigger_price = bar.low

        self._pending_setup = {
            'direction': direction,
            'bar': bar,
            'trigger_price': trigger_price,
            'timestamp': bar_dt,
        }

        logger.debug(
            f"[BT] {bar_dt.strftime('%Y-%m-%d %H:%M')} Pulse {direction.value} setup: "
            f"trigger {'above' if direction == TradeDirection.BULLISH else 'below'} "
            f"${trigger_price:,.2f}"
        )

    def _check_breakout(self, bar: Bar, bar_dt: datetime, vix: float):
        """Check if current bar triggers the pending setup's breakout."""
        setup = self._pending_setup
        direction = setup['direction']
        trigger_price = setup['trigger_price']

        # Check if breakout occurred within this bar
        triggered = False
        if direction == TradeDirection.BULLISH and bar.high > trigger_price:
            triggered = True
        elif direction == TradeDirection.BEARISH and bar.low < trigger_price:
            triggered = True

        if not triggered:
            return

        # Execute trade
        self._execute_entry(bar, bar_dt, direction, vix)

    def _execute_entry(self, bar: Bar, bar_dt: datetime, direction: TradeDirection, vix: float):
        """Execute a trade entry."""
        current_price = bar.close

        # Get options chain
        expiration_str = bar_dt.strftime('%Y-%m-%d')
        chain = self.broker.get_options_chain('SPX', expiration_str)

        if not chain:
            return

        # Construct spread using the SAME strategy logic
        spread = self.strategy.construct_spread(current_price, direction, chain)
        if spread is None:
            self._pending_setup = None
            return

        # Calculate quantity
        quantity = min(self.max_contracts, 1)  # Backtest uses fixed sizing

        # Place order (instant fill)
        order_id = self.broker.place_spread_order(spread, quantity)
        order_status = self.broker.get_order_status(order_id)

        if order_status['status'] != 'filled':
            self._pending_setup = None
            return

        # Create Trade object (SAME as live trading)
        trade = Trade(
            id=str(uuid.uuid4()),
            spread=spread,
            status=TradeStatus.ACTIVE,
            setup_bar=self._pending_setup['bar'],
            entry_price=order_status['fill_price'],
            entry_time=bar_dt,
            entry_order_id=order_id,
            quantity=quantity,
        )

        self._active_trade = trade
        self._pending_setup = None
        self._daily_trades += 1

        logger.debug(
            f"[BT] {bar_dt.strftime('%Y-%m-%d %H:%M')} ENTRY: {direction.value} "
            f"${spread.short_leg.strike}/{spread.long_leg.strike} "
            f"credit=${order_status['fill_price']:.2f}"
        )

    def _monitor_position(self, bar: Bar, bar_dt: datetime):
        """Monitor active position for exit conditions."""
        trade = self._active_trade
        if not trade or trade.status != TradeStatus.ACTIVE:
            return

        # Get current spread value
        current_value = self.broker.get_position_value(trade.spread)
        trade.update_pnl(current_value)

        # Inject SPX price for 1pm check
        trade._current_spx_price = bar.close

        # Check exit conditions using the SAME strategy logic
        should_exit, reason = self.strategy.should_exit(trade, current_value, bar_dt)

        if should_exit:
            self._close_position(trade, bar, bar_dt, reason, current_value)

    def _close_position(self, trade: Trade, bar: Bar, bar_dt: datetime, reason: str, current_value: float):
        """Close an active position."""
        if "expiration" in reason.lower():
            # Settlement at bar price
            final_pnl = trade.spread.profit_at_price(bar.close) * trade.quantity
            trade.exit_price = 0.0
            trade.exit_time = bar_dt
            trade.exit_reason = reason
            trade.status = TradeStatus.CLOSED
            trade.pnl = final_pnl
            max_profit = trade.spread.max_profit * trade.quantity
            trade.pnl_percent = (final_pnl / max_profit * 100) if max_profit > 0 else 0
        else:
            # Active close
            limit_price = min(current_value + 0.10, trade.spread.spread_width)
            order_id = self.broker.close_spread(trade.spread, trade.quantity, limit_price)
            order_status = self.broker.get_order_status(order_id)
            exit_price = order_status['fill_price']

            trade.close(
                exit_price=exit_price,
                exit_time=bar_dt,
                reason=reason,
            )

        self._daily_pnl += trade.pnl or 0

        # Record trade
        bt_trade = BacktestTrade(
            trade_id=trade.id,
            entry_time=trade.entry_time,
            exit_time=trade.exit_time,
            direction=trade.spread.direction.value,
            short_strike=trade.spread.short_leg.strike,
            long_strike=trade.spread.long_leg.strike,
            credit_received=trade.spread.credit_received,
            entry_price=trade.entry_price,
            exit_price=trade.exit_price or 0,
            pnl=trade.pnl or 0,
            pnl_pct=trade.pnl_percent or 0,
            exit_reason=trade.exit_reason or '',
            quantity=trade.quantity,
            spx_at_entry=trade.spread.underlying_price_at_entry or 0,
            spx_at_exit=bar.close,
            vix_at_entry=self.broker._current_vix,
            duration_minutes=int((trade.exit_time - trade.entry_time).total_seconds() / 60)
            if trade.exit_time else 0,
        )
        self.trades.append(bt_trade)

        logger.debug(
            f"[BT] {bar_dt.strftime('%Y-%m-%d %H:%M')} EXIT: {reason} "
            f"P&L=${trade.pnl:+.2f}"
        )

        self._active_trade = None

    def _expire_position(self, settlement_price: float, trading_day: date):
        """Expire active position at end of day (4:00 PM settlement)."""
        trade = self._active_trade
        if not trade:
            return

        expire_dt = ET.localize(datetime.combine(trading_day, time(16, 0)))
        final_pnl = trade.spread.profit_at_price(settlement_price) * trade.quantity

        trade.exit_price = 0.0
        trade.exit_time = expire_dt
        trade.exit_reason = 'Expiration (4:00 PM)'
        trade.status = TradeStatus.CLOSED
        trade.pnl = final_pnl
        max_profit = trade.spread.max_profit * trade.quantity
        trade.pnl_percent = (final_pnl / max_profit * 100) if max_profit > 0 else 0

        # Update balance for expiration P&L
        # For expiration, P&L = credit received - any ITM obligation
        # The broker already credited us at entry, so we just need to debit any ITM loss
        if final_pnl < trade.spread.max_profit * trade.quantity:
            # Position lost money relative to max profit
            itm_cost = (trade.spread.max_profit * trade.quantity - final_pnl) / 100
            self.broker.balance -= itm_cost

        self._daily_pnl += final_pnl

        bt_trade = BacktestTrade(
            trade_id=trade.id,
            entry_time=trade.entry_time,
            exit_time=expire_dt,
            direction=trade.spread.direction.value,
            short_strike=trade.spread.short_leg.strike,
            long_strike=trade.spread.long_leg.strike,
            credit_received=trade.spread.credit_received,
            entry_price=trade.entry_price,
            exit_price=0.0,
            pnl=final_pnl,
            pnl_pct=trade.pnl_percent or 0,
            exit_reason='Expiration (4:00 PM)',
            quantity=trade.quantity,
            spx_at_entry=trade.spread.underlying_price_at_entry or 0,
            spx_at_exit=settlement_price,
            vix_at_entry=self.broker._current_vix,
            duration_minutes=int((expire_dt - trade.entry_time).total_seconds() / 60)
            if trade.entry_time else 0,
        )
        self.trades.append(bt_trade)

        logger.debug(
            f"[BT] {trading_day} EXPIRED: SPX=${settlement_price:,.2f} "
            f"P&L=${final_pnl:+.2f}"
        )

        self._active_trade = None
