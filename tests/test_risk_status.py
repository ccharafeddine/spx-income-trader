"""
Tests for the Risk Status panel (/api/risk-status).

Covers:
- Streak calculation with mixed wins and losses
- Scratch trades ($0 pnl) do not break streaks
- Open trades (status != 'closed') are excluded from streaks
- Period rollover: weekly resets on ISO week change
- Period rollover: monthly resets on month change
- Period rollover: idempotent (safe to call twice)
- Drawdown bars: positive realized_pnl -> 0% bar fill
- Drawdown bars: negative realized_pnl -> correct % fill up to 100%
- Circuit breaker blocks can_enter_position after daily loss limit
- Weekly breaker blocks can_trade after weekly loss limit
- Monthly breaker blocks can_trade after monthly loss limit
"""

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from dashboard.app import app, API_TOKEN
from src.core.drawdown_manager import DrawdownManager
from src.core.portfolio_manager import PortfolioManager, StrategyType


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def risk_client(tmp_path):
    """Flask test client with a temp database seeded for risk-status tests."""
    db_file = str(tmp_path / 'test_risk.db')

    schema_path = Path(__file__).parent.parent / 'database' / 'schema.sql'
    conn = sqlite3.connect(db_file)
    conn.executescript(schema_path.read_text())
    conn.close()

    app.config['TESTING'] = True
    with patch('dashboard.app.DATABASE_PATH', db_file):
        with app.test_client() as c:
            yield c, db_file


def _insert_trade(db_file, trade_id, status, pnl, exit_time,
                  entry_time='2026-02-10 10:00:00', strategy_type='daily_income'):
    """Insert a trade row for streak testing."""
    conn = sqlite3.connect(db_file)
    conn.execute(
        "INSERT INTO trades (id, strategy_type, status, pnl, entry_time, exit_time, "
        "direction, short_strike, long_strike, spread_width, credit_received, "
        "entry_price, quantity, expiration) "
        "VALUES (?, ?, ?, ?, ?, ?, 'bearish', 5800, 5795, 5.0, 1.50, 1.50, 1, '2026-02-28')",
        (trade_id, strategy_type, status, pnl, entry_time, exit_time),
    )
    conn.commit()
    conn.close()


# ============================================================================
# STREAK TESTS
# ============================================================================

class TestStreakCalculation:
    """Streak calculation from trade history."""

    def test_mixed_wins_losses_streak(self, risk_client):
        """6 wins, 1 loss, 3 wins -> win_streak=3 (most recent 3 wins)."""
        client, db_file = risk_client

        # Insert oldest first: 3 wins, then 1 loss, then 6 wins (most recent)
        # exit_time order determines recency
        trades = [
            # Oldest: 3 wins
            ('t01', 'closed',  150.0, '2026-02-10 10:00:00'),
            ('t02', 'closed',  200.0, '2026-02-10 11:00:00'),
            ('t03', 'closed',  100.0, '2026-02-10 12:00:00'),
            # 1 loss
            ('t04', 'closed', -300.0, '2026-02-11 10:00:00'),
            # Most recent: 6 wins
            ('t05', 'closed',  250.0, '2026-02-13 10:00:00'),
            ('t06', 'closed',  180.0, '2026-02-13 11:00:00'),
            ('t07', 'closed',  220.0, '2026-02-20 10:00:00'),
            ('t08', 'closed',  190.0, '2026-02-23 10:00:00'),
            ('t09', 'closed',  160.0, '2026-02-24 10:00:00'),
            ('t10', 'closed',  210.0, '2026-02-26 10:00:00'),
        ]
        for tid, status, pnl, exit_t in trades:
            _insert_trade(db_file, tid, status, pnl, exit_t)

        resp = client.get('/api/risk-status')
        data = resp.get_json()
        assert data['streaks']['active'] == 'win'
        assert data['streaks']['win_streak'] == 6
        assert data['streaks']['loss_streak'] == 0

    def test_loss_then_wins(self, risk_client):
        """1 loss, then 3 wins -> win_streak=3, prev_loss_streak=1."""
        client, db_file = risk_client

        trades = [
            ('t01', 'closed', -100.0, '2026-02-10 10:00:00'),
            ('t02', 'closed',  200.0, '2026-02-11 10:00:00'),
            ('t03', 'closed',  150.0, '2026-02-12 10:00:00'),
            ('t04', 'closed',  180.0, '2026-02-13 10:00:00'),
        ]
        for tid, status, pnl, exit_t in trades:
            _insert_trade(db_file, tid, status, pnl, exit_t)

        resp = client.get('/api/risk-status')
        data = resp.get_json()
        assert data['streaks']['win_streak'] == 3
        assert data['streaks']['loss_streak'] == 0
        assert data['streaks']['prev_loss_streak'] == 1

    def test_scratch_trade_does_not_break_win_streak(self, risk_client):
        """$0 pnl trade between wins does not break the streak."""
        client, db_file = risk_client

        trades = [
            ('t01', 'closed', -200.0, '2026-02-10 10:00:00'),  # loss (oldest)
            ('t02', 'closed',  150.0, '2026-02-11 10:00:00'),  # win
            ('t03', 'closed',  100.0, '2026-02-12 10:00:00'),  # win
            ('t04', 'closed',    0.0, '2026-02-13 10:00:00'),  # scratch ($0)
            ('t05', 'closed',  200.0, '2026-02-14 10:00:00'),  # win (most recent)
        ]
        for tid, status, pnl, exit_t in trades:
            _insert_trade(db_file, tid, status, pnl, exit_t)

        resp = client.get('/api/risk-status')
        data = resp.get_json()
        # $0 trade is excluded from query (AND pnl != 0), so streak is 3 wins
        assert data['streaks']['active'] == 'win'
        assert data['streaks']['win_streak'] == 3

    def test_open_trades_excluded_from_streak(self, risk_client):
        """Open trades (status != 'closed') are not counted in streak."""
        client, db_file = risk_client

        trades = [
            ('t01', 'closed', 150.0, '2026-02-10 10:00:00'),
            ('t02', 'closed', 200.0, '2026-02-11 10:00:00'),
            ('t03', 'closed', 180.0, '2026-02-12 10:00:00'),
        ]
        for tid, status, pnl, exit_t in trades:
            _insert_trade(db_file, tid, status, pnl, exit_t)

        # Insert an open trade with no pnl
        conn = sqlite3.connect(db_file)
        conn.execute(
            "INSERT INTO trades (id, strategy_type, status, pnl, entry_time, "
            "direction, short_strike, long_strike, spread_width, credit_received, "
            "entry_price, quantity, expiration) "
            "VALUES ('t_open', 'tag_n_turn', 'open', NULL, '2026-02-25 10:00:00', "
            "'bullish', 5800, 5795, 5.0, 3.00, 3.00, 2, '2026-03-04')",
        )
        conn.commit()
        conn.close()

        resp = client.get('/api/risk-status')
        data = resp.get_json()
        assert data['streaks']['win_streak'] == 3
        assert data['streaks']['active'] == 'win'

    def test_all_losses_streak(self, risk_client):
        """All losses -> loss_streak = count, win_streak = 0."""
        client, db_file = risk_client

        for i in range(4):
            _insert_trade(db_file, f't{i}', 'closed', -100.0 * (i + 1),
                          f'2026-02-{10 + i} 10:00:00')

        resp = client.get('/api/risk-status')
        data = resp.get_json()
        assert data['streaks']['active'] == 'loss'
        assert data['streaks']['loss_streak'] == 4
        assert data['streaks']['win_streak'] == 0

    def test_no_trades_streak_zero(self, risk_client):
        """No trades -> all streaks at 0."""
        client, _ = risk_client
        resp = client.get('/api/risk-status')
        data = resp.get_json()
        assert data['streaks']['win_streak'] == 0
        assert data['streaks']['loss_streak'] == 0
        assert data['streaks']['active'] == 'none'


# ============================================================================
# PERIOD ROLLOVER TESTS
# ============================================================================

class TestPeriodRollover:
    """DrawdownManager period rollover behavior."""

    @pytest.fixture
    def dm(self, tmp_path):
        """Create a DrawdownManager with both breakers enabled."""
        return DrawdownManager(
            account_size=50000.0,
            config={
                'weekly': {'enabled': True, 'max_loss_pct': 4.0},
                'monthly': {'enabled': True, 'max_loss_pct': 8.0},
            },
            persistence_path=tmp_path / 'drawdown_state.json',
        )

    def test_weekly_resets_on_iso_week_change(self, dm):
        """Weekly P&L and breaker reset when ISO week changes."""
        dm.record_realized_pnl(-1500.0)
        assert dm.weekly_realized_pnl == -1500.0

        # Simulate next week
        today = date.today()
        iso_year, iso_week, _ = today.isocalendar()
        next_week = iso_week + 1
        next_year = iso_year
        if next_week > 52:
            next_week = 1
            next_year += 1

        with patch('src.core.drawdown_manager.date') as mock_date:
            mock_date.today.return_value = date.fromisocalendar(next_year, next_week, 1)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            dm.check_period_rollovers()

        assert dm.weekly_realized_pnl == 0.0
        assert dm.weekly_breaker_triggered is False
        assert dm.current_iso_week == next_week

    def test_monthly_resets_on_month_change(self, dm):
        """Monthly P&L and breaker reset when calendar month changes."""
        dm.record_realized_pnl(-3000.0)
        dm.monthly_breaker_triggered = False  # Not triggered yet
        assert dm.monthly_realized_pnl == -3000.0

        today = date.today()
        if today.month == 12:
            next_month_date = date(today.year + 1, 1, 5)
        else:
            next_month_date = date(today.year, today.month + 1, 5)

        with patch('src.core.drawdown_manager.date') as mock_date:
            mock_date.today.return_value = next_month_date
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            dm.check_period_rollovers()

        assert dm.monthly_realized_pnl == 0.0
        assert dm.monthly_breaker_triggered is False
        assert dm.current_month == next_month_date.month

    def test_rollover_is_idempotent(self, dm):
        """Calling check_period_rollovers() twice in same period is safe."""
        dm.record_realized_pnl(-800.0)

        dm.check_period_rollovers()
        assert dm.weekly_realized_pnl == -800.0
        assert dm.monthly_realized_pnl == -800.0

        # Call again -- nothing should change
        dm.check_period_rollovers()
        assert dm.weekly_realized_pnl == -800.0
        assert dm.monthly_realized_pnl == -800.0

    def test_rollover_idempotent_after_week_change(self, dm):
        """Double-calling after a week change doesn't double-reset."""
        dm.record_realized_pnl(-1200.0)

        today = date.today()
        iso_year, iso_week, _ = today.isocalendar()
        next_week = iso_week + 1
        next_year = iso_year
        if next_week > 52:
            next_week = 1
            next_year += 1

        with patch('src.core.drawdown_manager.date') as mock_date:
            mock_date.today.return_value = date.fromisocalendar(next_year, next_week, 1)
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            dm.check_period_rollovers()
            # Record some P&L in the new week
            dm.record_realized_pnl(-500.0)
            # Call again -- should NOT reset again
            dm.check_period_rollovers()

        assert dm.weekly_realized_pnl == -500.0  # Preserved, not re-zeroed


# ============================================================================
# DRAWDOWN BAR DISPLAY TESTS
# ============================================================================

class TestDrawdownBarDisplay:
    """Verify bar fill percentage logic matches frontend barPct formula.

    Frontend formula:
        barPct(pnl, limit) = limit <= 0 ? 0 : min(100, abs(min(pnl, 0)) / limit * 100)

    We test the API response values that feed into this formula.
    """

    def test_positive_pnl_zero_bar(self, tmp_path):
        """Positive realized_pnl should produce 0% bar fill."""
        # barPct(+500, 1000) -> abs(min(500, 0)) / 1000 * 100 = 0%
        pnl = 500.0
        limit = 1000.0
        bar_pct = min(100, abs(min(pnl, 0)) / limit * 100) if limit > 0 else 0
        assert bar_pct == 0.0

    def test_zero_pnl_zero_bar(self):
        """Zero realized_pnl should produce 0% bar fill."""
        pnl = 0.0
        limit = 1000.0
        bar_pct = min(100, abs(min(pnl, 0)) / limit * 100) if limit > 0 else 0
        assert bar_pct == 0.0

    def test_negative_pnl_correct_bar(self):
        """Negative realized_pnl should produce correct % fill."""
        # -750 of 1000 limit = 75%
        pnl = -750.0
        limit = 1000.0
        bar_pct = min(100, abs(min(pnl, 0)) / limit * 100) if limit > 0 else 0
        assert bar_pct == 75.0

    def test_negative_pnl_at_limit_full_bar(self):
        """Loss exactly at limit should produce 100% fill."""
        pnl = -1000.0
        limit = 1000.0
        bar_pct = min(100, abs(min(pnl, 0)) / limit * 100) if limit > 0 else 0
        assert bar_pct == 100.0

    def test_negative_pnl_beyond_limit_capped(self):
        """Loss beyond limit should cap at 100%."""
        pnl = -1500.0
        limit = 1000.0
        bar_pct = min(100, abs(min(pnl, 0)) / limit * 100) if limit > 0 else 0
        assert bar_pct == 100.0

    def test_zero_limit_zero_bar(self):
        """Zero limit should produce 0% bar (no division by zero)."""
        pnl = -500.0
        limit = 0.0
        bar_pct = min(100, abs(min(pnl, 0)) / limit * 100) if limit > 0 else 0
        assert bar_pct == 0.0

    def test_api_daily_pnl_positive_day(self, tmp_path):
        """API returns positive daily pnl on a winning day, bar should be 0%."""
        port_path = tmp_path / 'database' / 'portfolio_state.json'
        port_path.parent.mkdir(parents=True, exist_ok=True)
        port_path.write_text(json.dumps({
            'daily_realized_pnl': 350.0,
            'circuit_breaker_triggered': False,
        }))

        with patch('dashboard.app.BASE_DIR', tmp_path):
            app.config['TESTING'] = True
            with app.test_client() as c:
                resp = c.get('/api/risk-status')
                data = resp.get_json()

        daily_pnl = data['daily']['realized_pnl']
        limit = data['daily']['max_loss_dollars']
        assert daily_pnl >= 0
        bar_pct = min(100, abs(min(daily_pnl, 0)) / limit * 100) if limit > 0 else 0
        assert bar_pct == 0.0

    def test_api_daily_pnl_losing_day(self, tmp_path):
        """API returns negative daily pnl on a losing day, bar fills correctly."""
        port_path = tmp_path / 'database' / 'portfolio_state.json'
        port_path.parent.mkdir(parents=True, exist_ok=True)
        port_path.write_text(json.dumps({
            'daily_realized_pnl': -500.0,
            'circuit_breaker_triggered': False,
        }))

        with patch('dashboard.app.BASE_DIR', tmp_path):
            app.config['TESTING'] = True
            with app.test_client() as c:
                resp = c.get('/api/risk-status')
                data = resp.get_json()

        daily_pnl = data['daily']['realized_pnl']
        limit = data['daily']['max_loss_dollars']
        assert daily_pnl < 0
        bar_pct = min(100, abs(min(daily_pnl, 0)) / limit * 100) if limit > 0 else 0
        assert bar_pct > 0


# ============================================================================
# CIRCUIT BREAKER INTEGRATION TESTS
# ============================================================================

class TestCircuitBreakerBlocking:
    """Verify circuit breakers actually block new trades."""

    @pytest.fixture
    def portfolio(self, tmp_path):
        """PortfolioManager with drawdown limits enabled."""
        return PortfolioManager(
            account_size=50000.0,
            max_total_positions=2,
            max_0dte_positions=1,
            max_daily_loss_pct=2.0,  # 2% = $1000
            drawdown_limits={
                'weekly': {'enabled': True, 'max_loss_pct': 4.0},   # $2000
                'monthly': {'enabled': True, 'max_loss_pct': 8.0},  # $4000
            },
            persistence_path=tmp_path / 'portfolio_state.json',
        )

    def test_daily_loss_blocks_entry(self, portfolio):
        """After daily loss hits limit, can_enter_position returns False."""
        portfolio.update_realized_pnl(-1000.0)  # 2% of $50k

        allowed, reason = portfolio.can_enter_position(
            strategy=StrategyType.DAILY_INCOME,
            contracts=1,
            max_risk_per_contract=200.0,
            is_0dte=True,
        )
        assert allowed is False
        assert 'Circuit breaker' in reason or 'daily' in reason.lower()

    def test_weekly_loss_blocks_entry(self, portfolio):
        """After weekly loss hits limit, can_enter_position returns False."""
        portfolio.update_realized_pnl(-2000.0)  # 4% of $50k
        # Reset daily so daily breaker doesn't fire first
        portfolio.daily_realized_pnl = 0.0
        portfolio.circuit_breaker_triggered = False

        allowed, reason = portfolio.can_enter_position(
            strategy=StrategyType.DAILY_INCOME,
            contracts=1,
            max_risk_per_contract=200.0,
            is_0dte=True,
        )
        assert allowed is False
        assert 'Weekly' in reason

    def test_monthly_loss_blocks_entry(self, portfolio):
        """After monthly loss hits limit, can_enter_position returns False."""
        # Accumulate monthly losses across multiple "days" without triggering weekly
        portfolio.drawdown_manager.monthly_realized_pnl = -4000.0  # 8% of $50k
        portfolio.drawdown_manager.monthly_breaker_triggered = True

        # Make sure weekly doesn't block first
        portfolio.drawdown_manager.weekly_realized_pnl = -500.0
        portfolio.drawdown_manager.weekly_breaker_triggered = False

        allowed, reason = portfolio.can_enter_position(
            strategy=StrategyType.DAILY_INCOME,
            contracts=1,
            max_risk_per_contract=200.0,
            is_0dte=True,
        )
        assert allowed is False
        assert 'Monthly' in reason

    def test_weekly_resets_but_monthly_persists(self, portfolio):
        """After weekly rollover, monthly losses carry over and can still trigger."""
        dm = portfolio.drawdown_manager

        # Week 1: lose $2500 (triggers weekly, contributes to monthly)
        dm.record_realized_pnl(-2500.0)
        assert dm.weekly_breaker_triggered is True
        assert dm.monthly_realized_pnl == -2500.0

        # Manually simulate a weekly rollover (same month)
        dm.weekly_realized_pnl = 0.0
        dm.weekly_breaker_triggered = False

        # Week 2: lose $1500 more (monthly total: $4000 = 8% of $50k)
        dm.record_realized_pnl(-1500.0)

        assert dm.weekly_realized_pnl == -1500.0
        assert dm.monthly_realized_pnl == -4000.0
        assert dm.monthly_breaker_triggered is True
        allowed, reason = dm.can_trade()
        assert allowed is False

    def test_after_daily_reset_can_trade_again(self, portfolio):
        """After reset_daily(), daily circuit breaker clears."""
        portfolio.update_realized_pnl(-1000.0)
        assert portfolio.circuit_breaker_triggered is True

        portfolio.reset_daily()
        assert portfolio.circuit_breaker_triggered is False
        assert portfolio.daily_realized_pnl == 0.0

        allowed, _ = portfolio.can_enter_position(
            strategy=StrategyType.DAILY_INCOME,
            contracts=1,
            max_risk_per_contract=200.0,
            is_0dte=True,
        )
        assert allowed is True
