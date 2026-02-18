"""
Tests for compute_account() to verify flagged trades are excluded from P&L.

Flagged trades (e.g. LOW_CREDIT) were placed before safeguards existed
and should not count toward realized P&L, daily P&L, cash balance, or
total equity.
"""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime
import pytz

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

ET = pytz.timezone('US/Eastern')

from dashboard.app import (
    compute_account, _annotate_trade, MIN_CREDIT_THRESHOLD
)

STARTING_CAPITAL = 50000.0  # Default for tests; compute_account reads from YAML


def _make_trade(trade_id, credit, pnl, status='closed', entry_time='2026-02-03T10:00:00',
                exit_time='2026-02-03T15:00:00', direction='bullish',
                short_strike=6000, long_strike=5995, quantity=1):
    """Build a minimal trade dict matching DB row format, with annotation."""
    t = {
        'id': trade_id,
        'status': status,
        'direction': direction,
        'short_strike': short_strike,
        'long_strike': long_strike,
        'credit_received': credit,
        'quantity': quantity,
        'pnl': pnl,
        'entry_time': entry_time,
        'exit_time': exit_time,
        'expiration': '2026-02-03',
    }
    _annotate_trade(t)  # Sets flag/flag_note like classify_trades does
    return t


def _run_compute(trades, spx_price=6050.0):
    """Run compute_account with mocked classify_trades returning given trades."""
    with patch('dashboard.app.classify_trades') as mock_classify:
        mock_classify.return_value = ([], trades)
        conn = MagicMock()
        return compute_account(conn, spx_price, starting_capital=STARTING_CAPITAL)


class TestFlaggedTradeExclusion:
    """Verify flagged trades are excluded from all P&L calculations."""

    def test_flagged_trade_excluded_from_realized_pnl(self):
        """A LOW_CREDIT flagged trade should not count in realized_pnl."""
        valid_trade = _make_trade('t1', credit=1.50, pnl=150.0)
        flagged_trade = _make_trade('t2', credit=0.50, pnl=50.0)

        # Verify the annotation worked as expected
        assert valid_trade['flag'] is None
        assert flagged_trade['flag'] == 'LOW_CREDIT'

        result = _run_compute([valid_trade, flagged_trade])

        assert result['realized_pnl'] == 150.0
        assert result['current_balance'] == STARTING_CAPITAL + 150.0

    def test_unflagged_trades_included(self):
        """Trades with credit >= threshold should be included."""
        t1 = _make_trade('t1', credit=1.50, pnl=150.0)
        t2 = _make_trade('t2', credit=2.00, pnl=200.0)

        result = _run_compute([t1, t2])

        assert result['realized_pnl'] == 350.0
        assert result['current_balance'] == STARTING_CAPITAL + 350.0

    def test_flagged_trade_excluded_from_daily_pnl(self):
        """Flagged trades should not count in today's daily P&L."""
        today_str = datetime.now(ET).strftime('%Y-%m-%d')
        valid_trade = _make_trade('t1', credit=1.50, pnl=100.0,
                                  entry_time=f'{today_str}T10:00:00',
                                  exit_time=f'{today_str}T14:00:00')
        flagged_trade = _make_trade('t2', credit=0.10, pnl=10.0,
                                    entry_time=f'{today_str}T10:30:00',
                                    exit_time=f'{today_str}T14:30:00')

        result = _run_compute([valid_trade, flagged_trade])

        assert result['daily_pnl'] == 100.0

    def test_total_equity_excludes_flagged(self):
        """Total equity = starting + valid realized + unrealized.
        Flagged trades should not inflate equity."""
        valid_trade = _make_trade('t1', credit=1.50, pnl=150.0)
        flagged_trade = _make_trade('t2', credit=0.30, pnl=30.0)

        result = _run_compute([valid_trade, flagged_trade])

        expected_balance = STARTING_CAPITAL + 150.0
        assert result['total_equity'] == expected_balance  # no open positions

    def test_all_flagged_means_zero_pnl(self):
        """If every trade is flagged, realized P&L should be zero."""
        t1 = _make_trade('t1', credit=0.10, pnl=10.0)
        t2 = _make_trade('t2', credit=0.05, pnl=5.0)

        result = _run_compute([t1, t2])

        assert result['realized_pnl'] == 0.0
        assert result['current_balance'] == STARTING_CAPITAL
        assert result['daily_pnl'] == 0.0

    def test_closed_trades_still_returned_unfiltered(self):
        """The closed_trades list should still contain ALL trades (for display)."""
        valid_trade = _make_trade('t1', credit=1.50, pnl=150.0)
        flagged_trade = _make_trade('t2', credit=0.50, pnl=50.0)

        result = _run_compute([valid_trade, flagged_trade])

        # Both trades should be in the returned list for display purposes
        assert len(result['closed_trades']) == 2
        # But P&L only counts the valid one
        assert result['realized_pnl'] == 150.0
