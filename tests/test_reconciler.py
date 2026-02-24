"""
Tests for TradeReconciler - P&L reconciliation between DB and broker.
"""

import sys
from pathlib import Path
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.reconciler import TradeReconciler


def _make_trade(**overrides):
    """Build a fixture trade dict matching the DB schema."""
    defaults = {
        'id': 'trade-001',
        'entry_time': '2026-02-20 10:05:00',
        'exit_time': '2026-02-20 14:30:00',
        'direction': 'bearish',
        'status': 'closed',
        'strategy_type': 'daily_income',
        'short_strike': 6000.0,
        'long_strike': 5995.0,
        'spread_width': 5.0,
        'credit_received': 0.85,
        'entry_price': 0.85,
        'entry_order_id': 'ORD-ENTRY-001',
        'exit_price': 0.10,
        'exit_order_id': 'ORD-EXIT-001',
        'exit_reason': 'profit_target',
        'pnl': 75.0,
        'pnl_percent': 88.2,
        'quantity': 1,
        'expiration': '2026-02-20',
    }
    defaults.update(overrides)
    return defaults


class TestReconciler:
    """Tests for TradeReconciler."""

    def setup_method(self):
        self.broker = MagicMock()
        self.db = MagicMock()
        self.reconciler = TradeReconciler(self.broker, self.db)

    def test_perfect_match(self):
        """All fields match within tolerance - 0 discrepancies."""
        trade = _make_trade()
        self.db.get_trades_by_date.return_value = [trade]
        self.broker.get_order_status.side_effect = [
            {'status': 'filled', 'fill_price': 0.85, 'filled_quantity': 1},  # entry
            {'status': 'filled', 'fill_price': 0.10, 'filled_quantity': 1},  # exit
        ]
        # No get_orders attribute
        del self.broker.get_orders

        result = self.reconciler.reconcile(date(2026, 2, 20))

        assert result['trades_in_db'] == 1
        assert result['matched'] == 1
        assert len(result['discrepancies']) == 0

    def test_entry_price_discrepancy(self):
        """Entry price differs > $0.05 threshold."""
        trade = _make_trade(entry_price=0.85)
        self.db.get_trades_by_date.return_value = [trade]
        self.broker.get_order_status.side_effect = [
            {'status': 'filled', 'fill_price': 0.95, 'filled_quantity': 1},  # entry: 0.10 diff
            {'status': 'filled', 'fill_price': 0.10, 'filled_quantity': 1},  # exit: match
        ]
        del self.broker.get_orders

        result = self.reconciler.reconcile(date(2026, 2, 20))

        assert len(result['discrepancies']) == 1
        d = result['discrepancies'][0]
        assert d['field'] == 'entry_price'
        assert d['status'] == 'price_discrepancy'
        assert d['db_value'] == 0.85
        assert d['broker_value'] == 0.95
        assert d['delta'] == 0.10
        assert result['matched'] == 0

    def test_exit_price_discrepancy(self):
        """Exit price differs > $0.05 threshold."""
        trade = _make_trade(exit_price=0.10)
        self.db.get_trades_by_date.return_value = [trade]
        self.broker.get_order_status.side_effect = [
            {'status': 'filled', 'fill_price': 0.85, 'filled_quantity': 1},  # entry: match
            {'status': 'filled', 'fill_price': 0.22, 'filled_quantity': 1},  # exit: 0.12 diff
        ]
        del self.broker.get_orders

        result = self.reconciler.reconcile(date(2026, 2, 20))

        assert len(result['discrepancies']) == 1
        d = result['discrepancies'][0]
        assert d['field'] == 'exit_price'
        assert d['status'] == 'price_discrepancy'
        assert d['delta'] == 0.12

    def test_quantity_mismatch(self):
        """DB says 2 contracts, broker says 1."""
        trade = _make_trade(quantity=2)
        self.db.get_trades_by_date.return_value = [trade]
        self.broker.get_order_status.side_effect = [
            {'status': 'filled', 'fill_price': 0.85, 'filled_quantity': 1},  # partial fill
            {'status': 'filled', 'fill_price': 0.10, 'filled_quantity': 2},  # exit ok
        ]
        del self.broker.get_orders

        result = self.reconciler.reconcile(date(2026, 2, 20))

        qty_disc = [d for d in result['discrepancies'] if d['field'] == 'quantity']
        assert len(qty_disc) == 1
        assert qty_disc[0]['status'] == 'quantity_mismatch'
        assert qty_disc[0]['db_value'] == 2
        assert qty_disc[0]['broker_value'] == 1
        assert qty_disc[0]['delta'] == 1

    def test_missing_from_broker(self):
        """get_order_status returns not_found for entry order."""
        trade = _make_trade()
        self.db.get_trades_by_date.return_value = [trade]
        self.broker.get_order_status.return_value = {
            'status': 'not_found', 'fill_price': 0, 'filled_quantity': 0,
        }
        del self.broker.get_orders

        result = self.reconciler.reconcile(date(2026, 2, 20))

        assert len(result['discrepancies']) >= 1
        d = result['discrepancies'][0]
        assert d['status'] == 'missing_from_broker'
        assert d['field'] == 'entry_order'

    def test_missing_from_db(self):
        """Broker has an executed order not in DB (E*TRADE get_orders)."""
        trade = _make_trade()
        self.db.get_trades_by_date.return_value = [trade]
        self.broker.get_order_status.side_effect = [
            {'status': 'filled', 'fill_price': 0.85, 'filled_quantity': 1},
            {'status': 'filled', 'fill_price': 0.10, 'filled_quantity': 1},
        ]
        # Broker has get_orders and returns an extra order
        self.broker.get_orders.return_value = [
            {'orderId': 'ORD-ENTRY-001'},
            {'orderId': 'ORD-EXIT-001'},
            {'orderId': 'ORD-UNKNOWN-999'},  # Not in DB
        ]

        result = self.reconciler.reconcile(date(2026, 2, 20))

        assert result['broker_supports_order_list'] is True
        missing = [d for d in result['discrepancies'] if d['status'] == 'missing_from_db']
        assert len(missing) == 1
        assert missing[0]['broker_value'] == 'ORD-UNKNOWN-999'

    def test_empty_trades(self):
        """No trades for the date produces a clean result."""
        self.db.get_trades_by_date.return_value = []
        del self.broker.get_orders

        result = self.reconciler.reconcile(date(2026, 2, 20))

        assert result['trades_in_db'] == 0
        assert result['matched'] == 0
        assert result['discrepancies'] == []
        assert result['total_db_pnl'] == 0

    def test_broker_api_error(self):
        """Broker raises an exception - graceful fallback."""
        trade = _make_trade()
        self.db.get_trades_by_date.return_value = [trade]
        self.broker.get_order_status.side_effect = ConnectionError("API timeout")
        del self.broker.get_orders

        result = self.reconciler.reconcile(date(2026, 2, 20))

        assert len(result['discrepancies']) == 1
        d = result['discrepancies'][0]
        assert d['status'] == 'broker_error'
        assert 'API timeout' in d['broker_value']

    def test_no_order_id_skipped(self):
        """Trade without entry_order_id is skipped entirely."""
        trade = _make_trade(entry_order_id=None)
        self.db.get_trades_by_date.return_value = [trade]
        del self.broker.get_orders

        result = self.reconciler.reconcile(date(2026, 2, 20))

        assert result['trades_in_db'] == 1
        assert result['trades_checked'] == 0
        assert result['matched'] == 0
        assert result['discrepancies'] == []
        self.broker.get_order_status.assert_not_called()

    def test_within_tolerance_no_discrepancy(self):
        """Price difference within $0.05 tolerance is not flagged."""
        trade = _make_trade(entry_price=0.85, exit_price=0.10)
        self.db.get_trades_by_date.return_value = [trade]
        self.broker.get_order_status.side_effect = [
            {'status': 'filled', 'fill_price': 0.88, 'filled_quantity': 1},  # 0.03 diff (within)
            {'status': 'filled', 'fill_price': 0.14, 'filled_quantity': 1},  # 0.04 diff (within)
        ]
        del self.broker.get_orders

        result = self.reconciler.reconcile(date(2026, 2, 20))

        assert len(result['discrepancies']) == 0
        assert result['matched'] == 1

    def test_pnl_total(self):
        """Total DB P&L sums correctly across multiple closed trades."""
        trades = [
            _make_trade(id='t1', pnl=75.0),
            _make_trade(id='t2', entry_order_id='ORD-2', exit_order_id='ORD-2X', pnl=-120.0),
            _make_trade(id='t3', entry_order_id='ORD-3', exit_order_id=None,
                        status='active', pnl=None),  # open trade, no pnl
        ]
        self.db.get_trades_by_date.return_value = trades
        self.broker.get_order_status.return_value = {
            'status': 'filled', 'fill_price': 0.85, 'filled_quantity': 1,
        }
        del self.broker.get_orders

        result = self.reconciler.reconcile(date(2026, 2, 20))

        assert result['total_db_pnl'] == -45.0  # 75 + (-120)
