"""
VIX Level Provider with Caching and Regime Classification

Fetches ^VIX from Yahoo Finance (same library used for SPX data in dry-run mode).
Caches the value for 5 minutes to avoid excessive API calls.
Returns None gracefully on failure -- never blocks a trade due to VIX fetch failure.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Regime thresholds (upper bound exclusive)
_REGIME_THRESHOLDS = [
    (15, "LOW"),
    (20, "NORMAL"),
    (25, "ELEVATED"),
    (30, "HIGH"),
]
_REGIME_EXTREME = "EXTREME"


def classify_vix(level: Optional[float]) -> Optional[str]:
    """
    Classify a VIX level into a volatility regime label.

    Returns:
        "LOW"      (VIX < 15)
        "NORMAL"   (15 <= VIX < 20)
        "ELEVATED" (20 <= VIX < 25)
        "HIGH"     (25 <= VIX < 30)
        "EXTREME"  (VIX >= 30)
        None       if level is None
    """
    if level is None:
        return None
    for threshold, label in _REGIME_THRESHOLDS:
        if level < threshold:
            return label
    return _REGIME_EXTREME


class VixProvider:
    """
    Cached VIX price provider.

    Uses the existing YahooFinanceProvider when available, falls back to
    raw yfinance. Caches for 5 minutes (300 seconds).
    """

    CACHE_TTL = 300  # 5 minutes

    def __init__(self, yahoo_provider=None):
        """
        Args:
            yahoo_provider: Optional YahooFinanceProvider instance for
                            shared caching with the rest of the system.
        """
        self._yahoo = yahoo_provider
        self._cached_price: Optional[float] = None
        self._cache_time: float = 0

    def get_current_vix(self) -> Optional[float]:
        """
        Fetch the current VIX level with 5-minute caching.

        Returns:
            VIX price as float, or None on failure.
        """
        now = time.monotonic()
        if self._cached_price is not None and (now - self._cache_time) < self.CACHE_TTL:
            return self._cached_price

        price = self._fetch()
        if price is not None:
            self._cached_price = price
            self._cache_time = now

        # If fetch failed, return stale cache if available
        return self._cached_price

    def get_vix_with_regime(self) -> Tuple[Optional[float], Optional[str]]:
        """
        Convenience: fetch VIX and classify in one call.

        Returns:
            (vix_level, vix_regime) -- either may be None on failure.
        """
        level = self.get_current_vix()
        return level, classify_vix(level)

    def _fetch(self) -> Optional[float]:
        """Try all sources to get a VIX price. Never raises."""
        # Source 1: YahooFinanceProvider (has its own 60s cache + rate limiting)
        if self._yahoo is not None:
            try:
                quote = self._yahoo.get_vix_quote()
                if quote and quote.get('price'):
                    price = float(quote['price'])
                    logger.debug(f"VIX from YahooFinanceProvider: {price:.2f}")
                    return price
            except Exception as e:
                logger.debug(f"YahooFinanceProvider VIX failed: {e}")

        # Source 2: raw yfinance (with thread-based timeout)
        try:
            import yfinance as yf
            ticker = yf.Ticker("^VIX")
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(ticker.history, period="1d", interval="1d")
                hist = future.result(timeout=10)
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])
                logger.debug(f"VIX from yfinance: {price:.2f}")
                return price
        except FuturesTimeout:
            logger.warning("yfinance VIX timed out (10s)")
        except Exception as e:
            logger.debug(f"yfinance VIX failed: {e}")

        logger.warning("Could not fetch VIX from any source")
        return None
