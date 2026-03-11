"""
Tests for PriceFeed abstraction layer.

Tests cover:
- YahooPriceFeed: success, failure, bar_data
- EtradePriceFeed: delegation to broker, exception handling
- SchwabPriceFeed: delegation to broker, exception handling
- TTL caching: cache hit, expiry, stale fallback
- Health monitoring: healthy after success, unhealthy after timeout, CRITICAL log
- Factory: dry-run -> Yahoo, live+ETrade -> ETrade, live+Schwab -> Schwab
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.data.price_feed import (
    PriceFeed,
    _BasePriceFeed,
    YahooPriceFeed,
    EtradePriceFeed,
    SchwabPriceFeed,
    create_price_feed,
    MAX_CONSECUTIVE_FAILURES,
    STALE_TIMEOUT_SECONDS,
)


# ============================================================================
# YahooPriceFeed
# ============================================================================

class TestYahooPriceFeed:
    """Tests for Yahoo Finance price feed."""

    def test_success_returns_float(self):
        yahoo = MagicMock()
        yahoo.get_spx_quote.return_value = {'price': 5850.50, 'open': 5840, 'high': 5860, 'low': 5830, 'previous_close': 5845}
        feed = YahooPriceFeed(yahoo_provider=yahoo)
        price = feed.get_latest_price()
        assert price == 5850.50
        assert isinstance(price, float)

    def test_failure_returns_none(self):
        yahoo = MagicMock()
        yahoo.get_spx_quote.return_value = None
        feed = YahooPriceFeed(yahoo_provider=yahoo)
        price = feed.get_latest_price()
        assert price is None

    def test_bar_data_populated(self):
        yahoo = MagicMock()
        yahoo.get_spx_quote.return_value = {
            'price': 5850, 'open': 5840, 'high': 5860,
            'low': 5830, 'previous_close': 5845,
        }
        feed = YahooPriceFeed(yahoo_provider=yahoo)
        bar = feed.get_latest_bar_data()
        assert bar is not None
        assert bar['open'] == 5840
        assert bar['close'] == 5850
        assert bar['previous_close'] == 5845

    def test_ttl_is_30_seconds(self):
        yahoo = MagicMock()
        feed = YahooPriceFeed(yahoo_provider=yahoo)
        assert feed.ttl_seconds == 30


# ============================================================================
# EtradePriceFeed
# ============================================================================

class TestEtradePriceFeed:
    """Tests for E*TRADE broker price feed."""

    def test_delegates_to_broker(self):
        broker = MagicMock()
        broker.get_current_price.return_value = 5900.0
        feed = EtradePriceFeed(broker)
        price = feed.get_latest_price()
        assert price == 5900.0
        broker.get_current_price.assert_called_with("SPX")

    def test_exception_handled_gracefully(self):
        broker = MagicMock()
        broker.get_current_price.side_effect = ConnectionError("API down")
        feed = EtradePriceFeed(broker)
        price = feed.get_latest_price()
        assert price is None

    def test_ttl_is_10_seconds(self):
        broker = MagicMock()
        feed = EtradePriceFeed(broker)
        assert feed.ttl_seconds == 10


# ============================================================================
# SchwabPriceFeed
# ============================================================================

class TestSchwabPriceFeed:
    """Tests for Schwab broker price feed."""

    def test_delegates_to_broker(self):
        broker = MagicMock()
        broker.get_current_price.return_value = 5910.25
        feed = SchwabPriceFeed(broker)
        price = feed.get_latest_price()
        assert price == 5910.25
        broker.get_current_price.assert_called_with("SPX")

    def test_exception_handled_gracefully(self):
        broker = MagicMock()
        broker.get_current_price.side_effect = TimeoutError("timeout")
        feed = SchwabPriceFeed(broker)
        price = feed.get_latest_price()
        assert price is None

    def test_ttl_is_10_seconds(self):
        broker = MagicMock()
        feed = SchwabPriceFeed(broker)
        assert feed.ttl_seconds == 10


# ============================================================================
# Caching
# ============================================================================

class TestCaching:
    """Tests for TTL cache behavior."""

    def test_second_call_uses_cache(self):
        """Within TTL, the provider is not called again."""
        yahoo = MagicMock()
        yahoo.get_spx_quote.return_value = {'price': 5800}
        feed = YahooPriceFeed(yahoo_provider=yahoo)

        feed.get_latest_price()
        feed.get_latest_price()
        # Only one call to provider (second hit cache)
        assert yahoo.get_spx_quote.call_count == 1

    def test_ttl_expiry_triggers_refetch(self):
        """After TTL expires, provider is called again."""
        yahoo = MagicMock()
        yahoo.get_spx_quote.return_value = {'price': 5800}
        feed = YahooPriceFeed(yahoo_provider=yahoo)
        feed.ttl_seconds = 0.01  # 10ms TTL for test speed

        feed.get_latest_price()
        time.sleep(0.02)
        feed.get_latest_price()
        # Two calls: initial + after expiry
        assert yahoo.get_spx_quote.call_count == 2

    def test_stale_cache_on_failure(self):
        """When fetch fails, return stale cached price."""
        yahoo = MagicMock()
        # First call succeeds
        yahoo.get_spx_quote.return_value = {'price': 5800}
        feed = YahooPriceFeed(yahoo_provider=yahoo)
        feed.ttl_seconds = 0.01

        price1 = feed.get_latest_price()
        assert price1 == 5800

        # Second call fails after TTL
        time.sleep(0.02)
        yahoo.get_spx_quote.return_value = None
        price2 = feed.get_latest_price()
        assert price2 == 5800  # stale cache


# ============================================================================
# Health Monitoring
# ============================================================================

class TestHealthMonitoring:
    """Tests for health tracking and failure alerting."""

    def test_healthy_after_success(self):
        yahoo = MagicMock()
        yahoo.get_spx_quote.return_value = {'price': 5800}
        feed = YahooPriceFeed(yahoo_provider=yahoo)
        feed.get_latest_price()
        assert feed.is_healthy() is True

    def test_unhealthy_before_any_fetch(self):
        yahoo = MagicMock()
        feed = YahooPriceFeed(yahoo_provider=yahoo)
        assert feed.is_healthy() is False

    @patch('src.data.price_feed.time')
    def test_unhealthy_after_stale_timeout(self, mock_time):
        """Feed becomes unhealthy when last success is older than STALE_TIMEOUT_SECONDS."""
        yahoo = MagicMock()
        yahoo.get_spx_quote.return_value = {'price': 5800}
        feed = YahooPriceFeed(yahoo_provider=yahoo)

        # First fetch succeeds at t=100
        mock_time.monotonic.return_value = 100.0
        feed.get_latest_price()
        assert feed.is_healthy() is True

        # Check health at t=100+STALE_TIMEOUT+1
        mock_time.monotonic.return_value = 100.0 + STALE_TIMEOUT_SECONDS + 1
        assert feed.is_healthy() is False

    def test_critical_log_after_consecutive_failures(self):
        yahoo = MagicMock()
        yahoo.get_spx_quote.return_value = None
        feed = YahooPriceFeed(yahoo_provider=yahoo)
        feed.ttl_seconds = 0  # no caching

        with patch('src.data.price_feed.logger') as mock_logger:
            for _ in range(MAX_CONSECUTIVE_FAILURES):
                feed.get_latest_price()
            mock_logger.critical.assert_called_once()

    def test_health_status_dict(self):
        yahoo = MagicMock()
        yahoo.get_spx_quote.return_value = {'price': 5800}
        feed = YahooPriceFeed(yahoo_provider=yahoo)
        feed.get_latest_price()

        status = feed.get_health_status()
        assert status['source'] == 'yahoo'
        assert status['healthy'] is True
        assert status['consecutive_failures'] == 0
        assert status['last_update_secs_ago'] is not None


# ============================================================================
# Factory
# ============================================================================

class TestFactory:
    """Tests for create_price_feed factory function."""

    @patch('src.data.price_feed.YahooPriceFeed')
    def test_dry_run_returns_yahoo(self, mock_cls):
        mock_cls.return_value = MagicMock(spec=YahooPriceFeed)
        mock_cls.return_value.ttl_seconds = 30
        feed = create_price_feed('dry-run', broker=None)
        assert mock_cls.called

    def test_live_etrade_returns_etrade_feed(self):
        broker = MagicMock()
        type(broker).__name__ = 'ETradeBroker'
        feed = create_price_feed('live', broker=broker)
        assert isinstance(feed, EtradePriceFeed)

    def test_live_schwab_returns_schwab_feed(self):
        broker = MagicMock()
        type(broker).__name__ = 'SchwabBroker'
        feed = create_price_feed('live', broker=broker)
        assert isinstance(feed, SchwabPriceFeed)

    @patch('src.data.price_feed.YahooPriceFeed')
    def test_live_no_broker_falls_back_to_yahoo(self, mock_cls):
        mock_cls.return_value = MagicMock(spec=YahooPriceFeed)
        mock_cls.return_value.ttl_seconds = 30
        feed = create_price_feed('live', broker=None)
        assert mock_cls.called


class TestPriceFeedHealthReset:
    """Verify price feed starts with fresh health state (no stale carry-over)."""

    def test_new_feed_starts_with_zero_failures(self):
        feed = YahooPriceFeed()
        status = feed.get_health_status()
        assert status['consecutive_failures'] == 0

    def test_new_feed_source_is_correct(self):
        feed = YahooPriceFeed()
        status = feed.get_health_status()
        assert status['source'] == 'yahoo'

    def test_new_feed_health_status_has_required_keys(self):
        feed = YahooPriceFeed()
        status = feed.get_health_status()
        assert 'source' in status
        assert 'healthy' in status
        assert 'last_update_secs_ago' in status
        assert 'consecutive_failures' in status

    def test_health_resets_on_new_instance_after_failures(self):
        """Simulate a previous session's failures then verify a new instance starts clean."""
        feed1 = YahooPriceFeed()
        # Simulate failures
        feed1._consecutive_failures = 5
        assert feed1.get_health_status()['consecutive_failures'] == 5

        # New instance should start fresh (as happens on bot restart)
        feed2 = YahooPriceFeed()
        assert feed2.get_health_status()['consecutive_failures'] == 0
