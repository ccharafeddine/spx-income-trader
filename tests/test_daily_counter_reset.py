"""
Tests that 0DTE and swing trade counters reset at midnight ET.

The dashboard /api/status endpoint must compute counters from the DB
for today's ET date, not from a stale portfolio_state.json file that
may contain yesterday's values.
"""

import sys
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta

import pytest
import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

ET = pytz.timezone('America/New_York')


def _setup_db_and_state(tmp_path, last_updated, dte0=1, tnt=1,
                        daily_pnl=500.0, risk=1000.0, cb=True):
    """Create a temp DB (with schema) and a portfolio_state.json."""
    from database.db_manager import DatabaseManager

    db_path = str(tmp_path / 'trades.db')
    DatabaseManager(db_path)

    state_path = tmp_path / 'portfolio_state.json'
    state_data = {
        'active_positions': {},
        'daily_realized_pnl': daily_pnl,
        'daily_risk_used': risk,
        'dte0_trades_today': dte0,
        'tnt_trades_today': tnt,
        'circuit_breaker_triggered': cb,
        'last_updated': last_updated.isoformat(),
    }
    state_path.write_text(json.dumps(state_data))

    return db_path, str(state_path)


def _insert_trade(db_path, entry_time, strategy_type='daily_income',
                  pnl=100.0, status='closed'):
    """Insert a minimal trade record into the DB."""
    conn = sqlite3.connect(db_path)
    trade_id = f"test-{entry_time.isoformat()}-{strategy_type}"
    conn.execute(
        """INSERT INTO trades (
            id, entry_time, exit_time, direction, status,
            short_strike, long_strike, spread_width, credit_received,
            entry_price, quantity, expiration, strategy_type, pnl
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (trade_id, entry_time.isoformat(),
         (entry_time + timedelta(hours=2)).isoformat(),
         'bearish', status,
         5800.0, 5805.0, 5.0, 2.00,
         2.00, 1, entry_time.replace(hour=16, minute=0).isoformat(),
         strategy_type, pnl),
    )
    conn.commit()
    conn.close()


class TestCounterResetAtMidnight:
    """Counters must show 0 on a new day with no trades."""

    def test_stale_state_file_counters_are_zero(self, tmp_path):
        """When portfolio_state.json is from yesterday, trade counters
        must be 0 (from DB) not 1 (from stale file)."""
        from dashboard.app import _build_portfolio_status

        yesterday = datetime.now(ET) - timedelta(days=1)
        db_path, state_path = _setup_db_and_state(
            tmp_path, last_updated=yesterday, dte0=1, tnt=1,
        )

        result = _build_portfolio_status(
            state_path=state_path, db_path=db_path,
        )

        assert result is not None
        assert result['dte0_trades_today'] == 0, (
            f"0DTE counter should be 0 on new day, got {result['dte0_trades_today']}"
        )
        assert result['tnt_trades_today'] == 0, (
            f"Swing counter should be 0 on new day, got {result['tnt_trades_today']}"
        )

    def test_stale_state_resets_pnl_risk_cb(self, tmp_path):
        """Stale state file P&L, risk, and circuit breaker should reset."""
        from dashboard.app import _build_portfolio_status

        yesterday = datetime.now(ET) - timedelta(days=1)
        db_path, state_path = _setup_db_and_state(
            tmp_path, last_updated=yesterday,
            daily_pnl=500.0, risk=1000.0, cb=True,
        )

        result = _build_portfolio_status(
            state_path=state_path, db_path=db_path,
        )

        assert result['daily_realized_pnl'] == 0.0, (
            f"Daily P&L should be 0 on new day, got {result['daily_realized_pnl']}"
        )
        assert result['daily_risk_used'] == 0, (
            f"Daily risk should be 0 on new day, got {result['daily_risk_used']}"
        )
        assert result['circuit_breaker'] is False


class TestCounterAfterTrades:
    """Counters reflect today's trades from the DB."""

    def test_one_di_trade_today(self, tmp_path):
        """After a daily_income trade today, 0DTE shows 1."""
        from dashboard.app import _build_portfolio_status

        yesterday = datetime.now(ET) - timedelta(days=1)
        db_path, state_path = _setup_db_and_state(
            tmp_path, last_updated=yesterday,
        )
        _insert_trade(db_path, datetime.now(ET), strategy_type='daily_income')

        result = _build_portfolio_status(
            state_path=state_path, db_path=db_path,
        )

        assert result['dte0_trades_today'] == 1
        assert result['tnt_trades_today'] == 0

    def test_one_tnt_trade_today(self, tmp_path):
        """After a tag_n_turn trade today, Swing shows 1."""
        from dashboard.app import _build_portfolio_status

        yesterday = datetime.now(ET) - timedelta(days=1)
        db_path, state_path = _setup_db_and_state(
            tmp_path, last_updated=yesterday,
        )
        _insert_trade(db_path, datetime.now(ET), strategy_type='tag_n_turn')

        result = _build_portfolio_status(
            state_path=state_path, db_path=db_path,
        )

        assert result['dte0_trades_today'] == 0
        assert result['tnt_trades_today'] == 1

    def test_orb_counts_toward_0dte(self, tmp_path):
        """ORB trades share the 0DTE slot with daily_income."""
        from dashboard.app import _build_portfolio_status

        yesterday = datetime.now(ET) - timedelta(days=1)
        db_path, state_path = _setup_db_and_state(
            tmp_path, last_updated=yesterday,
        )
        _insert_trade(db_path, datetime.now(ET), strategy_type='orb')

        result = _build_portfolio_status(
            state_path=state_path, db_path=db_path,
        )

        assert result['dte0_trades_today'] == 1

    def test_yesterday_trades_not_counted(self, tmp_path):
        """Trades from yesterday should NOT appear in today's counters."""
        from dashboard.app import _build_portfolio_status

        yesterday = datetime.now(ET) - timedelta(days=1)
        db_path, state_path = _setup_db_and_state(
            tmp_path, last_updated=yesterday,
        )
        _insert_trade(db_path, yesterday, strategy_type='daily_income')
        _insert_trade(db_path, yesterday, strategy_type='tag_n_turn')

        result = _build_portfolio_status(
            state_path=state_path, db_path=db_path,
        )

        assert result['dte0_trades_today'] == 0
        assert result['tnt_trades_today'] == 0

    def test_daily_pnl_from_db_when_stale(self, tmp_path):
        """Daily P&L should reflect today's closed trades from DB
        when state file is stale."""
        from dashboard.app import _build_portfolio_status

        yesterday = datetime.now(ET) - timedelta(days=1)
        db_path, state_path = _setup_db_and_state(
            tmp_path, last_updated=yesterday,
        )
        now_et = datetime.now(ET)
        _insert_trade(db_path, now_et, strategy_type='daily_income',
                      pnl=250.0, status='closed')
        _insert_trade(db_path, now_et, strategy_type='tag_n_turn',
                      pnl=-100.0, status='closed')

        result = _build_portfolio_status(
            state_path=state_path, db_path=db_path,
        )

        assert result['daily_realized_pnl'] == 150.0


class TestFreshStateFile:
    """When state file is from today, use its live values for P&L/risk."""

    def test_today_state_uses_live_pnl_but_db_counters(self, tmp_path):
        """If state file was updated today, use its P&L and risk,
        but still get trade counters from DB."""
        from dashboard.app import _build_portfolio_status

        now = datetime.now(ET)
        db_path, state_path = _setup_db_and_state(
            tmp_path, last_updated=now,
            daily_pnl=777.0, risk=500.0, dte0=99, tnt=99, cb=False,
        )

        result = _build_portfolio_status(
            state_path=state_path, db_path=db_path,
        )

        # Trade counters always from DB (no trades inserted -> 0)
        assert result['dte0_trades_today'] == 0
        assert result['tnt_trades_today'] == 0
        # P&L and risk from state file when fresh
        assert result['daily_realized_pnl'] == 777.0
        assert result['daily_risk_used'] == 500.0
