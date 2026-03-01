"""
Tests for Consecutive Loss Pause feature in DrawdownManager.

Tests cover:
- Counting logic (increment on loss, reset on win, unchanged on zero)
- Pause activation at exact threshold (5 losses)
- Time-based resume (pause expires after pause_hours)
- Persistence of consecutive loss state across restarts
- Interaction with weekly/monthly breakers (consec checked first)
- Period rollovers do NOT reset consecutive counter or clear pause
- Edge cases (zero P&L, exactly N losses, disabled mode, win during pause)
- get_status() consecutive_losses fields
- PortfolioManager integration
"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.drawdown_manager import DrawdownManager


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def temp_state_file(tmp_path):
    """Isolated temp dir per test."""
    return tmp_path / "drawdown_state.json"


@pytest.fixture
def consec_config():
    """Config with consecutive losses enabled, weekly/monthly disabled."""
    return {
        'weekly': {'enabled': False},
        'monthly': {'enabled': False},
        'consecutive_losses': {
            'enabled': True,
            'max_consecutive': 5,
            'pause_hours': 24,
        },
    }


@pytest.fixture
def full_config():
    """Config with all three breakers enabled."""
    return {
        'weekly': {'enabled': True, 'max_loss_pct': 4.0},
        'monthly': {'enabled': True, 'max_loss_pct': 8.0},
        'consecutive_losses': {
            'enabled': True,
            'max_consecutive': 5,
            'pause_hours': 24,
        },
    }


@pytest.fixture
def manager(temp_state_file, consec_config):
    """DrawdownManager with only consecutive losses enabled."""
    return DrawdownManager(
        account_size=50000.0,
        config=consec_config,
        persistence_path=temp_state_file,
        max_daily_loss_pct=100.0,  # High so daily breaker doesn't interfere
    )


# ============================================================================
# COUNTING LOGIC
# ============================================================================

class TestCountingLogic:
    """Test consecutive loss counter increments and resets."""

    def test_loss_increments_counter(self, manager):
        """Each negative P&L increments the counter."""
        manager.record_realized_pnl(-100.0)
        assert manager.consecutive_losses == 1
        manager.record_realized_pnl(-200.0)
        assert manager.consecutive_losses == 2
        manager.record_realized_pnl(-50.0)
        assert manager.consecutive_losses == 3

    def test_win_resets_counter(self, manager):
        """Positive P&L resets counter to 0."""
        manager.record_realized_pnl(-100.0)
        manager.record_realized_pnl(-200.0)
        assert manager.consecutive_losses == 2
        manager.record_realized_pnl(50.0)
        assert manager.consecutive_losses == 0

    def test_zero_pnl_unchanged(self, manager):
        """Zero P&L (scratch trade) does not change counter."""
        manager.record_realized_pnl(-100.0)
        manager.record_realized_pnl(-200.0)
        assert manager.consecutive_losses == 2
        manager.record_realized_pnl(0.0)
        assert manager.consecutive_losses == 2

    def test_zero_after_win_stays_zero(self, manager):
        """Zero P&L after a win keeps counter at 0."""
        manager.record_realized_pnl(100.0)
        assert manager.consecutive_losses == 0
        manager.record_realized_pnl(0.0)
        assert manager.consecutive_losses == 0

    def test_loss_win_loss_pattern(self, manager):
        """Win in the middle resets streak, losses resume from 0."""
        manager.record_realized_pnl(-100.0)
        manager.record_realized_pnl(-100.0)
        manager.record_realized_pnl(-100.0)
        assert manager.consecutive_losses == 3
        manager.record_realized_pnl(50.0)  # Reset
        assert manager.consecutive_losses == 0
        manager.record_realized_pnl(-100.0)
        manager.record_realized_pnl(-100.0)
        assert manager.consecutive_losses == 2

    def test_starts_at_zero(self, manager):
        """Fresh manager starts with 0 consecutive losses."""
        assert manager.consecutive_losses == 0


# ============================================================================
# PAUSE ACTIVATION
# ============================================================================

class TestPauseActivation:
    """Test that pause triggers at the right threshold."""

    def test_no_pause_at_4_losses(self, manager):
        """4 losses (under threshold of 5) does not trigger pause."""
        for _ in range(4):
            manager.record_realized_pnl(-100.0)
        assert manager.consecutive_losses == 4
        assert manager.consec_pause_until is None
        allowed, _ = manager.can_trade()
        assert allowed is True

    def test_pause_at_exactly_5_losses(self, manager):
        """5th consecutive loss triggers the pause."""
        for _ in range(5):
            manager.record_realized_pnl(-100.0)
        assert manager.consecutive_losses == 5
        assert manager.consec_pause_until is not None
        allowed, reason = manager.can_trade()
        assert allowed is False
        assert "Consecutive loss pause" in reason
        assert "5 losses" in reason

    def test_pause_at_6_losses_no_double_trigger(self, manager):
        """6th loss doesn't reset the pause timer (already paused)."""
        for _ in range(5):
            manager.record_realized_pnl(-100.0)
        first_pause = manager.consec_pause_until
        manager.record_realized_pnl(-100.0)  # 6th loss
        assert manager.consecutive_losses == 6
        # Pause time should not change
        assert manager.consec_pause_until == first_pause

    def test_custom_threshold_3(self, temp_state_file):
        """Custom max_consecutive of 3 triggers after 3 losses."""
        cfg = {
            'consecutive_losses': {
                'enabled': True,
                'max_consecutive': 3,
                'pause_hours': 12,
            },
        }
        mgr = DrawdownManager(
            account_size=50000.0,
            config=cfg,
            persistence_path=temp_state_file,
        )
        for _ in range(3):
            mgr.record_realized_pnl(-100.0)
        assert mgr.consec_pause_until is not None
        assert mgr.consecutive_losses == 3

    def test_disabled_never_pauses(self, temp_state_file):
        """Disabled consecutive losses never triggers pause."""
        cfg = {'consecutive_losses': {'enabled': False, 'max_consecutive': 5}}
        mgr = DrawdownManager(
            account_size=50000.0,
            config=cfg,
            persistence_path=temp_state_file,
            max_daily_loss_pct=100.0,
        )
        for _ in range(10):
            mgr.record_realized_pnl(-100.0)
        assert mgr.consecutive_losses == 10
        assert mgr.consec_pause_until is None
        allowed, _ = mgr.can_trade()
        assert allowed is True


# ============================================================================
# TIME-BASED RESUME
# ============================================================================

class TestTimeBasedResume:
    """Test that pause expires after pause_hours."""

    def test_blocks_during_pause(self, manager):
        """Trading blocked while pause is active."""
        for _ in range(5):
            manager.record_realized_pnl(-100.0)
        allowed, reason = manager.can_trade()
        assert allowed is False
        assert "Resumes in" in reason

    def test_resumes_after_pause_expires(self, manager):
        """Trading resumes after pause_hours have elapsed."""
        for _ in range(5):
            manager.record_realized_pnl(-100.0)

        # Simulate time passage: set pause_until to the past
        manager.consec_pause_until = datetime.now() - timedelta(hours=1)
        allowed, reason = manager.can_trade()
        assert allowed is True
        assert reason == "Drawdown limits OK"
        # Pause should be cleared
        assert manager.consec_pause_until is None

    def test_resume_clears_pause_timestamp(self, manager):
        """After expiry, consec_pause_until is set to None."""
        for _ in range(5):
            manager.record_realized_pnl(-100.0)
        manager.consec_pause_until = datetime.now() - timedelta(minutes=1)
        manager.can_trade()
        assert manager.consec_pause_until is None

    def test_pause_duration_matches_config(self, temp_state_file):
        """Pause duration matches configured pause_hours."""
        cfg = {
            'consecutive_losses': {
                'enabled': True,
                'max_consecutive': 3,
                'pause_hours': 48,
            },
        }
        mgr = DrawdownManager(
            account_size=50000.0,
            config=cfg,
            persistence_path=temp_state_file,
        )
        before = datetime.now()
        for _ in range(3):
            mgr.record_realized_pnl(-100.0)
        after = datetime.now()

        # Pause should be approximately 48 hours from now
        expected_min = before + timedelta(hours=48)
        expected_max = after + timedelta(hours=48)
        assert expected_min <= mgr.consec_pause_until <= expected_max

    def test_win_during_pause_does_not_clear_pause(self, manager):
        """A win while paused resets the counter but NOT the pause timer."""
        for _ in range(5):
            manager.record_realized_pnl(-100.0)
        pause_time = manager.consec_pause_until
        assert pause_time is not None

        manager.record_realized_pnl(500.0)  # Big win
        assert manager.consecutive_losses == 0  # Counter reset
        assert manager.consec_pause_until == pause_time  # Pause still active

        allowed, _ = manager.can_trade()
        assert allowed is False  # Still blocked

    def test_new_streak_after_pause_expires(self, manager):
        """After pause expires and clears, a new streak can trigger again."""
        # First streak
        for _ in range(5):
            manager.record_realized_pnl(-100.0)
        assert manager.consec_pause_until is not None

        # Expire the pause
        manager.consec_pause_until = datetime.now() - timedelta(hours=1)
        manager.can_trade()  # Clears the expired pause
        assert manager.consec_pause_until is None

        # Reset counter with a win, then build new streak
        manager.record_realized_pnl(50.0)
        assert manager.consecutive_losses == 0
        for _ in range(5):
            manager.record_realized_pnl(-100.0)
        assert manager.consec_pause_until is not None  # New pause


# ============================================================================
# PERSISTENCE
# ============================================================================

class TestConsecPersistence:
    """Test that consecutive loss state persists across restarts."""

    def test_counter_persists(self, temp_state_file, consec_config):
        """Consecutive loss count survives restart."""
        mgr1 = DrawdownManager(
            account_size=50000.0,
            config=consec_config,
            persistence_path=temp_state_file,
        )
        mgr1.record_realized_pnl(-100.0)
        mgr1.record_realized_pnl(-100.0)
        mgr1.record_realized_pnl(-100.0)

        mgr2 = DrawdownManager(
            account_size=50000.0,
            config=consec_config,
            persistence_path=temp_state_file,
        )
        assert mgr2.consecutive_losses == 3

    def test_pause_timestamp_persists(self, temp_state_file, consec_config):
        """Active pause timestamp survives restart."""
        mgr1 = DrawdownManager(
            account_size=50000.0,
            config=consec_config,
            persistence_path=temp_state_file,
        )
        for _ in range(5):
            mgr1.record_realized_pnl(-100.0)
        pause_time = mgr1.consec_pause_until

        mgr2 = DrawdownManager(
            account_size=50000.0,
            config=consec_config,
            persistence_path=temp_state_file,
        )
        assert mgr2.consec_pause_until is not None
        # Allow 1 second tolerance for serialization rounding
        assert abs((mgr2.consec_pause_until - pause_time).total_seconds()) < 1.0

    def test_no_pause_in_file_loads_none(self, temp_state_file, consec_config):
        """Missing consec_pause_until in state file loads as None."""
        today = date.today()
        iso_year, iso_week, _ = today.isocalendar()
        data = {
            'consecutive_losses': 2,
            'consec_pause_until': None,
            'current_iso_year': iso_year,
            'current_iso_week': iso_week,
            'current_month': today.month,
            'current_month_year': today.year,
        }
        temp_state_file.write_text(json.dumps(data))

        mgr = DrawdownManager(
            account_size=50000.0,
            config=consec_config,
            persistence_path=temp_state_file,
        )
        assert mgr.consecutive_losses == 2
        assert mgr.consec_pause_until is None

    def test_consec_state_in_json(self, temp_state_file, consec_config):
        """Verify consecutive fields appear in saved JSON."""
        mgr = DrawdownManager(
            account_size=50000.0,
            config=consec_config,
            persistence_path=temp_state_file,
        )
        for _ in range(5):
            mgr.record_realized_pnl(-100.0)

        data = json.loads(temp_state_file.read_text())
        assert data['consecutive_losses'] == 5
        assert data['consec_pause_until'] is not None


# ============================================================================
# PERIOD ROLLOVER INTERACTION
# ============================================================================

class TestPeriodRolloverInteraction:
    """Test that period rollovers do NOT reset consecutive counter or pause."""

    def test_weekly_rollover_preserves_consec(self, temp_state_file, full_config):
        """Weekly rollover does not reset consecutive_losses."""
        mgr = DrawdownManager(
            account_size=50000.0,
            config=full_config,
            persistence_path=temp_state_file,
        )
        mgr.record_realized_pnl(-100.0)
        mgr.record_realized_pnl(-100.0)
        mgr.record_realized_pnl(-100.0)
        assert mgr.consecutive_losses == 3

        # Simulate weekly rollover
        today = date.today()
        iso_year, iso_week, _ = today.isocalendar()
        next_week = iso_week + 1 if iso_week < 52 else 1
        next_year = iso_year if iso_week < 52 else iso_year + 1
        next_week_date = date.fromisocalendar(next_year, next_week, 1)
        mgr.check_period_rollovers(now=next_week_date)

        # Weekly P&L reset, but consecutive counter untouched
        assert mgr.weekly_realized_pnl == 0.0
        assert mgr.consecutive_losses == 3

    def test_monthly_rollover_preserves_consec(self, temp_state_file, full_config):
        """Monthly rollover does not reset consecutive_losses."""
        mgr = DrawdownManager(
            account_size=50000.0,
            config=full_config,
            persistence_path=temp_state_file,
        )
        for _ in range(4):
            mgr.record_realized_pnl(-100.0)

        today = date.today()
        if today.month == 12:
            next_month_date = date(today.year + 1, 1, 5)
        else:
            next_month_date = date(today.year, today.month + 1, 5)

        mgr.check_period_rollovers(now=next_month_date)

        assert mgr.monthly_realized_pnl == 0.0
        assert mgr.consecutive_losses == 4

    def test_rollover_preserves_active_pause(self, temp_state_file, full_config):
        """Active pause survives period rollover."""
        mgr = DrawdownManager(
            account_size=50000.0,
            config=full_config,
            persistence_path=temp_state_file,
        )
        for _ in range(5):
            mgr.record_realized_pnl(-100.0)
        pause_time = mgr.consec_pause_until
        assert pause_time is not None

        today = date.today()
        future = date(today.year + 1, 1, 10)
        mgr.check_period_rollovers(now=future)

        assert mgr.consec_pause_until == pause_time


# ============================================================================
# INTERACTION WITH WEEKLY/MONTHLY BREAKERS
# ============================================================================

class TestBreakerInteraction:
    """Test consecutive loss pause priority vs weekly/monthly breakers."""

    def test_consec_checked_before_weekly(self, temp_state_file, full_config):
        """Consecutive loss reason returned even when weekly also triggered."""
        mgr = DrawdownManager(
            account_size=50000.0,
            config=full_config,
            persistence_path=temp_state_file,
            max_daily_loss_pct=100.0,
        )
        # Trigger all: 5 losses of $500 each = -$2500 total (>$2000 weekly limit)
        for _ in range(5):
            mgr.record_realized_pnl(-500.0)

        assert mgr.consec_pause_until is not None
        assert mgr.weekly_breaker_triggered is True

        allowed, reason = mgr.can_trade()
        assert allowed is False
        assert "Consecutive loss pause" in reason  # Checked first

    def test_weekly_blocks_after_consec_expires(self, temp_state_file, full_config):
        """After consec pause expires, weekly breaker still blocks."""
        mgr = DrawdownManager(
            account_size=50000.0,
            config=full_config,
            persistence_path=temp_state_file,
            max_daily_loss_pct=100.0,
        )
        for _ in range(5):
            mgr.record_realized_pnl(-500.0)

        # Expire the pause
        mgr.consec_pause_until = datetime.now() - timedelta(hours=1)
        allowed, reason = mgr.can_trade()
        assert allowed is False
        assert "Weekly" in reason  # Now weekly is the blocker

    def test_consec_only_blocks_when_all_others_ok(self, temp_state_file):
        """Consec alone can block even when weekly/monthly are fine."""
        cfg = {
            'weekly': {'enabled': True, 'max_loss_pct': 50.0},  # Very high
            'monthly': {'enabled': True, 'max_loss_pct': 50.0},
            'consecutive_losses': {
                'enabled': True,
                'max_consecutive': 3,
                'pause_hours': 24,
            },
        }
        mgr = DrawdownManager(
            account_size=50000.0,
            config=cfg,
            persistence_path=temp_state_file,
        )
        # 3 small losses won't trigger weekly/monthly
        for _ in range(3):
            mgr.record_realized_pnl(-10.0)

        assert mgr.weekly_breaker_triggered is False
        assert mgr.monthly_breaker_triggered is False
        allowed, reason = mgr.can_trade()
        assert allowed is False
        assert "Consecutive loss pause" in reason


# ============================================================================
# GET_STATUS() TESTS
# ============================================================================

class TestConsecGetStatus:
    """Test get_status() consecutive_losses fields."""

    def test_initial_status(self, manager):
        """Initial status shows zero count, not paused."""
        status = manager.get_status()
        consec = status['consecutive_losses']
        assert consec['enabled'] is True
        assert consec['count'] == 0
        assert consec['max_consecutive'] == 5
        assert consec['pause_hours'] == 24
        assert consec['paused'] is False
        assert consec['resume_time'] is None

    def test_status_during_streak(self, manager):
        """Status shows count during losing streak (no pause yet)."""
        for _ in range(3):
            manager.record_realized_pnl(-100.0)
        status = manager.get_status()
        consec = status['consecutive_losses']
        assert consec['count'] == 3
        assert consec['paused'] is False
        assert consec['resume_time'] is None

    def test_status_during_pause(self, manager):
        """Status shows paused state and resume time."""
        for _ in range(5):
            manager.record_realized_pnl(-100.0)
        status = manager.get_status()
        consec = status['consecutive_losses']
        assert consec['count'] == 5
        assert consec['paused'] is True
        assert consec['resume_time'] is not None
        # Verify resume_time is valid ISO format
        datetime.fromisoformat(consec['resume_time'])

    def test_status_after_pause_expires(self, manager):
        """Status shows not paused after expiry (before can_trade clears it)."""
        for _ in range(5):
            manager.record_realized_pnl(-100.0)
        manager.consec_pause_until = datetime.now() - timedelta(hours=1)
        status = manager.get_status()
        consec = status['consecutive_losses']
        # paused is false because time has passed
        assert consec['paused'] is False
        # resume_time still set (will be cleared on next can_trade)
        assert consec['resume_time'] is not None

    def test_status_disabled(self, temp_state_file):
        """Disabled consecutive losses shows in status."""
        mgr = DrawdownManager(
            account_size=50000.0,
            config={'consecutive_losses': {'enabled': False}},
            persistence_path=temp_state_file,
        )
        status = mgr.get_status()
        assert status['consecutive_losses']['enabled'] is False
        assert status['consecutive_losses']['paused'] is False


# ============================================================================
# EDGE CASES
# ============================================================================

class TestConsecEdgeCases:
    """Edge cases for consecutive loss tracking."""

    def test_very_small_losses_still_count(self, manager):
        """Even tiny losses (e.g., -$0.01) count as consecutive losses."""
        for _ in range(5):
            manager.record_realized_pnl(-0.01)
        assert manager.consecutive_losses == 5
        assert manager.consec_pause_until is not None

    def test_max_consecutive_of_1(self, temp_state_file):
        """Max consecutive of 1 pauses after a single loss."""
        cfg = {
            'consecutive_losses': {
                'enabled': True,
                'max_consecutive': 1,
                'pause_hours': 1,
            },
        }
        mgr = DrawdownManager(
            account_size=50000.0,
            config=cfg,
            persistence_path=temp_state_file,
        )
        mgr.record_realized_pnl(-100.0)
        assert mgr.consec_pause_until is not None
        allowed, _ = mgr.can_trade()
        assert allowed is False

    def test_alternating_loss_win_never_triggers(self, manager):
        """Alternating L-W-L-W never reaches 5 consecutive."""
        for _ in range(100):
            manager.record_realized_pnl(-100.0)
            manager.record_realized_pnl(50.0)
        assert manager.consecutive_losses == 0
        assert manager.consec_pause_until is None

    def test_zero_pause_hours(self, temp_state_file):
        """Zero pause_hours means pause expires immediately."""
        cfg = {
            'consecutive_losses': {
                'enabled': True,
                'max_consecutive': 3,
                'pause_hours': 0,
            },
        }
        mgr = DrawdownManager(
            account_size=50000.0,
            config=cfg,
            persistence_path=temp_state_file,
        )
        for _ in range(3):
            mgr.record_realized_pnl(-100.0)
        # Pause is set but essentially expired (0 hours from when it was set)
        # A tiny delay means now() > pause_until
        # Force can_trade check
        import time
        time.sleep(0.01)
        allowed, _ = mgr.can_trade()
        assert allowed is True

    def test_counter_continues_across_restart(self, temp_state_file, consec_config):
        """Counter at 3 persists; 2 more losses after restart triggers pause."""
        mgr1 = DrawdownManager(
            account_size=50000.0,
            config=consec_config,
            persistence_path=temp_state_file,
        )
        for _ in range(3):
            mgr1.record_realized_pnl(-100.0)

        # Restart
        mgr2 = DrawdownManager(
            account_size=50000.0,
            config=consec_config,
            persistence_path=temp_state_file,
        )
        assert mgr2.consecutive_losses == 3
        mgr2.record_realized_pnl(-100.0)
        assert mgr2.consecutive_losses == 4
        assert mgr2.consec_pause_until is None
        mgr2.record_realized_pnl(-100.0)
        assert mgr2.consecutive_losses == 5
        assert mgr2.consec_pause_until is not None


# ============================================================================
# PORTFOLIO MANAGER INTEGRATION
# ============================================================================

class TestConsecPortfolioIntegration:
    """Test consecutive loss pause through PortfolioManager."""

    @pytest.fixture
    def portfolio(self, tmp_path):
        from src.core.portfolio_manager import PortfolioManager
        return PortfolioManager(
            account_size=50000.0,
            max_total_positions=2,
            max_0dte_positions=1,
            max_daily_loss_pct=10.0,  # High so daily breaker doesn't interfere
            drawdown_limits={
                'weekly': {'enabled': False},
                'monthly': {'enabled': False},
                'consecutive_losses': {
                    'enabled': True,
                    'max_consecutive': 3,
                    'pause_hours': 24,
                },
            },
            persistence_path=tmp_path / "portfolio_state.json",
        )

    def test_consec_blocks_can_enter(self, portfolio):
        """Consecutive loss pause blocks can_enter_position."""
        from src.core.portfolio_manager import StrategyType
        for _ in range(3):
            portfolio.update_realized_pnl(-100.0)

        allowed, reason = portfolio.can_enter_position(
            strategy=StrategyType.DAILY_INCOME,
            contracts=1,
            max_risk_per_contract=200.0,
        )
        assert allowed is False
        assert "Consecutive loss pause" in reason

    def test_close_position_increments_counter(self, portfolio):
        """close_position with loss increments consecutive counter."""
        from src.core.portfolio_manager import StrategyType
        portfolio.register_position(
            position_id='t1',
            strategy=StrategyType.DAILY_INCOME,
            direction='bearish',
            contracts=1,
            max_risk=200.0,
        )
        portfolio.close_position('t1', realized_pnl=-150.0)
        assert portfolio.drawdown_manager.consecutive_losses == 1

    def test_close_position_win_resets_counter(self, portfolio):
        """close_position with profit resets consecutive counter."""
        from src.core.portfolio_manager import StrategyType
        # Build a streak
        portfolio.update_realized_pnl(-100.0)
        portfolio.update_realized_pnl(-100.0)
        assert portfolio.drawdown_manager.consecutive_losses == 2

        portfolio.register_position(
            position_id='t1',
            strategy=StrategyType.DAILY_INCOME,
            direction='bearish',
            contracts=1,
            max_risk=200.0,
        )
        portfolio.close_position('t1', realized_pnl=50.0)
        assert portfolio.drawdown_manager.consecutive_losses == 0

    def test_consec_in_get_status(self, portfolio):
        """PortfolioManager get_status includes consecutive_losses."""
        status = portfolio.get_status()
        assert 'drawdown_limits' in status
        assert 'consecutive_losses' in status['drawdown_limits']
        consec = status['drawdown_limits']['consecutive_losses']
        assert consec['enabled'] is True
        assert consec['max_consecutive'] == 3
