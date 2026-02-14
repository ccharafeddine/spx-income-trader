"""
Economic Calendar for SPX Options Trading

Loads a static JSON calendar of high-impact US economic events (FOMC, CPI,
NFP, GDP, PCE) and provides date-based lookup functions. Used to tag trades
with same-day market events for journal analysis.
"""

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

_calendar_cache: Optional[List[dict]] = None
_calendar_path: Optional[Path] = None


def _default_calendar_path() -> Path:
    """Resolve the default path to economic_calendar.json."""
    # src/data/economic_calendar.py -> parent.parent.parent = project root
    return Path(__file__).resolve().parent.parent.parent / 'config' / 'economic_calendar.json'


def load_calendar(path: Optional[Path] = None) -> List[dict]:
    """Load economic calendar events from JSON file.

    Results are cached after first load. Pass a path to override
    the default config/economic_calendar.json location.

    Returns:
        List of event dicts with keys: date, time, event, impact, description
    """
    global _calendar_cache, _calendar_path

    # If cache exists and caller didn't request a specific path, return cache
    if _calendar_cache is not None and path is None:
        return _calendar_cache

    resolve_path = path or _default_calendar_path()

    # Return cache if same path was already loaded
    if _calendar_cache is not None and _calendar_path == resolve_path:
        return _calendar_cache

    if not resolve_path.exists():
        logger.warning(f"Economic calendar not found at {resolve_path}")
        _calendar_cache = []
        _calendar_path = resolve_path
        return _calendar_cache

    try:
        with open(resolve_path, 'r') as f:
            data = json.load(f)
        _calendar_cache = data.get('events', [])
        _calendar_path = resolve_path
        logger.info(f"Loaded {len(_calendar_cache)} economic calendar events")
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning(f"Failed to parse economic calendar: {e}")
        _calendar_cache = []
        _calendar_path = resolve_path

    return _calendar_cache


def invalidate_cache():
    """Clear the cached calendar (useful for testing)."""
    global _calendar_cache, _calendar_path
    _calendar_cache = None
    _calendar_path = None


def get_events_for_date(target_date: date) -> List[dict]:
    """Get all economic events for a specific date.

    Args:
        target_date: The date to look up (date object or string 'YYYY-MM-DD')

    Returns:
        List of event dicts for that date, empty list if none.
    """
    if isinstance(target_date, str):
        target_date = datetime.strptime(target_date, '%Y-%m-%d').date()

    date_str = target_date.strftime('%Y-%m-%d')
    events = load_calendar()
    return [e for e in events if e.get('date') == date_str]


def get_today_events(today: Optional[date] = None) -> List[dict]:
    """Get economic events for today (or specified date).

    Args:
        today: Override date for testing. Defaults to current ET date.

    Returns:
        List of event dicts for today.
    """
    if today is None:
        try:
            import pytz
            et = pytz.timezone('America/New_York')
            today = datetime.now(et).date()
        except Exception:
            today = date.today()

    return get_events_for_date(today)


def is_high_impact_day(target_date: Optional[date] = None) -> bool:
    """Check if the given date has any high-impact economic events.

    Args:
        target_date: Date to check. Defaults to today (ET).

    Returns:
        True if any high-impact events are scheduled.
    """
    if target_date is None:
        try:
            import pytz
            et = pytz.timezone('America/New_York')
            target_date = datetime.now(et).date()
        except Exception:
            target_date = date.today()

    events = get_events_for_date(target_date)
    return any(e.get('impact') == 'high' for e in events)


def get_upcoming_events(days: int = 7, from_date: Optional[date] = None) -> List[dict]:
    """Get economic events in the next N days (inclusive of from_date).

    Args:
        days: Number of days to look ahead (default 7).
        from_date: Start date. Defaults to today (ET).

    Returns:
        List of event dicts sorted by date, then time.
    """
    if from_date is None:
        try:
            import pytz
            et = pytz.timezone('America/New_York')
            from_date = datetime.now(et).date()
        except Exception:
            from_date = date.today()

    end_date = from_date + timedelta(days=days - 1)
    events = load_calendar()

    upcoming = []
    for e in events:
        try:
            event_date = datetime.strptime(e['date'], '%Y-%m-%d').date()
        except (ValueError, KeyError):
            continue
        if from_date <= event_date <= end_date:
            upcoming.append(e)

    upcoming.sort(key=lambda e: (e.get('date', ''), e.get('time', '')))
    return upcoming


def format_events_short(events: List[dict]) -> str:
    """Format a list of events into a compact string for logging/display.

    Example: "CPI 08:30, FOMC 14:00"
    """
    if not events:
        return ""
    parts = [f"{e['event']} {e.get('time', '')}" for e in events]
    return ", ".join(parts)
