"""
Tests for the Finnhub Calendar Service module.

Tests cover:
- Event standardization (field mapping, impact conversion, missing fields)
- Short code mapping (FOMC/CPI/NFP/GDP/PCE detection, unknown passthrough)
- Caching behavior (cache hit, force_refresh bypass)
- Fallback logic (no API key, empty response, API error -> static)
- Backward compatibility (static calendar functions still work)
- Date range filtering
- /api/calendar endpoint structure
"""

import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.data.finnhub_calendar import (
    _event_short_code,
    _standardize_finnhub_event,
    get_calendar_events,
    invalidate_cache,
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
def finnhub_event():
    """A sample Finnhub API event dict."""
    return {
        'country': 'US',
        'date': '2026-03-05',
        'event': 'Non-Farm Payrolls',
        'impact': 'high',
        'prev': 256.0,
        'estimate': 170.0,
        'actual': 185.0,
        'unit': 'K',
        'time': '08:30',
    }


@pytest.fixture
def finnhub_response():
    """A sample Finnhub API full response."""
    return {
        'economicCalendar': [
            {
                'country': 'US',
                'date': '2026-03-05',
                'event': 'Non-Farm Payrolls',
                'impact': 'high',
                'prev': 256.0,
                'estimate': 170.0,
                'actual': '',
                'unit': 'K',
                'time': '08:30',
            },
            {
                'country': 'US',
                'date': '2026-03-05',
                'event': 'CPI MoM',
                'impact': 'high',
                'prev': 0.4,
                'estimate': 0.3,
                'actual': '',
                'unit': '%',
                'time': '08:30',
            },
            {
                'country': 'DE',
                'date': '2026-03-05',
                'event': 'German CPI',
                'impact': 'high',
                'prev': 0.2,
                'estimate': 0.3,
                'actual': '',
                'unit': '%',
                'time': '07:00',
            },
        ]
    }


@pytest.fixture
def sample_calendar(tmp_path):
    """Create a temporary static calendar JSON file."""
    data = {
        "events": [
            {"date": "2026-03-05", "time": "08:30", "event": "NFP", "impact": "high", "description": "Nonfarm Payrolls"},
            {"date": "2026-03-12", "time": "14:00", "event": "FOMC", "impact": "high", "description": "FOMC Rate Decision"},
        ]
    }
    cal_file = tmp_path / "test_calendar.json"
    cal_file.write_text(json.dumps(data))
    return cal_file


# ============================================================================
# Event Standardization
# ============================================================================

class TestStandardizeEvent:
    """Tests for _standardize_finnhub_event() function."""

    def test_field_mapping(self, finnhub_event):
        result = _standardize_finnhub_event(finnhub_event)
        assert result['date'] == '2026-03-05'
        assert result['time'] == '08:30'
        assert result['event'] == 'NFP'  # short code
        assert result['event_full'] == 'Non-Farm Payrolls'
        assert result['impact'] == 'high'
        assert result['description'] == 'Non-Farm Payrolls'
        assert result['actual'] == 185.0
        assert result['forecast'] == 170.0
        assert result['previous'] == 256.0
        assert result['unit'] == 'K'

    def test_numeric_impact_conversion(self):
        ev = {'event': 'Test', 'impact': 3}
        result = _standardize_finnhub_event(ev)
        assert result['impact'] == 'high'

        ev['impact'] = 2
        result = _standardize_finnhub_event(ev)
        assert result['impact'] == 'medium'

        ev['impact'] = 1
        result = _standardize_finnhub_event(ev)
        assert result['impact'] == 'low'

    def test_missing_time(self):
        ev = {'event': 'Test', 'impact': 'high'}
        result = _standardize_finnhub_event(ev)
        assert result['time'] == ''

    def test_missing_data_fields(self):
        ev = {'event': 'Test', 'impact': 'low'}
        result = _standardize_finnhub_event(ev)
        assert result['actual'] == ''
        assert result['forecast'] == ''
        assert result['previous'] == ''
        assert result['unit'] == ''

    def test_none_impact_defaults_to_medium(self):
        ev = {'event': 'Test', 'impact': None}
        result = _standardize_finnhub_event(ev)
        assert result['impact'] == 'medium'


# ============================================================================
# Short Code Mapping
# ============================================================================

class TestEventShortCode:
    """Tests for _event_short_code() function."""

    def test_fomc_detection(self):
        assert _event_short_code('FOMC Rate Decision') == 'FOMC'
        assert _event_short_code('Fed Interest Rate Decision') == 'FOMC'
        assert _event_short_code('Federal Funds Rate') == 'FOMC'

    def test_cpi_detection(self):
        assert _event_short_code('CPI MoM') == 'CPI'
        assert _event_short_code('Consumer Price Index') == 'CPI'
        assert _event_short_code('Core CPI YoY') == 'CPI'

    def test_nfp_detection(self):
        assert _event_short_code('Non-Farm Payrolls') == 'NFP'
        assert _event_short_code('Nonfarm Payrolls') == 'NFP'

    def test_gdp_detection(self):
        assert _event_short_code('GDP Growth Rate QoQ') == 'GDP'
        assert _event_short_code('Gross Domestic Product') == 'GDP'

    def test_pce_detection(self):
        assert _event_short_code('PCE Price Index MoM') == 'PCE'
        assert _event_short_code('Core PCE Price Index') == 'PCE'
        assert _event_short_code('Personal Consumption Expenditures') == 'PCE'

    def test_unknown_event_passthrough(self):
        assert _event_short_code('Retail Sales') == 'Retail Sales'

    def test_long_unknown_truncated(self):
        long_name = 'Very Long Economic Indicator Name That Exceeds Twenty Characters'
        result = _event_short_code(long_name)
        assert len(result) == 20


# ============================================================================
# Caching
# ============================================================================

class TestCaching:
    """Tests for cache behavior in get_calendar_events()."""

    @patch('src.data.finnhub_calendar._fetch_finnhub')
    @patch('config.settings.get_finnhub_api_key', return_value='test-key')
    def test_cache_prevents_duplicate_calls(self, mock_key, mock_fetch):
        mock_fetch.return_value = [{'date': '2026-03-05', 'event': 'NFP', 'impact': 'high'}]

        from_d = date.today().strftime('%Y-%m-%d')
        to_d = (date.today() + timedelta(days=30)).strftime('%Y-%m-%d')

        result1 = get_calendar_events(from_date=from_d, to_date=to_d)
        result2 = get_calendar_events(from_date=from_d, to_date=to_d)

        assert mock_fetch.call_count == 1
        assert result1['source'] == result2['source']

    @patch('src.data.finnhub_calendar._fetch_finnhub')
    @patch('config.settings.get_finnhub_api_key', return_value='test-key')
    def test_force_refresh_bypasses_cache(self, mock_key, mock_fetch):
        mock_fetch.return_value = [{'date': '2026-03-05', 'event': 'NFP', 'impact': 'high'}]

        from_d = date.today().strftime('%Y-%m-%d')
        to_d = (date.today() + timedelta(days=30)).strftime('%Y-%m-%d')

        get_calendar_events(from_date=from_d, to_date=to_d)
        get_calendar_events(from_date=from_d, to_date=to_d, force_refresh=True)

        assert mock_fetch.call_count == 2


# ============================================================================
# Fallback
# ============================================================================

class TestFallback:
    """Tests for fallback to static calendar."""

    @patch('config.settings.get_finnhub_api_key', return_value=None)
    def test_no_api_key_falls_back_to_static(self, mock_key):
        result = get_calendar_events()
        assert result['source'] == 'static'

    @patch('src.data.finnhub_calendar._fetch_finnhub', return_value=[])
    @patch('config.settings.get_finnhub_api_key', return_value='test-key')
    def test_empty_response_falls_back_to_static(self, mock_key, mock_fetch):
        result = get_calendar_events()
        assert result['source'] == 'static'

    @patch('src.data.finnhub_calendar._fetch_finnhub', side_effect=Exception('API error'))
    @patch('config.settings.get_finnhub_api_key', return_value='test-key')
    def test_api_error_falls_back_to_static(self, mock_key, mock_fetch):
        result = get_calendar_events()
        assert result['source'] == 'static'


# ============================================================================
# Backward Compatibility
# ============================================================================

class TestBackwardCompatibility:
    """Verify the existing static calendar module still works."""

    def test_load_calendar_still_works(self):
        from src.data.economic_calendar import load_calendar
        events = load_calendar()
        assert isinstance(events, list)

    def test_get_today_events_still_works(self):
        from src.data.economic_calendar import get_today_events
        events = get_today_events()
        assert isinstance(events, list)

    def test_is_high_impact_day_still_works(self):
        from src.data.economic_calendar import is_high_impact_day
        result = is_high_impact_day()
        assert isinstance(result, bool)


# ============================================================================
# Date Range Filtering
# ============================================================================

class TestFilterByDateRange:
    """Tests for date boundary filtering in Finnhub fetch."""

    @patch('src.data.finnhub_calendar._fetch_finnhub')
    @patch('config.settings.get_finnhub_api_key', return_value='test-key')
    def test_passes_date_range_to_fetch(self, mock_key, mock_fetch):
        mock_fetch.return_value = [{'date': '2026-03-10', 'event': 'Test', 'impact': 'low'}]

        get_calendar_events(from_date='2026-03-01', to_date='2026-03-31')

        mock_fetch.assert_called_once_with('test-key', '2026-03-01', '2026-03-31')

    @patch('src.data.finnhub_calendar._fetch_finnhub')
    @patch('config.settings.get_finnhub_api_key', return_value='test-key')
    def test_different_range_bypasses_cache(self, mock_key, mock_fetch):
        mock_fetch.return_value = [{'date': '2026-03-10', 'event': 'Test', 'impact': 'low'}]

        get_calendar_events(from_date='2026-03-01', to_date='2026-03-15')
        get_calendar_events(from_date='2026-03-16', to_date='2026-03-31')

        assert mock_fetch.call_count == 2


# ============================================================================
# API Endpoint
# ============================================================================

class TestApiEndpoint:
    """Tests for the /api/calendar Flask endpoint."""

    def test_endpoint_returns_correct_structure(self):
        """The /api/calendar endpoint should return today/week/upcoming/source/last_updated."""
        sys.path.insert(0, str(Path(__file__).parent.parent / 'dashboard'))
        from dashboard.app import app as flask_app

        with flask_app.test_client() as client:
            resp = client.get('/api/calendar')
            assert resp.status_code == 200
            data = resp.get_json()
            assert 'today' in data
            assert 'week' in data
            assert 'upcoming' in data
            assert 'source' in data
            assert 'last_updated' in data
            assert isinstance(data['today'], list)
            assert isinstance(data['week'], list)
            assert isinstance(data['upcoming'], list)
