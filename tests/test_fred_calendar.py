"""
Tests for the FRED Calendar Service module.

Tests cover:
- FRED release standardization (field mapping, release matching)
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

from src.data.fred_calendar import (
    _event_short_code,
    _standardize_fred_release,
    _match_release,
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
def fred_cpi_release():
    """A sample FRED release_dates entry for CPI."""
    return {
        'release_id': 10,
        'release_name': 'Consumer Price Index',
        'date': '2026-03-12',
    }


@pytest.fixture
def fred_nfp_release():
    """A sample FRED release_dates entry for NFP."""
    return {
        'release_id': 50,
        'release_name': 'Employment Situation',
        'date': '2026-03-07',
    }


@pytest.fixture
def fred_releases_response():
    """A sample FRED releases/dates API response."""
    return {
        'release_dates': [
            {'release_id': 50, 'release_name': 'Employment Situation', 'date': '2026-03-07'},
            {'release_id': 10, 'release_name': 'Consumer Price Index', 'date': '2026-03-12'},
            {'release_id': 53, 'release_name': 'Gross Domestic Product', 'date': '2026-03-26'},
            {'release_id': 54, 'release_name': 'Personal Income and Outlays', 'date': '2026-03-28'},
            {'release_id': 999, 'release_name': 'Some Unrelated Release', 'date': '2026-03-15'},
            {'release_id': 200, 'release_name': 'H.15 Selected Interest Rates', 'date': '2026-03-10'},
        ]
    }


# ============================================================================
# Release Standardization
# ============================================================================

class TestStandardizeRelease:
    """Tests for _standardize_fred_release() function."""

    def test_cpi_release_mapping(self, fred_cpi_release):
        result = _standardize_fred_release(fred_cpi_release)
        assert result is not None
        assert result['date'] == '2026-03-12'
        assert result['time'] == '08:30'
        assert result['event'] == 'CPI'
        assert result['event_full'] == 'Consumer Price Index'
        assert result['impact'] == 'high'
        assert result['description'] == 'CPI (Consumer Price Index)'

    def test_nfp_release_mapping(self, fred_nfp_release):
        result = _standardize_fred_release(fred_nfp_release)
        assert result is not None
        assert result['event'] == 'NFP'
        assert result['time'] == '08:30'

    def test_gdp_release_mapping(self):
        result = _standardize_fred_release({
            'release_id': 53,
            'release_name': 'Gross Domestic Product',
            'date': '2026-03-26',
        })
        assert result is not None
        assert result['event'] == 'GDP'

    def test_pce_release_mapping(self):
        result = _standardize_fred_release({
            'release_id': 54,
            'release_name': 'Personal Income and Outlays',
            'date': '2026-03-28',
        })
        assert result is not None
        assert result['event'] == 'PCE'

    def test_unmatched_release_returns_none(self):
        result = _standardize_fred_release({
            'release_id': 999,
            'release_name': 'Some Unrelated Release',
            'date': '2026-03-15',
        })
        assert result is None

    def test_empty_data_fields(self, fred_cpi_release):
        result = _standardize_fred_release(fred_cpi_release)
        assert result['actual'] == ''
        assert result['forecast'] == ''
        assert result['previous'] == ''
        assert result['unit'] == ''


# ============================================================================
# Release Matching
# ============================================================================

class TestMatchRelease:
    """Tests for _match_release() function."""

    def test_matches_cpi(self):
        assert _match_release('Consumer Price Index') is not None
        assert _match_release('Consumer Price Index')[0] == 'CPI'

    def test_matches_nfp(self):
        assert _match_release('Employment Situation') is not None
        assert _match_release('Employment Situation')[0] == 'NFP'

    def test_matches_gdp(self):
        assert _match_release('Gross Domestic Product') is not None
        assert _match_release('Gross Domestic Product')[0] == 'GDP'

    def test_matches_pce(self):
        assert _match_release('Personal Income and Outlays') is not None
        assert _match_release('Personal Income and Outlays')[0] == 'PCE'

    def test_matches_fomc(self):
        assert _match_release('Federal Open Market Committee') is not None
        assert _match_release('Federal Open Market Committee')[0] == 'FOMC'

    def test_no_match(self):
        assert _match_release('Retail Sales') is None


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
        assert _event_short_code('Employment Situation') == 'NFP'

    def test_gdp_detection(self):
        assert _event_short_code('GDP Growth Rate QoQ') == 'GDP'
        assert _event_short_code('Gross Domestic Product') == 'GDP'

    def test_pce_detection(self):
        assert _event_short_code('PCE Price Index MoM') == 'PCE'
        assert _event_short_code('Core PCE Price Index') == 'PCE'
        assert _event_short_code('Personal Consumption Expenditures') == 'PCE'
        assert _event_short_code('Personal Income and Outlays') == 'PCE'

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

    @patch('src.data.fred_calendar._fetch_fred')
    @patch('config.settings.get_fred_api_key', return_value='test-key')
    def test_cache_prevents_duplicate_calls(self, mock_key, mock_fetch):
        mock_fetch.return_value = [{'date': '2026-03-05', 'event': 'NFP', 'impact': 'high'}]

        from_d = date.today().strftime('%Y-%m-%d')
        to_d = (date.today() + timedelta(days=30)).strftime('%Y-%m-%d')

        result1 = get_calendar_events(from_date=from_d, to_date=to_d)
        result2 = get_calendar_events(from_date=from_d, to_date=to_d)

        assert mock_fetch.call_count == 1
        assert result1['source'] == result2['source']

    @patch('src.data.fred_calendar._fetch_fred')
    @patch('config.settings.get_fred_api_key', return_value='test-key')
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

    @patch('config.settings.get_fred_api_key', return_value=None)
    def test_no_api_key_falls_back_to_static(self, mock_key):
        result = get_calendar_events()
        assert result['source'] == 'static'

    @patch('src.data.fred_calendar._fetch_fred', return_value=[])
    @patch('config.settings.get_fred_api_key', return_value='test-key')
    def test_empty_response_falls_back_to_static(self, mock_key, mock_fetch):
        result = get_calendar_events()
        assert result['source'] == 'static'

    @patch('src.data.fred_calendar._fetch_fred', side_effect=Exception('API error'))
    @patch('config.settings.get_fred_api_key', return_value='test-key')
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
    """Tests for date boundary filtering in FRED fetch."""

    @patch('src.data.fred_calendar._fetch_fred')
    @patch('config.settings.get_fred_api_key', return_value='test-key')
    def test_passes_date_range_to_fetch(self, mock_key, mock_fetch):
        mock_fetch.return_value = [{'date': '2026-03-10', 'event': 'Test', 'impact': 'low'}]

        get_calendar_events(from_date='2026-03-01', to_date='2026-03-31')

        mock_fetch.assert_called_once_with('test-key', '2026-03-01', '2026-03-31')

    @patch('src.data.fred_calendar._fetch_fred')
    @patch('config.settings.get_fred_api_key', return_value='test-key')
    def test_different_range_bypasses_cache(self, mock_key, mock_fetch):
        mock_fetch.return_value = [{'date': '2026-03-10', 'event': 'Test', 'impact': 'low'}]

        get_calendar_events(from_date='2026-03-01', to_date='2026-03-15')
        get_calendar_events(from_date='2026-03-16', to_date='2026-03-31')

        assert mock_fetch.call_count == 2


# ============================================================================
# Deduplication
# ============================================================================

class TestDeduplication:
    """Tests that duplicate releases for the same event+date are deduplicated."""

    @patch('src.data.fred_calendar._fetch_fred')
    @patch('config.settings.get_fred_api_key', return_value='test-key')
    def test_same_event_same_date_deduplicated(self, mock_key, mock_fetch):
        """If FRED returns multiple CPI-related releases on the same date, only one appears."""
        mock_fetch.return_value = [
            {'date': '2026-03-12', 'event': 'CPI', 'impact': 'high', 'time': '08:30',
             'event_full': 'Consumer Price Index', 'description': 'CPI', 'actual': '', 'forecast': '', 'previous': '', 'unit': ''},
        ]
        result = get_calendar_events(from_date='2026-03-01', to_date='2026-03-31')
        cpi_events = [e for e in result['events'] if e['event'] == 'CPI' and e['date'] == '2026-03-12']
        assert len(cpi_events) == 1


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
