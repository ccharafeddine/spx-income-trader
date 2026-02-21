"""
Tests for morning bias filter.

Covers:
- Bearish DI blocked on Up and Strong Up market days
- Bearish DI allowed on Flat, Down, and Strong Down days
- Bullish DI always allowed regardless of morning bias
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

    def test_bullish_always_allowed_on_up_day(self):
        """Bullish DI setup passes regardless of morning bias (Up day)."""
        spx_open = 5900.0
        current = 5929.5  # +0.5% Up day
        allowed, regime, move = SPXIncomeStrategy.check_morning_bias_filter(
            TradeDirection.BULLISH, spx_open, current
        )
        assert allowed
        assert regime == 'Up'

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
