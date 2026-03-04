"""
FRED Economic Calendar Service

Fetches upcoming US economic release dates from the FRED API (Federal
Reserve Bank of St. Louis). Free API key, 120 req/min, JSON format.
Falls back to the static config/economic_calendar.json when no key is
configured or the API is unreachable.

Used by the dashboard Calendar tab.
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

# FRED release names -> (short_code, impact, typical_time_ET, description)
_RELEASE_MAP = {
    'consumer price index': ('CPI', 'high', '08:30', 'CPI (Consumer Price Index)'),
    'employment situation': ('NFP', 'high', '08:30', 'Nonfarm Payrolls'),
    'gross domestic product': ('GDP', 'high', '08:30', 'GDP (Gross Domestic Product)'),
    'personal income and outlays': ('PCE', 'high', '08:30', 'PCE Price Index'),
    'personal consumption expenditures': ('PCE', 'high', '08:30', 'PCE Price Index'),
    # FOMC is NOT matched here.  It's a policy meeting, not a statistical
    # release, so FRED's releases/dates endpoint returns misleading daily
    # entries for related Fed publications.  FOMC dates come from the static
    # calendar instead (merged in _merge_fomc_from_static).
}

# Mapping of verbose names to short badge codes (used by _event_short_code)
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
    'personal income and outlays': 'PCE',
    'employment situation': 'NFP',
}


def _event_short_code(event_name: str) -> str:
    """Map verbose event name to a short badge code.

    Returns the short code (FOMC, CPI, NFP, GDP, PCE) if matched,
    otherwise returns the original event name truncated to 20 chars.
    """
    lower = event_name.lower().strip()
    for key, code in _SHORT_CODES.items():
        if key in lower:
            return code
    return event_name[:20] if len(event_name) > 20 else event_name


def _match_release(release_name: str) -> tuple | None:
    """Match a FRED release name to our tracked events.

    Returns (short_code, impact, time, description) or None.
    """
    lower = release_name.lower().strip()
    for key, info in _RELEASE_MAP.items():
        if key in lower:
            return info
    return None


def _standardize_fred_release(release: dict) -> dict | None:
    """Convert a FRED release_dates entry to our standard event format.

    FRED format: {release_id, release_name, date}
    Our format: {date, time, event, event_full, impact, description, actual, forecast, previous, unit}

    Returns None if the release doesn't match any tracked event type.
    """
    match = _match_release(release.get('release_name', ''))
    if match is None:
        return None

    short_code, impact, time_et, description = match
    return {
        'date': release.get('date', ''),
        'time': time_et,
        'event': short_code,
        'event_full': release.get('release_name', ''),
        'impact': impact,
        'description': description,
        'actual': '',
        'forecast': '',
        'previous': '',
        'unit': '',
    }


def _fetch_fred(api_key: str, from_date: str, to_date: str) -> List[dict]:
    """Fetch upcoming economic release dates from the FRED API.

    Uses the fred/releases/dates endpoint which returns all scheduled
    release dates across all data sources. We filter to our 5 tracked
    event types (FOMC, CPI, NFP, GDP, PCE).

    Args:
        api_key: FRED API key (free at fred.stlouisfed.org)
        from_date: Start date (YYYY-MM-DD)
        to_date: End date (YYYY-MM-DD)

    Returns:
        List of standardized event dicts for tracked releases.
    """
    url = 'https://api.stlouisfed.org/fred/releases/dates'
    params = {
        'api_key': api_key,
        'file_type': 'json',
        'realtime_start': from_date,
        'realtime_end': to_date,
        'include_release_dates_with_no_data': 'true',
        'limit': 1000,
        'order_by': 'release_date',
        'sort_order': 'asc',
    }

    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()

    data = resp.json()
    raw_releases = data.get('release_dates', [])

    # Filter and standardize to our tracked event types
    events = []
    seen = set()  # Deduplicate (same event+date)
    for r in raw_releases:
        ev = _standardize_fred_release(r)
        if ev is not None:
            key = (ev['event'], ev['date'])
            if key not in seen:
                seen.add(key)
                events.append(ev)

    events.sort(key=lambda e: (e.get('date', ''), e.get('time', '')))

    # FOMC dates don't live in FRED releases/dates - merge from static calendar
    events = _merge_fomc_from_static(events, from_date, to_date)

    return events


def _merge_fomc_from_static(
    events: List[dict], from_date: str, to_date: str,
) -> List[dict]:
    """Merge FOMC meeting dates from the static calendar.

    FOMC is a policy meeting, not a FRED statistical release.  The static
    economic_calendar.json has the correct FOMC schedule, so we pull those
    dates and splice them into the FRED results.
    """
    try:
        from src.data.economic_calendar import load_calendar
        static_events = load_calendar()
        existing_fomc_dates = {e['date'] for e in events if e.get('event') == 'FOMC'}
        for se in static_events:
            if (se.get('event') == 'FOMC'
                    and from_date <= se.get('date', '') <= to_date
                    and se.get('date') not in existing_fomc_dates):
                events.append({
                    'date': se.get('date', ''),
                    'time': se.get('time', '14:00'),
                    'event': 'FOMC',
                    'event_full': se.get('description', 'FOMC Rate Decision'),
                    'impact': 'high',
                    'description': se.get('description', 'FOMC Rate Decision'),
                    'actual': '',
                    'forecast': '',
                    'previous': '',
                    'unit': '',
                })
        events.sort(key=lambda e: (e.get('date', ''), e.get('time', '')))
    except Exception as e:
        logger.debug("Could not merge FOMC from static calendar: %s", e)
    return events


def get_calendar_events(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    force_refresh: bool = False,
) -> dict:
    """Main entry point: get economic calendar events with caching and fallback.

    Returns:
        {events: [...], source: "fred"|"static", last_updated: float}
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
        logger.debug("Calendar fetch: source=%s (cached, age=%.0fs)", _cache['source'], now - _cache_ts)
        return {
            'events': _cache['events'],
            'source': _cache['source'],
            'last_updated': _cache_ts,
        }

    # Try FRED API
    try:
        from config.settings import get_fred_api_key
        api_key = get_fred_api_key()
    except Exception:
        api_key = None

    if api_key:
        try:
            events = _fetch_fred(api_key, from_date, to_date)
            if events:
                _cache = {
                    'events': events,
                    'source': 'fred',
                    'from_date': from_date,
                    'to_date': to_date,
                }
                _cache_ts = now
                logger.debug("Calendar fetch: source=fred (live, %d events)", len(events))
                return {
                    'events': events,
                    'source': 'fred',
                    'last_updated': _cache_ts,
                }
            else:
                logger.info("FRED returned no matching releases, falling back to static calendar")
        except Exception as e:
            logger.warning("FRED API request failed, falling back to static: %s", e)
    else:
        logger.debug("Calendar fetch: no FRED API key configured, using static")

    # Fallback to static calendar
    result = _fallback_static(from_date, to_date)
    logger.debug("Calendar fetch: source=static (%d events)", len(result.get('events', [])))
    return result


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
        logger.warning("Static calendar fallback also failed: %s", e)
        return {'events': [], 'source': 'static', 'last_updated': 0}


def invalidate_cache():
    """Clear the cached calendar data (useful for testing)."""
    global _cache, _cache_ts
    _cache = None
    _cache_ts = 0.0
