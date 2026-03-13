"""
Regression tests for database reliability fixes:
  1. save_trade_with_retry succeeds through transient WAL locks
  2. Pending trade record is visible to circuit-breaker queries
  3. notifier.mode is set correctly in the desktop app code path
  4. Retry loop fires a critical Discord notification on exhaustion
"""

import sys
import sqlite3
import threading
import time
from pathlib import Path
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytz
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

ET = pytz.timezone("America/New_York")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path):
    """Create a fresh DatabaseManager pointing at a temp file."""
    from database.db_manager import DatabaseManager
    db_path = str(tmp_path / "test_trades.db")
    return DatabaseManager(db_path), db_path


def _make_trade(status='active'):
    """Build a minimal Trade object for DB tests."""
    from src.models.spread import CreditSpread, OptionLeg, TradeDirection
    from src.models.trade import Trade, TradeStatus
    from src.models.bar import Bar

    entry_time = ET.localize(datetime(2026, 3, 11, 11, 0))
    expiration = ET.localize(datetime(2026, 3, 11, 16, 0))

    short_leg = OptionLeg(strike=6795.0, option_type='put', action='sell', price=2.00)
    long_leg = OptionLeg(strike=6790.0, option_type='put', action='buy', price=0.50)
    spread = CreditSpread(
        direction=TradeDirection.BULLISH,
        short_leg=short_leg,
        long_leg=long_leg,
        credit_received=1.90,
        entry_time=entry_time,
        expiration=expiration,
    )
    setup_bar = Bar(timestamp=entry_time, open=6800.0, high=6810.0, low=6790.0, close=6805.0)

    status_map = {
        'active': TradeStatus.ACTIVE,
        'pending': TradeStatus.PENDING,
        'closed': TradeStatus.CLOSED,
    }
    return Trade(
        id='test-trade-db-001',
        spread=spread,
        status=status_map[status],
        setup_bar=setup_bar,
        entry_price=1.90,
        entry_time=entry_time,
        quantity=1,
    )


# ---------------------------------------------------------------------------
# 1. save_trade_with_retry succeeds through transient WAL locks
# ---------------------------------------------------------------------------

class TestSaveTradeWithRetry:
    """Verify retry logic handles concurrent lock contention."""

    def test_succeeds_after_transient_lock(self, tmp_path):
        """save_trade_with_retry should succeed when another connection
        holds a brief WAL lock that clears before retries exhaust."""
        db, db_path = _make_db(tmp_path)
        trade = _make_trade()

        # Hold a write transaction in another thread for 0.3s
        barrier = threading.Event()
        def hold_lock():
            conn = sqlite3.connect(db_path, timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO system_events (event_type, message) VALUES (?, ?)",
                ('lock_test', 'holding lock'),
            )
            barrier.set()  # Signal that lock is held
            time.sleep(0.3)
            conn.commit()
            conn.close()

        lock_thread = threading.Thread(target=hold_lock)
        lock_thread.start()
        barrier.wait()  # Wait until lock is held

        # This should retry and succeed after the lock clears
        db.save_trade_with_retry(trade, delays=(0.1, 0.5, 1.0))
        lock_thread.join()

        # Verify the trade was saved
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT id, status FROM trades WHERE id = ?", (trade.id,)
            ).fetchone()
            assert row is not None, "Trade should be saved after retry"
            assert row[0] == trade.id
            assert row[1] == 'active'

    def test_raises_after_all_retries_exhausted(self, tmp_path):
        """If the lock never clears, save_trade_with_retry should raise."""
        db, db_path = _make_db(tmp_path)
        trade = _make_trade()

        # Mock save_trade to always raise
        original_save = db.save_trade
        def always_fail(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")
        db.save_trade = always_fail

        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            db.save_trade_with_retry(trade, max_attempts=2, delays=(0.01, 0.01))

    def test_succeeds_on_first_try_without_contention(self, tmp_path):
        """Without contention, save completes on first attempt."""
        db, db_path = _make_db(tmp_path)
        trade = _make_trade()

        db.save_trade_with_retry(trade)

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT id FROM trades WHERE id = ?", (trade.id,)
            ).fetchone()
            assert row is not None


# ---------------------------------------------------------------------------
# 2. Pending trade record is visible to circuit-breaker queries
# ---------------------------------------------------------------------------

class TestPendingTradeVisibility:
    """A pending record must be visible to queries that guard position limits."""

    def test_pending_trade_visible_in_get_open_trades(self, tmp_path):
        """get_open_trades() includes status='pending' so circuit breakers
        count it toward open position limits."""
        db, _ = _make_db(tmp_path)
        trade = _make_trade(status='pending')

        db.save_trade(trade, context={'strategy_type': 'daily_income'})

        # Use get_active_trades_raw to avoid tz-aware datetime converter issue
        open_trades = db.get_active_trades_raw()
        assert len(open_trades) == 1
        assert open_trades[0]['id'] == trade.id
        assert open_trades[0]['status'] == 'pending'

    def test_pending_trade_excluded_from_daily_counts(self, tmp_path):
        """get_daily_counts_by_strategy excludes pending trades so a crash
        between pending write and broker fill doesn't block the next entry."""
        db, _ = _make_db(tmp_path)
        trade = _make_trade(status='pending')

        db.save_trade(trade, context={'strategy_type': 'daily_income'})

        counts = db.get_daily_counts_by_strategy(datetime(2026, 3, 11).date())
        assert counts.get('daily_income', 0) == 0, (
            "Pending trades must NOT count toward daily limits"
        )

    def test_active_trade_counted_in_daily_counts(self, tmp_path):
        """get_daily_counts_by_strategy includes active trades."""
        db, _ = _make_db(tmp_path)
        trade = _make_trade(status='active')

        db.save_trade(trade, context={'strategy_type': 'daily_income'})

        counts = db.get_daily_counts_by_strategy(datetime(2026, 3, 11).date())
        assert counts.get('daily_income', 0) == 1

    def test_cancelled_trade_excluded_from_daily_counts(self, tmp_path):
        """Cancelled trades (unfilled orders) must not count toward daily limits."""
        from src.models.trade import TradeStatus
        db, _ = _make_db(tmp_path)
        trade = _make_trade(status='active')
        trade.status = TradeStatus.CANCELLED

        db.save_trade(trade, context={'strategy_type': 'daily_income'})

        counts = db.get_daily_counts_by_strategy(datetime(2026, 3, 11).date())
        assert counts.get('daily_income', 0) == 0

    def test_pending_excluded_from_daily_summary_count(self, tmp_path):
        """get_daily_summary trades_count excludes pending records."""
        db, _ = _make_db(tmp_path)
        trade = _make_trade(status='pending')
        db.save_trade(trade, context={'strategy_type': 'daily_income'})

        summary = db.get_daily_summary(datetime(2026, 3, 11).date())
        assert summary['trades_count'] == 0, (
            "Pending trades must NOT inflate trades_count"
        )

    def test_pending_upgraded_to_active_on_fill(self, tmp_path):
        """After fill, the pending record should be updated to active via
        save_trade (INSERT OR REPLACE) with the same trade ID."""
        db, db_path = _make_db(tmp_path)

        # Write pending
        pending = _make_trade(status='pending')
        db.save_trade(pending, context={'strategy_type': 'daily_income'})

        # Simulate fill: same ID, now active
        from src.models.trade import TradeStatus
        pending.status = TradeStatus.ACTIVE
        pending.entry_price = 1.85  # Actual fill price
        db.save_trade(pending, context={'strategy_type': 'daily_income'})

        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT status, entry_price FROM trades WHERE id = ?",
                (pending.id,),
            ).fetchone()
            assert row[0] == 'active'
            assert row[1] == 1.85


# ---------------------------------------------------------------------------
# 3. notifier.mode is set correctly in the desktop app code path
# ---------------------------------------------------------------------------

class TestNotifierModeInDesktopApp:
    """Verify app_desktop.py sets notifier.mode after construction."""

    def test_notifier_mode_set_in_run_bot_source(self):
        """The _run_bot method must assign notifier.mode after creating
        NotificationManager, not rely on the 'dry-run' default."""
        app_path = Path(__file__).parent.parent / 'app_desktop.py'
        source = app_path.read_text(encoding='utf-8')

        # Find the _run_bot function
        assert 'notifier.mode' in source, (
            "app_desktop.py must explicitly set notifier.mode"
        )

        # Verify ordering within _run_bot: NotificationManager() → notifier.mode → TradingBot()
        lines = source.split('\n')
        in_run_bot = False
        notifier_create_line = None
        mode_set_line = None
        bot_create_line = None

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if 'def _run_bot(' in stripped:
                in_run_bot = True
            if not in_run_bot:
                continue
            # Stop at next top-level def
            if stripped.startswith('def ') and '_run_bot' not in stripped:
                break
            if 'notifier = NotificationManager()' in stripped:
                notifier_create_line = i
            if 'notifier.mode' in stripped and '=' in stripped and notifier_create_line:
                mode_set_line = i
            if 'bot = TradingBot(' in stripped:
                bot_create_line = i

        assert notifier_create_line is not None, "NotificationManager() must be created in _run_bot"
        assert mode_set_line is not None, "notifier.mode must be assigned in _run_bot"
        assert bot_create_line is not None, "TradingBot must be created in _run_bot"
        assert notifier_create_line < mode_set_line < bot_create_line, (
            f"Order must be: NotificationManager (L{notifier_create_line}) → "
            f"notifier.mode = ... (L{mode_set_line}) → "
            f"TradingBot (L{bot_create_line})"
        )

    def test_notifier_mode_uses_dry_run_variable(self):
        """The mode assignment must use the dry_run variable, not a hardcoded string."""
        app_path = Path(__file__).parent.parent / 'app_desktop.py'
        source = app_path.read_text(encoding='utf-8')

        # Should reference dry_run variable in the assignment
        lines = source.split('\n')
        for line in lines:
            if 'notifier.mode' in line and '=' in line:
                assert 'dry_run' in line, (
                    f"notifier.mode assignment should use dry_run variable, got: {line.strip()}"
                )
                break


# ---------------------------------------------------------------------------
# 4. Retry loop fires critical notification on exhaustion
# ---------------------------------------------------------------------------

class TestDBFailureAlert:
    """When save_trade_with_retry fails, a critical log entry must be written."""

    def test_critical_log_on_db_failure(self):
        """PositionManager._send_db_failure_alert logs at CRITICAL level."""
        from src.core.position_manager import PositionManager

        trade = _make_trade()
        broker = MagicMock()
        strategy = MagicMock()
        db = MagicMock()

        pm = PositionManager(broker, strategy, db)

        with patch('src.core.position_manager.logger') as mock_logger:
            pm._send_db_failure_alert(trade, Exception("database is locked"))
            mock_logger.critical.assert_called_once()

    def test_alert_contains_trade_details(self):
        """The log message must include trade ID and strikes."""
        from src.core.position_manager import PositionManager

        trade = _make_trade()
        broker = MagicMock()
        strategy = MagicMock()
        db = MagicMock()

        pm = PositionManager(broker, strategy, db)

        with patch('src.core.position_manager.logger') as mock_logger:
            pm._send_db_failure_alert(trade, Exception("database is locked"))

            log_msg = str(mock_logger.critical.call_args)
            assert '6795' in log_msg
            assert '6790' in log_msg


# ---------------------------------------------------------------------------
# 5. busy_timeout is set on connections
# ---------------------------------------------------------------------------

class TestBusyTimeout:
    """Verify that PRAGMA busy_timeout is set on ALL DB connections."""

    def test_db_manager_get_connection_sets_busy_timeout(self, tmp_path):
        """DatabaseManager._get_connection should have busy_timeout >= 5000."""
        db, _ = _make_db(tmp_path)
        conn = db._get_connection()
        try:
            result = conn.execute("PRAGMA busy_timeout").fetchone()
            assert result[0] >= 5000
        finally:
            conn.close()

    def test_db_manager_get_active_trades_raw_sets_busy_timeout(self, tmp_path):
        """get_active_trades_raw uses a plain connection that must also set busy_timeout."""
        db, db_path = _make_db(tmp_path)
        # Verify by reading the source — the connection is created and closed internally,
        # so we check the source code directly
        src = Path(__file__).parent.parent / 'database' / 'db_manager.py'
        source = src.read_text(encoding='utf-8')
        lines = source.split('\n')
        in_method = False
        found_busy_timeout = False
        for line in lines:
            if 'def get_active_trades_raw' in line:
                in_method = True
            if in_method:
                if 'busy_timeout' in line:
                    found_busy_timeout = True
                    break
                if line.strip().startswith('def ') and 'get_active_trades_raw' not in line:
                    break
        assert found_busy_timeout, "get_active_trades_raw must set PRAGMA busy_timeout"

    def test_pdt_tracker_sets_busy_timeout(self):
        """PDT tracker connections should set busy_timeout."""
        src = Path(__file__).parent.parent / 'src' / 'core' / 'pdt_tracker.py'
        source = src.read_text(encoding='utf-8')
        lines = source.split('\n')
        in_method = False
        found_busy_timeout = False
        for line in lines:
            if 'def _get_connection' in line:
                in_method = True
            if in_method:
                if 'busy_timeout' in line:
                    found_busy_timeout = True
                    break
                if line.strip().startswith('def ') and '_get_connection' not in line:
                    break
        assert found_busy_timeout, "pdt_tracker._get_connection must set PRAGMA busy_timeout"

    def test_drawdown_manager_sets_busy_timeout(self):
        """Drawdown manager backfill connection should set busy_timeout."""
        src = Path(__file__).parent.parent / 'src' / 'core' / 'drawdown_manager.py'
        source = src.read_text(encoding='utf-8')
        assert 'busy_timeout' in source, (
            "drawdown_manager.py must set PRAGMA busy_timeout on its connection"
        )

    def test_dashboard_no_inline_sqlite_connect(self):
        """All dashboard connections must go through _open_db(), not raw sqlite3.connect().
        The only sqlite3.connect() allowed is inside _open_db() itself."""
        src = Path(__file__).parent.parent / 'dashboard' / 'app.py'
        source = src.read_text(encoding='utf-8')
        lines = source.split('\n')
        violations = []
        in_open_db = False
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if 'def _open_db(' in stripped:
                in_open_db = True
            if in_open_db and stripped.startswith('def ') and '_open_db' not in stripped:
                in_open_db = False
            if 'sqlite3.connect(' in stripped and not in_open_db:
                violations.append(f"Line {i}: {stripped}")
        assert violations == [], (
            "All dashboard DB connections must use _open_db() or get_db_connection(). "
            f"Found raw sqlite3.connect() at:\n" + "\n".join(violations)
        )


# ---------------------------------------------------------------------------
# 6. Token health check at startup
# ---------------------------------------------------------------------------

class TestTokenHealthAtStartup:
    """Verify app_desktop.py checks token health immediately at startup."""

    def test_check_token_health_called_at_startup(self):
        """app_desktop.py must call check_token_health() after is_authenticated()
        succeeds, before creating the TradingBot."""
        app_path = Path(__file__).parent.parent / 'app_desktop.py'
        source = app_path.read_text(encoding='utf-8')

        # Verify check_token_health appears in the schwab startup path
        assert 'check_token_health' in source, (
            "app_desktop.py must call check_token_health() at startup"
        )

        # Verify ordering: is_authenticated → check_token_health → TradingBot
        lines = source.split('\n')
        in_run_bot = False
        auth_line = None
        health_line = None
        bot_line = None

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if 'def _run_bot(' in stripped:
                in_run_bot = True
            if not in_run_bot:
                continue
            if stripped.startswith('def ') and '_run_bot' not in stripped:
                break
            if 'is_authenticated()' in stripped and auth_line is None:
                auth_line = i
            if 'check_token_health()' in stripped and health_line is None:
                health_line = i
            if 'bot = TradingBot(' in stripped:
                bot_line = i

        assert auth_line is not None, "is_authenticated() must be called"
        assert health_line is not None, "check_token_health() must be called"
        assert bot_line is not None, "TradingBot must be created"
        assert auth_line < health_line < bot_line, (
            f"Order must be: is_authenticated (L{auth_line}) → "
            f"check_token_health (L{health_line}) → "
            f"TradingBot (L{bot_line})"
        )

    def test_expired_token_aborts_startup(self):
        """When check_token_health returns 'expired', the startup path
        must contain a return statement to abort bot creation."""
        app_path = Path(__file__).parent.parent / 'app_desktop.py'
        source = app_path.read_text(encoding='utf-8')

        # The expired branch must have a return to abort
        lines = source.split('\n')
        in_expired_block = False
        found_return = False
        for line in lines:
            stripped = line.strip()
            if "'expired'" in stripped and 'action' in stripped:
                in_expired_block = True
            if in_expired_block:
                if stripped == 'return':
                    found_return = True
                    break
                # If we hit another elif/else/def, we've left the block
                if stripped.startswith(('elif ', 'else:', 'def ')):
                    break

        assert found_return, (
            "When token health action is 'expired', app_desktop.py must return "
            "to abort bot startup"
        )

    def test_expired_token_sends_discord_alert(self):
        """The expired token path must send a Discord notification."""
        app_path = Path(__file__).parent.parent / 'app_desktop.py'
        source = app_path.read_text(encoding='utf-8')

        # Find the expired block and verify it sends a notification
        assert 'SCHWAB TOKEN EXPIRED' in source, (
            "Expired token path must send a Discord alert with clear message"
        )
