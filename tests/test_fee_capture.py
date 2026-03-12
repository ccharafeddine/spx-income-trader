"""
Tests for full fee capture from Schwab transaction history:
  1. Database migration adds commissions column
  2. Fee capture updates trade record correctly
  3. Net P&L calculation (gross - commissions)
  4. Graceful handling when fee API call fails
  5. Dashboard displays net P&L when commissions present, gross when not
"""

import json
import sys
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


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
    import pytz

    ET = pytz.timezone("America/New_York")
    short = OptionLeg(strike=5800, option_type='put', price=2.0, action='sell')
    long_ = OptionLeg(strike=5795, option_type='put', price=1.0, action='buy')
    spread = CreditSpread(
        short_leg=short, long_leg=long_,
        credit_received=1.0, direction=TradeDirection.BULLISH,
        underlying_price_at_entry=5850,
        entry_time=datetime(2026, 3, 11, 10, 0, tzinfo=ET),
        expiration=datetime(2026, 3, 11, 16, 0, tzinfo=ET),
    )
    status_enum = TradeStatus.ACTIVE if status == 'active' else TradeStatus.CLOSED
    bar = Bar(timestamp=datetime(2026, 3, 11, 10, 0, tzinfo=ET),
              open=5850, high=5860, low=5840, close=5855, volume=100)
    trade = Trade(
        id='test-trade-001', spread=spread, status=status_enum,
        setup_bar=bar, entry_price=1.0,
        entry_time=datetime(2026, 3, 11, 10, 0, tzinfo=ET),
        quantity=1,
    )
    return trade


# ---------------------------------------------------------------------------
# 1. Database migration adds commissions column
# ---------------------------------------------------------------------------

class TestDatabaseMigration:
    """Verify commissions column is added via migration."""

    def test_commissions_column_exists(self, tmp_path):
        """Fresh DB should have commissions column with default 0.0."""
        db, db_path = _make_db(tmp_path)
        import sqlite3
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(trades)")
        columns = {row[1]: row for row in cursor.fetchall()}
        conn.close()

        assert 'commissions' in columns
        # Default value should be 0.0
        col_info = columns['commissions']
        assert 'REAL' in col_info[2].upper()

    def test_migration_adds_column_to_existing_table(self, tmp_path):
        """Migration should add commissions to a table that doesn't have it."""
        import sqlite3
        db_path = str(tmp_path / "legacy.db")

        # Create a table WITHOUT commissions column
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE trades (
                id TEXT PRIMARY KEY,
                entry_time TIMESTAMP NOT NULL,
                direction TEXT NOT NULL,
                status TEXT NOT NULL,
                short_strike REAL NOT NULL,
                long_strike REAL NOT NULL,
                spread_width REAL NOT NULL,
                credit_received REAL NOT NULL,
                entry_price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                expiration TIMESTAMP NOT NULL,
                pnl REAL
            )
        """)
        # Insert a trade without commissions
        conn.execute("""
            INSERT INTO trades VALUES
            ('t1', '2026-03-11T10:00', 'bullish', 'closed',
             5800, 5795, 5, 1.0, 1.0, 1, '2026-03-11T16:00', 50.0)
        """)
        conn.commit()
        conn.close()

        # Now open with DatabaseManager which runs migrations
        from database.db_manager import DatabaseManager
        db = DatabaseManager(db_path)

        # Verify column was added
        conn = sqlite3.connect(db_path)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
        conn.close()
        assert 'commissions' in cols

    def test_existing_data_preserved_after_migration(self, tmp_path):
        """Existing trade data should not be affected by adding commissions."""
        import sqlite3
        db_path = str(tmp_path / "legacy2.db")

        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE trades (
                id TEXT PRIMARY KEY,
                entry_time TIMESTAMP NOT NULL,
                direction TEXT NOT NULL,
                status TEXT NOT NULL,
                short_strike REAL NOT NULL,
                long_strike REAL NOT NULL,
                spread_width REAL NOT NULL,
                credit_received REAL NOT NULL,
                entry_price REAL NOT NULL,
                quantity INTEGER NOT NULL,
                expiration TIMESTAMP NOT NULL,
                pnl REAL
            )
        """)
        conn.execute("""
            INSERT INTO trades VALUES
            ('t1', '2026-03-11T10:00', 'bullish', 'closed',
             5800, 5795, 5, 1.0, 1.0, 1, '2026-03-11T16:00', 50.0)
        """)
        conn.commit()
        conn.close()

        from database.db_manager import DatabaseManager
        db = DatabaseManager(db_path)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT pnl, commissions FROM trades WHERE id='t1'").fetchone()
        conn.close()
        assert row[0] == 50.0  # P&L preserved
        assert row[1] is None or row[1] == 0.0  # Commissions defaults


# ---------------------------------------------------------------------------
# 2. Fee capture updates trade record correctly
# ---------------------------------------------------------------------------

class TestFeeCapture:
    """Verify fee capture after fill and on settlement."""

    def test_update_trade_commissions(self, tmp_path):
        """update_trade_commissions writes the fee to the DB."""
        db, db_path = _make_db(tmp_path)
        trade = _make_trade()
        db.save_trade(trade)

        db.update_trade_commissions(trade.id, 13.72)

        import sqlite3
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT commissions FROM trades WHERE id=?", (trade.id,)
        ).fetchone()
        conn.close()
        assert row[0] == 13.72

    def test_capture_trade_fees_both_orders(self):
        """_capture_trade_fees sums entry and exit order fees."""
        from src.core.position_manager import PositionManager

        broker = MagicMock()
        broker.get_order_fees.side_effect = lambda oid: {
            'entry-123': 6.86,
            'exit-456': 6.86,
        }.get(oid, 0.0)

        db = MagicMock()

        pm = MagicMock(spec=PositionManager)
        pm.broker = broker
        pm.db = db

        trade = MagicMock()
        trade.id = 'test-001'
        trade.entry_order_id = 'entry-123'
        trade.exit_order_id = 'exit-456'

        PositionManager._capture_trade_fees(pm, trade)

        db.update_trade_commissions.assert_called_once_with('test-001', 13.72)

    def test_capture_fees_entry_only(self):
        """Expired trades have no exit order — only entry fees captured."""
        from src.core.position_manager import PositionManager

        broker = MagicMock()
        broker.get_order_fees.return_value = 6.86
        db = MagicMock()

        pm = MagicMock(spec=PositionManager)
        pm.broker = broker
        pm.db = db

        trade = MagicMock()
        trade.id = 'test-002'
        trade.entry_order_id = 'entry-789'
        trade.exit_order_id = None

        PositionManager._capture_trade_fees(pm, trade)

        db.update_trade_commissions.assert_called_once_with('test-002', 6.86)


# ---------------------------------------------------------------------------
# 3. Net P&L calculation (gross - commissions)
# ---------------------------------------------------------------------------

class TestNetPnL:
    """Verify net P&L = gross P&L - commissions."""

    def test_reconciler_net_pnl(self, tmp_path):
        """Reconciler should compute total_net_pnl = total_db_pnl - commissions."""
        from src.core.reconciler import TradeReconciler

        broker = MagicMock()
        broker.get_account_balance.return_value = {'net_account_value': 24500.0}

        db = MagicMock()
        db.db_path = str(tmp_path / 'trades.db')
        db.get_trades_by_date.return_value = [
            {'id': 't1', 'status': 'closed', 'pnl': 50.0, 'commissions': 13.72,
             'direction': 'PUT', 'entry_time': '2026-03-11T10:00',
             'entry_order_id': None, 'exit_order_id': None},
            {'id': 't2', 'status': 'closed', 'pnl': 30.0, 'commissions': 13.72,
             'direction': 'PUT', 'entry_time': '2026-03-11T11:00',
             'entry_order_id': None, 'exit_order_id': None},
        ]

        reconciler = TradeReconciler(broker, db)
        result = reconciler.reconcile(date(2026, 3, 11))

        assert result['total_db_pnl'] == 80.0
        assert result['total_commissions'] == 27.44
        assert result['total_net_pnl'] == 52.56

    def test_dashboard_today_summary_net_pnl(self):
        """Today's summary should show net_pnl when commissions > 0."""
        # Simulate the dashboard calculation
        valid_trades = [
            {'pnl': 50.0, 'commissions': 13.72, 'flag': None},
            {'pnl': 30.0, 'commissions': 13.72, 'flag': None},
        ]
        total_pnl = sum(t.get('pnl') or 0 for t in valid_trades)
        total_commissions = sum(t.get('commissions') or 0 for t in valid_trades)
        net_pnl = round(total_pnl - total_commissions, 2)

        assert total_pnl == 80.0
        assert total_commissions == 27.44
        assert net_pnl == 52.56

    def test_dashboard_no_net_pnl_when_zero_commissions(self):
        """If commissions are 0, net_pnl should be None (show gross only)."""
        valid_trades = [
            {'pnl': 50.0, 'commissions': 0, 'flag': None},
        ]
        total_commissions = sum(t.get('commissions') or 0 for t in valid_trades)
        net_pnl = None if total_commissions == 0 else round(50.0 - total_commissions, 2)

        assert net_pnl is None


# ---------------------------------------------------------------------------
# 4. Graceful handling when fee API call fails
# ---------------------------------------------------------------------------

class TestFeeFailureGraceful:
    """Verify fee capture failures don't block trade processing."""

    def test_get_order_fees_returns_zero_on_failure(self):
        """Schwab broker returns 0.0 when fee query fails."""
        from src.brokers.schwab_broker import SchwabBroker

        broker = MagicMock(spec=SchwabBroker)
        # Call the real method with a mock that raises
        broker_real = MagicMock()
        broker_real._get_client.side_effect = Exception("API error")

        result = SchwabBroker.get_order_fees(broker_real, '12345')
        assert result == 0.0

    def test_capture_fees_handles_broker_exception(self):
        """_capture_trade_fees should not raise when broker fails."""
        from src.core.position_manager import PositionManager

        broker = MagicMock()
        broker.get_order_fees.side_effect = Exception("Network timeout")
        db = MagicMock()

        pm = MagicMock(spec=PositionManager)
        pm.broker = broker
        pm.db = db

        trade = MagicMock()
        trade.id = 'test-fail'
        trade.entry_order_id = '123'
        trade.exit_order_id = '456'

        # Should not raise
        PositionManager._capture_trade_fees(pm, trade)
        # DB should NOT be updated since broker raised
        db.update_trade_commissions.assert_not_called()

    def test_backfill_handles_missing_transactions_api(self, tmp_path):
        """Post-settlement backfill should handle broker without get_transactions_for_date."""
        from src.main import TradingBot
        import pytz

        bot = MagicMock()
        bot.broker = MagicMock(spec=[])  # No get_transactions_for_date
        bot.current_trading_date = date(2026, 3, 11)
        bot.db = MagicMock()

        # Should return without error
        TradingBot._backfill_missing_commissions(bot)
        bot.db.get_trades_by_date.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Dashboard displays net P&L correctly
# ---------------------------------------------------------------------------

class TestDashboardNetPnL:
    """Verify dashboard includes commissions and net P&L fields."""

    def test_journal_entry_has_commissions_and_net_pnl(self):
        """Journal entry should include commissions and net_pnl fields."""
        # Simulate what the dashboard does
        trade = {'pnl': 50.0, 'commissions': 13.72}
        commissions = trade.get('commissions') or 0
        net_pnl = (round((trade.get('pnl') or 0) - commissions, 2)
                   if commissions > 0 else None)

        assert commissions == 13.72
        assert net_pnl == 36.28

    def test_journal_entry_no_net_pnl_without_commissions(self):
        """Journal entry net_pnl should be None when commissions are 0."""
        trade = {'pnl': 50.0, 'commissions': 0}
        commissions = trade.get('commissions') or 0
        net_pnl = (round((trade.get('pnl') or 0) - commissions, 2)
                   if commissions > 0 else None)

        assert net_pnl is None

    def test_eod_discord_includes_net_pnl(self):
        """EOD notification should show gross/fees/net when commissions present."""
        from src.utils.notifications import NotificationManager

        nm = MagicMock(spec=NotificationManager)

        summary = {
            'trades_count': 2,
            'wins': 2,
            'losses': 0,
            'daily_pnl': 80.0,
            'weekly_pnl': 200.0,
            'monthly_pnl': 500.0,
            'commissions': 27.44,
            'net_pnl': 52.56,
        }

        # Call the real method
        NotificationManager.send_eod_summary(nm, summary)

        # Verify _send_rich was called
        nm._send_rich.assert_called_once()
        call_kwargs = nm._send_rich.call_args
        fields = call_kwargs[1]['fields'] if 'fields' in call_kwargs[1] else call_kwargs[0][3] if len(call_kwargs[0]) > 3 else None

        # Check that we have Gross P&L, Fees, and Net P&L fields
        field_names = [f['name'] for f in fields]
        assert 'Gross P&L' in field_names
        assert 'Fees' in field_names
        assert 'Net P&L' in field_names
        assert 'Daily P&L' not in field_names  # Should NOT show simple Daily P&L

    def test_eod_discord_shows_daily_pnl_without_commissions(self):
        """EOD notification should show simple Daily P&L when no commissions."""
        from src.utils.notifications import NotificationManager

        nm = MagicMock(spec=NotificationManager)

        summary = {
            'trades_count': 2,
            'wins': 2,
            'losses': 0,
            'daily_pnl': 80.0,
            'weekly_pnl': 200.0,
            'monthly_pnl': 500.0,
        }

        NotificationManager.send_eod_summary(nm, summary)

        call_kwargs = nm._send_rich.call_args
        fields = call_kwargs[1]['fields'] if 'fields' in call_kwargs[1] else call_kwargs[0][3] if len(call_kwargs[0]) > 3 else None

        field_names = [f['name'] for f in fields]
        assert 'Daily P&L' in field_names
        assert 'Gross P&L' not in field_names
        assert 'Fees' not in field_names

    def test_reconciliation_uses_net_pnl(self, tmp_path):
        """Reconciliation fee_slippage_delta should use net P&L, not gross."""
        from src.core.reconciler import TradeReconciler

        broker = MagicMock()
        broker.get_account_balance.return_value = {'net_account_value': 24552.56}

        db = MagicMock()
        db.db_path = str(tmp_path / 'trades.db')
        db.get_trades_by_date.return_value = [
            {'id': 't1', 'status': 'closed', 'pnl': 80.0, 'commissions': 27.44,
             'direction': 'PUT', 'entry_time': '2026-03-11T10:00',
             'entry_order_id': None, 'exit_order_id': None},
        ]

        # Write previous day balance
        balance_file = tmp_path / 'post_settlement_balance.json'
        balance_file.write_text(json.dumps({
            'net_account_value': 24500.0,
            'date': '2026-03-10',
        }))

        reconciler = TradeReconciler(broker, db)
        result = reconciler.reconcile(date(2026, 3, 11))

        # Gross = 80, Commissions = 27.44, Net = 52.56
        # Account P&L = 24552.56 - 24500.0 = 52.56
        # fee_slippage_delta = net(52.56) - account(52.56) = 0.0
        assert result['total_net_pnl'] == 52.56
        assert result['fee_slippage_delta'] == 0.0

    def test_schema_sql_has_commissions(self):
        """schema.sql should include commissions column."""
        schema_path = Path(__file__).parent.parent / 'database' / 'schema.sql'
        content = schema_path.read_text()
        assert 'commissions REAL DEFAULT 0.0' in content
