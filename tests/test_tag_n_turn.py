"""
Tests for Tag 'n Turn weekend hold prevention.

Covers:
- Weekend exit triggers on Friday >= 15:00 ET when profit >= threshold
- Weekend exit does NOT trigger if profit below threshold
- Weekend exit does NOT trigger on non-Friday
- Weekend exit does NOT trigger before weekend_exit_time on Friday
- Target/stop takes priority over weekend exit
- weekend_exit_enabled: false disables the feature entirely
"""

import pytest
from unittest.mock import patch
from datetime import datetime
from pathlib import Path
import sys

import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.tag_n_turn import TagNTurnStrategy, TNTState

ET = pytz.timezone("America/New_York")


# ============================================================================
# FIXTURES
# ============================================================================

def _make_tnt(weekend_enabled=True, weekend_time="15:00", weekend_min_pct=30):
    """Create a TagNTurnStrategy with a position open and weekend config."""
    config = {
        'enabled': True,
        'bb_period': 50,
        'bb_std': 2.0,
        'pulse_threshold': 10.0,
        'min_bar_range_points': 1.0,
        'max_hold_days': 7,
        'use_credit_spreads': True,
        'spread_width': 10.0,
        'min_credit': 2.00,
        'min_dte': 3,
        'max_dte': 7,
        'weekend_exit_enabled': weekend_enabled,
        'weekend_exit_time': weekend_time,
        'weekend_exit_min_profit_pct': weekend_min_pct,
    }
    # Use a temp persistence path that won't exist (avoids loading stale state)
    tnt = TagNTurnStrategy(config, persistence_path=Path("/tmp/_tnt_test_nonexistent.json"))
    return tnt


def _open_bullish_position(tnt, entry_price=5800.0, target_price=5900.0, stop_price=5750.0):
    """Put the strategy into POSITION_OPEN state with a bullish position."""
    tnt.state = TNTState.POSITION_OPEN
    tnt.active_position = {
        'direction': 'bullish',
        'entry_price': entry_price,
        'target_price': target_price,
        'stop_price': stop_price,
        'entry_time': datetime(2026, 2, 23, 10, 0, tzinfo=ET).isoformat(),
    }


def _open_bearish_position(tnt, entry_price=5900.0, target_price=5800.0, stop_price=5950.0):
    """Put the strategy into POSITION_OPEN state with a bearish position."""
    tnt.state = TNTState.POSITION_OPEN
    tnt.active_position = {
        'direction': 'bearish',
        'entry_price': entry_price,
        'target_price': target_price,
        'stop_price': stop_price,
        'entry_time': datetime(2026, 2, 23, 10, 0, tzinfo=ET).isoformat(),
    }


# ============================================================================
# TESTS
# ============================================================================

class TestWeekendExitTriggers:
    """Weekend exit triggers on Friday at or after 15:00 ET when profit >= threshold."""

    def test_bullish_triggers_friday_1530_above_threshold(self):
        tnt = _make_tnt(weekend_min_pct=30)
        _open_bullish_position(tnt, entry_price=5800.0, target_price=5900.0)
        # 40% progress: 5800 + 0.4 * 100 = 5840
        friday_1530 = datetime(2026, 2, 27, 15, 30, tzinfo=ET)
        result = tnt.check_exit_conditions(5840.0, current_time=friday_1530)
        assert result is not None
        assert result['reason'] == 'weekend_hold_prevention'

    def test_bearish_triggers_friday_1500_above_threshold(self):
        tnt = _make_tnt(weekend_min_pct=30)
        _open_bearish_position(tnt, entry_price=5900.0, target_price=5800.0)
        # 40% progress: 5900 - 0.4 * 100 = 5860
        friday_1500 = datetime(2026, 2, 27, 15, 0, tzinfo=ET)
        result = tnt.check_exit_conditions(5860.0, current_time=friday_1500)
        assert result is not None
        assert result['reason'] == 'weekend_hold_prevention'


class TestWeekendExitBelowThreshold:
    """Weekend exit does NOT trigger if profit below threshold."""

    def test_bullish_below_threshold_returns_none(self):
        tnt = _make_tnt(weekend_min_pct=30)
        _open_bullish_position(tnt, entry_price=5800.0, target_price=5900.0)
        # 15% progress: 5800 + 0.15 * 100 = 5815
        friday_1530 = datetime(2026, 2, 27, 15, 30, tzinfo=ET)
        result = tnt.check_exit_conditions(5815.0, current_time=friday_1530)
        assert result is None

    def test_bearish_below_threshold_returns_none(self):
        tnt = _make_tnt(weekend_min_pct=30)
        _open_bearish_position(tnt, entry_price=5900.0, target_price=5800.0)
        # 10% progress: 5900 - 0.1 * 100 = 5890
        friday_1530 = datetime(2026, 2, 27, 15, 30, tzinfo=ET)
        result = tnt.check_exit_conditions(5890.0, current_time=friday_1530)
        assert result is None


class TestWeekendExitNonFriday:
    """Weekend exit does NOT trigger on non-Friday."""

    def test_wednesday_above_threshold_returns_none(self):
        tnt = _make_tnt(weekend_min_pct=30)
        _open_bullish_position(tnt, entry_price=5800.0, target_price=5900.0)
        # 40% progress, but Wednesday
        wednesday_1530 = datetime(2026, 2, 25, 15, 30, tzinfo=ET)
        assert wednesday_1530.weekday() == 2  # sanity check: Wednesday
        result = tnt.check_exit_conditions(5840.0, current_time=wednesday_1530)
        assert result is None


class TestWeekendExitBeforeTime:
    """Weekend exit does NOT trigger before weekend_exit_time on Friday."""

    def test_friday_1430_above_threshold_returns_none(self):
        tnt = _make_tnt(weekend_min_pct=30, weekend_time="15:00")
        _open_bullish_position(tnt, entry_price=5800.0, target_price=5900.0)
        # 40% progress, but before 15:00
        friday_1430 = datetime(2026, 2, 27, 14, 30, tzinfo=ET)
        result = tnt.check_exit_conditions(5840.0, current_time=friday_1430)
        assert result is None


class TestTargetStopPriority:
    """Target/stop takes priority over weekend exit."""

    def test_target_hit_overrides_weekend_exit(self):
        tnt = _make_tnt(weekend_min_pct=30)
        _open_bullish_position(tnt, entry_price=5800.0, target_price=5900.0)
        # Price at target (100% progress) on Friday 15:30
        friday_1530 = datetime(2026, 2, 27, 15, 30, tzinfo=ET)
        result = tnt.check_exit_conditions(5900.0, current_time=friday_1530)
        assert result is not None
        assert result['reason'] == 'target_hit'

    def test_stop_hit_overrides_weekend_exit(self):
        tnt = _make_tnt(weekend_min_pct=30)
        _open_bullish_position(tnt, entry_price=5800.0, target_price=5900.0, stop_price=5750.0)
        # Price at stop on Friday 15:30
        friday_1530 = datetime(2026, 2, 27, 15, 30, tzinfo=ET)
        result = tnt.check_exit_conditions(5750.0, current_time=friday_1530)
        assert result is not None
        assert result['reason'] == 'stop_hit'


class TestWeekendExitDisabled:
    """weekend_exit_enabled: false disables the feature entirely."""

    def test_disabled_returns_none_on_friday(self):
        tnt = _make_tnt(weekend_enabled=False, weekend_min_pct=30)
        _open_bullish_position(tnt, entry_price=5800.0, target_price=5900.0)
        # 40% progress on Friday 15:30, but feature disabled
        friday_1530 = datetime(2026, 2, 27, 15, 30, tzinfo=ET)
        result = tnt.check_exit_conditions(5840.0, current_time=friday_1530)
        assert result is None
