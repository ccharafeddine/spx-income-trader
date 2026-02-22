"""
Price Feed Abstraction Layer

Provides a unified interface for fetching SPX prices across all trading modes:
- YahooPriceFeed: dry-run mode (30s TTL, includes OHLC bar data)
- EtradePriceFeed: live mode with E*TRADE broker (10s TTL)
- SchwabPriceFeed: live mode with Schwab broker (10s TTL)

Each feed tracks health: consecutive failures, last successful update time,
and stale-cache fallback. The dashboard reads health from a persisted JSON file.
"""

import logging
import time
from abc import ABC, abstractmethod
from typing import Optional, Dict

logger = logging.getLogger(__name__)

# Health thresholds
MAX_CONSECUTIVE_FAILURES = 3   # Log CRITICAL after this many in a row
STALE_TIMEOUT_SECONDS = 120    # is_healthy() -> False if no update in this window


class PriceFeed(ABC):
    """Abstract price feed interface."""

    @abstractmethod
    def get_latest_price(self) -> Optional[float]:
        """Return the latest SPX price, or None on failure."""

    @abstractmethod
    def get_latest_bar_data(self) -> Optional[Dict]:
        """Return OHLC + prev_close dict, or None if unavailable."""

    @abstractmethod
    def is_healthy(self) -> bool:
        """True if the feed has delivered a price within STALE_TIMEOUT_SECONDS."""

    @abstractmethod
    def get_health_status(self) -> Dict:
        """Return health dict for dashboard consumption."""


class _BasePriceFeed(PriceFeed):
    """Shared caching, failure tracking, and health monitoring."""

    def __init__(self, source: str, ttl_seconds: float):
        self.source = source
        self.ttl_seconds = ttl_seconds

        # Cache state (monotonic clock for TTL)
        self._cached_price: Optional[float] = None
        self._cached_bar_data: Optional[Dict] = None
        self._cache_time: float = 0.0  # monotonic timestamp of last success

        # Health tracking
        self._consecutive_failures: int = 0
        self._last_success_time: float = 0.0  # monotonic
        self._critical_logged: bool = False

    # -- Subclass hook --
    def _fetch_price(self) -> Optional[float]:
        """Subclasses override to fetch raw price. Return None on failure."""
        raise NotImplementedError

    def _fetch_bar_data(self) -> Optional[Dict]:
        """Subclasses override to fetch OHLC data. Return None if unsupported."""
        return None

    # -- Public API --
    def get_latest_price(self) -> Optional[float]:
        now = time.monotonic()

        # Serve from cache if still fresh
        if self._cached_price is not None and (now - self._cache_time) < self.ttl_seconds:
            return self._cached_price

        # Fetch fresh price
        try:
            price = self._fetch_price()
        except Exception as e:
            logger.error(f"PriceFeed[{self.source}] fetch error: {e}")
            price = None

        if price is not None and price > 0:
            self._cached_price = price
            self._cache_time = now
            self._last_success_time = now
            self._consecutive_failures = 0
            self._critical_logged = False

            # Also refresh bar data (best-effort, ignore failures)
            try:
                self._cached_bar_data = self._fetch_bar_data()
            except Exception:
                pass

            return price

        # Failure path
        self._consecutive_failures += 1
        if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES and not self._critical_logged:
            logger.critical(
                f"PriceFeed[{self.source}] {self._consecutive_failures} consecutive "
                f"failures - price data unavailable"
            )
            self._critical_logged = True

        # Return stale cache as fallback
        if self._cached_price is not None:
            logger.warning(
                f"PriceFeed[{self.source}] fetch failed, using stale cache "
                f"(age {now - self._cache_time:.0f}s)"
            )
            return self._cached_price

        return None

    def get_latest_bar_data(self) -> Optional[Dict]:
        # Trigger a price fetch to refresh cache if needed
        self.get_latest_price()
        return self._cached_bar_data

    def is_healthy(self) -> bool:
        if self._last_success_time == 0.0:
            return False
        elapsed = time.monotonic() - self._last_success_time
        return elapsed < STALE_TIMEOUT_SECONDS

    def get_health_status(self) -> Dict:
        now = time.monotonic()
        last_update_ago = round(now - self._last_success_time, 1) if self._last_success_time > 0 else None
        return {
            'source': self.source,
            'healthy': self.is_healthy(),
            'last_update_secs_ago': last_update_ago,
            'consecutive_failures': self._consecutive_failures,
        }


# ============================================================================
# Concrete feeds
# ============================================================================

class YahooPriceFeed(_BasePriceFeed):
    """Wraps YahooFinanceProvider for dry-run mode. Includes OHLC bar data."""

    def __init__(self, yahoo_provider=None):
        super().__init__(source='yahoo', ttl_seconds=30)
        if yahoo_provider is None:
            from src.data.yahoo_finance import YahooFinanceProvider
            yahoo_provider = YahooFinanceProvider()
        self._yahoo = yahoo_provider
        self._last_quote: Optional[Dict] = None  # single-cycle cache

    def _fetch_price(self) -> Optional[float]:
        quote = self._yahoo.get_spx_quote()
        self._last_quote = quote  # stash for _fetch_bar_data
        if quote and quote.get('price'):
            return float(quote['price'])
        return None

    def _fetch_bar_data(self) -> Optional[Dict]:
        quote = self._last_quote  # reuse quote from _fetch_price
        if not quote or not quote.get('price'):
            return None
        return {
            'open': quote.get('open', 0),
            'high': quote.get('high', 0),
            'low': quote.get('low', 0),
            'close': quote['price'],
            'previous_close': quote.get('previous_close', 0),
        }


class EtradePriceFeed(_BasePriceFeed):
    """Wraps ETradeBroker.get_current_price for live E*TRADE mode."""

    def __init__(self, broker):
        super().__init__(source='etrade', ttl_seconds=10)
        self._broker = broker

    def _fetch_price(self) -> Optional[float]:
        price = self._broker.get_current_price("SPX")
        return float(price) if price and price > 0 else None


class SchwabPriceFeed(_BasePriceFeed):
    """Wraps SchwabBroker.get_current_price for live Schwab mode."""

    def __init__(self, broker):
        super().__init__(source='schwab', ttl_seconds=10)
        self._broker = broker

    def _fetch_price(self) -> Optional[float]:
        price = self._broker.get_current_price("SPX")
        return float(price) if price and price > 0 else None


# ============================================================================
# Factory
# ============================================================================

def create_price_feed(trading_mode: str, broker=None) -> PriceFeed:
    """Create the appropriate PriceFeed based on trading mode and broker type.

    Args:
        trading_mode: 'dry-run' or 'live'
        broker: Broker instance (required for live mode)

    Returns:
        Configured PriceFeed instance
    """
    if trading_mode == 'dry-run' or broker is None:
        feed = YahooPriceFeed()
        logger.info(f"Price feed: YahooPriceFeed (dry-run mode, {feed.ttl_seconds}s TTL)")
        return feed

    # Live mode: pick feed based on broker class name
    broker_class = type(broker).__name__

    if 'Schwab' in broker_class:
        feed = SchwabPriceFeed(broker)
        logger.info(f"Price feed: SchwabPriceFeed (live mode, {feed.ttl_seconds}s TTL)")
        return feed

    if 'ETrade' in broker_class:
        feed = EtradePriceFeed(broker)
        logger.info(f"Price feed: EtradePriceFeed (live mode, {feed.ttl_seconds}s TTL)")
        return feed

    # Unknown broker type in live mode - fall back to Yahoo
    logger.warning(f"Unknown broker type '{broker_class}' - falling back to YahooPriceFeed")
    feed = YahooPriceFeed()
    return feed
