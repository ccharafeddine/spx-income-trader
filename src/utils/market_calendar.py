"""NYSE market calendar helpers.

Provides early close day detection and market close time lookup.
"""

from datetime import date, time, datetime


# NYSE early close days (market closes at 1:00 PM ET).
# Includes day before Independence Day, Black Friday, and Christmas Eve.
EARLY_CLOSE_DATES = {
    date(2025, 7, 3),    # Day before Independence Day
    date(2025, 11, 28),  # Black Friday
    date(2025, 12, 24),  # Christmas Eve
    date(2026, 7, 2),    # Day before Independence Day (observed)
    date(2026, 11, 27),  # Black Friday
    date(2026, 12, 24),  # Christmas Eve
    date(2027, 7, 2),    # Day before Independence Day (observed, 4th is Sun)
    date(2027, 11, 26),  # Black Friday
    date(2027, 12, 23),  # Christmas Eve (observed, 25th is Sat)
}


def get_market_close_time(d) -> time:
    """Return market close time for a given date.

    Returns 13:00 ET for NYSE early close days, 16:00 ET otherwise.
    """
    check_date = d.date() if isinstance(d, datetime) else d
    if check_date in EARLY_CLOSE_DATES:
        return time(13, 0)
    return time(16, 0)


def is_early_close(d) -> bool:
    """Check if a date is an NYSE early close day."""
    check_date = d.date() if isinstance(d, datetime) else d
    return check_date in EARLY_CLOSE_DATES
