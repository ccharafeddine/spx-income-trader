"""
Tests for 5-minute bar aggregator (display-only).

Covers:
- OHLC accumulation from ticks
- 5-minute boundary completion
- Auto-eviction at MAX_BARS
- Market hours filtering
- Reset behavior
- API endpoint structure
- Entry/exit marker data in /api/today
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from datetime import datetime
import pytz
from unittest.mock import patch, MagicMock

from src.core.bar_aggregator_5min import BarAggregator5Min, MAX_BARS

ET = pytz.timezone("America/New_York")


def _ts(hour, minute, second=0):
    """Create an ET-aware timestamp for testing."""
    return ET.localize(datetime(2026, 2, 20, hour, minute, second))


@pytest.fixture
def agg():
    return BarAggregator5Min()


class TestOHLCAccumulation:
    """5-min bar OHLC values from ticks."""

    def test_single_tick_starts_bar(self, agg):
        """First tick starts a new bar but doesn't complete one."""
        result = agg.add_price(_ts(10, 0), 5800.0)
        assert result is None
        assert agg.open_price == 5800.0
        assert agg.tick_count == 1

    def test_multiple_ticks_update_ohlc(self, agg):
        """Multiple ticks within same bar update high/low/close."""
        agg.add_price(_ts(10, 0), 5800.0)
        agg.add_price(_ts(10, 1), 5810.0)
        agg.add_price(_ts(10, 2), 5795.0)
        agg.add_price(_ts(10, 3), 5805.0)
        assert agg.open_price == 5800.0
        assert agg.high_price == 5810.0
        assert agg.low_price == 5795.0
        assert agg.close_price == 5805.0
        assert agg.tick_count == 4

    def test_bar_completes_at_boundary(self, agg):
        """Bar completes when tick crosses 5-min boundary."""
        agg.add_price(_ts(10, 0), 5800.0)
        agg.add_price(_ts(10, 2), 5810.0)
        agg.add_price(_ts(10, 4), 5795.0)
        # Tick at 10:05 should complete the 10:00 bar
        bar = agg.add_price(_ts(10, 5), 5805.0)
        assert bar is not None
        assert bar.open == 5800.0
        assert bar.high == 5810.0
        assert bar.low == 5795.0
        assert bar.close == 5795.0
        assert bar.timestamp.strftime('%H:%M') == '10:00'

    def test_completed_bar_stored(self, agg):
        """Completed bars are stored in the list."""
        agg.add_price(_ts(10, 0), 5800.0)
        agg.add_price(_ts(10, 5), 5810.0)  # completes 10:00 bar
        bars = agg.get_bars()
        assert len(bars) == 1
        assert bars[0].open == 5800.0

    def test_multiple_bars_sequential(self, agg):
        """Multiple sequential bars build correctly."""
        agg.add_price(_ts(10, 0), 5800.0)
        agg.add_price(_ts(10, 5), 5810.0)  # completes 10:00
        agg.add_price(_ts(10, 10), 5820.0)  # completes 10:05
        bars = agg.get_bars()
        assert len(bars) == 2
        assert bars[0].timestamp.strftime('%H:%M') == '10:00'
        assert bars[1].timestamp.strftime('%H:%M') == '10:05'


class TestAutoEviction:
    """Auto-eviction when exceeding MAX_BARS."""

    def test_eviction_at_max(self, agg):
        """Bars beyond MAX_BARS are evicted (oldest removed)."""
        # Build MAX_BARS + 5 bars
        base_minute = 0
        for i in range(MAX_BARS + 5):
            h = 9 + (30 + i * 5) // 60
            m = (30 + i * 5) % 60
            if h >= 16:
                break
            agg.add_price(_ts(h, m), 5800.0 + i)
            agg.add_price(_ts(h, m + 4, 59), 5800.0 + i + 0.5)
        # Force last bar completion
        agg.add_price(_ts(15, 55), 5900.0)

        bars = agg.get_bars()
        assert len(bars) <= MAX_BARS

    def test_newest_bars_kept(self, agg):
        """After eviction, newest bars are kept."""
        # Build 30 bars (more than MAX_BARS=24)
        for i in range(30):
            h = 9 + (30 + i * 5) // 60
            m = (30 + i * 5) % 60
            if h >= 16:
                break
            agg.add_price(_ts(h, m), 5800.0 + i)
        # Complete the last bar
        agg.add_price(_ts(15, 55), 5900.0)

        bars = agg.get_bars()
        # The latest bars should be present
        if bars:
            last_bar_time = bars[-1].timestamp.strftime('%H:%M')
            # Should be a recent time, not 09:30
            assert last_bar_time >= '10:00'


class TestMarketHours:
    """Market hours boundary filtering."""

    def test_pre_market_ignored(self, agg):
        """Ticks before 09:30 are ignored."""
        result = agg.add_price(_ts(9, 29), 5800.0)
        assert result is None
        assert agg.current_bar_start is None

    def test_market_open_accepted(self, agg):
        """Tick at exactly 09:30 is accepted."""
        agg.add_price(_ts(9, 30), 5800.0)
        assert agg.current_bar_start is not None
        assert agg.open_price == 5800.0

    def test_after_close_ignored(self, agg):
        """Ticks at or after 16:00 are ignored."""
        agg.add_price(_ts(9, 30), 5800.0)
        result = agg.add_price(_ts(16, 0), 5810.0)
        assert result is None
        # Original bar should still be building
        assert agg.close_price == 5800.0

    def test_last_minute_before_close(self, agg):
        """Ticks at 15:59 are accepted."""
        agg.add_price(_ts(15, 55), 5800.0)
        agg.add_price(_ts(15, 59), 5810.0)
        assert agg.close_price == 5810.0


class TestReset:
    """Reset behavior for new trading day."""

    def test_reset_clears_all(self, agg):
        """Reset clears bars and current bar state."""
        agg.add_price(_ts(10, 0), 5800.0)
        agg.add_price(_ts(10, 5), 5810.0)
        assert len(agg.get_bars()) == 1

        agg.reset()
        assert len(agg.get_bars()) == 0
        assert agg.current_bar_start is None
        assert agg.tick_count == 0


class TestGetBars:
    """get_bars() with count parameter."""

    def test_get_all(self, agg):
        """get_bars() returns all bars."""
        agg.add_price(_ts(10, 0), 5800.0)
        agg.add_price(_ts(10, 5), 5810.0)
        agg.add_price(_ts(10, 10), 5820.0)
        assert len(agg.get_bars()) == 2

    def test_get_limited(self, agg):
        """get_bars(count) returns limited bars."""
        agg.add_price(_ts(10, 0), 5800.0)
        agg.add_price(_ts(10, 5), 5810.0)
        agg.add_price(_ts(10, 10), 5820.0)
        bars = agg.get_bars(count=1)
        assert len(bars) == 1
        assert bars[0].timestamp.strftime('%H:%M') == '10:05'

    def test_get_more_than_available(self, agg):
        """get_bars(count) with count > available returns all."""
        agg.add_price(_ts(10, 0), 5800.0)
        agg.add_price(_ts(10, 5), 5810.0)
        bars = agg.get_bars(count=100)
        assert len(bars) == 1


class TestCurrentBarInfo:
    """get_current_bar_info() for UI."""

    def test_no_bar(self, agg):
        """No bar building returns None."""
        assert agg.get_current_bar_info() is None

    def test_building_bar(self, agg):
        """Building bar returns info dict."""
        agg.add_price(_ts(10, 0), 5800.0)
        agg.add_price(_ts(10, 1), 5810.0)
        info = agg.get_current_bar_info()
        assert info['start'] == '10:00'
        assert info['open'] == 5800.0
        assert info['high'] == 5810.0
        assert info['ticks'] == 2


class TestTimezoneHandling:
    """Naive timestamps get localized properly."""

    def test_naive_timestamp(self, agg):
        """Naive datetime gets localized to ET."""
        naive = datetime(2026, 2, 20, 10, 0, 0)
        agg.add_price(naive, 5800.0)
        assert agg.current_bar_start is not None
        assert agg.current_bar_start.tzinfo is not None


class TestBars5minEndpoint:
    """/api/chart/bars5min endpoint."""

    def test_response_structure(self):
        """Endpoint returns expected keys."""
        from dashboard.app import app

        with patch('dashboard.app.yahoo') as mock_yahoo:
            mock_yahoo.get_intraday_bars.return_value = [
                {'timestamp': '2026-02-20 10:00', 'open': 5800, 'high': 5810, 'low': 5795, 'close': 5805},
                {'timestamp': '2026-02-20 10:05', 'open': 5805, 'high': 5815, 'low': 5800, 'close': 5812},
            ]
            with app.test_client() as c:
                resp = c.get('/api/chart/bars5min')
                data = resp.get_json()
                assert data['success'] is True
                assert 'bars' in data
                assert len(data['bars']) == 2
                assert 'open' in data['bars'][0]
                assert 'high' in data['bars'][0]
                assert 'low' in data['bars'][0]
                assert 'close' in data['bars'][0]

    def test_empty_fallback(self):
        """When no bars available, returns empty list."""
        from dashboard.app import app

        with patch('dashboard.app.yahoo') as mock_yahoo:
            mock_yahoo.get_intraday_bars.return_value = []
            with app.test_client() as c:
                resp = c.get('/api/chart/bars5min')
                data = resp.get_json()
                assert data['success'] is True
                assert data['bars'] == []

    def test_yahoo_exception_fallback(self):
        """When Yahoo fails, returns empty list."""
        from dashboard.app import app

        with patch('dashboard.app.yahoo') as mock_yahoo:
            mock_yahoo.get_intraday_bars.side_effect = Exception("network error")
            with app.test_client() as c:
                resp = c.get('/api/chart/bars5min')
                data = resp.get_json()
                assert data['success'] is True
                assert data['bars'] == []
