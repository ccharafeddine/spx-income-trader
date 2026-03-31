"""
Tests for morning bias filter.

Covers:
- Bearish DI blocked on Up and Strong Up market days
- Bearish DI allowed on Flat, Down, and Strong Down days
- Bullish DI blocked on Down and Strong Down market days
- Bullish DI allowed on Flat, Up, and Strong Up days
- Edge cases: None/zero SPX open
- Config flag disables the filter
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import patch

from src.models.spread import TradeDirection
from src.core.strategy import SPXIncomeStrategy


class TestMorningBiasFilter:
    """Tests for SPXIncomeStrategy.check_morning_bias_filter()."""

    def test_bearish_blocked_on_up_day(self):
        """Bearish DI setup blocked when SPX is up 0.5% (Up regime)."""
        spx_open = 5900.0
        current = 5929.5  # +0.5%
        allowed, regime, move = SPXIncomeStrategy.check_morning_bias_filter(
            TradeDirection.BEARISH, spx_open, current
        )
        assert not allowed
        assert regime == 'Up'
        assert move == pytest.approx(0.5, abs=0.01)

    def test_bearish_blocked_on_strong_up_day(self):
        """Bearish DI setup blocked when SPX is up 1.5% (Strong Up regime)."""
        spx_open = 5900.0
        current = 5988.5  # +1.5%
        allowed, regime, move = SPXIncomeStrategy.check_morning_bias_filter(
            TradeDirection.BEARISH, spx_open, current
        )
        assert not allowed
        assert regime == 'Strong Up'
        assert move == pytest.approx(1.5, abs=0.01)

    def test_bearish_allowed_on_flat_day(self):
        """Bearish DI setup allowed when SPX is flat (within +/-0.25%)."""
        spx_open = 5900.0
        current = 5905.0  # +0.08%
        allowed, regime, move = SPXIncomeStrategy.check_morning_bias_filter(
            TradeDirection.BEARISH, spx_open, current
        )
        assert allowed
        assert regime == 'Flat'

    def test_bearish_allowed_on_down_day(self):
        """Bearish DI setup allowed when SPX is down 0.5% (Down regime)."""
        spx_open = 5900.0
        current = 5870.5  # -0.5%
        allowed, regime, move = SPXIncomeStrategy.check_morning_bias_filter(
            TradeDirection.BEARISH, spx_open, current
        )
        assert allowed
        assert regime == 'Down'

    def test_bearish_allowed_on_strong_down_day(self):
        """Bearish DI setup allowed when SPX is down 1.5% (Strong Down regime)."""
        spx_open = 5900.0
        current = 5811.5  # -1.5%
        allowed, regime, move = SPXIncomeStrategy.check_morning_bias_filter(
            TradeDirection.BEARISH, spx_open, current
        )
        assert allowed
        assert regime == 'Strong Down'

    def test_bullish_blocked_on_down_day(self):
        """Bullish DI setup blocked when SPX is down 0.5% (Down regime)."""
        spx_open = 5900.0
        current = 5870.5  # -0.5%
        allowed, regime, move = SPXIncomeStrategy.check_morning_bias_filter(
            TradeDirection.BULLISH, spx_open, current
        )
        assert not allowed
        assert regime == 'Down'
        assert move == pytest.approx(-0.5, abs=0.01)

    def test_bullish_blocked_on_strong_down_day(self):
        """Bullish DI setup blocked when SPX is down 1.5% (Strong Down regime)."""
        spx_open = 5900.0
        current = 5811.5  # -1.5%
        allowed, regime, move = SPXIncomeStrategy.check_morning_bias_filter(
            TradeDirection.BULLISH, spx_open, current
        )
        assert not allowed
        assert regime == 'Strong Down'
        assert move == pytest.approx(-1.5, abs=0.01)

    def test_bullish_allowed_on_flat_day(self):
        """Bullish DI setup allowed when SPX is flat (within +/-0.25%)."""
        spx_open = 5900.0
        current = 5894.0  # -0.10%
        allowed, regime, move = SPXIncomeStrategy.check_morning_bias_filter(
            TradeDirection.BULLISH, spx_open, current
        )
        assert allowed
        assert regime == 'Flat'

    def test_bullish_allowed_on_up_day(self):
        """Bullish DI setup allowed when SPX is up 0.5% (Up regime)."""
        spx_open = 5900.0
        current = 5929.5  # +0.5%
        allowed, regime, move = SPXIncomeStrategy.check_morning_bias_filter(
            TradeDirection.BULLISH, spx_open, current
        )
        assert allowed
        assert regime == 'Up'

    def test_bullish_allowed_on_strong_up_day(self):
        """Bullish DI setup allowed when SPX is up 1.5% (Strong Up regime)."""
        spx_open = 5900.0
        current = 5988.5  # +1.5%
        allowed, regime, move = SPXIncomeStrategy.check_morning_bias_filter(
            TradeDirection.BULLISH, spx_open, current
        )
        assert allowed
        assert regime == 'Strong Up'

    def test_none_spx_open_allows_entry(self):
        """When SPX open is None (not yet set), filter allows entry."""
        allowed, regime, move = SPXIncomeStrategy.check_morning_bias_filter(
            TradeDirection.BEARISH, None, 5900.0
        )
        assert allowed
        assert regime == 'Unknown'
        assert move == 0.0

    def test_zero_spx_open_allows_entry(self):
        """When SPX open is 0, filter allows entry."""
        allowed, regime, move = SPXIncomeStrategy.check_morning_bias_filter(
            TradeDirection.BEARISH, 0, 5900.0
        )
        assert allowed
        assert regime == 'Unknown'


class TestMorningBiasConfig:
    """Tests for morning bias filter config flag on strategy instance."""

    def test_filter_enabled_by_default(self):
        """Filter defaults to enabled when config key is missing."""
        with patch('src.core.strategy.STRATEGY_PARAMS', {
            'execution': {}, 'timing': {},
        }):
            strategy = SPXIncomeStrategy()
            assert strategy.di_morning_bias_filter is True

    def test_filter_disabled_via_config(self):
        """Filter can be disabled via config flag."""
        with patch('src.core.strategy.STRATEGY_PARAMS', {
            'execution': {}, 'timing': {},
            'filters': {'di_morning_bias_filter': False},
        }):
            strategy = SPXIncomeStrategy()
            assert strategy.di_morning_bias_filter is False


# -------------------------------------------------------------------
# Backtest engine integration tests
# -------------------------------------------------------------------

from datetime import datetime, date
import pytz
from src.backtest.engine import BacktestEngine
from src.models.bar import Bar

ET = pytz.timezone('America/New_York')

FIXTURE_CSV = str(Path(__file__).parent / 'fixtures' / 'backtest_sample.csv')


def _make_engine(di_morning_bias_filter=True):
    """Create a minimal BacktestEngine for filter testing."""
    from src.backtest.data_loader import load_bars_from_csv
    bars_df = load_bars_from_csv(FIXTURE_CSV)
    engine = BacktestEngine(
        bars_df=bars_df,
        vix_daily={},
        initial_capital=50000,
        pulse_threshold=10.0,
        spread_width=5.0,
    )
    engine.strategy.di_morning_bias_filter = di_morning_bias_filter
    return engine


def _make_bearish_pulse_bar(dt):
    """Create a bar classified as BEARISH_PULSE by the pulse detector.

    Bearish pulse: close < open, close at bottom of range (<=10%).
    """
    return Bar(
        timestamp=dt,
        open=5010.0,
        high=5012.0,
        low=4990.0,
        close=4991.0,  # 4.5% of range
    )


def _make_bullish_pulse_bar(dt):
    """Create a bar classified as BULLISH_PULSE by the pulse detector.

    Bullish pulse: close > open, close at top of range (>=90%).
    """
    return Bar(
        timestamp=dt,
        open=4990.0,
        high=5012.0,
        low=4990.0,
        close=5010.0,  # 90.9% of range
    )


class TestMorningBiasFilterBacktest:
    """Test that morning bias filter uses full-day return in backtest engine."""

    def test_bearish_skipped_on_up_day(self):
        """Bearish DI pulse skipped when full-day return is Up (>0.25%)."""
        engine = _make_engine(di_morning_bias_filter=True)
        engine._spx_open = 5000.0
        engine._spx_close = 5020.0  # +0.4% full-day return (Up)

        bar_dt = ET.localize(datetime(2024, 1, 3, 10, 0))
        bar = _make_bearish_pulse_bar(bar_dt)
        engine._check_for_setup(bar, bar_dt, vix=15.0)

        assert engine._pending_setup is None
        assert engine._direction_filter_skips == 1

    def test_bullish_proceeds_on_up_day(self):
        """Bullish DI pulse proceeds when full-day return is Up."""
        engine = _make_engine(di_morning_bias_filter=True)
        engine._spx_open = 5000.0
        engine._spx_close = 5020.0  # +0.4% full-day return (Up)

        bar_dt = ET.localize(datetime(2024, 1, 3, 10, 0))
        bar = _make_bullish_pulse_bar(bar_dt)
        engine._check_for_setup(bar, bar_dt, vix=15.0)

        assert engine._pending_setup is not None
        assert engine._pending_setup['direction'] == TradeDirection.BULLISH
        assert engine._direction_filter_skips == 0

    def test_bullish_skipped_on_down_day(self):
        """Bullish DI pulse skipped when full-day return is Down (<-0.25%)."""
        engine = _make_engine(di_morning_bias_filter=True)
        engine._spx_open = 5000.0
        engine._spx_close = 4975.0  # -0.5% full-day return (Down)

        bar_dt = ET.localize(datetime(2024, 1, 3, 10, 0))
        bar = _make_bullish_pulse_bar(bar_dt)
        engine._check_for_setup(bar, bar_dt, vix=15.0)

        assert engine._pending_setup is None
        assert engine._direction_filter_skips == 1

    def test_bullish_skipped_on_strong_down_day(self):
        """Bullish DI pulse skipped on Strong Down (<-1%) full-day return."""
        engine = _make_engine(di_morning_bias_filter=True)
        engine._spx_open = 5000.0
        engine._spx_close = 4925.0  # -1.5% full-day return (Strong Down)

        bar_dt = ET.localize(datetime(2024, 1, 3, 10, 0))
        bar = _make_bullish_pulse_bar(bar_dt)
        engine._check_for_setup(bar, bar_dt, vix=15.0)

        assert engine._pending_setup is None
        assert engine._direction_filter_skips == 1

    def test_bullish_allowed_on_flat_day_backtest(self):
        """Bullish DI pulse proceeds when full-day return is Flat."""
        engine = _make_engine(di_morning_bias_filter=True)
        engine._spx_open = 5000.0
        engine._spx_close = 4995.0  # -0.1% full-day return (Flat)

        bar_dt = ET.localize(datetime(2024, 1, 3, 10, 0))
        bar = _make_bullish_pulse_bar(bar_dt)
        engine._check_for_setup(bar, bar_dt, vix=15.0)

        assert engine._pending_setup is not None
        assert engine._pending_setup['direction'] == TradeDirection.BULLISH
        assert engine._direction_filter_skips == 0

    def test_filter_disabled_allows_bearish_on_up_day(self):
        """With filter disabled, bearish DI pulse proceeds on Up days."""
        engine = _make_engine(di_morning_bias_filter=False)
        engine._spx_open = 5000.0
        engine._spx_close = 5020.0  # +0.4% full-day return (Up)

        bar_dt = ET.localize(datetime(2024, 1, 3, 10, 0))
        bar = _make_bearish_pulse_bar(bar_dt)
        engine._check_for_setup(bar, bar_dt, vix=15.0)

        assert engine._pending_setup is not None
        assert engine._pending_setup['direction'] == TradeDirection.BEARISH
        assert engine._direction_filter_skips == 0

    def test_bearish_allowed_on_flat_day(self):
        """Bearish DI pulse proceeds when full-day return is Flat."""
        engine = _make_engine(di_morning_bias_filter=True)
        engine._spx_open = 5000.0
        engine._spx_close = 5005.0  # +0.1% full-day return (Flat)

        bar_dt = ET.localize(datetime(2024, 1, 3, 10, 0))
        bar = _make_bearish_pulse_bar(bar_dt)
        engine._check_for_setup(bar, bar_dt, vix=15.0)

        assert engine._pending_setup is not None
        assert engine._direction_filter_skips == 0

    def test_bearish_allowed_on_down_day(self):
        """Bearish DI pulse proceeds when full-day return is Down."""
        engine = _make_engine(di_morning_bias_filter=True)
        engine._spx_open = 5000.0
        engine._spx_close = 4970.0  # -0.6% full-day return (Down)

        bar_dt = ET.localize(datetime(2024, 1, 3, 10, 0))
        bar = _make_bearish_pulse_bar(bar_dt)
        engine._check_for_setup(bar, bar_dt, vix=15.0)

        assert engine._pending_setup is not None
        assert engine._direction_filter_skips == 0

    def test_bearish_skipped_on_strong_up_day(self):
        """Bearish DI pulse skipped on Strong Up (>1%) full-day return."""
        engine = _make_engine(di_morning_bias_filter=True)
        engine._spx_open = 5000.0
        engine._spx_close = 5060.0  # +1.2% full-day return (Strong Up)

        bar_dt = ET.localize(datetime(2024, 1, 3, 10, 0))
        bar = _make_bearish_pulse_bar(bar_dt)
        engine._check_for_setup(bar, bar_dt, vix=15.0)

        assert engine._pending_setup is None
        assert engine._direction_filter_skips == 1

    def test_direction_filter_skips_in_report(self):
        """Backtest report should include direction_filter_skips count."""
        from src.backtest.data_loader import load_bars_from_csv
        bars_df = load_bars_from_csv(FIXTURE_CSV)
        engine = BacktestEngine(
            bars_df=bars_df,
            vix_daily={},
            initial_capital=50000,
        )
        results = engine.run()
        assert 'direction_filter_skips' in results

    def test_daily_move_pct_in_trades(self):
        """Backtest trades should include daily_move_pct field."""
        from src.backtest.data_loader import load_bars_from_csv
        bars_df = load_bars_from_csv(FIXTURE_CSV)
        engine = BacktestEngine(
            bars_df=bars_df,
            vix_daily={},
            initial_capital=50000,
            min_credit=0.50,
            slippage=0.02,
        )
        results = engine.run()
        trades = results['trades']
        if trades:
            # All trades should have daily_move_pct
            for t in trades:
                assert 'daily_move_pct' in t, f"Trade missing daily_move_pct: {t['trade_id']}"
