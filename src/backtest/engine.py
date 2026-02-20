"""
Backtest Engine

Replays historical SPX bars through the existing strategy engine.
Uses the same PulseDetector, BarBuilder, SPXIncomeStrategy, TagNTurnStrategy,
BnBStrategy, and ORBStrategy as live trading, but drives them from a for-loop
over historical data instead of the real-time main loop.

Supports 4 strategies running in parallel with 2 position slots:
  - 0DTE slot: shared by Daily Income, ORB, and B&B (1 trade/day max)
  - Swing slot: Tag 'n Turn only (positions can span multiple days)
"""

import logging
import math
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, date, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import pytz

from src.models.bar import Bar, BarType
from src.models.spread import CreditSpread, TradeDirection, OptionLeg
from src.models.trade import Trade, TradeStatus
from src.core.pulse_detector import PulseBarDetector
from src.core.bar_builder import BarBuilder
from src.core.strategy import SPXIncomeStrategy
from src.core.tag_n_turn import TagNTurnStrategy
from src.core.bnb_strategy import BnBStrategy
from src.core.orb_strategy import ORBStrategy
from src.backtest.sim_broker import BacktestBroker
from src.backtest.data_loader import get_trading_days, get_bars_for_day

logger = logging.getLogger(__name__)

ET = pytz.timezone('America/New_York')


def _nyse_half_days(year: int) -> set:
    """Return set of NYSE half-day dates (1:00 PM close) for a given year.

    NYSE half-days:
    - Day before Independence Day (Jul 3, unless Jul 4 is Sat/Sun)
    - Black Friday (4th Friday of November)
    - Christmas Eve (Dec 24, unless it's Sat/Sun)
    """
    half_days = set()

    # Day before Independence Day
    jul4 = date(year, 7, 4)
    if jul4.weekday() == 0:  # Monday -> Friday Jul 2 is half-day
        half_days.add(date(year, 7, 2))
    elif jul4.weekday() in (1, 2, 3, 4):  # Tue-Fri -> day before
        half_days.add(date(year, 7, 3))
    # If Jul 4 is Sat or Sun, no half-day

    # Black Friday (day after Thanksgiving = 4th Thursday of November)
    nov1 = date(year, 11, 1)
    first_thursday = nov1 + timedelta(days=(3 - nov1.weekday()) % 7)
    thanksgiving = first_thursday + timedelta(weeks=3)
    black_friday = thanksgiving + timedelta(days=1)
    half_days.add(black_friday)

    # Christmas Eve (Dec 24, if it falls Mon-Fri)
    dec24 = date(year, 12, 24)
    if dec24.weekday() < 5:  # Mon-Fri
        half_days.add(dec24)

    return half_days


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
    Replays historical bars through 4 strategy engines in parallel.

    Mirrors the live trading architecture with 2 position slots:
      - 0DTE slot: Daily Income, ORB, and B&B share (max 1 trade/day)
      - Swing slot: Tag 'n Turn (positions can span multiple days)

    When no `strategies` config is provided, only Daily Income runs
    (backward compatible with existing callers).
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
        slippage: Optional[float] = None,
        max_daily_loss_pct: float = 2.0,
        progress_callback=None,
        strategies: Optional[Dict] = None,
    ):
        self.bars_df = bars_df
        self.vix_daily = vix_daily
        self.initial_capital = initial_capital
        self.max_contracts = max_contracts
        self.max_daily_loss_pct = max_daily_loss_pct
        self.progress_callback = progress_callback

        # Slippage metadata
        self._slippage_param = slippage
        if slippage is None:
            self._slippage_model = 'vix_aware'
            self._slippage_detail = 'VIX-aware per-leg: $0.05-$0.20'
        else:
            self._slippage_model = 'flat'
            self._slippage_detail = f'${slippage:.2f}/leg (${slippage * 2:.2f}/contract)'

        # Initialize broker
        self.broker = BacktestBroker(
            initial_capital=initial_capital,
            slippage=slippage,
        )

        # Initialize DI strategy (SAME class as live trading)
        strat_cfg = strategies or {}
        self._di_enabled = strat_cfg.get('daily_income', {}).get('enabled', True)
        self.strategy = SPXIncomeStrategy(
            pulse_threshold=pulse_threshold,
            spread_width=spread_width,
            profit_target_pct=profit_target_pct,
            min_credit=min_credit,
        )
        self.pulse_detector = PulseBarDetector(pulse_threshold)
        self._di_spread_width = spread_width
        self._di_min_credit = min_credit

        # Initialize parallel strategies (TNT, B&B, ORB)
        self._init_strategies(strat_cfg)

        # Results
        self.trades: List[BacktestTrade] = []
        self.daily_results: List[DailyResult] = []
        self.equity_curve: List[Dict] = []

        # 0DTE slot (shared by DI, ORB, B&B)
        self._0dte_trade: Optional[Trade] = None
        self._0dte_strategy_type: Optional[str] = None
        self._pending_setup = None  # DI pulse -> breakout pending

        # Swing slot (TNT only, persists across days)
        self._tnt_trade: Optional[Trade] = None
        self._tnt_position: Optional[Dict] = None  # For TNT exit checks
        self._tnt_entry_date: Optional[date] = None

        # Daily counters
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0
        self._0dte_trades_today: int = 0
        self._tnt_trades_today: int = 0
        self._circuit_breaker_tripped: bool = False

        # Credit flagging (cumulative across entire backtest)
        self._flagged_credit_count: int = 0

        # Half-day calendar cache
        self._half_day_cache: Dict[int, set] = {}

        # Backward compat alias
        self._active_trade = None

    def _is_half_day(self, trading_day: date) -> bool:
        """Check if a trading day is an NYSE half-day (1:00 PM close)."""
        year = trading_day.year
        if year not in self._half_day_cache:
            self._half_day_cache[year] = _nyse_half_days(year)
        return trading_day in self._half_day_cache[year]

    def _calculate_position_size(
        self,
        max_risk_per_contract: float,
        max_contracts_override: Optional[int] = None,
    ) -> int:
        """
        Calculate position size based on current account balance and risk budget.

        Mirrors PortfolioManager._calculate_percent_risk_size():
            contracts = floor(account_balance * risk_pct / max_risk_per_contract)

        Clamps to [1, self.max_contracts] then applies per-strategy override.
        """
        if max_risk_per_contract <= 0:
            return 1

        current_balance = self.broker.balance
        risk_budget = current_balance * (self.max_daily_loss_pct / 100.0)
        contracts = int(math.floor(risk_budget / max_risk_per_contract))

        # Clamp to configured bounds
        contracts = max(1, min(contracts, self.max_contracts))

        # Apply per-strategy ceiling if provided
        if max_contracts_override is not None and max_contracts_override > 0:
            contracts = min(contracts, max_contracts_override)

        return contracts

    def _init_strategies(self, strategies: Dict):
        """Initialize parallel strategy instances for backtest."""
        tmp = Path(tempfile.mkdtemp())

        # Tag 'n Turn
        tnt_cfg = strategies.get('tag_n_turn', {})
        self._tnt_enabled = tnt_cfg.get('enabled', False)
        self._tnt_strat: Optional[TagNTurnStrategy] = None
        if self._tnt_enabled:
            cfg = {
                'enabled': True,
                'bb_period': tnt_cfg.get('bb_period', 50),
                'bb_std': tnt_cfg.get('bb_std', 2.0),
                'pulse_threshold': tnt_cfg.get('pulse_threshold', 10.0),
                'max_hold_days': tnt_cfg.get('max_hold_days', 7),
                'use_credit_spreads': True,
                'max_contracts_override': tnt_cfg.get('max_contracts_override', 2),
                'spread_width': tnt_cfg.get('spread_width', 10.0),
                'min_credit': tnt_cfg.get('min_credit', 2.00),
                'min_dte': tnt_cfg.get('min_dte', 3),
                'max_dte': tnt_cfg.get('max_dte', 7),
            }
            self._tnt_strat = TagNTurnStrategy(cfg, persistence_path=tmp / 'tnt.json')

        # B&B (Bed & Breakfast)
        bnb_cfg = strategies.get('bnb', {})
        self._bnb_enabled = bnb_cfg.get('enabled', False)
        self._bnb_strat: Optional[BnBStrategy] = None
        if self._bnb_enabled:
            cfg = {
                'enabled': True,
                'pulse_threshold': bnb_cfg.get('pulse_threshold', 10.0),
                'gap_invalidation_pct': bnb_cfg.get('gap_invalidation_pct', 0.3),
            }
            self._bnb_strat = BnBStrategy(cfg, persistence_path=tmp / 'bnb.json')

        # ORB (Opening Range Breakout)
        orb_cfg = strategies.get('orb', {})
        self._orb_enabled = orb_cfg.get('enabled', False)
        self._orb_strat: Optional[ORBStrategy] = None
        if self._orb_enabled:
            cfg = {
                'enabled': True,
                'min_threshold': orb_cfg.get('min_threshold', 10.0),
                'min_range_points': orb_cfg.get('min_range_points', 8.0),
                'confirmation_minutes': orb_cfg.get('confirmation_minutes', 3),
                'max_contracts_override': orb_cfg.get('max_contracts_override', 3),
            }
            self._orb_strat = ORBStrategy(cfg, persistence_path=tmp / 'orb.json')

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
            f"${self.initial_capital:,.0f} capital, "
            f"strategies: {'DI' if self._di_enabled else ''}"
            f"{'+TNT' if self._tnt_enabled else ''}"
            f"{'+BnB' if self._bnb_enabled else ''}"
            f"{'+ORB' if self._orb_enabled else ''}"
        )

        # Suppress strategy module logging during backtest
        _suppressed_loggers = [
            'src.core.pulse_detector',
            'src.core.strategy',
            'src.core.position_manager',
            'src.core.portfolio_manager',
            'src.core.bar_builder',
            'src.core.tag_n_turn',
            'src.core.bnb_strategy',
            'src.core.orb_strategy',
            'src.core.bollinger_filter',
        ]
        _saved_levels = {}
        for name in _suppressed_loggers:
            lg = logging.getLogger(name)
            _saved_levels[name] = lg.level
            lg.setLevel(logging.CRITICAL)

        try:
            for day_idx, trading_day in enumerate(trading_days):
                self._process_day(trading_day)

                if self.progress_callback:
                    self.progress_callback(day_idx + 1, total_days, trading_day)
        finally:
            for name, level in _saved_levels.items():
                logging.getLogger(name).setLevel(level)

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
            'slippage_model': self._slippage_model,
            'slippage_detail': self._slippage_detail,
            'half_day_calendar': True,
            'flagged_credit_count': self._flagged_credit_count,
        }

    def _process_day(self, trading_day: date):
        """Process a single trading day with all active strategies."""
        day_bars = get_bars_for_day(self.bars_df, trading_day)
        if day_bars.empty:
            return

        is_half_day = self._is_half_day(trading_day)
        close_time = time(13, 0) if is_half_day else time(16, 0)

        # Reset daily state (but NOT cross-day positions like TNT)
        self._daily_pnl = 0.0
        self._daily_trades = 0
        self._0dte_trades_today = 0
        self._tnt_trades_today = 0
        self._circuit_breaker_tripped = False
        self._pending_setup = None

        # Start-of-day hooks for parallel strategies
        if self._bnb_enabled and self._bnb_strat:
            self._bnb_strat.on_day_start()
        if self._orb_enabled and self._orb_strat:
            self._orb_strat.reset_daily()

        # Get VIX for the day (fallback to 15 if not available)
        vix = self.vix_daily.get(trading_day, 15.0)

        bars_for_day: List[Bar] = []
        spx_open = float(day_bars.iloc[0]['open'])
        spx_close = float(day_bars.iloc[-1]['close'])
        max_daily_loss = self.broker.balance * (self.max_daily_loss_pct / 100.0)

        for timestamp, row in day_bars.iterrows():
            bar_dt = timestamp.to_pydatetime()
            if bar_dt.tzinfo is None:
                bar_dt = ET.localize(bar_dt)

            # Skip bars after market close on half-days
            if is_half_day and bar_dt.time() >= close_time:
                continue

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

            # ---- Monitor ALL active positions (before circuit breaker gate) ----
            if self._0dte_trade:
                self._monitor_0dte_position(bar, bar_dt)

            if self._tnt_trade:
                self._monitor_tnt_position(bar, bar_dt, trading_day)

            # ---- Circuit breaker check ----
            if self._daily_pnl <= -max_daily_loss:
                self._circuit_breaker_tripped = True

            # ---- Feed bars to all strategies (always, even if breaker tripped) ----
            # TNT needs continuous bar feeding for BB filter
            if self._tnt_enabled and self._tnt_strat:
                self._tnt_strat.on_bar_complete(bar)

            # ORB: Set opening range on first bar of the day
            if self._orb_enabled and self._orb_strat:
                if bar_dt.time() == time(9, 30):
                    self._orb_strat.set_opening_range(bar)

            # B&B: Process bars for end-of-day signals (15:00-16:00)
            if self._bnb_enabled and self._bnb_strat:
                self._bnb_strat.on_bar_complete(bar, bar.close)

            if self._circuit_breaker_tripped:
                continue

            # ---- Check entries for all strategies ----

            # TNT: separate swing slot (checked before 0DTE strategies)
            if (self._tnt_enabled and self._tnt_strat
                    and self._tnt_trades_today < 1
                    and not self._tnt_trade):
                tnt_signal = self._tnt_strat.check_entry_signal(bar.close)
                if tnt_signal:
                    self._execute_tnt_entry(bar, bar_dt, tnt_signal, vix)

            # 0DTE slot: ORB -> DI (B&B is informational-only, no entry)
            if self._0dte_trades_today < 1 and not self._0dte_trade:
                # ORB breakout
                if (self._orb_enabled and self._orb_strat
                        and not self._0dte_trade):
                    orb_signal = self._orb_strat.check_breakout(bar.close, bar_dt)
                    if orb_signal:
                        ok = self._execute_0dte_entry(
                            bar, bar_dt, orb_signal['direction'], vix,
                            strategy_type='orb',
                            quantity=self._orb_strat.max_contracts_override,
                        )
                        if not ok:
                            self._orb_strat.rollback_entry()

                # DI: pulse -> breakout flow
                if self._di_enabled and not self._0dte_trade:
                    bar_time = bar_dt.time()

                    # Check breakout trigger
                    if self._pending_setup:
                        self._check_breakout(bar, bar_dt, vix)

                    # Check for new DI setups (9:30-11:30 window)
                    if time(9, 30) <= bar_time <= time(11, 30):
                        if not self._pending_setup and not self._0dte_trade:
                            self._check_for_setup(bar, bar_dt, vix)

                    # Expire pending DI setup after 11:30
                    if self._pending_setup and bar_time > time(11, 30):
                        self._pending_setup = None

        # ---- End of day ----

        # Use last processed bar's close for settlement (respects half-day filtering)
        if bars_for_day:
            spx_close = bars_for_day[-1].close

        # Expire 0DTE positions at settlement
        if self._0dte_trade:
            self._expire_0dte_position(spx_close, trading_day, close_time)

        # TNT: Do NOT expire - swing positions carry across days

        # B&B: Finalize overnight signal for next day
        if self._bnb_enabled and self._bnb_strat:
            self._bnb_strat.on_day_end(spx_close)

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

    # ------------------------------------------------------------------
    # DI: Setup detection and breakout (unchanged from original)
    # ------------------------------------------------------------------

    def _check_for_setup(self, bar: Bar, bar_dt: datetime, vix: float):
        """Check if a completed bar creates a valid DI pulse setup."""
        direction = self.strategy.evaluate_setup(bar, bar.close)
        if direction is None:
            return

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
            f"[BT] {bar_dt.strftime('%Y-%m-%d %H:%M')} DI Pulse {direction.value} setup: "
            f"trigger {'above' if direction == TradeDirection.BULLISH else 'below'} "
            f"${trigger_price:,.2f}"
        )

    def _check_breakout(self, bar: Bar, bar_dt: datetime, vix: float):
        """Check if current bar triggers the pending DI setup's breakout."""
        setup = self._pending_setup
        direction = setup['direction']
        trigger_price = setup['trigger_price']

        triggered = False
        if direction == TradeDirection.BULLISH and bar.high > trigger_price:
            triggered = True
        elif direction == TradeDirection.BEARISH and bar.low < trigger_price:
            triggered = True

        if not triggered:
            return

        # Execute DI entry into the 0DTE slot
        self._execute_0dte_entry(
            bar, bar_dt, direction, vix,
            strategy_type='daily_income',
            setup_bar=setup['bar'],
        )

    # ------------------------------------------------------------------
    # Entry execution
    # ------------------------------------------------------------------

    def _execute_0dte_entry(
        self,
        bar: Bar,
        bar_dt: datetime,
        direction,
        vix: float,
        strategy_type: str = 'daily_income',
        setup_bar: Optional[Bar] = None,
        quantity: Optional[int] = None,
        spread_width: Optional[float] = None,
        min_credit: Optional[float] = None,
    ) -> bool:
        """Execute a 0DTE trade entry into the shared slot.

        Returns True if trade was successfully placed.
        """
        # Normalize direction to TradeDirection enum
        if isinstance(direction, str):
            direction = TradeDirection.BULLISH if 'bullish' in direction.lower() else TradeDirection.BEARISH

        current_price = bar.close
        expiration_str = bar_dt.strftime('%Y-%m-%d')
        chain = self.broker.get_options_chain('SPX', expiration_str)
        if not chain:
            return False

        # Temporarily swap strategy params if overridden
        sw = spread_width or self._di_spread_width
        mc = min_credit or self._di_min_credit
        saved_sw = self.strategy.spread_width
        saved_mc = self.strategy.min_credit
        self.strategy.spread_width = sw
        self.strategy.min_credit = mc

        spread = self.strategy.construct_spread(current_price, direction, chain)

        self.strategy.spread_width = saved_sw
        self.strategy.min_credit = saved_mc

        if spread is None:
            self._pending_setup = None
            return False

        # Position sizing: scale with current account balance
        max_risk_per_contract = spread.max_risk  # spread_width * 100 minus credit
        qty = self._calculate_position_size(
            max_risk_per_contract=max_risk_per_contract,
            max_contracts_override=quantity,  # per-strategy cap (B&B/ORB override)
        )
        order_id = self.broker.place_spread_order(spread, qty)
        order_status = self.broker.get_order_status(order_id)

        if order_status['status'] != 'filled':
            self._pending_setup = None
            return False

        trade = Trade(
            id=str(uuid.uuid4()),
            spread=spread,
            status=TradeStatus.ACTIVE,
            setup_bar=setup_bar or bar,
            entry_price=order_status['fill_price'],
            entry_time=bar_dt,
            entry_order_id=order_id,
            quantity=qty,
        )

        self._0dte_trade = trade
        self._0dte_strategy_type = strategy_type
        self._pending_setup = None
        self._0dte_trades_today += 1
        self._daily_trades += 1

        # Credit validation: flag unusual fills
        fill_credit = order_status['fill_price']
        if fill_credit > 3.50 or fill_credit < 1.50:
            self._flagged_credit_count += 1
            logger.warning(
                f"[BT] UNUSUAL CREDIT: ${fill_credit:.2f} on {strategy_type} trade"
            )

        logger.debug(
            f"[BT] {bar_dt.strftime('%Y-%m-%d %H:%M')} {strategy_type.upper()} ENTRY: "
            f"{direction.value} ${spread.short_leg.strike}/{spread.long_leg.strike} "
            f"credit=${order_status['fill_price']:.2f}"
        )

        return True

    def _execute_tnt_entry(
        self,
        bar: Bar,
        bar_dt: datetime,
        tnt_signal: Dict,
        vix: float,
    ) -> bool:
        """Execute a TNT swing trade entry into the swing slot."""
        direction = tnt_signal['direction']
        current_price = bar.close

        # TNT uses multi-day DTE for better option pricing
        tnt_cfg = self._tnt_strat.config if self._tnt_strat else {}
        dte_mid = (tnt_cfg.get('min_dte', 3) + tnt_cfg.get('max_dte', 7)) / 2
        tnt_spread_width = tnt_cfg.get('spread_width', 10.0)
        tnt_min_credit = tnt_cfg.get('min_credit', 2.00)
        tnt_qty = tnt_cfg.get('max_contracts_override', 2)

        # Temporarily set DTE on broker for realistic TNT option pricing
        saved_dte = self.broker._current_dte
        self.broker._current_dte = dte_mid

        expiration_str = bar_dt.strftime('%Y-%m-%d')
        chain = self.broker.get_options_chain('SPX', expiration_str)

        self.broker._current_dte = saved_dte

        if not chain:
            self._tnt_strat._reset_to_idle("No options chain available")
            return False

        # Temporarily swap strategy params for TNT spread construction
        saved_sw = self.strategy.spread_width
        saved_mc = self.strategy.min_credit
        self.strategy.spread_width = tnt_spread_width
        self.strategy.min_credit = tnt_min_credit

        spread = self.strategy.construct_spread(current_price, direction, chain)

        self.strategy.spread_width = saved_sw
        self.strategy.min_credit = saved_mc

        if spread is None:
            self._tnt_strat._reset_to_idle("Spread construction failed")
            return False

        # Position sizing: scale with current account balance
        max_risk_per_contract = spread.max_risk
        tnt_qty = self._calculate_position_size(
            max_risk_per_contract=max_risk_per_contract,
            max_contracts_override=tnt_cfg.get('max_contracts_override'),
        )

        order_id = self.broker.place_spread_order(spread, tnt_qty)
        order_status = self.broker.get_order_status(order_id)

        if order_status['status'] != 'filled':
            self._tnt_strat._reset_to_idle("Order not filled")
            return False

        trade = Trade(
            id=str(uuid.uuid4()),
            spread=spread,
            status=TradeStatus.ACTIVE,
            setup_bar=bar,
            entry_price=order_status['fill_price'],
            entry_time=bar_dt,
            entry_order_id=order_id,
            quantity=tnt_qty,
        )

        self._tnt_trade = trade
        self._tnt_entry_date = bar_dt.date()
        self._tnt_trades_today += 1
        self._daily_trades += 1

        # Credit validation: flag unusual TNT fills (wider spreads = higher credits)
        fill_credit = order_status['fill_price']
        if fill_credit > 5.00 or fill_credit < 2.00:
            self._flagged_credit_count += 1
            logger.warning(
                f"[BT] UNUSUAL CREDIT: ${fill_credit:.2f} on tag_n_turn trade"
            )

        # Notify TNT strategy of position open
        self._tnt_position = {
            'id': trade.id,
            'direction': direction.value,
            'entry_price': current_price,
            'target_price': tnt_signal.get('target_price', 0),
            'stop_price': tnt_signal.get('stop_price', 0),
            'entry_time': bar_dt.isoformat(),
        }
        self._tnt_strat.on_position_opened(self._tnt_position)

        logger.debug(
            f"[BT] {bar_dt.strftime('%Y-%m-%d %H:%M')} TNT ENTRY: "
            f"{direction.value} ${spread.short_leg.strike}/{spread.long_leg.strike} "
            f"credit=${order_status['fill_price']:.2f}, "
            f"target=${tnt_signal.get('target_price', 0):,.2f}, "
            f"stop=${tnt_signal.get('stop_price', 0):,.2f}"
        )

        return True

    # ------------------------------------------------------------------
    # Position monitoring
    # ------------------------------------------------------------------

    def _monitor_0dte_position(self, bar: Bar, bar_dt: datetime):
        """Monitor the 0DTE position (DI/ORB/B&B) for exit conditions."""
        trade = self._0dte_trade
        if not trade or trade.status != TradeStatus.ACTIVE:
            return

        current_value = self.broker.get_position_value(trade.spread)
        trade.update_pnl(current_value)
        trade._current_spx_price = bar.close

        # Use DI exit logic (profit target, 1pm management, expiration)
        should_exit, reason = self.strategy.should_exit(trade, current_value, bar_dt)

        if should_exit:
            self._close_0dte_position(trade, bar, bar_dt, reason, current_value)

    def _monitor_tnt_position(self, bar: Bar, bar_dt: datetime, trading_day: date):
        """Monitor the TNT swing position for exit conditions."""
        trade = self._tnt_trade
        if not trade or trade.status != TradeStatus.ACTIVE:
            return

        current_value = self.broker.get_position_value(trade.spread)
        trade.update_pnl(current_value)

        # Check target/stop via TNT strategy (works with simulated price)
        tnt_exit = None
        if self._tnt_strat:
            tnt_exit = self._tnt_strat.check_exit_conditions(bar.close)

        # Manual max_hold check (TNT uses datetime.now() internally, wrong for backtest)
        if not tnt_exit and self._tnt_entry_date and self._tnt_strat:
            days_held = (trading_day - self._tnt_entry_date).days
            if days_held >= self._tnt_strat.max_hold_days:
                tnt_exit = {
                    'reason': 'max_hold_exceeded',
                    'exit_price': bar.close,
                    'strategy': 'tag_n_turn',
                }

        if tnt_exit:
            reason = tnt_exit.get('reason', 'unknown')
            self._close_tnt_position(trade, bar, bar_dt, reason, current_value)

    # ------------------------------------------------------------------
    # Position closing
    # ------------------------------------------------------------------

    def _close_0dte_position(
        self,
        trade: Trade,
        bar: Bar,
        bar_dt: datetime,
        reason: str,
        current_value: float,
    ):
        """Close the active 0DTE position."""
        strategy_type = self._0dte_strategy_type or 'daily_income'

        if "expiration" in reason.lower():
            final_pnl = trade.spread.profit_at_price(bar.close) * trade.quantity
            trade.exit_price = 0.0
            trade.exit_time = bar_dt
            trade.exit_reason = reason
            trade.status = TradeStatus.CLOSED
            trade.pnl = final_pnl
            max_profit = trade.spread.max_profit * trade.quantity
            trade.pnl_percent = (final_pnl / max_profit * 100) if max_profit > 0 else 0
        else:
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
        self._record_trade(trade, bar, strategy_type)

        logger.debug(
            f"[BT] {bar_dt.strftime('%Y-%m-%d %H:%M')} {strategy_type.upper()} EXIT: "
            f"{reason} P&L=${trade.pnl:+.2f}"
        )

        self._0dte_trade = None
        self._0dte_strategy_type = None
        self._active_trade = None

        # Notify ORB strategy of close
        if strategy_type == 'orb' and self._orb_strat:
            self._orb_strat.on_position_closed(bar.close, reason)

    def _close_tnt_position(
        self,
        trade: Trade,
        bar: Bar,
        bar_dt: datetime,
        reason: str,
        current_value: float,
    ):
        """Close the active TNT swing position."""
        limit_price = min(current_value + 0.10, trade.spread.spread_width)
        order_id = self.broker.close_spread(trade.spread, trade.quantity, limit_price)
        order_status = self.broker.get_order_status(order_id)
        exit_price = order_status['fill_price']

        trade.close(
            exit_price=exit_price,
            exit_time=bar_dt,
            reason=f"TNT: {reason}",
        )

        self._daily_pnl += trade.pnl or 0
        self._record_trade(trade, bar, 'tag_n_turn')

        logger.debug(
            f"[BT] {bar_dt.strftime('%Y-%m-%d %H:%M')} TNT EXIT: "
            f"{reason} P&L=${trade.pnl:+.2f}"
        )

        # Notify TNT strategy
        if self._tnt_strat:
            self._tnt_strat.on_position_closed({
                'reason': reason,
                'exit_price': bar.close,
                'exit_time': bar_dt.isoformat(),
                'pnl': trade.pnl or 0,
            })

        self._tnt_trade = None
        self._tnt_position = None
        self._tnt_entry_date = None

    # ------------------------------------------------------------------
    # 0DTE expiration at end of day
    # ------------------------------------------------------------------

    def _expire_0dte_position(
        self,
        settlement_price: float,
        trading_day: date,
        close_time: time = time(16, 0),
    ):
        """Expire the 0DTE position at end of day."""
        trade = self._0dte_trade
        if not trade:
            return

        strategy_type = self._0dte_strategy_type or 'daily_income'
        expire_dt = ET.localize(datetime.combine(trading_day, close_time))
        final_pnl = trade.spread.profit_at_price(settlement_price) * trade.quantity
        expire_label = f"Expiration ({close_time.strftime('%I:%M %p').lstrip('0')})"

        trade.exit_price = 0.0
        trade.exit_time = expire_dt
        trade.exit_reason = expire_label
        trade.status = TradeStatus.CLOSED
        trade.pnl = final_pnl
        max_profit = trade.spread.max_profit * trade.quantity
        trade.pnl_percent = (final_pnl / max_profit * 100) if max_profit > 0 else 0

        # Update balance for expiration P&L
        # max_profit and profit_at_price() both return dollar values (multiplier baked in),
        # so the difference is already in dollars -- no /100 conversion needed.
        if final_pnl < trade.spread.max_profit * trade.quantity:
            itm_cost = trade.spread.max_profit * trade.quantity - final_pnl
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
            exit_reason=expire_label,
            quantity=trade.quantity,
            spx_at_entry=trade.spread.underlying_price_at_entry or 0,
            spx_at_exit=settlement_price,
            vix_at_entry=self.broker._current_vix,
            duration_minutes=int((expire_dt - trade.entry_time).total_seconds() / 60)
            if trade.entry_time else 0,
            strategy_type=strategy_type,
        )
        self.trades.append(bt_trade)

        logger.debug(
            f"[BT] {trading_day} {strategy_type.upper()} EXPIRED: "
            f"SPX=${settlement_price:,.2f} P&L=${final_pnl:+.2f}"
        )

        # Notify ORB strategy of close
        if strategy_type == 'orb' and self._orb_strat:
            self._orb_strat.on_position_closed(settlement_price, expire_label)

        self._0dte_trade = None
        self._0dte_strategy_type = None
        self._active_trade = None

    # ------------------------------------------------------------------
    # Trade recording helper
    # ------------------------------------------------------------------

    def _record_trade(self, trade: Trade, bar: Bar, strategy_type: str):
        """Record a closed trade to the results list."""
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
            strategy_type=strategy_type,
        )
        self.trades.append(bt_trade)
