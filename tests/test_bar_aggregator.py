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
from datetime import datetime, timedelta
import pytz
from unittest.mock import patch, MagicMock

from src.core.bar_aggregator_5min import BarAggregator5Min, MAX_BARS
from src.models.bar import Bar

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


class TestBotBarsIntegration:
    """Bot in-memory bars served through chart endpoints."""

    def test_5min_bot_bars_returned(self):
        """When desktop getter provides bars, endpoint returns them."""
        from dashboard.app import app

        mock_bars = [
            {'time': '10:00', 'open': 5800, 'high': 5810, 'low': 5795, 'close': 5805},
            {'time': '10:05', 'open': 5805, 'high': 5815, 'low': 5800, 'close': 5812},
        ]
        app._desktop_get_bot_bars_5min = lambda: mock_bars
        try:
            with app.test_client() as c:
                resp = c.get('/api/chart/bars5min')
                data = resp.get_json()
                assert data['success'] is True
                assert len(data['bars']) == 2
                assert data['bars'][0]['time'] == '10:00'
                assert data['bars'][1]['open'] == 5805
        finally:
            if hasattr(app, '_desktop_get_bot_bars_5min'):
                del app._desktop_get_bot_bars_5min

    def test_5min_bot_bars_skip_yahoo(self):
        """Bot bars take priority -- Yahoo is not called."""
        from dashboard.app import app

        mock_bars = [
            {'time': '10:00', 'open': 5800, 'high': 5810, 'low': 5795, 'close': 5805},
        ]
        app._desktop_get_bot_bars_5min = lambda: mock_bars
        try:
            with patch('dashboard.app.yahoo') as mock_yahoo:
                with app.test_client() as c:
                    resp = c.get('/api/chart/bars5min')
                    data = resp.get_json()
                    assert len(data['bars']) == 1
                    mock_yahoo.get_intraday_bars.assert_not_called()
        finally:
            if hasattr(app, '_desktop_get_bot_bars_5min'):
                del app._desktop_get_bot_bars_5min

    def test_empty_bot_bars_falls_to_yahoo(self):
        """Empty list from getter falls through to Yahoo fallback."""
        from dashboard.app import app

        app._desktop_get_bot_bars_5min = lambda: []
        try:
            with patch('dashboard.app.yahoo') as mock_yahoo:
                mock_yahoo.get_intraday_bars.return_value = [
                    {'timestamp': datetime(2026, 2, 20, 10, 0), 'open': 5800,
                     'high': 5810, 'low': 5795, 'close': 5805},
                ]
                with app.test_client() as c:
                    resp = c.get('/api/chart/bars5min')
                    data = resp.get_json()
                    assert len(data['bars']) == 1
                    mock_yahoo.get_intraday_bars.assert_called_once()
        finally:
            if hasattr(app, '_desktop_get_bot_bars_5min'):
                del app._desktop_get_bot_bars_5min

    def test_none_bot_bars_falls_to_yahoo(self):
        """None from getter (bot not running) falls through to Yahoo."""
        from dashboard.app import app

        app._desktop_get_bot_bars_5min = lambda: None
        try:
            with patch('dashboard.app.yahoo') as mock_yahoo:
                mock_yahoo.get_intraday_bars.return_value = [
                    {'timestamp': datetime(2026, 2, 20, 10, 0), 'open': 5800,
                     'high': 5810, 'low': 5795, 'close': 5805},
                ]
                with app.test_client() as c:
                    resp = c.get('/api/chart/bars5min')
                    data = resp.get_json()
                    assert len(data['bars']) == 1
        finally:
            if hasattr(app, '_desktop_get_bot_bars_5min'):
                del app._desktop_get_bot_bars_5min

    def test_30min_bot_bars_in_today(self):
        """Bot 30m bars appear in /api/today response."""
        from dashboard.app import app

        mock_bars = [
            {'time': '10:00', 'open': 5800, 'high': 5810, 'low': 5795, 'close': 5805},
        ]
        app._desktop_get_bot_bars = lambda: mock_bars
        try:
            with patch('dashboard.app.yahoo') as mock_yahoo, \
                 patch('dashboard.app.parse_todays_log') as mock_log, \
                 patch('dashboard.app.get_db_connection') as mock_db, \
                 patch('dashboard.app.classify_trades') as mock_ct, \
                 patch('dashboard.app.load_signals') as mock_sig:
                mock_yahoo.get_intraday_bars.return_value = []
                mock_yahoo.get_spx_quote.return_value = {'price': 5800, 'previous_close': 5790}
                mock_log.return_value = {'bars': [], 'pulses': [], 'building_bar': None}
                mock_sig.return_value = []
                mock_ct.return_value = ([], [])
                mock_db.return_value = MagicMock()

                with app.test_client() as c:
                    resp = c.get('/api/today')
                    data = resp.get_json()
                    assert data['bot_bars'] is not None
                    assert len(data['bot_bars']) == 1
                    assert data['bot_bars'][0]['time'] == '10:00'
        finally:
            if hasattr(app, '_desktop_get_bot_bars'):
                del app._desktop_get_bot_bars


class TestBuildingBarIncluded:
    """The in-progress (building) bar is included in bot bars."""

    def test_building_bar_in_5min_aggregator(self, agg):
        """Building bar shows in get_current_bar_info after first tick."""
        agg.add_price(_ts(10, 0), 5800.0)
        agg.add_price(_ts(10, 1), 5810.0)
        # No completed bars yet (boundary not crossed)
        assert len(agg.get_bars()) == 0
        # But there IS a building bar
        assert agg.current_bar_start is not None
        assert agg.open_price == 5800.0
        assert agg.high_price == 5810.0


class TestBackfill:
    """Backfill aggregator with historical bars."""

    def test_backfill_populates_completed_bars(self, agg):
        """Backfilling with valid bars populates completed_bars."""
        now = datetime.now(ET)
        bars = []
        for i in range(5):
            ts = ET.localize(datetime(now.year, now.month, now.day, 10, i * 5))
            # Only include bars far enough in the past to have fully elapsed
            bar_end = ts + timedelta(minutes=5)
            if bar_end > now:
                break
            bars.append(Bar(
                timestamp=ts,
                open=5800.0 + i,
                high=5810.0 + i,
                low=5790.0 + i,
                close=5805.0 + i,
                volume=100 + i,
            ))

        if not bars:
            pytest.skip("No fully-elapsed 5-min bars available at current time")

        count = agg.backfill(bars)
        assert count == len(bars)
        assert len(agg.get_bars()) == len(bars)
        # Bars are ordered oldest-first
        assert agg.get_bars()[0].open == bars[0].open
        assert agg.get_bars()[-1].close == bars[-1].close

    def test_backfill_skips_future_bars(self, agg):
        """Bars whose interval hasn't elapsed yet are excluded."""
        now = datetime.now(ET)
        # Bar far in the past (should be included)
        past_ts = ET.localize(datetime(now.year, now.month, now.day, 9, 30))
        # Bar in the future (should be excluded)
        future_ts = now + timedelta(hours=1)

        bars = [
            Bar(timestamp=past_ts, open=5800, high=5810, low=5790, close=5805, volume=100),
            Bar(timestamp=future_ts, open=5900, high=5910, low=5890, close=5905, volume=200),
        ]

        # Only the past bar's interval has elapsed
        past_end = past_ts + timedelta(minutes=5)
        if past_end > now:
            pytest.skip("Cannot test past bar at current time")

        count = agg.backfill(bars)
        assert count == 1
        assert len(agg.get_bars()) == 1
        assert agg.get_bars()[0].open == 5800

    def test_backfill_respects_max_bars(self, agg):
        """Backfilling more than MAX_BARS keeps only the newest."""
        # Use a fixed past date so all bars are fully elapsed
        with patch('src.core.bar_aggregator_5min.datetime') as mock_dt:
            mock_dt.now.return_value = ET.localize(datetime(2026, 2, 20, 15, 55))
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            bars = []
            for i in range(30):
                h = 9 + (30 + i * 5) // 60
                m = (30 + i * 5) % 60
                if h >= 16:
                    break
                ts = ET.localize(datetime(2026, 2, 20, h, m))
                bars.append(Bar(
                    timestamp=ts,
                    open=5800.0 + i,
                    high=5810.0 + i,
                    low=5790.0 + i,
                    close=5805.0 + i,
                    volume=100,
                ))

            count = agg.backfill(bars)
            assert count == len(bars)
            stored = agg.get_bars()
            assert len(stored) <= MAX_BARS
            # Should keep the newest bars
            assert stored[-1].open == bars[-1].open


class TestLabelOverlapResolution:
    """Overlapping chart labels at the same candle get offset."""

    def test_overlapping_labels_get_directional_offset(self):
        """When entry marker and price tag share x, later label is offset."""
        from dashboard.chart_utils import resolve_label_overlaps

        annotations = [
            # Paper-anchored watermark (should be ignored)
            {'x': 0.5, 'y': 0.5, 'xref': 'paper', 'yref': 'paper',
             'showarrow': False, 'ay': 0},
            # Open position entry at 10:00 (bearish)
            {'x': '10:00', 'y': 5800, 'xref': 'x', 'yref': 'y',
             'showarrow': True, 'ay': -22, '_direction': 'bearish'},
            # Closed trade entry also at 10:00 (bullish)
            {'x': '10:00', 'y': 5810, 'xref': 'x', 'yref': 'y',
             'showarrow': True, 'ay': -18, '_direction': 'bullish'},
            # Entry at different candle, on a pulse bar (bearish)
            {'x': '10:30', 'y': 5820, 'xref': 'x', 'yref': 'y',
             'showarrow': True, 'ay': -22, '_direction': 'bearish'},
            # Lone entry at 11:00, no collision, no pulse
            {'x': '11:00', 'y': 5830, 'xref': 'x', 'yref': 'y',
             'showarrow': True, 'ay': -22, '_direction': 'bullish'},
        ]

        resolve_label_overlaps(annotations, pulse_x_set={'10:30'})

        # Paper-anchored annotation untouched
        assert annotations[0]['ay'] == 0
        # First at 10:00: unchanged (keeps original position)
        assert annotations[1]['ay'] == -22
        # Second at 10:00: bullish -> -15px offset
        assert annotations[2]['ay'] == -18 + (-15)
        # Single at pulse bar 10:30: bearish -> +15px offset
        assert annotations[3]['ay'] == -22 + 15
        # No collision at 11:00: unchanged
        assert annotations[4]['ay'] == -22
