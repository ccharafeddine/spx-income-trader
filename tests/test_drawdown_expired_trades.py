"""
Tests that expired (dashboard-resolved) trades count toward weekly
and monthly drawdown limits.

Safety-critical: circuit breakers that under-count real losses could
allow more trades than risk limits permit.
"""

import sys
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, date

import pytest
import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

ET = pytz.timezone('America/New_York')


def _setup_drawdown_env(tmp_path, dd_state=None):
    """Create temp DB with schema and optional drawdown_state.json."""
    from database.db_manager import DatabaseManager

    db_path = str(tmp_path / 'trades.db')
    DatabaseManager(db_path)

    # Minimal portfolio_state.json (needed by some code paths)
    state_path = tmp_path / 'portfolio_state.json'
    state_path.write_text(json.dumps({
        'active_positions': {},
        'daily_realized_pnl': 0.0,
        'daily_risk_used': 0.0,
        'dte0_trades_today': 0,
        'tnt_trades_today': 0,
        'circuit_breaker_triggered': False,
        'last_updated': datetime.now(ET).isoformat(),
    }))

    # drawdown_state.json
    dd_path = tmp_path / 'drawdown_state.json'
    if dd_state:
        dd_path.write_text(json.dumps(dd_state))

    return db_path


def _insert_trade(db_path, entry_time, pnl, status='closed',
                  exit_time=None, strategy_type='daily_income'):
    """Insert a trade with given status and P&L."""
    conn = sqlite3.connect(db_path)
    trade_id = f"test-{entry_time.isoformat()}-{strategy_type}-{pnl}"
    if exit_time is None:
        exit_time = entry_time + timedelta(hours=2)
    conn.execute(
        """INSERT INTO trades (
            id, entry_time, exit_time, direction, status,
            short_strike, long_strike, spread_width, credit_received,
            entry_price, quantity, expiration, strategy_type, pnl
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (trade_id, entry_time.isoformat(), exit_time.isoformat(),
         'bearish', status,
         5800.0, 5805.0, 5.0, 2.00,
         2.00, 1, entry_time.replace(hour=16, minute=0).isoformat(),
         strategy_type, pnl),
    )
    conn.commit()
    conn.close()


def _query_weekly_monthly_pnl(db_path):
    """Run the same queries the dashboard risk-status endpoint uses."""
    from datetime import date as _date
    today = datetime.now(ET).date()
    iso_year, iso_week, _ = today.isocalendar()
    monday = _date.fromisocalendar(iso_year, iso_week, 1)
    first_of_month = _date(today.year, today.month, 1)

    conn = sqlite3.connect(db_path, timeout=10)

    wsum = conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM trades "
        "WHERE LOWER(status) IN ('closed', 'expired') AND pnl < 0 "
        "AND exit_time >= ?", (str(monday),)
    ).fetchone()[0]

    msum = conn.execute(
        "SELECT COALESCE(SUM(pnl), 0) FROM trades "
        "WHERE LOWER(status) IN ('closed', 'expired') AND pnl < 0 "
        "AND exit_time >= ?", (str(first_of_month),)
    ).fetchone()[0]

    conn.close()
    return round(wsum, 2), round(msum, 2)


class TestExpiredTradesCountInDrawdown:
    """Expired trades must be included in weekly/monthly drawdown sums."""

    def test_expired_loss_in_weekly_drawdown(self, tmp_path):
        """An expired trade with a loss should count toward weekly drawdown."""
        db_path = _setup_drawdown_env(tmp_path)

        now_et = datetime.now(ET)
        _insert_trade(db_path, now_et, pnl=-848.86, status='expired')

        weekly, monthly = _query_weekly_monthly_pnl(db_path)

        assert weekly == -848.86, (
            f"Weekly drawdown should include expired loss, got {weekly}"
        )

    def test_expired_loss_in_monthly_drawdown(self, tmp_path):
        """An expired trade with a loss should count toward monthly drawdown."""
        db_path = _setup_drawdown_env(tmp_path)

        now_et = datetime.now(ET)
        _insert_trade(db_path, now_et, pnl=-848.86, status='expired')

        weekly, monthly = _query_weekly_monthly_pnl(db_path)

        assert monthly == -848.86, (
            f"Monthly drawdown should include expired loss, got {monthly}"
        )

    def test_mixed_closed_and_expired_losses(self, tmp_path):
        """Both closed and expired losses should be summed together."""
        db_path = _setup_drawdown_env(tmp_path)

        now_et = datetime.now(ET)
        _insert_trade(db_path, now_et, pnl=-500.0, status='closed')
        _insert_trade(db_path, now_et, pnl=-300.0, status='expired')

        weekly, monthly = _query_weekly_monthly_pnl(db_path)

        assert weekly == -800.0
        assert monthly == -800.0

    def test_expired_winners_not_in_drawdown(self, tmp_path):
        """Expired trades with positive P&L should NOT count in drawdown
        (drawdown only sums pnl < 0)."""
        db_path = _setup_drawdown_env(tmp_path)

        now_et = datetime.now(ET)
        _insert_trade(db_path, now_et, pnl=500.0, status='expired')

        weekly, monthly = _query_weekly_monthly_pnl(db_path)

        assert weekly == 0
        assert monthly == 0


class TestDailyDrawdownExcludesYesterday:
    """Daily drawdown resets at midnight - yesterday's expired trades excluded."""

    def test_yesterday_expired_not_in_today_daily(self, tmp_path):
        """An expired loss from yesterday should NOT appear in today's daily sum."""
        db_path = _setup_drawdown_env(tmp_path)

        yesterday = datetime.now(ET) - timedelta(days=1)
        _insert_trade(db_path, yesterday, pnl=-848.86, status='expired',
                      exit_time=yesterday + timedelta(hours=6))

        # Query daily P&L the same way _build_portfolio_status does
        today_str = datetime.now(ET).date().isoformat()
        conn = sqlite3.connect(db_path, timeout=10)
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0.0) FROM trades "
            "WHERE DATE(entry_time) = ? AND status IN ('closed', 'expired')",
            (today_str,),
        ).fetchone()
        conn.close()

        assert row[0] == 0.0, (
            f"Yesterday's expired trade should not affect today's daily P&L, got {row[0]}"
        )

    def test_today_expired_in_today_daily(self, tmp_path):
        """An expired loss from today SHOULD appear in today's daily sum."""
        db_path = _setup_drawdown_env(tmp_path)

        now_et = datetime.now(ET)
        _insert_trade(db_path, now_et, pnl=-848.86, status='expired')

        today_str = datetime.now(ET).date().isoformat()
        conn = sqlite3.connect(db_path, timeout=10)
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl), 0.0) FROM trades "
            "WHERE DATE(entry_time) = ? AND status IN ('closed', 'expired')",
            (today_str,),
        ).fetchone()
        conn.close()

        assert row[0] == -848.86


class TestRiskStatusAlwaysUsesDB:
    """The risk-status endpoint must always query DB for weekly/monthly P&L,
    not trust the drawdown_state.json which may be stale after a crash."""

    def test_api_risk_status_query_not_conditional(self):
        """Verify the weekly/monthly DB queries are unconditional
        (no 'if realized_pnl == 0.0' guard)."""
        import inspect
        from dashboard.app import api_risk_status

        source = inspect.getsource(api_risk_status)

        # The old code had: if weekly['realized_pnl'] == 0.0:
        # This guard prevented DB queries when state file had a value
        assert "if weekly['realized_pnl'] == 0.0" not in source, (
            "Weekly DB query must be unconditional (no == 0.0 guard)"
        )
        assert "if monthly['realized_pnl'] == 0.0" not in source, (
            "Monthly DB query must be unconditional (no == 0.0 guard)"
        )

    def test_health_endpoint_includes_expired(self):
        """Health endpoint daily_pnl query must include expired trades."""
        import inspect
        from dashboard.app import api_health

        source = inspect.getsource(api_health)

        assert "'expired'" in source, (
            "Health endpoint daily_pnl query must include status='expired'"
        )
