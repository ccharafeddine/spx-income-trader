"""
SPX Moving Average and Prior-Day Range Provider

Fetches daily OHLC data from Yahoo Finance and computes:
- 50-day and 200-day simple moving averages
- Prior day's high/low range

Values are cached for the calendar day since daily bars only change once.
Returns None gracefully on failure (never blocks a trade).
"""

import logging
import time as _time
from datetime import date, datetime
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Module-level daily cache
_cache_date: Optional[date] = None
_sma50: Optional[float] = None
_sma200: Optional[float] = None
_prior_high: Optional[float] = None
_prior_low: Optional[float] = None


def _fetch_daily_data():
    """Fetch daily OHLC data from Yahoo Finance and compute SMAs + prior range.

    Populates module-level cache variables. Called at most once per calendar day.
    """
    global _cache_date, _sma50, _sma200, _prior_high, _prior_low

    try:
        import yfinance as yf

        # Need 200 trading days + buffer for SMA200
        ticker = yf.Ticker("^GSPC")
        hist = ticker.history(period="1y", interval="1d")

        if hist.empty or len(hist) < 2:
            logger.warning("SMA provider: insufficient daily data from Yahoo Finance")
            return

        # Flatten MultiIndex columns if present (yfinance quirk)
        if hasattr(hist.columns, 'nlevels') and hist.columns.nlevels > 1:
            hist.columns = hist.columns.get_level_values(0)

        closes = hist['Close'].dropna()

        # Compute SMAs
        if len(closes) >= 50:
            _sma50 = round(float(closes.iloc[-50:].mean()), 2)
        if len(closes) >= 200:
            _sma200 = round(float(closes.iloc[-200:].mean()), 2)

        # Prior day's high/low (second-to-last row = prior completed day)
        if len(hist) >= 2:
            prior_row = hist.iloc[-2]
            _prior_high = round(float(prior_row['High']), 2)
            _prior_low = round(float(prior_row['Low']), 2)

        try:
            import pytz
            et = pytz.timezone('America/New_York')
            _cache_date = datetime.now(et).date()
        except Exception:
            _cache_date = date.today()

        logger.info(
            f"SMA provider cached: SMA50={_sma50}, SMA200={_sma200}, "
            f"prior_range=[{_prior_low}, {_prior_high}]"
        )

    except ImportError:
        logger.warning("SMA provider: yfinance not installed")
    except Exception as e:
        logger.warning(f"SMA provider fetch failed: {e}")


def _ensure_cache():
    """Refresh cache if stale (different calendar day) or empty."""
    try:
        import pytz
        et = pytz.timezone('America/New_York')
        today = datetime.now(et).date()
    except Exception:
        today = date.today()

    if _cache_date != today:
        _fetch_daily_data()


def invalidate_cache():
    """Clear cache (for testing)."""
    global _cache_date, _sma50, _sma200, _prior_high, _prior_low
    _cache_date = None
    _sma50 = None
    _sma200 = None
    _prior_high = None
    _prior_low = None


def get_sma50() -> Optional[float]:
    """Get the current 50-day SMA for SPX. Returns None on failure."""
    _ensure_cache()
    return _sma50


def get_sma200() -> Optional[float]:
    """Get the current 200-day SMA for SPX. Returns None on failure."""
    _ensure_cache()
    return _sma200


def get_prior_day_range() -> Tuple[Optional[float], Optional[float]]:
    """Get prior trading day's (high, low). Returns (None, None) on failure."""
    _ensure_cache()
    return _prior_high, _prior_low


def compute_sma_distance(spx_price: float, sma_value: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    """Compute distance from SPX price to an SMA value.

    Returns:
        (points, pct) where points = spx - sma, pct = (points/sma)*100.
        Returns (None, None) if sma_value is None.
    """
    if sma_value is None or sma_value == 0:
        return None, None
    points = round(spx_price - sma_value, 2)
    pct = round((points / sma_value) * 100, 2)
    return points, pct


def classify_vs_prior_range(spx_price: float, prior_high: Optional[float], prior_low: Optional[float]) -> Optional[str]:
    """Classify SPX price relative to prior day's range.

    Returns:
        'above' if spx > prior high
        'below' if spx < prior low
        'within' if prior_low <= spx <= prior_high
        None if prior range data unavailable
    """
    if prior_high is None or prior_low is None:
        return None
    if spx_price > prior_high:
        return 'above'
    elif spx_price < prior_low:
        return 'below'
    else:
        return 'within'


def classify_entry_time_bucket(entry_time: datetime) -> str:
    """Classify entry time into a trading session bucket.

    Args:
        entry_time: ET-aware datetime of trade entry.

    Returns:
        'early' (9:30-10:00), 'mid' (10:00-10:30), 'late' (10:30+)
    """
    try:
        import pytz
        et = pytz.timezone('America/New_York')
        if entry_time.tzinfo is None:
            entry_time = et.localize(entry_time)
        else:
            entry_time = entry_time.astimezone(et)
    except Exception:
        pass

    t = entry_time.time()
    from datetime import time as dt_time
    if t < dt_time(10, 0):
        return 'early'
    elif t < dt_time(10, 30):
        return 'mid'
    else:
        return 'late'
