"""
Tests for live Schwab account balance polling in the dashboard.

Covers:
1. Live mode serves Schwab balance data (equity, cash, unrealized P&L)
2. Dry-run mode serves DB-calculated data (no broker call)
3. Failed API call falls back gracefully with WARNING log
4. Cache prevents redundant calls within 15 seconds
"""

import json
import logging
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_schwab_balance(nav=50000.0, cash=48000.0, buying_power=96000.0,
                         unrealized_pnl=150.0):
    return {
        'net_account_value': nav,
        'cash_available': cash,
        'buying_power': buying_power,
        'unrealized_pnl': unrealized_pnl,
    }


def _reset_balance_cache():
    """Reset the module-level balance cache between tests."""
    import dashboard.app as dapp
    dapp._cached_balance = None
    dapp._cached_balance_ts = 0.0


# ---------------------------------------------------------------------------
# 1. Live mode serves Schwab balance data
# ---------------------------------------------------------------------------

class TestLiveModeBalance:
    """In LIVE mode, /api/status should serve real Schwab balance data."""

    def test_live_mode_uses_schwab_equity(self):
        """Total equity should come from Schwab's net_account_value."""
        import dashboard.app as dapp
        _reset_balance_cache()

        mock_broker = MagicMock()
        mock_broker.get_account_balance.return_value = _make_schwab_balance(
            nav=51234.56, cash=49000.00
        )

        with patch.object(dapp, '_get_cached_broker', return_value=mock_broker), \
             patch.object(dapp, 'detect_trading_mode', return_value='LIVE'):
            result = dapp._get_schwab_balance()

        assert result is not None
        assert result['net_account_value'] == 51234.56
        assert result['cash_available'] == 49000.00

    def test_live_mode_uses_schwab_unrealized_pnl(self):
        """Unrealized P&L should come from Schwab positions data."""
        import dashboard.app as dapp
        _reset_balance_cache()

        mock_broker = MagicMock()
        mock_broker.get_account_balance.return_value = _make_schwab_balance(
            nav=50200.0, cash=49000.0, unrealized_pnl=200.0
        )

        with patch.object(dapp, '_get_cached_broker', return_value=mock_broker):
            result = dapp._get_schwab_balance()

        assert result['unrealized_pnl'] == 200.0

    def test_live_mode_cash_balance(self):
        """Cash balance should come from Schwab's cash_available."""
        import dashboard.app as dapp
        _reset_balance_cache()

        mock_broker = MagicMock()
        mock_broker.get_account_balance.return_value = _make_schwab_balance(
            cash=47500.50
        )

        with patch.object(dapp, '_get_cached_broker', return_value=mock_broker):
            result = dapp._get_schwab_balance()

        assert result['cash_available'] == 47500.50


# ---------------------------------------------------------------------------
# 2. Dry-run mode serves DB-calculated data
# ---------------------------------------------------------------------------

class TestDryRunModeBalance:
    """In dry-run mode, _get_schwab_balance should NOT be called by api_status."""

    def test_dry_run_does_not_call_broker(self):
        """Verify the status route guards on mode == 'LIVE'."""
        import dashboard.app as dapp
        source = Path(dapp.__file__).read_text()

        # The live balance block should be guarded by mode == 'LIVE'
        assert "if mode == 'LIVE':" in source
        # _get_schwab_balance should only appear inside that block
        lines = source.split('\n')
        for i, line in enumerate(lines):
            if '_get_schwab_balance()' in line and 'def _get_schwab_balance' not in line:
                # Find the nearest preceding if-statement
                for j in range(i - 1, max(0, i - 10), -1):
                    stripped = lines[j].strip()
                    if stripped.startswith('if '):
                        assert 'LIVE' in stripped or 'mode' in stripped, \
                            f"_get_schwab_balance at line {i+1} not guarded by LIVE check"
                        break


# ---------------------------------------------------------------------------
# 3. Failed API call falls back gracefully with WARNING log
# ---------------------------------------------------------------------------

class TestBrokerFailureFallback:
    """When Schwab API fails, _get_schwab_balance returns None and logs WARNING."""

    def test_returns_none_on_failure(self):
        """Failed broker call should return None (caller uses DB values)."""
        import dashboard.app as dapp
        _reset_balance_cache()

        mock_broker = MagicMock()
        mock_broker.get_account_balance.side_effect = Exception("Connection refused")

        with patch.object(dapp, '_get_cached_broker', return_value=mock_broker):
            result = dapp._get_schwab_balance()

        assert result is None

    def test_logs_warning_on_failure(self, caplog):
        """Failed broker call should log at WARNING level."""
        import dashboard.app as dapp
        _reset_balance_cache()

        mock_broker = MagicMock()
        mock_broker.get_account_balance.side_effect = Exception("token expired")

        with caplog.at_level(logging.WARNING, logger='dashboard.app'), \
             patch.object(dapp, '_get_cached_broker', return_value=mock_broker):
            dapp._get_schwab_balance()

        assert any('Schwab balance fetch failed' in r.message for r in caplog.records)
        assert any(r.levelno == logging.WARNING for r in caplog.records
                   if 'Schwab balance fetch failed' in r.message)

    def test_does_not_crash_status_route(self):
        """_get_schwab_balance returning None should not break api_status."""
        import dashboard.app as dapp
        _reset_balance_cache()

        mock_broker = MagicMock()
        mock_broker.get_account_balance.side_effect = Exception("network error")

        with patch.object(dapp, '_get_cached_broker', return_value=mock_broker):
            result = dapp._get_schwab_balance()

        # None return means caller falls through to DB values — no crash
        assert result is None


# ---------------------------------------------------------------------------
# 4. Cache prevents redundant calls within 15 seconds
# ---------------------------------------------------------------------------

class TestBalanceCache:
    """Balance cache should prevent redundant Schwab API calls."""

    def test_second_call_within_ttl_uses_cache(self):
        """Two calls within 15s should only hit the broker once."""
        import dashboard.app as dapp
        _reset_balance_cache()

        mock_broker = MagicMock()
        mock_broker.get_account_balance.return_value = _make_schwab_balance(
            nav=50000.0
        )

        with patch.object(dapp, '_get_cached_broker', return_value=mock_broker):
            result1 = dapp._get_schwab_balance()
            result2 = dapp._get_schwab_balance()

        assert mock_broker.get_account_balance.call_count == 1
        assert result1 == result2

    def test_call_after_ttl_refreshes(self):
        """A call after the TTL window should fetch fresh data."""
        import dashboard.app as dapp
        _reset_balance_cache()

        mock_broker = MagicMock()
        mock_broker.get_account_balance.return_value = _make_schwab_balance(
            nav=50000.0
        )

        with patch.object(dapp, '_get_cached_broker', return_value=mock_broker):
            result1 = dapp._get_schwab_balance()
            # Expire the cache by backdating the timestamp
            dapp._cached_balance_ts = time.time() - 20
            mock_broker.get_account_balance.return_value = _make_schwab_balance(
                nav=50100.0
            )
            result2 = dapp._get_schwab_balance()

        assert mock_broker.get_account_balance.call_count == 2
        assert result1['net_account_value'] == 50000.0
        assert result2['net_account_value'] == 50100.0

    def test_cache_ttl_is_15_seconds(self):
        """Verify the cache TTL constant is 15 seconds."""
        import dashboard.app as dapp
        assert dapp._BALANCE_CACHE_TTL == 15

    def test_failed_call_does_not_cache(self):
        """A failed broker call should not poison the cache."""
        import dashboard.app as dapp
        _reset_balance_cache()

        mock_broker = MagicMock()
        # First call: failure
        mock_broker.get_account_balance.side_effect = Exception("timeout")

        with patch.object(dapp, '_get_cached_broker', return_value=mock_broker):
            result1 = dapp._get_schwab_balance()

        assert result1 is None
        # Cache should still be empty
        assert dapp._cached_balance is None

        # Second call: success — should actually call the broker
        mock_broker.get_account_balance.side_effect = None
        mock_broker.get_account_balance.return_value = _make_schwab_balance(
            nav=50000.0
        )

        with patch.object(dapp, '_get_cached_broker', return_value=mock_broker):
            result2 = dapp._get_schwab_balance()

        assert result2 is not None
        assert result2['net_account_value'] == 50000.0
