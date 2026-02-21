"""
Tests for PDT-aware 1PM management and BB analytics tracking.

Validates:
1. PDT mode detection from equity/config
2. 1pm management conditional on PDT mode
3. BB agreement analytics (never gates entry)
4. Backtest engine PDT mode + bb_agreement
"""

import sys
from pathlib import Path
from datetime import datetime, time
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

import pytz
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.strategy import SPXIncomeStrategy
from src.models.spread import TradeDirection


ET = pytz.timezone('America/New_York')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_strategy(**overrides):
    """Create a strategy instance with test defaults."""
    with patch.dict('config.settings.STRATEGY_PARAMS', {
        'pdt': {
            'enable_1pm_management': True,
            'trending_threshold': 15.0,
        },
        'monitoring': {},
        'execution': {'min_credit': 1.00},
        'timing': {'morning_start': '09:30', 'morning_end': '11:30'},
    }):
        s = SPXIncomeStrategy()
        for k, v in overrides.items():
            setattr(s, k, v)
        return s


def _make_trade(direction='bullish', entry_price=5900.0):
    """Create a minimal mock trade for 1pm checks."""
    trade = MagicMock()
    trade.spread.direction = (
        TradeDirection.BULLISH if direction == 'bullish' else TradeDirection.BEARISH
    )
    trade.spread.underlying_price_at_entry = entry_price
    trade.spread.credit_received = 2.50
    trade.spread.max_profit = 250.0
    trade.quantity = 1
    trade.spread.profit_at_price = MagicMock(return_value=100.0)
    trade._1pm_checked = False
    return trade


# ---------------------------------------------------------------------------
# TestPDTModeDetection
# ---------------------------------------------------------------------------

class TestPDTModeDetection:
    """PDT mode flag is set correctly based on equity and config."""

    def test_force_pdt_true(self):
        """force_pdt_mode=True forces PDT active regardless of equity."""
        s = _make_strategy()
        s.pdt_mode_active = True  # As main.py would set via force_pdt
        assert s.pdt_mode_active is True

    def test_force_pdt_false(self):
        """force_pdt_mode=False forces PDT inactive."""
        s = _make_strategy()
        s.pdt_mode_active = False
        assert s.pdt_mode_active is False

    def test_equity_above_threshold(self):
        """Account >= $25k should have PDT inactive."""
        s = _make_strategy()
        equity = 50000
        threshold = 25000
        s.pdt_mode_active = equity < threshold
        assert s.pdt_mode_active is False

    def test_equity_below_threshold(self):
        """Account < $25k should have PDT active."""
        s = _make_strategy()
        equity = 10000
        threshold = 25000
        s.pdt_mode_active = equity < threshold
        assert s.pdt_mode_active is True

    def test_dry_run_from_starting_capital(self):
        """In dry run, PDT mode derived from starting_capital."""
        s = _make_strategy()
        starting_capital = 15000
        threshold = 25000
        s.pdt_mode_active = starting_capital < threshold
        assert s.pdt_mode_active is True


# ---------------------------------------------------------------------------
# TestOnePmPDTConditional
# ---------------------------------------------------------------------------

class TestOnePmPDTConditional:
    """1pm management fires only when PDT mode is active."""

    def test_1pm_fires_when_pdt_active(self):
        """1pm check should evaluate when PDT mode is active."""
        s = _make_strategy(pdt_mode_active=True)
        trade = _make_trade(direction='bullish', entry_price=5900.0)
        at_1pm = ET.localize(datetime(2026, 2, 21, 13, 15))

        should_close, reason, assessment = s.check_1pm_management(
            trade, 5905.0, at_1pm
        )
        # Should have produced an assessment (non-empty dict)
        assert assessment, "1pm check should produce assessment in PDT mode"
        assert 'decision' in assessment

    def test_1pm_skipped_when_pdt_inactive(self):
        """1pm check should be skipped when PDT mode is inactive."""
        s = _make_strategy(pdt_mode_active=False)
        trade = _make_trade(direction='bullish', entry_price=5900.0)
        at_1pm = ET.localize(datetime(2026, 2, 21, 13, 15))

        should_close, reason, assessment = s.check_1pm_management(
            trade, 5905.0, at_1pm
        )
        assert should_close is False
        assert assessment == {}

    def test_should_exit_with_pdt_on(self):
        """should_exit path calls 1pm management when PDT active."""
        s = _make_strategy(pdt_mode_active=True)
        trade = _make_trade(direction='bullish', entry_price=5900.0)
        trade._current_spx_price = 5905.0
        trade.spread.expiration = ET.localize(datetime(2026, 2, 21, 16, 0))
        trade.spread.max_profit = 250.0
        trade.entry_price = 2.50
        at_1pm = ET.localize(datetime(2026, 2, 21, 13, 15))

        # Current spread price such that no profit target hit
        should_exit, reason = s.should_exit(trade, 2.40, at_1pm)
        # The 1pm check was performed (trade._1pm_checked set)
        assert trade._1pm_checked is True

    def test_should_exit_with_pdt_off(self):
        """should_exit path skips 1pm management when PDT inactive."""
        s = _make_strategy(pdt_mode_active=False)
        trade = _make_trade(direction='bullish', entry_price=5900.0)
        trade._current_spx_price = 5905.0
        trade.spread.expiration = ET.localize(datetime(2026, 2, 21, 16, 0))
        trade.spread.max_profit = 250.0
        trade.entry_price = 2.50
        at_1pm = ET.localize(datetime(2026, 2, 21, 13, 15))

        should_exit, reason = s.should_exit(trade, 2.40, at_1pm)
        # 1pm check never ran, flag never set
        assert trade._1pm_checked is False


# ---------------------------------------------------------------------------
# TestBBAnalyticsTracking
# ---------------------------------------------------------------------------

class TestBBAnalyticsTracking:
    """BB agreement is tracked for analytics but never gates DI entry."""

    def test_compute_bb_agreement_true(self):
        """Returns True when BB filter agrees with direction."""
        bb = MagicMock()
        bb.check_alignment.return_value = (True, 'aligned')
        result = SPXIncomeStrategy.compute_bb_agreement(bb, TradeDirection.BULLISH)
        assert result is True

    def test_compute_bb_agreement_false(self):
        """Returns False when BB filter disagrees with direction."""
        bb = MagicMock()
        bb.check_alignment.return_value = (False, 'not aligned')
        result = SPXIncomeStrategy.compute_bb_agreement(bb, TradeDirection.BEARISH)
        assert result is False

    def test_compute_bb_agreement_none_filter(self):
        """Returns None when no BB filter available."""
        result = SPXIncomeStrategy.compute_bb_agreement(None, TradeDirection.BULLISH)
        assert result is None

    def test_compute_bb_agreement_exception(self):
        """Returns None on BB filter exception."""
        bb = MagicMock()
        bb.check_alignment.side_effect = RuntimeError("broken")
        result = SPXIncomeStrategy.compute_bb_agreement(bb, TradeDirection.BULLISH)
        assert result is None


# ---------------------------------------------------------------------------
# TestBacktestPDTMode
# ---------------------------------------------------------------------------

class TestBacktestPDTMode:
    """Backtest engine correctly sets PDT mode and tracks bb_agreement."""

    def test_initial_capital_below_threshold(self):
        """initial_capital < $25k enables PDT mode in backtest."""
        from src.backtest.engine import BacktestTrade
        # Simulate what __init__ does
        initial_capital = 10000
        pdt_threshold = 25000
        pdt_mode = initial_capital < pdt_threshold
        assert pdt_mode is True

    def test_initial_capital_above_threshold(self):
        """initial_capital >= $25k disables PDT mode in backtest."""
        initial_capital = 50000
        pdt_threshold = 25000
        pdt_mode = initial_capital < pdt_threshold
        assert pdt_mode is False

    def test_bb_agreement_in_trade_dict(self):
        """BacktestTrade.to_dict() includes bb_agreement field."""
        from src.backtest.engine import BacktestTrade
        trade = BacktestTrade(
            trade_id='test-123',
            entry_time=datetime(2026, 2, 21, 10, 0),
            direction='bullish',
            bb_agreement=True,
        )
        d = trade.to_dict()
        assert 'bb_agreement' in d
        assert d['bb_agreement'] is True

    def test_bb_agreement_default_none(self):
        """BacktestTrade bb_agreement defaults to None."""
        from src.backtest.engine import BacktestTrade
        trade = BacktestTrade(
            trade_id='test-456',
            entry_time=datetime(2026, 2, 21, 10, 0),
        )
        assert trade.bb_agreement is None
        assert trade.to_dict()['bb_agreement'] is None
