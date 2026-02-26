"""
Tests that the main loop does not self-stop during market hours
unless a risk limit is explicitly triggered.

Covers:
- PositionManager method existence checks
- Consecutive error threshold and reset
- Yahoo Finance retry and last-known cache
- Exception isolation for monitor_positions and _update_market_state
- Watchdog auto-restart and 3/hour cap
- Clean stop doesn't trigger restart
- Auto-restart badge in bot status
"""

import ast
import sys
import time
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock
from datetime import datetime

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestPositionManagerMethodCalls:
    """Verify main.py only calls methods that actually exist on PositionManager."""

    def test_no_get_open_positions_on_position_manager(self):
        """main.py must never call position_manager.get_open_positions().

        PositionManager exposes get_open_trades(), not get_open_positions().
        Calling the wrong name causes an AttributeError on every loop
        iteration, which accumulates to 5 consecutive errors and triggers
        an auto-shutdown during market hours.
        """
        main_path = Path(__file__).parent.parent / 'src' / 'main.py'
        source = main_path.read_text(encoding='utf-8')

        # Parse the AST and look for position_manager.get_open_positions calls
        tree = ast.parse(source, filename=str(main_path))
        bad_calls = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                # Match: self.position_manager.get_open_positions()
                if (isinstance(func, ast.Attribute)
                        and func.attr == 'get_open_positions'
                        and isinstance(func.value, ast.Attribute)
                        and func.value.attr == 'position_manager'):
                    bad_calls.append(node.lineno)

        assert bad_calls == [], (
            f"main.py calls position_manager.get_open_positions() on lines {bad_calls}. "
            f"PositionManager only has get_open_trades(). This will cause "
            f"AttributeError -> 5 consecutive errors -> bot auto-shutdown."
        )

    def test_get_open_trades_exists_on_position_manager(self):
        """PositionManager must expose get_open_trades()."""
        from src.core.position_manager import PositionManager
        assert hasattr(PositionManager, 'get_open_trades'), (
            "PositionManager.get_open_trades() is missing - main.py depends on it"
        )

    def test_position_manager_method_calls_in_main(self):
        """Every position_manager method *call* in main.py must exist on PositionManager."""
        from src.core.position_manager import PositionManager

        main_path = Path(__file__).parent.parent / 'src' / 'main.py'
        source = main_path.read_text(encoding='utf-8')
        tree = ast.parse(source, filename=str(main_path))

        pm_methods_called = set()
        for node in ast.walk(tree):
            # Only check Call nodes (method invocations), not plain attribute access
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                func = node.func
                if (isinstance(func.value, ast.Attribute)
                        and func.value.attr == 'position_manager'):
                    pm_methods_called.add(func.attr)

        pm_methods = {name for name in dir(PositionManager) if callable(getattr(PositionManager, name, None))}
        missing = pm_methods_called - pm_methods
        assert missing == set(), (
            f"main.py calls position_manager methods that don't exist: {missing}"
        )


class TestConsecutiveErrorShutdown:
    """Verify the consecutive error handler doesn't shut down for recoverable errors."""

    def test_max_consecutive_errors_is_reasonable(self):
        """The error threshold should be at least 5 to tolerate transient issues."""
        main_path = Path(__file__).parent.parent / 'src' / 'main.py'
        source = main_path.read_text(encoding='utf-8')
        assert 'max_consecutive_errors = 5' in source

    def test_error_counter_resets_on_success(self):
        """consecutive_errors must be reset to 0 after a successful loop iteration."""
        main_path = Path(__file__).parent.parent / 'src' / 'main.py'
        source = main_path.read_text(encoding='utf-8')
        assert 'consecutive_errors = 0' in source


class TestYahooFinanceRetry:
    """Yahoo Finance retry logic and last-known cache."""

    def test_retry_on_transient_error(self):
        """Transient network error triggers retry, succeeds on second attempt."""
        from src.data.yahoo_finance import YahooFinanceProvider

        provider = YahooFinanceProvider()
        # Clear cache so _get_quote actually fetches
        provider._cache.clear()
        provider._last_request_time = 0

        call_count = 0
        good_result = {
            'symbol': '^GSPC', 'price': 5800.0, 'open': 5790,
            'high': 5810, 'low': 5785, 'previous_close': 5780,
            'change': 20, 'change_pct': 0.35, 'volume': 0,
            'timestamp': '2026-02-26', 'market_time': None,
            'market_time_epoch': 0,
        }

        def mock_fetch(symbol):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None  # First attempt fails
            return good_result

        with patch.object(provider, '_fetch_direct_http', side_effect=mock_fetch), \
             patch.object(provider, '_fetch_via_yfinance', return_value=None), \
             patch('src.data.yahoo_finance.time.sleep'):
            result = provider._get_quote('^GSPC')

        assert result is not None
        assert result['price'] == 5800.0
        assert call_count >= 2  # At least 2 attempts

    def test_last_known_price_on_total_failure(self):
        """After total failure, returns last-known-good price."""
        from src.data.yahoo_finance import YahooFinanceProvider

        provider = YahooFinanceProvider()
        provider._cache.clear()
        provider._last_request_time = 0

        # Seed the last-known cache
        provider._last_known['^GSPC'] = {
            'symbol': '^GSPC', 'price': 5750.0, 'open': 5740,
            'high': 5760, 'low': 5735, 'previous_close': 5730,
            'change': 20, 'change_pct': 0.35, 'volume': 0,
            'timestamp': '2026-02-26', 'market_time': None,
            'market_time_epoch': 0,
        }

        with patch.object(provider, '_fetch_direct_http', return_value=None), \
             patch.object(provider, '_fetch_via_yfinance', return_value=None), \
             patch('src.data.yahoo_finance.time.sleep'):
            result = provider._get_quote('^GSPC')

        assert result is not None
        assert result['price'] == 5750.0


class TestExceptionIsolation:
    """Market state and position monitor exceptions don't kill the loop."""

    def test_market_state_exception_isolated(self):
        """_update_market_state is wrapped in try/except in main loop source."""
        main_path = Path(__file__).parent.parent / 'src' / 'main.py'
        source = main_path.read_text(encoding='utf-8')

        # Find the isolation pattern: try block around _update_market_state
        # The source should contain a try/except that catches market state errors
        # without incrementing consecutive_errors
        lines = source.split('\n')
        found_isolation = False
        for i, line in enumerate(lines):
            if '_update_market_state(current_time)' in line:
                # Look backwards for the try
                context = '\n'.join(lines[max(0, i-3):i+5])
                if 'try:' in context and 'non-fatal' in context:
                    found_isolation = True
                    break
        assert found_isolation, (
            "_update_market_state must be wrapped in its own try/except "
            "with 'non-fatal' logging to prevent consecutive_errors accumulation"
        )

    def test_monitor_positions_exception_isolated(self):
        """monitor_positions() is wrapped in try/except in main loop source."""
        main_path = Path(__file__).parent.parent / 'src' / 'main.py'
        source = main_path.read_text(encoding='utf-8')

        lines = source.split('\n')
        found_isolation = False
        for i, line in enumerate(lines):
            if 'monitor_positions()' in line:
                context = '\n'.join(lines[max(0, i-3):i+5])
                if 'try:' in context and 'non-fatal' in context:
                    found_isolation = True
                    break
        assert found_isolation, (
            "monitor_positions() must be wrapped in its own try/except "
            "with 'non-fatal' logging to prevent consecutive_errors accumulation"
        )


class TestWatchdogAutoRestart:
    """Watchdog auto-restart behavior."""

    def _make_desktop(self):
        """Create a minimal DesktopApp-like object for testing watchdog logic."""
        obj = MagicMock()
        obj._shutting_down = False
        obj._bot_lock = threading.Lock()
        obj._bot = None
        obj._bot_thread = None
        obj._bot_mode = 'dry-run'
        obj._bot_started_at = None
        obj._bot_starting = False
        obj._bot_crashed = False
        obj._bot_crash_error = None
        obj._bot_crash_time = None
        obj._stop_requested = False
        obj._auto_restart_times = []
        obj._auto_restarted = False
        obj._max_restarts_per_hour = 3
        return obj

    def test_watchdog_restarts_after_crash(self):
        """Watchdog calls start_bot when bot thread dies unexpectedly."""
        from app_desktop import DesktopApp

        desktop = self._make_desktop()

        # Simulate: bot was running (bot_instance is not None), thread is dead
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = False
        desktop._bot_thread = mock_thread
        desktop._bot = MagicMock()  # was_running = True
        desktop._stop_requested = False

        # start_bot should be called after 30s wait
        desktop.start_bot = MagicMock(return_value=(True, None))
        desktop._update_tray_icon = MagicMock()

        # Run the watchdog method directly (patch sleep to avoid waiting)
        with patch('time.sleep'):
            DesktopApp._run_watchdog(desktop)

        desktop.start_bot.assert_called_once_with('dry-run')
        assert desktop._auto_restarted is True

    def test_watchdog_respects_3_per_hour_cap(self):
        """Watchdog refuses restart when 3 restarts already happened this hour."""
        from app_desktop import DesktopApp

        desktop = self._make_desktop()

        # Simulate recent restarts at capacity
        now = time.time()
        desktop._auto_restart_times = [now - 100, now - 50, now - 10]

        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = False
        desktop._bot_thread = mock_thread
        desktop._bot = MagicMock()
        desktop._stop_requested = False
        desktop.start_bot = MagicMock(return_value=(True, None))
        desktop._update_tray_icon = MagicMock()

        with patch('time.sleep'):
            DesktopApp._run_watchdog(desktop)

        desktop.start_bot.assert_not_called()

    def test_clean_stop_no_restart(self):
        """User-initiated stop does not trigger auto-restart."""
        from app_desktop import DesktopApp

        desktop = self._make_desktop()

        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = False
        desktop._bot_thread = mock_thread
        desktop._bot = MagicMock()
        desktop._stop_requested = True  # User clicked Stop

        desktop.start_bot = MagicMock(return_value=(True, None))
        desktop._update_tray_icon = MagicMock()

        with patch('time.sleep'):
            DesktopApp._run_watchdog(desktop)

        desktop.start_bot.assert_not_called()
        assert desktop._auto_restarted is False

    def test_auto_restart_badge_in_status(self):
        """Bot status includes auto_restarted field when running after restart."""
        from app_desktop import DesktopApp

        desktop = self._make_desktop()
        desktop._auto_restarted = True

        # Simulate running bot
        mock_bot = MagicMock()
        mock_bot.running = True
        mock_bot.dte0_trades_today = 0
        mock_bot.tnt_trades_today = 0
        mock_bot.portfolio = MagicMock()
        mock_bot.portfolio.daily_realized_pnl = 0.0
        desktop._bot = mock_bot
        desktop._bot_mode = 'dry-run'
        desktop._bot_started_at = datetime.now()

        status = DesktopApp.get_bot_status(desktop)
        assert status['auto_restarted'] is True
        assert status['running'] is True
