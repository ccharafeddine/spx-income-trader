"""
Regression tests for broker P&L reconciliation from Schwab transaction amounts.

Verifies:
- broker_pnl is calculated from summed netAmount fields
- gross_pnl and commissions (fees) fields are populated correctly
- Falls back to price-based calc if transaction fetch fails
- All 4 legs are matched before accepting the broker_pnl value
- Retry logic fires if fewer than 4 legs returned on first fetch
"""

import sqlite3
import threading
import time
from datetime import datetime, date
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytz
import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ET = pytz.timezone('US/Eastern')


def _make_db(tmp_path):
    """Create a temporary database with the trades schema."""
    db_path = str(tmp_path / 'test.db')
    schema_file = Path(__file__).parent.parent / 'database' / 'schema.sql'
    conn = sqlite3.connect(db_path)
    conn.executescript(schema_file.read_text())
    conn.close()
    return db_path


def _insert_trade(db_path, trade_id='t1', pnl=420.0, entry_order_id='100',
                   exit_order_id='200', commissions=0.0, gross_pnl=None):
    """Insert a test trade into the database."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO trades (id, entry_time, direction, status, short_strike, "
        "long_strike, spread_width, credit_received, entry_price, quantity, "
        "expiration, pnl, commissions, entry_order_id, exit_order_id, gross_pnl) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (trade_id, '2026-03-13 10:00:00', 'bearish', 'closed',
         6660.0, 6665.0, 5.0, 2.50, 2.50, 2, '2026-03-13',
         pnl, commissions, entry_order_id, exit_order_id, gross_pnl),
    )
    conn.commit()
    conn.close()


def _read_trade(db_path, trade_id='t1'):
    """Read a trade from the database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _make_transactions(entry_order_id='100', exit_order_id='200'):
    """Create 4 mock Schwab transaction legs matching a spread trade."""
    return [
        # Entry: Sell to Open short leg
        {'order_id': entry_order_id, 'net_amount': 4477.56,
         'transaction_id': '1', 'type': 'TRADE', 'description': 'Sell to Open',
         'fees': 2.44},
        # Entry: Buy to Open long leg
        {'order_id': entry_order_id, 'net_amount': -3972.44,
         'transaction_id': '2', 'type': 'TRADE', 'description': 'Buy to Open',
         'fees': 2.44},
        # Exit: Buy to Close short leg
        {'order_id': exit_order_id, 'net_amount': -210.44,
         'transaction_id': '3', 'type': 'TRADE', 'description': 'Buy to Close',
         'fees': 0.44},
        # Exit: Sell to Close long leg
        {'order_id': exit_order_id, 'net_amount': 115.74,
         'transaction_id': '4', 'type': 'TRADE', 'description': 'Sell to Close',
         'fees': 0.26},
    ]


# ---------------------------------------------------------------------------
# Test: update_trade_broker_pnl database method
# ---------------------------------------------------------------------------

class TestUpdateTradeBrokerPnl:
    """Test the DB method that writes reconciled P&L."""

    def test_writes_broker_pnl_as_canonical(self, tmp_path):
        """pnl field should contain the broker net amount."""
        from database.db_manager import DatabaseManager
        db = DatabaseManager(str(tmp_path / 'test.db'))
        _insert_trade(db.db_path, pnl=420.0)

        db.update_trade_broker_pnl('t1', broker_pnl=410.42, gross_pnl=420.0,
                                   commissions=9.58)

        trade = _read_trade(db.db_path)
        assert trade['pnl'] == 410.42
        assert trade['gross_pnl'] == 420.0
        assert trade['commissions'] == 9.58

    def test_pnl_percent_recalculated(self, tmp_path):
        """pnl_percent should be based on broker_pnl, not gross."""
        from database.db_manager import DatabaseManager
        db = DatabaseManager(str(tmp_path / 'test.db'))
        _insert_trade(db.db_path, pnl=420.0)

        db.update_trade_broker_pnl('t1', broker_pnl=410.42, gross_pnl=420.0,
                                   commissions=9.58)

        trade = _read_trade(db.db_path)
        # credit_received=2.50, quantity=2 → max_profit=500
        expected_pct = round(410.42 / 500.0 * 100, 2)
        assert trade['pnl_percent'] == expected_pct


# ---------------------------------------------------------------------------
# Test: broker_pnl calculation from summed netAmounts
# ---------------------------------------------------------------------------

class TestBrokerPnlCalculation:
    """Test that broker_pnl = sum(netAmount) for matched legs."""

    def test_sum_of_four_legs(self):
        """Summing 4 leg netAmounts gives correct broker P&L."""
        txns = _make_transactions()
        broker_pnl = round(sum(t['net_amount'] for t in txns), 2)
        assert broker_pnl == 410.42

    def test_fees_derived_correctly(self):
        """fees = gross_pnl - broker_pnl."""
        gross_pnl = 420.0
        broker_pnl = 410.42
        fees = round(gross_pnl - broker_pnl, 2)
        assert fees == 9.58


# ---------------------------------------------------------------------------
# Test: reconciliation worker logic
# ---------------------------------------------------------------------------

class TestReconcileBrokerPnlWorker:
    """Test the background worker that fetches transactions and updates DB."""

    def _make_position_manager(self, tmp_path, transactions=None):
        """Build a minimal PositionManager with mocked broker and real DB."""
        from database.db_manager import DatabaseManager

        db = DatabaseManager(str(tmp_path / 'test.db'))
        _insert_trade(db.db_path, pnl=420.0, entry_order_id='100',
                      exit_order_id='200')

        broker = MagicMock()
        if transactions is not None:
            broker.get_transactions_for_date.return_value = transactions
        else:
            broker.get_transactions_for_date.return_value = _make_transactions()

        pm = MagicMock()
        pm.broker = broker
        pm.db = db
        pm.tz = ET

        return pm

    def test_successful_reconciliation(self, tmp_path):
        """Worker should update DB with broker P&L when 4 legs matched."""
        from src.core.position_manager import PositionManager

        pm = self._make_position_manager(tmp_path)

        # Call the worker directly (skip sleep by patching time.sleep)
        with patch('time.sleep'):
            PositionManager._reconcile_broker_pnl_worker(
                pm, 't1', '100', '200', 420.0
            )

        trade = _read_trade(pm.db.db_path)
        assert trade['pnl'] == 410.42
        assert trade['gross_pnl'] == 420.0
        assert trade['commissions'] == 9.58

    def test_fallback_on_fetch_failure(self, tmp_path):
        """Worker should keep price-based P&L if broker fetch fails."""
        from src.core.position_manager import PositionManager

        pm = self._make_position_manager(tmp_path, transactions=[])

        with patch('time.sleep'):
            PositionManager._reconcile_broker_pnl_worker(
                pm, 't1', '100', '200', 420.0
            )

        trade = _read_trade(pm.db.db_path)
        # Fallback: pnl stays as gross, gross_pnl also set to gross, commissions=0
        assert trade['pnl'] == 420.0
        assert trade['gross_pnl'] == 420.0
        assert trade['commissions'] == 0.0

    def test_requires_4_legs(self, tmp_path):
        """Worker should not accept fewer than 4 matched legs."""
        from src.core.position_manager import PositionManager

        # Only 2 entry legs, missing exit legs
        partial = _make_transactions()[:2]
        pm = self._make_position_manager(tmp_path, transactions=partial)

        with patch('time.sleep'):
            PositionManager._reconcile_broker_pnl_worker(
                pm, 't1', '100', '200', 420.0
            )

        trade = _read_trade(pm.db.db_path)
        # Fallback: keeps gross P&L
        assert trade['pnl'] == 420.0
        assert trade['gross_pnl'] == 420.0

    def test_retry_on_partial_legs(self, tmp_path):
        """Worker should retry once after 15s if first fetch returns < 4 legs."""
        from src.core.position_manager import PositionManager

        partial = _make_transactions()[:2]
        full = _make_transactions()

        pm = self._make_position_manager(tmp_path)
        # First call returns partial, second returns full
        pm.broker.get_transactions_for_date.side_effect = [partial, full]

        sleep_calls = []
        with patch('time.sleep', side_effect=lambda s: sleep_calls.append(s)):
            PositionManager._reconcile_broker_pnl_worker(
                pm, 't1', '100', '200', 420.0
            )

        # Should have slept twice: 8s initial + 15s retry
        assert len(sleep_calls) == 2
        assert sleep_calls[0] == 8
        assert sleep_calls[1] == 15

        # Should have succeeded on second attempt
        trade = _read_trade(pm.db.db_path)
        assert trade['pnl'] == 410.42
        assert trade['gross_pnl'] == 420.0

    def test_no_order_ids_skips(self, tmp_path):
        """Worker should skip if no order IDs are available."""
        from src.core.position_manager import PositionManager

        pm = self._make_position_manager(tmp_path)

        with patch('time.sleep'):
            PositionManager._reconcile_broker_pnl_worker(
                pm, 't1', None, None, 420.0
            )

        # broker should not have been called
        pm.broker.get_transactions_for_date.assert_not_called()

    def test_spawns_background_thread(self, tmp_path):
        """_reconcile_broker_pnl should spawn a daemon thread."""
        from src.core.position_manager import PositionManager

        pm = self._make_position_manager(tmp_path)

        trade = MagicMock()
        trade.id = 't1'
        trade.entry_order_id = '100'
        trade.exit_order_id = '200'
        trade.pnl = 420.0

        with patch('threading.Thread') as mock_thread:
            mock_instance = MagicMock()
            mock_thread.return_value = mock_instance
            PositionManager._reconcile_broker_pnl(pm, trade)

            mock_thread.assert_called_once()
            assert mock_thread.call_args[1]['daemon'] is True
            mock_instance.start.assert_called_once()

    def test_exception_in_fetch_does_not_crash(self, tmp_path):
        """Worker should handle broker exceptions gracefully."""
        from src.core.position_manager import PositionManager

        pm = self._make_position_manager(tmp_path)
        pm.broker.get_transactions_for_date.side_effect = Exception("API error")

        with patch('time.sleep'):
            # Should not raise
            PositionManager._reconcile_broker_pnl_worker(
                pm, 't1', '100', '200', 420.0
            )

        trade = _read_trade(pm.db.db_path)
        # Fallback path writes gross_pnl = gross, pnl = gross, commissions = 0
        assert trade['pnl'] == 420.0
        assert trade['gross_pnl'] == 420.0


# ---------------------------------------------------------------------------
# Test: downstream _trade_net_pnl helper
# ---------------------------------------------------------------------------

class TestTradeNetPnlHelper:
    """Test the helper that computes net P&L for both old and new trades."""

    def test_reconciled_trade(self):
        """For reconciled trades (gross_pnl set), pnl is already net."""
        from dashboard.app import _trade_net_pnl
        trade = {'pnl': 410.42, 'gross_pnl': 420.0, 'commissions': 9.58}
        assert _trade_net_pnl(trade) == 410.42

    def test_legacy_trade_with_commissions(self):
        """For legacy trades, net = pnl - commissions."""
        from dashboard.app import _trade_net_pnl
        trade = {'pnl': 420.0, 'gross_pnl': None, 'commissions': 9.58}
        assert _trade_net_pnl(trade) == 420.0 - 9.58

    def test_legacy_trade_no_commissions(self):
        """For legacy trades without commissions, net = pnl."""
        from dashboard.app import _trade_net_pnl
        trade = {'pnl': 420.0, 'gross_pnl': None, 'commissions': 0}
        assert _trade_net_pnl(trade) == 420.0

    def test_missing_gross_pnl_key(self):
        """Trade dict without gross_pnl key should use legacy path."""
        from dashboard.app import _trade_net_pnl
        trade = {'pnl': 420.0, 'commissions': 5.0}
        assert _trade_net_pnl(trade) == 415.0


# ---------------------------------------------------------------------------
# Test: schema has gross_pnl column
# ---------------------------------------------------------------------------

class TestSchema:
    """Verify database schema changes."""

    def test_gross_pnl_in_schema(self):
        schema = (Path(__file__).parent.parent / 'database' / 'schema.sql').read_text()
        assert 'gross_pnl REAL' in schema

    def test_gross_pnl_migration(self, tmp_path):
        """DatabaseManager should add gross_pnl column if missing."""
        from database.db_manager import DatabaseManager
        db = DatabaseManager(str(tmp_path / 'test.db'))
        conn = sqlite3.connect(db.db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
        conn.close()
        assert 'gross_pnl' in cols
