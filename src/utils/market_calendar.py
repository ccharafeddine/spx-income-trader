"""NYSE market calendar helpers.

Provides holiday detection, early close day detection, and market close time lookup.
"""

from datetime import date, time, datetime


# NYSE full-closure holidays (2025-2027).
NYSE_HOLIDAYS = {
    # 2025
    date(2025, 1, 1),    # New Year's Day
    date(2025, 1, 9),    # National Day of Mourning (Carter)
    date(2025, 1, 20),   # MLK Day
    date(2025, 2, 17),   # Presidents' Day
    date(2025, 4, 18),   # Good Friday
    date(2025, 5, 26),   # Memorial Day
    date(2025, 6, 19),   # Juneteenth
    date(2025, 7, 4),    # Independence Day
    date(2025, 9, 1),    # Labor Day
    date(2025, 11, 27),  # Thanksgiving
    date(2025, 12, 25),  # Christmas
    # 2026
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # MLK Day
    date(2026, 2, 16),   # Presidents' Day
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed, 4th is Sat)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
    # 2027
    date(2027, 1, 1),    # New Year's Day
    date(2027, 1, 18),   # MLK Day
    date(2027, 2, 15),   # Presidents' Day
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),   # Memorial Day
    date(2027, 6, 18),   # Juneteenth (observed, 19th is Sat)
    date(2027, 7, 5),    # Independence Day (observed, 4th is Sun)
    date(2027, 9, 6),    # Labor Day
    date(2027, 11, 25),  # Thanksgiving
    date(2027, 12, 24),  # Christmas (observed, 25th is Sat)
}


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


def is_trading_day(d) -> bool:
    """Check if a date is an NYSE trading day (weekday and not a holiday)."""
    check_date = d.date() if isinstance(d, datetime) else d
    if check_date.weekday() >= 5:  # Saturday or Sunday
        return False
    return check_date not in NYSE_HOLIDAYS
