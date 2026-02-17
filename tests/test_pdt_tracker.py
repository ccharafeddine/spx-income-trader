"""
Comprehensive tests for PDT (Pattern Day Trader) Tracker

Tests cover:
- Rolling window counting
- Weekend handling
- Expiration vs active close distinction
- Account equity threshold toggle
- Edge cases (mid-week equity crossing $25k)
"""

import pytest
from unittest.mock import Mock, patch
from datetime import date, datetime, timedelta
import tempfile
import os

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.pdt_tracker import (
    PDTTracker,
    is_business_day,
    get_business_days_ago,
    get_rolling_window_start,
    NYSE_HOLIDAYS,
    ACTIVE_CLOSE_REASONS,
)


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def temp_db():
    """Create a temporary database file."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def tracker(temp_db):
    """Create PDTTracker with temp database."""
    return PDTTracker(
        db_path=temp_db,
        enabled=True,
        threshold=25000.0,
        max_day_trades=3,
        window_days=5,
    )


@pytest.fixture
def tracker_with_equity(temp_db):
    """Create PDTTracker with mocked account equity."""
    equity_value = [20000.0]  # Mutable so we can change it

    def get_equity():
        return equity_value[0]

    tracker = PDTTracker(
        db_path=temp_db,
        enabled=True,
        threshold=25000.0,
        max_day_trades=3,
        window_days=5,
        get_account_equity=get_equity,
    )
    tracker._equity_value = equity_value  # Store reference for tests
    return tracker


# ============================================================================
# TEST: BUSINESS DAY HELPERS
# ============================================================================

class TestBusinessDayHelpers:
    """Test business day utility functions."""

    def test_weekday_is_business_day(self):
        """Monday-Friday (non-holiday) should be business days."""
        # A regular Wednesday
        assert is_business_day(date(2026, 2, 4)) is True

    def test_saturday_not_business_day(self):
        """Saturday should not be a business day."""
        assert is_business_day(date(2026, 2, 7)) is False

    def test_sunday_not_business_day(self):
        """Sunday should not be a business day."""
        assert is_business_day(date(2026, 2, 8)) is False

    def test_holiday_not_business_day(self):
        """NYSE holidays should not be business days."""
        # Christmas 2026
        assert is_business_day(date(2026, 12, 25)) is False
        # MLK Day 2026
        assert is_business_day(date(2026, 1, 19)) is False

    def test_get_business_days_ago_simple(self):
        """Test getting N business days ago (no weekends crossed)."""
        # Wednesday Feb 4, 2026 - 2 business days ago = Monday Feb 2
        result = get_business_days_ago(2, date(2026, 2, 4))
        assert result == date(2026, 2, 2)

    def test_get_business_days_ago_crosses_weekend(self):
        """Test getting N business days ago crossing a weekend."""
        # Monday Feb 2, 2026 - 1 business day ago = Friday Jan 30
        result = get_business_days_ago(1, date(2026, 2, 2))
        assert result == date(2026, 1, 30)

    def test_rolling_window_start_5_days(self):
        """Test 5-business-day rolling window start."""
        # Friday Feb 6, 2026 - window includes 5 business days
        # So window start = 4 business days ago = Monday Feb 2
        # Window: Feb 2, Feb 3, Feb 4, Feb 5, Feb 6 = 5 business days
        result = get_rolling_window_start(5, date(2026, 2, 6))
        assert result == date(2026, 2, 2)


# ============================================================================
# TEST: ROLLING WINDOW COUNTING
# ============================================================================

class TestRollingWindowCounting:
    """Test day trade counting over rolling window."""

    def test_empty_window(self, tracker):
        """No day trades should return 0 count."""
        assert tracker.get_day_trades_count() == 0
        assert tracker.get_remaining_day_trades() == 3

    def test_single_day_trade(self, tracker):
        """One day trade should be counted."""
        tracker.record_day_trade('trade1', date.today(), 'profit_target')
        assert tracker.get_day_trades_count() == 1
        assert tracker.get_remaining_day_trades() == 2

    def test_multiple_day_trades(self, tracker):
        """Multiple day trades should all be counted."""
        today = date.today()
        tracker.record_day_trade('trade1', today, 'profit_target')
        tracker.record_day_trade('trade2', today, 'early exit')
        tracker.record_day_trade('trade3', today, 'stop loss')

        assert tracker.get_day_trades_count() == 3
        assert tracker.get_remaining_day_trades() == 0

    def test_duplicate_trade_id_ignored(self, tracker):
        """Same trade_id should only be counted once."""
        today = date.today()
        tracker.record_day_trade('trade1', today, 'profit_target')
        tracker.record_day_trade('trade1', today, 'profit_target')  # Duplicate

        assert tracker.get_day_trades_count() == 1

    def test_trades_outside_window_not_counted(self, tracker):
        """Trades older than 5 business days should not count."""
        today = date.today()
        old_date = get_business_days_ago(6, today)  # Outside 5-day window

        tracker.record_day_trade('old_trade', old_date, 'profit_target')
        tracker.record_day_trade('new_trade', today, 'profit_target')

        assert tracker.get_day_trades_count() == 1  # Only new_trade counts

    def test_window_boundary_trade_counted(self, tracker):
        """Trade exactly at window start should be counted."""
        today = date.today()
        window_start = get_rolling_window_start(5, today)

        tracker.record_day_trade('boundary_trade', window_start, 'profit_target')
        tracker.record_day_trade('today_trade', today, 'profit_target')

        assert tracker.get_day_trades_count() == 2


# ============================================================================
# TEST: WEEKEND HANDLING
# ============================================================================

class TestWeekendHandling:
    """Test that weekends are properly handled in window calculations."""

    def test_friday_trade_counted_on_monday(self, tracker):
        """Friday trade should count in window checked on Monday."""
        # Simulate: Today is Monday Feb 2, 2026
        monday = date(2026, 2, 2)
        friday = date(2026, 1, 30)

        tracker.record_day_trade('friday_trade', friday, 'profit_target')

        # Friday is 1 business day ago, within 5-day window
        count = tracker.get_day_trades_count(from_date=monday)
        assert count == 1

    def test_monday_trade_within_window_from_friday(self, tracker):
        """Monday trade should be within 5-day window from Friday."""
        # 5-day window from Friday Feb 6 covers: Mon Feb 2 - Fri Feb 6
        this_friday = date(2026, 2, 6)
        monday = date(2026, 2, 2)  # First day of 5-day window

        tracker.record_day_trade('monday_trade', monday, 'profit_target')

        count = tracker.get_day_trades_count(from_date=this_friday)
        assert count == 1  # Should count (within window)

    def test_last_friday_outside_window(self, tracker):
        """Last Friday should be OUTSIDE 5-day window from this Friday."""
        # 5-day window from Friday Feb 6 covers: Mon Feb 2 - Fri Feb 6
        # Previous Friday (Jan 30) is outside this window
        this_friday = date(2026, 2, 6)
        last_friday = date(2026, 1, 30)

        tracker.record_day_trade('last_friday', last_friday, 'profit_target')

        count = tracker.get_day_trades_count(from_date=this_friday)
        assert count == 0  # Should NOT count (outside window)


# ============================================================================
# TEST: EXPIRATION VS ACTIVE CLOSE DISTINCTION
# ============================================================================

class TestExpirationVsActiveClose:
    """Test that only active closes count as day trades."""

    def test_profit_target_counts(self, tracker):
        """Profit target exit should count as day trade."""
        result = tracker.record_day_trade('t1', date.today(), 'profit_target')
        assert result is True
        assert tracker.get_day_trades_count() == 1

    def test_80_profit_target_counts(self, tracker):
        """80% profit target should count as day trade."""
        result = tracker.record_day_trade('t1', date.today(), '80% profit target')
        assert result is True

    def test_1pm_check_counts(self, tracker):
        """1pm trend check exit should count as day trade."""
        result = tracker.record_day_trade('t1', date.today(), '1PM CHECK - unfavorable')
        assert result is True

    def test_stop_loss_counts(self, tracker):
        """Stop loss should count as day trade."""
        result = tracker.record_day_trade('t1', date.today(), 'stop loss')
        assert result is True

    def test_early_exit_counts(self, tracker):
        """Early exit should count as day trade."""
        result = tracker.record_day_trade('t1', date.today(), 'early exit')
        assert result is True

    def test_manual_close_counts(self, tracker):
        """Manual close should count as day trade."""
        result = tracker.record_day_trade('t1', date.today(), 'manual close')
        assert result is True

    def test_expiration_does_not_count(self, tracker):
        """Expiration should NOT count as day trade."""
        result = tracker.record_day_trade('t1', date.today(), 'expiration')
        assert result is False
        assert tracker.get_day_trades_count() == 0

    def test_expired_worthless_does_not_count(self, tracker):
        """Expired worthless should NOT count."""
        result = tracker.record_day_trade('t1', date.today(), 'expired worthless')
        assert result is False

    def test_max_profit_expiration_does_not_count(self, tracker):
        """Max profit at expiration should NOT count."""
        result = tracker.record_day_trade('t1', date.today(), 'max profit - expiration')
        assert result is False

    def test_check_and_record_same_day(self, tracker):
        """check_and_record_day_trade should record same-day active closes."""
        today = date.today()
        result = tracker.check_and_record_day_trade(
            trade_id='t1',
            entry_date=today,
            exit_date=today,
            exit_reason='profit_target',
        )
        assert result is True
        assert tracker.get_day_trades_count() == 1

    def test_check_and_record_different_days(self, tracker):
        """check_and_record_day_trade should NOT record if different days."""
        today = date.today()
        yesterday = today - timedelta(days=1)
        result = tracker.check_and_record_day_trade(
            trade_id='t1',
            entry_date=yesterday,
            exit_date=today,
            exit_reason='profit_target',
        )
        assert result is False
        assert tracker.get_day_trades_count() == 0


# ============================================================================
# TEST: ACCOUNT EQUITY THRESHOLD TOGGLE
# ============================================================================

class TestAccountEquityThreshold:
    """Test PDT restriction based on account equity."""

    def test_under_threshold_is_restricted(self, tracker_with_equity):
        """Account under $25k should be PDT restricted."""
        tracker_with_equity._equity_value[0] = 20000.0
        tracker_with_equity._cached_equity = None  # Force refresh
        assert tracker_with_equity.is_pdt_restricted() is True

    def test_at_threshold_not_restricted(self, tracker_with_equity):
        """Account at exactly $25k should NOT be restricted."""
        tracker_with_equity._equity_value[0] = 25000.0
        tracker_with_equity._cached_equity = None
        assert tracker_with_equity.is_pdt_restricted() is False

    def test_above_threshold_not_restricted(self, tracker_with_equity):
        """Account above $25k should NOT be restricted."""
        tracker_with_equity._equity_value[0] = 50000.0
        tracker_with_equity._cached_equity = None
        assert tracker_with_equity.is_pdt_restricted() is False

    def test_can_close_early_above_threshold(self, tracker_with_equity):
        """Above threshold should always allow early close."""
        tracker_with_equity._equity_value[0] = 50000.0
        tracker_with_equity._cached_equity = None

        # Use all 3 day trade slots
        today = date.today()
        tracker_with_equity.record_day_trade('t1', today, 'profit_target')
        tracker_with_equity.record_day_trade('t2', today, 'profit_target')
        tracker_with_equity.record_day_trade('t3', today, 'profit_target')

        # Still should be able to close (above threshold = unlimited)
        assert tracker_with_equity.can_close_early() is True

    def test_can_close_early_below_threshold_with_slots(self, tracker_with_equity):
        """Below threshold with slots available should allow close."""
        tracker_with_equity._equity_value[0] = 20000.0
        tracker_with_equity._cached_equity = None

        # Only 1 day trade used
        tracker_with_equity.record_day_trade('t1', date.today(), 'profit_target')

        assert tracker_with_equity.can_close_early() is True
        assert tracker_with_equity.get_remaining_day_trades() == 2

    def test_cannot_close_early_at_limit(self, tracker_with_equity):
        """Below threshold at limit should block early close."""
        tracker_with_equity._equity_value[0] = 20000.0
        tracker_with_equity._cached_equity = None

        # Use all 3 slots
        today = date.today()
        tracker_with_equity.record_day_trade('t1', today, 'profit_target')
        tracker_with_equity.record_day_trade('t2', today, 'profit_target')
        tracker_with_equity.record_day_trade('t3', today, 'profit_target')

        assert tracker_with_equity.can_close_early() is False
        assert tracker_with_equity.get_remaining_day_trades() == 0


# ============================================================================
# TEST: MID-WEEK EQUITY CROSSING $25K
# ============================================================================

class TestMidWeekEquityCrossing:
    """Test behavior when account crosses $25k threshold mid-week."""

    def test_crosses_above_threshold_allows_unlimited(self, tracker_with_equity):
        """Crossing above $25k should immediately allow unlimited day trades."""
        tracker_with_equity._equity_value[0] = 20000.0
        tracker_with_equity._cached_equity = None

        # Use all slots while under threshold
        today = date.today()
        tracker_with_equity.record_day_trade('t1', today, 'profit_target')
        tracker_with_equity.record_day_trade('t2', today, 'profit_target')
        tracker_with_equity.record_day_trade('t3', today, 'profit_target')

        # Can't close early (at limit, under threshold)
        assert tracker_with_equity.can_close_early() is False

        # Account grows above threshold
        tracker_with_equity._equity_value[0] = 30000.0
        tracker_with_equity._cached_equity = None  # Force refresh

        # Now should be able to close (above threshold)
        assert tracker_with_equity.can_close_early() is True

    def test_drops_below_threshold_restricts(self, tracker_with_equity):
        """Dropping below $25k should immediately restrict."""
        tracker_with_equity._equity_value[0] = 30000.0
        tracker_with_equity._cached_equity = None

        # Use all slots while above threshold (no problem)
        today = date.today()
        tracker_with_equity.record_day_trade('t1', today, 'profit_target')
        tracker_with_equity.record_day_trade('t2', today, 'profit_target')
        tracker_with_equity.record_day_trade('t3', today, 'profit_target')

        assert tracker_with_equity.can_close_early() is True

        # Account drops below threshold
        tracker_with_equity._equity_value[0] = 24000.0
        tracker_with_equity._cached_equity = None

        # Now restricted
        assert tracker_with_equity.can_close_early() is False

    def test_equity_cached_daily(self, tracker_with_equity):
        """Equity should be cached and not re-fetched same day."""
        tracker_with_equity._equity_value[0] = 20000.0
        tracker_with_equity._cached_equity = None

        # First call fetches equity
        assert tracker_with_equity.is_pdt_restricted() is True

        # Change equity value
        tracker_with_equity._equity_value[0] = 50000.0

        # Should still use cached value (20k = restricted)
        assert tracker_with_equity.is_pdt_restricted() is True

        # Force refresh
        tracker_with_equity.refresh_account_equity()
        assert tracker_with_equity.is_pdt_restricted() is False


# ============================================================================
# TEST: DISABLED PDT PROTECTION
# ============================================================================

class TestDisabledPDT:
    """Test behavior when PDT protection is disabled."""

    def test_disabled_always_allows_close(self, temp_db):
        """Disabled PDT should always allow early close."""
        tracker = PDTTracker(
            db_path=temp_db,
            enabled=False,
            threshold=25000.0,
        )

        # Use many "day trades"
        today = date.today()
        for i in range(10):
            tracker.record_day_trade(f't{i}', today, 'profit_target')

        # Still allowed (disabled)
        assert tracker.can_close_early() is True

    def test_disabled_not_restricted(self, temp_db):
        """Disabled PDT should never be restricted."""
        tracker = PDTTracker(
            db_path=temp_db,
            enabled=False,
            threshold=25000.0,
        )
        assert tracker.is_pdt_restricted() is False


# ============================================================================
# TEST: CAN OPEN TRADE (ENTRY GATE)
# ============================================================================

class TestCanOpenTrade:
    """Test PDT entry gating for new trades."""

    def test_disabled_allows_entry(self, temp_db):
        """Disabled PDT should always allow new entries."""
        tracker = PDTTracker(db_path=temp_db, enabled=False)
        assert tracker.can_open_trade() is True

    def test_above_threshold_allows_entry(self, tracker_with_equity):
        """Above $25k should always allow new entries."""
        tracker_with_equity._equity_value[0] = 50000.0
        tracker_with_equity._cached_equity = None

        # Use all 3 slots
        today = date.today()
        tracker_with_equity.record_day_trade('t1', today, 'profit_target')
        tracker_with_equity.record_day_trade('t2', today, 'profit_target')
        tracker_with_equity.record_day_trade('t3', today, 'profit_target')

        assert tracker_with_equity.can_open_trade() is True

    def test_below_threshold_with_slots_allows_entry(self, tracker_with_equity):
        """Below $25k with remaining slots should allow entries."""
        tracker_with_equity._equity_value[0] = 20000.0
        tracker_with_equity._cached_equity = None

        tracker_with_equity.record_day_trade('t1', date.today(), 'profit_target')

        assert tracker_with_equity.can_open_trade() is True
        assert tracker_with_equity.get_remaining_day_trades() == 2

    def test_below_threshold_no_slots_blocks_entry(self, tracker_with_equity):
        """Below $25k with no remaining slots should block entries."""
        tracker_with_equity._equity_value[0] = 20000.0
        tracker_with_equity._cached_equity = None

        today = date.today()
        tracker_with_equity.record_day_trade('t1', today, 'profit_target')
        tracker_with_equity.record_day_trade('t2', today, 'profit_target')
        tracker_with_equity.record_day_trade('t3', today, 'profit_target')

        assert tracker_with_equity.can_open_trade() is False

    def test_crosses_above_threshold_unblocks(self, tracker_with_equity):
        """Crossing above $25k should immediately unblock entries."""
        tracker_with_equity._equity_value[0] = 20000.0
        tracker_with_equity._cached_equity = None

        today = date.today()
        tracker_with_equity.record_day_trade('t1', today, 'profit_target')
        tracker_with_equity.record_day_trade('t2', today, 'profit_target')
        tracker_with_equity.record_day_trade('t3', today, 'profit_target')

        assert tracker_with_equity.can_open_trade() is False

        # Account grows above threshold
        tracker_with_equity._equity_value[0] = 30000.0
        tracker_with_equity._cached_equity = None

        assert tracker_with_equity.can_open_trade() is True

    def test_status_includes_can_open_trade(self, tracker_with_equity):
        """get_pdt_status() should include can_open_trade field."""
        status = tracker_with_equity.get_pdt_status()
        assert 'can_open_trade' in status
        assert isinstance(status['can_open_trade'], bool)


# ============================================================================
# TEST: PDT STATUS FOR DASHBOARD
# ============================================================================

class TestPDTStatus:
    """Test PDT status output for dashboard display."""

    def test_status_structure(self, tracker_with_equity):
        """Status should contain all required fields."""
        status = tracker_with_equity.get_pdt_status()

        assert 'enabled' in status
        assert 'account_value' in status
        assert 'threshold' in status
        assert 'is_restricted' in status
        assert 'day_trades_used' in status
        assert 'day_trades_remaining' in status
        assert 'max_day_trades' in status
        assert 'window_days' in status
        assert 'next_slot_frees_on' in status
        assert 'can_close_early' in status

    def test_status_values_accurate(self, tracker_with_equity):
        """Status values should be accurate."""
        tracker_with_equity._equity_value[0] = 20000.0
        tracker_with_equity._cached_equity = None

        tracker_with_equity.record_day_trade('t1', date.today(), 'profit_target')

        status = tracker_with_equity.get_pdt_status()

        assert status['enabled'] is True
        assert status['account_value'] == 20000.0
        assert status['threshold'] == 25000.0
        assert status['is_restricted'] is True
        assert status['day_trades_used'] == 1
        assert status['day_trades_remaining'] == 2
        assert status['can_close_early'] is True


# ============================================================================
# TEST: NEXT SLOT FREES ON
# ============================================================================

class TestNextSlotFreesOn:
    """Test calculation of when next slot becomes available."""

    def test_slots_available_returns_none(self, tracker):
        """If slots available, next_slot_frees_on should be None."""
        assert tracker.get_next_slot_frees_on() is None

    def test_at_limit_returns_date(self, tracker):
        """At limit, should return date when oldest trade drops off."""
        today = date.today()
        yesterday = today - timedelta(days=1)
        if not is_business_day(yesterday):
            yesterday = get_business_days_ago(1, today)

        # Fill all slots with oldest on yesterday
        tracker.record_day_trade('t1', yesterday, 'profit_target')
        tracker.record_day_trade('t2', today, 'profit_target')
        tracker.record_day_trade('t3', today, 'profit_target')

        next_free = tracker.get_next_slot_frees_on()

        # Should be 5 business days after the oldest trade
        assert next_free is not None
        # The oldest trade (yesterday) should exit window after 5 business days


# ============================================================================
# TEST: ACTIVE CLOSE REASONS LIST
# ============================================================================

class TestActiveCloseReasons:
    """Verify the ACTIVE_CLOSE_REASONS list is correct."""

    def test_profit_target_in_list(self):
        assert any('profit_target' in r.lower() for r in ACTIVE_CLOSE_REASONS)

    def test_stop_loss_in_list(self):
        assert any('stop loss' in r.lower() for r in ACTIVE_CLOSE_REASONS)

    def test_early_exit_in_list(self):
        assert any('early exit' in r.lower() for r in ACTIVE_CLOSE_REASONS)

    def test_1pm_check_in_list(self):
        assert any('1pm' in r.lower() for r in ACTIVE_CLOSE_REASONS)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
