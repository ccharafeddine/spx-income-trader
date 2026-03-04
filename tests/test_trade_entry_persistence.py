"""
Tests that trades are persisted to the DB at entry time (not just at exit),
so a bot crash mid-trade leaves a recoverable 'active' record.

Also tests that dashboard-resolved expired trades appear on the chart
(status='expired' included in chart query alongside 'closed').
"""

import sys
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime

import pytest
import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

ET = pytz.timezone('America/New_York')


def _make_pm_with_real_db(tmp_path):
    """Create a PositionManager wired to a real SQLite database."""
    from database.db_manager import DatabaseManager
    from src.core.position_manager import PositionManager
    from src.models.spread import CreditSpread, OptionLeg, TradeDirection

    db_path = str(tmp_path / "test_trades.db")
    db = DatabaseManager(db_path)

    broker = MagicMock()
    broker.place_spread_order.return_value = "order-crash-test-1"
    broker.get_order_status.return_value = {
        'status': 'filled', 'fill_price': 2.50,
    }

    strategy = MagicMock()
    strategy.classify_credit_quality.return_value = {
        'quality': 'GOOD', 'credit_received': 2.50, 'moneyness': 'ATM',
        'expected_range': '$2.40-$2.60', 'expected_min': 2.40,
        'expected_max': 2.60, 'pct_of_expected_min': 104.2,
        'is_simulated_concern': False,
    }

    pm = PositionManager(broker=broker, strategy=strategy, db_manager=db)

    now = ET.localize(datetime(2026, 3, 3, 10, 0))
    spread = CreditSpread(
        direction=TradeDirection.BEARISH,
        short_leg=OptionLeg(strike=6730, option_type='call', action='sell', price=5.00),
        long_leg=OptionLeg(strike=6735, option_type='call', action='buy', price=3.00),
        credit_received=2.00,
        entry_time=now,
        expiration=now.replace(hour=16, minute=0),
        underlying_price_at_entry=6732.0,
    )

    bar = MagicMock()
    bar.timestamp = now
    bar.open = 6740.0
    bar.high = 6750.0
    bar.low = 6720.0
    bar.close = 6732.0
    bar.range = 30.0
    bar.close_percentage_in_range.return_value = 40.0

    return pm, spread, bar, db, db_path


class TestTradeEntryPersistence:
    """Verify save_trade() is called at entry, creating a DB record immediately."""

    def test_entry_creates_active_db_record(self, tmp_path):
        """A trade that opens must exist in the DB with status='active'
        even if the bot crashes before exit (no update_trade_close called)."""
        pm, spread, bar, db, db_path = _make_pm_with_real_db(tmp_path)

        with patch('time.sleep'):
            trade = pm.enter_trade(spread, bar, quantity=1)

        assert trade is not None, "enter_trade should return a Trade object"

        # Simulate bot crash: do NOT call update_trade_close or _exit_trade.
        # The trade should already exist in the DB from save_trade() at entry.
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM trades WHERE id = ?", (trade.id,)
        ).fetchone()
        conn.close()

        assert row is not None, "Trade must exist in DB after entry (before any exit)"
        assert row['status'] == 'active', (
            f"Trade status should be 'active' at entry, got '{row['status']}'"
        )

    def test_entry_record_has_entry_fields(self, tmp_path):
        """The entry record should have all entry fields populated."""
        pm, spread, bar, db, db_path = _make_pm_with_real_db(tmp_path)

        with patch('time.sleep'):
            trade = pm.enter_trade(spread, bar, quantity=1)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM trades WHERE id = ?", (trade.id,)
        ).fetchone()
        conn.close()

        assert row['entry_time'] is not None
        assert row['entry_price'] == 2.50
        assert row['short_strike'] == 6730.0
        assert row['long_strike'] == 6735.0
        assert row['direction'] == 'bearish'
        assert row['underlying_price_at_entry'] == 6732.0

    def test_entry_record_has_null_exit_fields(self, tmp_path):
        """At entry time, exit fields should be NULL (trade is still open)."""
        pm, spread, bar, db, db_path = _make_pm_with_real_db(tmp_path)

        with patch('time.sleep'):
            trade = pm.enter_trade(spread, bar, quantity=1)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM trades WHERE id = ?", (trade.id,)
        ).fetchone()
        conn.close()

        assert row['exit_time'] is None
        assert row['exit_price'] is None
        assert row['exit_reason'] is None
        assert row['pnl'] is None

    def test_crash_scenario_trade_survives(self, tmp_path):
        """Full crash simulation: open trade, 'crash' (abandon PM), verify
        a fresh DB connection still finds the active trade."""
        pm, spread, bar, db, db_path = _make_pm_with_real_db(tmp_path)

        with patch('time.sleep'):
            trade = pm.enter_trade(spread, bar, quantity=1)

        trade_id = trade.id

        # Simulate crash: delete all references to PM and DB
        del pm
        del db

        # A new process starts: connect to the same DB
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, status, entry_time, short_strike, long_strike, "
            "exit_time, pnl FROM trades WHERE id = ?",
            (trade_id,)
        ).fetchone()
        conn.close()

        assert row is not None, "Trade record must survive process crash"
        assert row['status'] == 'active'
        assert row['exit_time'] is None
        assert row['pnl'] is None


class TestDashboardExpiredTradeResolution:
    """Verify _resolve_expired_trade sets spx_at_exit and uses 'expired' status."""

    def test_resolve_sets_spx_at_exit(self):
        """Dashboard resolver should record the SPX price used to calculate P&L."""
        from dashboard.app import _resolve_expired_trade

        trade = {
            'short_strike': 6730.0, 'long_strike': 6735.0,
            'direction': 'bearish', 'credit_received': 2.00,
            'quantity': 1, 'expiration': '2026-03-03 16:00:00-05:00',
        }

        _resolve_expired_trade(trade, spx_price=6800.0)

        assert trade['status'] == 'expired'
        assert trade['spx_at_exit'] == 6800.0
        assert trade['exit_reason'] == 'Expired at close (dashboard-resolved)'

    def test_resolve_no_spx_price_skips_spx_at_exit(self):
        """When SPX price is unavailable, spx_at_exit should not be set."""
        from dashboard.app import _resolve_expired_trade

        trade = {
            'short_strike': 6730.0, 'long_strike': 6735.0,
            'direction': 'bearish', 'credit_received': 2.00,
            'quantity': 1, 'expiration': '2026-03-03 16:00:00-05:00',
        }

        _resolve_expired_trade(trade, spx_price=None)

        assert trade['status'] == 'expired'
        assert 'spx_at_exit' not in trade


class TestChartQueryIncludesExpired:
    """Verify the chart trades API returns both 'closed' and 'expired' trades."""

    def test_chart_query_includes_expired_status(self):
        """The SQL in api_chart_trades must match status IN ('closed', 'expired')."""
        import inspect
        from dashboard.app import api_chart_trades

        source = inspect.getsource(api_chart_trades)
        assert "'expired'" in source, (
            "api_chart_trades query must include status='expired' "
            "to show dashboard-resolved trades on the chart"
        )
        assert "'closed'" in source, (
            "api_chart_trades query must include status='closed'"
        )
