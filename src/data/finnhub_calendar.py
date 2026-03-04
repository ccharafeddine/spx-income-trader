"""
Finnhub Economic Calendar Service

Fetches economic calendar data from Finnhub's free API with caching and
fallback to the static config/economic_calendar.json file. Used by the
dashboard Calendar tab for always-current economic event data.
"""

import logging
import time
from datetime import date, datetime, timedelta
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

# Cache: {events, source, last_updated, from_date, to_date}
_cache: Optional[dict] = None
_cache_ts: float = 0.0
_CACHE_TTL = 4 * 3600  # 4 hours

# Mapping of verbose Finnhub event names to short badge codes
_SHORT_CODES = {
    'fomc': 'FOMC',
    'federal funds rate': 'FOMC',
    'fed interest rate decision': 'FOMC',
    'fed funds rate': 'FOMC',
    'cpi': 'CPI',
    'consumer price index': 'CPI',
    'core cpi': 'CPI',
    'nfp': 'NFP',
    'nonfarm payrolls': 'NFP',
    'non-farm payrolls': 'NFP',
    'non farm payrolls': 'NFP',
    'gdp': 'GDP',
    'gross domestic product': 'GDP',
    'gdp growth rate': 'GDP',
    'pce': 'PCE',
    'pce price index': 'PCE',
    'core pce price index': 'PCE',
    'personal consumption expenditures': 'PCE',
}


def _event_short_code(event_name: str) -> str:
    """Map verbose Finnhub event name to a short badge code.

    Returns the short code (FOMC, CPI, NFP, GDP, PCE) if matched,
    otherwise returns the original event name truncated to 20 chars.
    """
    lower = event_name.lower().strip()
    for key, code in _SHORT_CODES.items():
        if key in lower:
            return code
    return event_name[:20] if len(event_name) > 20 else event_name


def _standardize_finnhub_event(ev: dict) -> dict:
    """Convert a Finnhub API event dict to our standard format.

    Finnhub format: {country, date, event, impact, prev, estimate, actual, unit, time}
    Our format: {date, time, event, event_full, impact, description, actual, forecast, previous, unit}
    """
    # Impact: Finnhub uses "high"/"medium"/"low" strings or numeric 1/2/3
    impact_raw = ev.get('impact', 'medium')
    if isinstance(impact_raw, (int, float)):
        impact = {1: 'low', 2: 'medium', 3: 'high'}.get(int(impact_raw), 'medium')
    else:
        impact = str(impact_raw).lower() if impact_raw else 'medium'

    event_name = ev.get('event', '')
    return {
        'date': ev.get('date', ''),
        'time': ev.get('time', '') or '',
        'event': _event_short_code(event_name),
        'event_full': event_name,
        'impact': impact,
        'description': event_name,
        'actual': ev.get('actual', ''),
        'forecast': ev.get('estimate', ''),
        'previous': ev.get('prev', ''),
        'unit': ev.get('unit', ''),
    }


def _fetch_finnhub(api_key: str, from_date: str, to_date: str) -> List[dict]:
    """Fetch economic calendar events from Finnhub API.

    Args:
        api_key: Finnhub API key
        from_date: Start date (YYYY-MM-DD)
        to_date: End date (YYYY-MM-DD)

    Returns:
        List of standardized event dicts, filtered to US events only.
    """
    url = 'https://finnhub.io/api/v1/calendar/economic'
    params = {
        'from': from_date,
        'to': to_date,
        'token': api_key,
    }

    resp = requests.get(url, params=params, timeout=5)
    resp.raise_for_status()

    data = resp.json()
    raw_events = data.get('economicCalendar', [])

    # Filter to US events only
    us_events = [e for e in raw_events if e.get('country', '').upper() == 'US']

    standardized = [_standardize_finnhub_event(e) for e in us_events]
    standardized.sort(key=lambda e: (e.get('date', ''), e.get('time', '')))
    return standardized


def get_calendar_events(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    force_refresh: bool = False,
) -> dict:
    """Main entry point: get economic calendar events with caching and fallback.

    Returns:
        {events: [...], source: "finnhub"|"static", last_updated: float}
    """
    global _cache, _cache_ts

    now = time.monotonic()

    # Default date range: today to 30 days out
    if from_date is None:
        from_date = date.today().strftime('%Y-%m-%d')
    if to_date is None:
        to_date = (date.today() + timedelta(days=30)).strftime('%Y-%m-%d')

    # Return cache if fresh and covers the requested range
    if (not force_refresh
            and _cache is not None
            and (now - _cache_ts) < _CACHE_TTL
            and _cache.get('from_date') == from_date
            and _cache.get('to_date') == to_date):
        return {
            'events': _cache['events'],
            'source': _cache['source'],
            'last_updated': _cache_ts,
        }

    # Try Finnhub API
    try:
        from config.settings import get_finnhub_api_key
        api_key = get_finnhub_api_key()
    except Exception:
        api_key = None

    if api_key:
        try:
            events = _fetch_finnhub(api_key, from_date, to_date)
            if events:
                _cache = {
                    'events': events,
                    'source': 'finnhub',
                    'from_date': from_date,
                    'to_date': to_date,
                }
                _cache_ts = now
                return {
                    'events': events,
                    'source': 'finnhub',
                    'last_updated': _cache_ts,
                }
            else:
                logger.info("Finnhub returned no US events, falling back to static calendar")
        except Exception as e:
            logger.warning(f"Finnhub API request failed, falling back to static: {e}")

    # Fallback to static calendar
    return _fallback_static(from_date, to_date)


def _fallback_static(from_date: str, to_date: str) -> dict:
    """Load events from the static economic_calendar.json as fallback."""
    global _cache, _cache_ts

    try:
        from src.data.economic_calendar import get_upcoming_events
        start = datetime.strptime(from_date, '%Y-%m-%d').date()
        end = datetime.strptime(to_date, '%Y-%m-%d').date()
        days = (end - start).days + 1
        events = get_upcoming_events(days=days, from_date=start)

        # Normalize static events to match our enriched format
        normalized = []
        for e in events:
            normalized.append({
                'date': e.get('date', ''),
                'time': e.get('time', ''),
                'event': e.get('event', ''),
                'event_full': e.get('description', e.get('event', '')),
                'impact': e.get('impact', 'medium'),
                'description': e.get('description', ''),
                'actual': '',
                'forecast': '',
                'previous': '',
                'unit': '',
            })

        _cache = {
            'events': normalized,
            'source': 'static',
            'from_date': from_date,
            'to_date': to_date,
        }
        _cache_ts = time.monotonic()
        return {
            'events': normalized,
            'source': 'static',
            'last_updated': _cache_ts,
        }
    except Exception as e:
        logger.warning(f"Static calendar fallback also failed: {e}")
        return {'events': [], 'source': 'static', 'last_updated': 0}


def invalidate_cache():
    """Clear the cached calendar data (useful for testing)."""
    global _cache, _cache_ts
    _cache = None
    _cache_ts = 0.0
