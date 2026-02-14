"""
Tests for the Economic Calendar module.

Tests cover:
- JSON loading from file (valid, missing, corrupt)
- Date matching (exact date, no events, multiple events same day)
- High-impact day detection
- Empty day returns empty list
- Upcoming events window (N-day lookahead)
- Cache invalidation
- format_events_short helper
- DB migration adds economic_events column
- Integration: enter_trade tags economic events
"""

import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.data.economic_calendar import (
    load_calendar,
    invalidate_cache,
    get_events_for_date,
    get_today_events,
    is_high_impact_day,
    get_upcoming_events,
    format_events_short,
)


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the module cache before each test."""
    invalidate_cache()
    yield
    invalidate_cache()


@pytest.fixture
def sample_calendar(tmp_path):
    """Create a temporary calendar JSON file with known events."""
    data = {
        "events": [
            {"date": "2026-02-11", "time": "08:30", "event": "CPI", "impact": "high", "description": "CPI MoM/YoY (Jan)"},
            {"date": "2026-02-11", "time": "10:00", "event": "NFP", "impact": "high", "description": "Nonfarm Payrolls"},
            {"date": "2026-02-13", "time": "14:00", "event": "FOMC", "impact": "high", "description": "FOMC Rate Decision"},
            {"date": "2026-02-15", "time": "08:30", "event": "GDP", "impact": "high", "description": "GDP Advance (Q4)"},
            {"date": "2026-02-20", "time": "08:30", "event": "PCE", "impact": "high", "description": "PCE Price Index"},
            {"date": "2026-03-01", "time": "08:30", "event": "NFP", "impact": "high", "description": "Nonfarm Payrolls (Feb)"},
        ]
    }
    cal_file = tmp_path / "test_calendar.json"
    cal_file.write_text(json.dumps(data))
    return cal_file


@pytest.fixture
def empty_calendar(tmp_path):
    """Calendar file with no events."""
    data = {"events": []}
    cal_file = tmp_path / "empty_cal.json"
    cal_file.write_text(json.dumps(data))
    return cal_file


@pytest.fixture
def corrupt_calendar(tmp_path):
    """Calendar file with invalid JSON."""
    cal_file = tmp_path / "corrupt.json"
    cal_file.write_text("{not valid json")
    return cal_file


# ============================================================================
# JSON Loading
# ============================================================================

class TestLoadCalendar:
    """Tests for load_calendar() function."""

    def test_load_valid_file(self, sample_calendar):
        events = load_calendar(sample_calendar)
        assert len(events) == 6
        assert events[0]['event'] == 'CPI'

    def test_load_missing_file(self, tmp_path):
        missing = tmp_path / "nonexistent.json"
        events = load_calendar(missing)
        assert events == []

    def test_load_corrupt_file(self, corrupt_calendar):
        events = load_calendar(corrupt_calendar)
        assert events == []

    def test_load_empty_calendar(self, empty_calendar):
        events = load_calendar(empty_calendar)
        assert events == []

    def test_caches_results(self, sample_calendar):
        events1 = load_calendar(sample_calendar)
        events2 = load_calendar(sample_calendar)
        assert events1 is events2  # Same object reference = cached

    def test_invalidate_cache_forces_reload(self, sample_calendar):
        events1 = load_calendar(sample_calendar)
        invalidate_cache()
        events2 = load_calendar(sample_calendar)
        assert events1 is not events2  # Different objects after cache clear
        assert len(events1) == len(events2)


# ============================================================================
# Date Matching
# ============================================================================

class TestGetEventsForDate:
    """Tests for get_events_for_date() function."""

    def test_exact_date_match(self, sample_calendar):
        load_calendar(sample_calendar)
        events = get_events_for_date(date(2026, 2, 13))
        assert len(events) == 1
        assert events[0]['event'] == 'FOMC'

    def test_multiple_events_same_day(self, sample_calendar):
        load_calendar(sample_calendar)
        events = get_events_for_date(date(2026, 2, 11))
        assert len(events) == 2
        event_names = {e['event'] for e in events}
        assert event_names == {'CPI', 'NFP'}

    def test_no_events_for_date(self, sample_calendar):
        load_calendar(sample_calendar)
        events = get_events_for_date(date(2026, 2, 12))
        assert events == []

    def test_accepts_string_date(self, sample_calendar):
        load_calendar(sample_calendar)
        events = get_events_for_date("2026-02-13")
        assert len(events) == 1
        assert events[0]['event'] == 'FOMC'

    def test_no_events_for_far_future_date(self, sample_calendar):
        load_calendar(sample_calendar)
        events = get_events_for_date(date(2030, 1, 1))
        assert events == []


# ============================================================================
# Today Events
# ============================================================================

class TestGetTodayEvents:
    """Tests for get_today_events() function."""

    def test_with_explicit_date(self, sample_calendar):
        load_calendar(sample_calendar)
        events = get_today_events(today=date(2026, 2, 13))
        assert len(events) == 1
        assert events[0]['event'] == 'FOMC'

    def test_no_events_today(self, sample_calendar):
        load_calendar(sample_calendar)
        events = get_today_events(today=date(2026, 2, 12))
        assert events == []


# ============================================================================
# High-Impact Detection
# ============================================================================

class TestIsHighImpactDay:
    """Tests for is_high_impact_day() function."""

    def test_high_impact_day(self, sample_calendar):
        load_calendar(sample_calendar)
        assert is_high_impact_day(date(2026, 2, 13)) is True

    def test_not_high_impact_day(self, sample_calendar):
        load_calendar(sample_calendar)
        assert is_high_impact_day(date(2026, 2, 12)) is False

    def test_multiple_events_still_high(self, sample_calendar):
        load_calendar(sample_calendar)
        assert is_high_impact_day(date(2026, 2, 11)) is True

    def test_empty_calendar_not_high_impact(self, empty_calendar):
        load_calendar(empty_calendar)
        assert is_high_impact_day(date(2026, 2, 13)) is False


# ============================================================================
# Upcoming Events Window
# ============================================================================

class TestGetUpcomingEvents:
    """Tests for get_upcoming_events() function."""

    def test_7_day_window(self, sample_calendar):
        load_calendar(sample_calendar)
        upcoming = get_upcoming_events(days=7, from_date=date(2026, 2, 11))
        # 2/11: CPI+NFP, 2/13: FOMC, 2/15: GDP = 4 events in 5-day span
        assert len(upcoming) == 4
        assert upcoming[0]['event'] == 'CPI'  # sorted by date then time

    def test_1_day_window(self, sample_calendar):
        load_calendar(sample_calendar)
        upcoming = get_upcoming_events(days=1, from_date=date(2026, 2, 13))
        assert len(upcoming) == 1
        assert upcoming[0]['event'] == 'FOMC'

    def test_window_excludes_past(self, sample_calendar):
        load_calendar(sample_calendar)
        # Start from 2/14 - should not include 2/13 FOMC
        upcoming = get_upcoming_events(days=7, from_date=date(2026, 2, 14))
        event_names = [e['event'] for e in upcoming]
        assert 'FOMC' not in event_names
        assert 'GDP' in event_names  # 2/15 is in range

    def test_window_includes_end_date(self, sample_calendar):
        load_calendar(sample_calendar)
        # 3 days from 2/13 = 2/13, 2/14, 2/15
        upcoming = get_upcoming_events(days=3, from_date=date(2026, 2, 13))
        event_names = [e['event'] for e in upcoming]
        assert 'FOMC' in event_names
        assert 'GDP' in event_names

    def test_empty_window(self, sample_calendar):
        load_calendar(sample_calendar)
        upcoming = get_upcoming_events(days=1, from_date=date(2026, 2, 12))
        assert upcoming == []

    def test_sorted_by_date_then_time(self, sample_calendar):
        load_calendar(sample_calendar)
        upcoming = get_upcoming_events(days=10, from_date=date(2026, 2, 11))
        dates = [e['date'] for e in upcoming]
        assert dates == sorted(dates)

    def test_large_window(self, sample_calendar):
        load_calendar(sample_calendar)
        upcoming = get_upcoming_events(days=365, from_date=date(2026, 1, 1))
        assert len(upcoming) == 6  # All events in the sample


# ============================================================================
# Format Helper
# ============================================================================

class TestFormatEventsShort:
    """Tests for format_events_short() helper."""

    def test_single_event(self):
        events = [{"event": "CPI", "time": "08:30"}]
        assert format_events_short(events) == "CPI 08:30"

    def test_multiple_events(self):
        events = [
            {"event": "CPI", "time": "08:30"},
            {"event": "FOMC", "time": "14:00"},
        ]
        assert format_events_short(events) == "CPI 08:30, FOMC 14:00"

    def test_empty_list(self):
        assert format_events_short([]) == ""


# ============================================================================
# Real Calendar File
# ============================================================================

class TestRealCalendarFile:
    """Sanity checks against the actual config/economic_calendar.json."""

    def test_real_file_loads(self):
        """The shipped calendar file should load without errors."""
        events = load_calendar()
        assert len(events) > 50  # 2 years of events should have plenty

    def test_all_events_have_required_fields(self):
        events = load_calendar()
        for e in events:
            assert 'date' in e, f"Missing 'date' in {e}"
            assert 'event' in e, f"Missing 'event' in {e}"
            assert 'impact' in e, f"Missing 'impact' in {e}"

    def test_known_fomc_date(self):
        """Spot-check a known FOMC date."""
        events = load_calendar()
        fomc_dates = [e['date'] for e in events if e['event'] == 'FOMC']
        assert '2026-01-28' in fomc_dates

    def test_all_event_types_present(self):
        events = load_calendar()
        event_types = {e['event'] for e in events}
        assert event_types == {'FOMC', 'CPI', 'NFP', 'GDP', 'PCE'}


# ============================================================================
# DB Migration
# ============================================================================

class TestDBMigration:
    """Test that migration adds the economic_events column."""

    def test_economic_events_column_exists(self, tmp_path):
        from database.db_manager import DatabaseManager
        db_path = str(tmp_path / "test_econ.db")
        db = DatabaseManager(db_path)

        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(trades)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()

        assert 'economic_events' in columns


# ============================================================================
# Integration: enter_trade tags events
# ============================================================================

class TestEnterTradeEconomicEvents:
    """Integration test: enter_trade() tags economic events in entry_context."""

    def test_tags_events_on_event_day(self, sample_calendar):
        """Trade entered on an event day should have economic_events in context."""
        from src.core.position_manager import PositionManager
        from src.models.spread import CreditSpread, OptionLeg, TradeDirection
        import pytz

        ET = pytz.timezone("America/New_York")

        # Pre-load sample calendar
        load_calendar(sample_calendar)

        broker = MagicMock()
        broker.place_spread_order.return_value = "order-eco-1"
        broker.get_order_status.return_value = {'status': 'filled', 'fill_price': 2.50}

        strategy = MagicMock()
        strategy.classify_credit_quality.return_value = {
            'quality': 'GOOD', 'credit_received': 2.50, 'moneyness': 'ATM',
            'expected_range': '$2.40-$2.60', 'expected_min': 2.40,
            'expected_max': 2.60, 'pct_of_expected_min': 104.2,
            'is_simulated_concern': False,
        }

        saved_contexts = []
        db = MagicMock()
        db.save_trade.side_effect = lambda trade, context=None: saved_contexts.append(context)

        pm = PositionManager(broker=broker, strategy=strategy, db_manager=db)

        from datetime import datetime
        now = datetime.now(ET)
        spread = CreditSpread(
            direction=TradeDirection.BULLISH,
            short_leg=OptionLeg(strike=6000, option_type='put', action='sell', price=3.00),
            long_leg=OptionLeg(strike=5995, option_type='put', action='buy', price=1.00),
            credit_received=2.00,
            entry_time=now,
            expiration=now.replace(hour=16, minute=0),
            underlying_price_at_entry=6000.0,
        )

        bar = MagicMock()
        bar.timestamp = now
        bar.open = 6000.0
        bar.high = 6010.0
        bar.low = 5990.0
        bar.close = 6005.0
        bar.range = 20.0
        bar.close_percentage_in_range.return_value = 75.0

        # Mock get_today_events to return FOMC for "today"
        with patch('time.sleep'), \
             patch('src.data.economic_calendar.get_today_events',
                   return_value=[{"date": "2026-02-13", "time": "14:00", "event": "FOMC", "impact": "high", "description": "FOMC Rate Decision"}]):
            trade = pm.enter_trade(spread, bar, quantity=1)

        assert trade is not None
        ctx = saved_contexts[0]
        assert 'economic_events' in ctx
        parsed = json.loads(ctx['economic_events'])
        assert parsed == ['FOMC']

    def test_no_tag_on_quiet_day(self):
        """Trade entered on a non-event day should have no economic_events key."""
        from src.core.position_manager import PositionManager
        from src.models.spread import CreditSpread, OptionLeg, TradeDirection
        import pytz

        ET = pytz.timezone("America/New_York")

        broker = MagicMock()
        broker.place_spread_order.return_value = "order-eco-2"
        broker.get_order_status.return_value = {'status': 'filled', 'fill_price': 2.00}

        strategy = MagicMock()
        strategy.classify_credit_quality.return_value = {
            'quality': 'GOOD', 'credit_received': 2.00, 'moneyness': 'ATM',
            'expected_range': '$2.40-$2.60', 'expected_min': 2.40,
            'expected_max': 2.60, 'pct_of_expected_min': 83.3,
            'is_simulated_concern': False,
        }

        saved_contexts = []
        db = MagicMock()
        db.save_trade.side_effect = lambda trade, context=None: saved_contexts.append(context)

        pm = PositionManager(broker=broker, strategy=strategy, db_manager=db)

        from datetime import datetime
        now = datetime.now(ET)
        spread = CreditSpread(
            direction=TradeDirection.BULLISH,
            short_leg=OptionLeg(strike=6000, option_type='put', action='sell', price=3.00),
            long_leg=OptionLeg(strike=5995, option_type='put', action='buy', price=1.00),
            credit_received=2.00,
            entry_time=now,
            expiration=now.replace(hour=16, minute=0),
            underlying_price_at_entry=6000.0,
        )

        bar = MagicMock()
        bar.timestamp = now
        bar.open = 6000.0
        bar.high = 6010.0
        bar.low = 5990.0
        bar.close = 6005.0
        bar.range = 20.0
        bar.close_percentage_in_range.return_value = 75.0

        # Mock empty events
        with patch('time.sleep'), \
             patch('src.data.economic_calendar.get_today_events', return_value=[]):
            trade = pm.enter_trade(spread, bar, quantity=1)

        assert trade is not None
        ctx = saved_contexts[0]
        assert 'economic_events' not in ctx
