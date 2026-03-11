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
from src.core.bollinger_filter import BollingerFilter
from src.core.tag_n_turn import TagNTurnStrategy
from src.core.bnb_strategy import BnBStrategy
from src.core.orb_strategy import ORBStrategy
from src.backtest.sim_broker import BacktestBroker
from src.backtest.data_loader import get_trading_days, get_bars_for_day

logger = logging.getLogger(__name__)

ET = pytz.timezone('America/New_York')


def _classify_bt_time_bucket(dt):
    """Classify a datetime into a time-of-day bucket (same labels as dashboard)."""
    h = dt.hour
    if h < 10:
        return 'Open (9:30-10)'
    elif h < 11:
        return 'Mid-Morning (10-11)'
    elif h < 13:
        return 'Midday (11-1)'
    elif h < 15:
        return 'Afternoon (1-3)'
    else:
        return 'Close (3-4)'


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


def _normalize_exit_reason(raw: str) -> tuple:
    """Normalize a verbose exit reason to (category, detail).

    Returns (normalized_key, original_verbose_string).
    """
    if not raw:
        return 'unknown', ''
    r = raw.strip()
    if r.startswith('Profit target reached'):
        return 'profit_target', r
    if r.startswith('1PM CHECK: NON-TRENDING'):
        return '1pm_close_non_trending', r
    if r.startswith('1PM CHECK: TRENDING'):
        return '1pm_hold_trending', r
    if 'Expiration' in r:
        if '1:00 PM' in r:
            return 'expiration_1pm', r
        return 'expiration', r
    if r.startswith('TNT:') or r.startswith('TNT: '):
        sub = r.split(':', 1)[1].strip()
        if sub == 'target_hit':
            return 'tnt_target_hit', r
        if sub == 'stop_hit':
            return 'tnt_stop_hit', r
        if sub == 'max_hold_exceeded':
            return 'tnt_max_hold', r
        return f'tnt_{sub}', r
    if r.startswith('Expired (resolved at startup'):
        return 'expired_at_startup', r
    return r, ''


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
    exit_detail: str = ''
    quantity: int = 1
    spx_at_entry: float = 0.0
    spx_at_exit: float = 0.0
    vix_at_entry: float = 0.0
    vix_at_exit: float = 0.0
    duration_minutes: int = 0
    strategy_type: str = 'daily_income'
    theoretical_credit: float = 0.0
    actual_credit: float = 0.0
    slippage: float = 0.0
    slippage_pct: float = 0.0
    entry_time_bucket: str = ''
    bb_agreement: Optional[bool] = None
    daily_move_pct: Optional[float] = None  # Full-day SPX return (open-to-close)

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
            'exit_detail': self.exit_detail,
            'quantity': self.quantity,
            'spx_at_entry': self.spx_at_entry,
            'spx_at_exit': self.spx_at_exit,
            'vix_at_entry': self.vix_at_entry,
            'vix_at_exit': self.vix_at_exit,
            'duration_minutes': self.duration_minutes,
            'strategy_type': self.strategy_type,
            'theoretical_credit': self.theoretical_credit,
            'actual_credit': self.actual_credit,
            'slippage': self.slippage,
            'slippage_pct': self.slippage_pct,
            'entry_time_bucket': self.entry_time_bucket,
            'bb_agreement': self.bb_agreement,
            'daily_move_pct': self.daily_move_pct,
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
        max_contracts: int = 10,
        daily_contracts: Optional[int] = None,
        swing_contracts: Optional[int] = None,
        slippage: Optional[float] = None,
        fill_quality_factor: float = 1.0,
        max_daily_loss_pct: float = 2.0,
        progress_callback=None,
        strategies: Optional[Dict] = None,
    ):
        self.bars_df = bars_df
        self.vix_daily = vix_daily
        self.initial_capital = initial_capital
        self.max_contracts = max_contracts
        # DI scales with UI max_contracts; TNT stays fixed at 2 unless explicit.
        self.daily_contracts = daily_contracts if daily_contracts is not None else max_contracts
        self.swing_contracts = swing_contracts if swing_contracts is not None else 2
        self.max_daily_loss_pct = max_daily_loss_pct
        self.progress_callback = progress_callback

        # Fill quality factor (1.0 = theoretical mid, <1.0 = reduced entry credit)
        self._fill_quality_factor = fill_quality_factor

        # Slippage metadata
        self._slippage_param = slippage
        if slippage is None:
            self._slippage_model = 'vix_time_aware'
            self._slippage_detail = 'VIX + time-of-day per-leg: $0.02-$0.08'
        else:
            self._slippage_model = 'flat'
            self._slippage_detail = f'${slippage:.2f}/leg (${slippage * 2:.2f}/contract)'

        # Initialize broker
        self.broker = BacktestBroker(
            initial_capital=initial_capital,
            slippage=slippage,
            fill_quality_factor=fill_quality_factor,
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

        # PDT mode detection for 1pm management (based on initial capital)
        pdt_threshold = strat_cfg.get('pdt', {}).get('pdt_threshold', 25000)
        self.strategy.pdt_mode_active = initial_capital < pdt_threshold

        # BB filter for analytics tracking (never gates DI entry)
        filters_cfg = strat_cfg.get('filters', {})
        if filters_cfg.get('bollinger_enabled', True):
            self.bollinger_filter = BollingerFilter(
                period=filters_cfg.get('bollinger_period', 50),
                num_std=filters_cfg.get('bollinger_std', 2.0),
            )
        else:
            self.bollinger_filter = None

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

        # SPX open/close for morning bias filter (full-day return)
        self._spx_open: float = 0.0
        self._spx_close: float = 0.0  # Day close for full-day return filter
        self._direction_filter_skips: int = 0

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

    def _calculate_di_position_size(
        self,
        max_risk_per_contract: float,
    ) -> int:
        """
        Calculate DI position size from daily loss budget.

        Mirrors PortfolioManager.calculate_position_size() for DAILY_INCOME:
            contracts = floor(daily_loss_budget / max_risk_per_contract)
            capped by daily_contracts and max_contracts
        """
        if max_risk_per_contract <= 0:
            return 1

        current_balance = self.broker.balance
        if not isinstance(current_balance, (int, float)) or current_balance != current_balance:
            return 1
        budget = current_balance * (self.max_daily_loss_pct / 100.0)
        contracts = int(math.floor(budget / max_risk_per_contract))
        contracts = max(1, contracts)
        contracts = min(contracts, self.daily_contracts)
        contracts = min(contracts, self.max_contracts)
        return contracts

    def _get_swing_contracts(self) -> int:
        """Fixed swing contract count (TNT/ORB/BnB), clamped by max_contracts."""
        return max(1, min(self.swing_contracts, self.max_contracts))

    def _init_strategies(self, strategies: Dict):
        """Initialize parallel strategy instances for backtest."""
        tmp = Path(tempfile.mkdtemp())

        # Tag 'n Turn
        tnt_cfg = strategies.get('tag_n_turn', {})
        self._tnt_enabled = tnt_cfg.get('enabled', False)
        self._tnt_strat: Optional[TagNTurnStrategy] = None
        if self._tnt_enabled:
            cfg = {**tnt_cfg, 'enabled': True}
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
            'fill_quality_factor': self._fill_quality_factor,
            'flagged_credit_count': self._flagged_credit_count,
            'direction_filter_skips': self._direction_filter_skips,
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
        self._spx_open = spx_open  # For morning bias filter access
        spx_close = float(day_bars.iloc[-1]['close'])
        self._spx_close = spx_close  # For full-day return filter
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
                # VIX filter: skip TNT entries during high volatility
                tnt_max_vix = getattr(self._tnt_strat, 'max_vix', 0)
                if tnt_max_vix and vix > tnt_max_vix:
                    pass  # Skip TNT in high-vol environment
                else:
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

        # Morning bias filter: block bearish DI on Up/Strong Up days
        # In backtest mode, uses full-day return (spx_close vs spx_open) since
        # the day's data is known. This matches the dashboard direction drill-down.
        if self.strategy.di_morning_bias_filter and direction == TradeDirection.BEARISH:
            allowed, regime, move_pct = SPXIncomeStrategy.check_morning_bias_filter(
                direction, self._spx_open, self._spx_close
            )
            if not allowed:
                self._direction_filter_skips += 1
                logger.debug(
                    f"[BT] {bar_dt.strftime('%Y-%m-%d %H:%M')} DI bearish setup skipped - "
                    f"day return is {regime} ({'+' if move_pct >= 0 else ''}{move_pct:.2f}%)"
                )
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
        if strategy_type in ('orb', 'bnb'):
            qty = self._get_swing_contracts()
        else:
            qty = self._calculate_di_position_size(max_risk_per_contract)
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

        # Attach backtest slippage metadata for later BacktestTrade construction
        trade._bt_theoretical_credit = spread.credit_received
        trade._bt_actual_credit = order_status['fill_price']
        trade._bt_slippage = round(order_status['fill_price'] - spread.credit_received, 4)
        trade._bt_entry_time_bucket = _classify_bt_time_bucket(bar_dt)
        trade._bt_vix_at_entry = self.broker._current_vix

        # BB agreement analytics (never gates entry)
        if self.bollinger_filter:
            trade._bt_bb_agreement = SPXIncomeStrategy.compute_bb_agreement(
                self.bollinger_filter, direction
            )
        else:
            trade._bt_bb_agreement = None

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

        # Position sizing: fixed swing contracts
        tnt_qty = self._get_swing_contracts()

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

        # Attach backtest slippage metadata for later BacktestTrade construction
        trade._bt_theoretical_credit = spread.credit_received
        trade._bt_actual_credit = order_status['fill_price']
        trade._bt_slippage = round(order_status['fill_price'] - spread.credit_received, 4)
        trade._bt_entry_time_bucket = _classify_bt_time_bucket(bar_dt)
        trade._bt_vix_at_entry = self.broker._current_vix

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

        # Set correct DTE for swing position valuation (not 0DTE default)
        if trade.spread.expiration:
            exp_date = trade.spread.expiration.date() if hasattr(trade.spread.expiration, 'date') else trade.spread.expiration
            remaining_dte = max((exp_date - trading_day).days, 0.01)
            self.broker._current_dte = remaining_dte

        current_value = self.broker.get_position_value(trade.spread)
        trade.update_pnl(current_value)

        # Check target/stop via TNT strategy (works with simulated price)
        tnt_exit = None
        if self._tnt_strat:
            tnt_exit = self._tnt_strat.check_exit_conditions(bar.close, current_time=bar_dt)

        # Manual max_hold check (TNT uses datetime.now() internally, wrong for backtest)
        if not tnt_exit and self._tnt_entry_date and self._tnt_strat:
            days_held = (trading_day - self._tnt_entry_date).days
            if days_held >= self._tnt_strat.max_hold_days:
                tnt_exit = {
                    'reason': 'max_hold_exceeded',
                    'exit_price': bar.close,
                    'strategy': 'tag_n_turn',
                }

        # Restore DTE to 0DTE default for other position monitoring
        self.broker._current_dte = 0.01

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
        norm_reason, detail = _normalize_exit_reason(expire_label)

        trade.exit_price = 0.0
        trade.exit_time = expire_dt
        trade.exit_reason = norm_reason
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
            exit_reason=norm_reason,
            exit_detail=detail,
            quantity=trade.quantity,
            spx_at_entry=trade.spread.underlying_price_at_entry or 0,
            spx_at_exit=settlement_price,
            vix_at_entry=getattr(trade, '_bt_vix_at_entry', self.broker._current_vix),
            vix_at_exit=self.broker._current_vix,
            duration_minutes=int((expire_dt - trade.entry_time).total_seconds() / 60)
            if trade.entry_time else 0,
            strategy_type=strategy_type,
            theoretical_credit=getattr(trade, '_bt_theoretical_credit', trade.entry_price),
            actual_credit=getattr(trade, '_bt_actual_credit', trade.entry_price),
            slippage=getattr(trade, '_bt_slippage', 0.0),
            slippage_pct=round(getattr(trade, '_bt_slippage', 0) / trade.spread.credit_received * 100, 2) if trade.spread.credit_received else 0.0,
            entry_time_bucket=getattr(trade, '_bt_entry_time_bucket', ''),
            bb_agreement=getattr(trade, '_bt_bb_agreement', None),
            daily_move_pct=round(((self._spx_close - self._spx_open) / self._spx_open) * 100, 4) if self._spx_open > 0 else None,
        )
        self.trades.append(bt_trade)

        # Sanity check: flag if a single trade P&L exceeds 3x the daily loss limit
        # (uses current equity so compounding doesn't trigger false positives;
        #  floor at 50% of initial capital so tiny max_daily_loss_pct tests don't trip)
        trade_pnl = trade.pnl or 0
        current_equity = self.broker.balance
        max_acceptable = max(
            current_equity * (self.max_daily_loss_pct / 100) * 3,
            self.initial_capital * 0.50,
        )
        if abs(trade_pnl) > max_acceptable:
            raise ValueError(
                f"Abnormal trade P&L ${trade_pnl:.0f} exceeds 3x daily loss limit "
                f"(${max_acceptable:.0f}) on ${current_equity:.0f} equity — "
                f"likely a contract sizing bug. "
                f"Contracts: {trade.quantity}, strategy: {strategy_type}"
            )

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
        raw_reason = trade.exit_reason or ''
        norm_reason, detail = _normalize_exit_reason(raw_reason)
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
            exit_reason=norm_reason,
            exit_detail=detail,
            quantity=trade.quantity,
            spx_at_entry=trade.spread.underlying_price_at_entry or 0,
            spx_at_exit=bar.close,
            vix_at_entry=getattr(trade, '_bt_vix_at_entry', self.broker._current_vix),
            vix_at_exit=self.broker._current_vix,
            duration_minutes=int((trade.exit_time - trade.entry_time).total_seconds() / 60)
            if trade.exit_time else 0,
            strategy_type=strategy_type,
            theoretical_credit=getattr(trade, '_bt_theoretical_credit', trade.entry_price),
            actual_credit=getattr(trade, '_bt_actual_credit', trade.entry_price),
            slippage=getattr(trade, '_bt_slippage', 0.0),
            slippage_pct=round(getattr(trade, '_bt_slippage', 0) / trade.spread.credit_received * 100, 2) if trade.spread.credit_received else 0.0,
            entry_time_bucket=getattr(trade, '_bt_entry_time_bucket', ''),
            bb_agreement=getattr(trade, '_bt_bb_agreement', None),
            daily_move_pct=round(((self._spx_close - self._spx_open) / self._spx_open) * 100, 4) if self._spx_open > 0 else None,
        )
        self.trades.append(bt_trade)
