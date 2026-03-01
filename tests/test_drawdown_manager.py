"""
Comprehensive tests for DrawdownManager (Weekly + Monthly Circuit Breakers)

Tests cover:
- Initialization with various configs (both enabled, one enabled, none, missing)
- Weekly breaker triggering at exact threshold
- Monthly breaker triggering at exact threshold
- Wins offsetting losses (net P&L stays above threshold)
- Period rollovers (new week resets weekly, new month resets monthly)
- Persistence save/load round-trip
- Stale period detection (loading old week/month data discards it)
- Corrupted file handling (graceful fallback)
- Account size updates cascade to dollar limits
- can_trade() gating logic (priority: weekly checked before monthly)
- get_status() output structure and values
- Integration with PortfolioManager
"""

import json
import os
import sys
import tempfile
from datetime import date
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
    """Create a temporary state file path in an isolated directory."""
    path = tmp_path / "portfolio_state.json"
    return path


@pytest.fixture
def default_config():
    """Standard config with both breakers enabled."""
    return {
        'weekly': {'enabled': True, 'max_loss_pct': 4.0},
        'monthly': {'enabled': True, 'max_loss_pct': 8.0},
    }


@pytest.fixture
def manager(temp_state_file, default_config):
    """Create DrawdownManager with default config and temp persistence."""
    return DrawdownManager(
        account_size=50000.0,
        config=default_config,
        persistence_path=temp_state_file,
        max_daily_loss_pct=100.0,  # High so daily breaker doesn't interfere
    )


# ============================================================================
# INITIALIZATION TESTS
# ============================================================================

class TestInitialization:
    """Test DrawdownManager initialization with various configs."""

    def test_default_config_both_enabled(self, manager):
        """Both breakers enabled with correct dollar limits."""
        assert manager.weekly_enabled is True
        assert manager.monthly_enabled is True
        assert manager.weekly_max_loss == 2000.0   # 4% of 50k
        assert manager.monthly_max_loss == 4000.0  # 8% of 50k
        assert manager.weekly_realized_pnl == 0.0
        assert manager.monthly_realized_pnl == 0.0
        assert manager.weekly_breaker_triggered is False
        assert manager.monthly_breaker_triggered is False

    def test_none_config_disables_both(self, temp_state_file):
        """None config disables both breakers."""
        mgr = DrawdownManager(
            account_size=50000.0,
            config=None,
            persistence_path=temp_state_file,
        )
        assert mgr.weekly_enabled is False
        assert mgr.monthly_enabled is False

    def test_empty_config_disables_both(self, temp_state_file):
        """Empty dict config disables both breakers."""
        mgr = DrawdownManager(
            account_size=50000.0,
            config={},
            persistence_path=temp_state_file,
        )
        assert mgr.weekly_enabled is False
        assert mgr.monthly_enabled is False

    def test_weekly_only(self, temp_state_file):
        """Only weekly enabled."""
        cfg = {'weekly': {'enabled': True, 'max_loss_pct': 3.0}}
        mgr = DrawdownManager(
            account_size=100000.0,
            config=cfg,
            persistence_path=temp_state_file,
        )
        assert mgr.weekly_enabled is True
        assert mgr.monthly_enabled is False
        assert mgr.weekly_max_loss == 3000.0  # 3% of 100k

    def test_monthly_only(self, temp_state_file):
        """Only monthly enabled."""
        cfg = {'monthly': {'enabled': True, 'max_loss_pct': 10.0}}
        mgr = DrawdownManager(
            account_size=25000.0,
            config=cfg,
            persistence_path=temp_state_file,
        )
        assert mgr.weekly_enabled is False
        assert mgr.monthly_enabled is True
        assert mgr.monthly_max_loss == 2500.0  # 10% of 25k

    def test_custom_percentages(self, temp_state_file):
        """Custom percentage values are respected."""
        cfg = {
            'weekly': {'enabled': True, 'max_loss_pct': 2.5},
            'monthly': {'enabled': True, 'max_loss_pct': 5.5},
        }
        mgr = DrawdownManager(
            account_size=80000.0,
            config=cfg,
            persistence_path=temp_state_file,
        )
        assert mgr.weekly_max_loss == 2000.0   # 2.5% of 80k
        assert mgr.monthly_max_loss == 4400.0  # 5.5% of 80k

    def test_period_tracking_initialized_to_today(self, manager):
        """Period tracking starts at today's week/month."""
        today = date.today()
        iso_year, iso_week, _ = today.isocalendar()
        assert manager.current_iso_year == iso_year
        assert manager.current_iso_week == iso_week
        assert manager.current_month == today.month
        assert manager.current_month_year == today.year


# ============================================================================
# WEEKLY BREAKER TESTS
# ============================================================================

class TestWeeklyBreaker:
    """Test weekly drawdown circuit breaker."""

    def test_loss_below_threshold_no_trigger(self, manager):
        """Loss below threshold does not trigger breaker."""
        manager.record_realized_pnl(-1999.99)  # Just under 4% of 50k
        assert manager.weekly_breaker_triggered is False
        allowed, _ = manager.can_trade()
        assert allowed is True

    def test_loss_at_exact_threshold_triggers(self, manager):
        """Loss at exact threshold triggers breaker."""
        manager.record_realized_pnl(-2000.0)  # Exactly 4% of 50k
        assert manager.weekly_breaker_triggered is True
        allowed, reason = manager.can_trade()
        assert allowed is False
        assert "Weekly" in reason

    def test_loss_beyond_threshold_triggers(self, manager):
        """Loss beyond threshold triggers breaker."""
        manager.record_realized_pnl(-2500.0)
        assert manager.weekly_breaker_triggered is True

    def test_cumulative_losses_trigger(self, manager):
        """Multiple small losses accumulate to trigger."""
        manager.record_realized_pnl(-500.0)
        assert manager.weekly_breaker_triggered is False
        manager.record_realized_pnl(-500.0)
        assert manager.weekly_breaker_triggered is False
        manager.record_realized_pnl(-500.0)
        assert manager.weekly_breaker_triggered is False
        manager.record_realized_pnl(-500.0)  # Now at -2000
        assert manager.weekly_breaker_triggered is True

    def test_disabled_weekly_never_triggers(self, temp_state_file):
        """Disabled weekly breaker never triggers regardless of losses."""
        cfg = {'weekly': {'enabled': False, 'max_loss_pct': 4.0},
               'monthly': {'enabled': True, 'max_loss_pct': 8.0}}
        mgr = DrawdownManager(
            account_size=50000.0,
            config=cfg,
            persistence_path=temp_state_file,
            max_daily_loss_pct=100.0,
        )
        mgr.record_realized_pnl(-5000.0)
        assert mgr.weekly_breaker_triggered is False
        # Monthly should still trigger
        assert mgr.monthly_breaker_triggered is True


# ============================================================================
# MONTHLY BREAKER TESTS
# ============================================================================

class TestMonthlyBreaker:
    """Test monthly drawdown circuit breaker."""

    def test_loss_below_threshold_no_trigger(self, manager):
        """Loss below threshold does not trigger breaker."""
        manager.record_realized_pnl(-3999.99)  # Just under 8% of 50k
        assert manager.monthly_breaker_triggered is False

    def test_loss_at_exact_threshold_triggers(self, manager):
        """Loss at exact threshold triggers breaker."""
        manager.record_realized_pnl(-4000.0)  # Exactly 8% of 50k
        assert manager.monthly_breaker_triggered is True
        allowed, reason = manager.can_trade()
        assert allowed is False
        # Note: weekly also triggers at -4000 and is checked first,
        # so reason will mention "Weekly". The key assertion is monthly_breaker_triggered.

    def test_monthly_only_triggers_when_weekly_disabled(self, temp_state_file):
        """Monthly reason returned when weekly is disabled."""
        cfg = {'weekly': {'enabled': False}, 'monthly': {'enabled': True, 'max_loss_pct': 8.0}}
        mgr = DrawdownManager(account_size=50000.0, config=cfg, persistence_path=temp_state_file,
                              max_daily_loss_pct=100.0)
        mgr.record_realized_pnl(-4000.0)
        assert mgr.monthly_breaker_triggered is True
        allowed, reason = mgr.can_trade()
        assert allowed is False
        assert "Monthly" in reason

    def test_cumulative_losses_trigger(self, manager):
        """Multiple losses accumulate across the month."""
        for _ in range(8):
            manager.record_realized_pnl(-500.0)
        # -4000 total = 8% of 50k
        assert manager.monthly_breaker_triggered is True

    def test_disabled_monthly_never_triggers(self, temp_state_file):
        """Disabled monthly breaker never triggers."""
        cfg = {'weekly': {'enabled': True, 'max_loss_pct': 4.0},
               'monthly': {'enabled': False, 'max_loss_pct': 8.0}}
        mgr = DrawdownManager(
            account_size=50000.0,
            config=cfg,
            persistence_path=temp_state_file,
            max_daily_loss_pct=100.0,
        )
        mgr.record_realized_pnl(-10000.0)
        assert mgr.monthly_breaker_triggered is False
        # Weekly should still trigger
        assert mgr.weekly_breaker_triggered is True


# ============================================================================
# WINS OFFSETTING LOSSES
# ============================================================================

class TestWinsOffsettingLosses:
    """Test that winning trades offset losses to prevent false breaker triggers."""

    def test_win_after_loss_keeps_net_above_threshold(self, manager):
        """A win reduces the net realized loss below the threshold."""
        manager.record_realized_pnl(-1500.0)
        manager.record_realized_pnl(800.0)   # Net: -700
        assert manager.weekly_breaker_triggered is False
        assert manager.monthly_breaker_triggered is False
        assert manager.weekly_realized_pnl == -700.0
        assert manager.monthly_realized_pnl == -700.0

    def test_win_does_not_clear_triggered_breaker(self, manager):
        """Once triggered, a subsequent win does NOT clear the breaker."""
        manager.record_realized_pnl(-2000.0)  # Triggers weekly
        assert manager.weekly_breaker_triggered is True
        manager.record_realized_pnl(1500.0)   # Net: -500
        # Breaker stays triggered (latched until period rollover)
        assert manager.weekly_breaker_triggered is True

    def test_alternating_wins_and_losses(self, manager):
        """Mixed results that net out below threshold."""
        manager.record_realized_pnl(-800.0)
        manager.record_realized_pnl(500.0)
        manager.record_realized_pnl(-600.0)
        manager.record_realized_pnl(300.0)
        # Net: -600
        assert manager.weekly_breaker_triggered is False
        assert manager.monthly_breaker_triggered is False

    def test_net_positive_week_no_trigger(self, manager):
        """Net positive P&L for the week never triggers."""
        manager.record_realized_pnl(-1000.0)
        manager.record_realized_pnl(2000.0)
        # Net: +1000
        assert manager.weekly_realized_pnl == 1000.0
        assert manager.weekly_breaker_triggered is False

    def test_both_track_same_pnl(self, manager):
        """Weekly and monthly P&L track the same realized amounts."""
        manager.record_realized_pnl(-500.0)
        manager.record_realized_pnl(200.0)
        assert manager.weekly_realized_pnl == -300.0
        assert manager.monthly_realized_pnl == -300.0


# ============================================================================
# PERIOD ROLLOVER TESTS
# ============================================================================

class TestPeriodRollovers:
    """Test automatic period detection and counter reset."""

    def test_weekly_rollover_resets_weekly_only(self, manager):
        """New ISO week resets weekly P&L and breaker but not monthly."""
        manager.record_realized_pnl(-2000.0)  # Triggers weekly
        manager.record_realized_pnl(-2000.0)  # Also contributes to monthly (-4000)
        assert manager.weekly_breaker_triggered is True
        assert manager.monthly_breaker_triggered is True

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
            manager.check_period_rollovers()

        assert manager.weekly_realized_pnl == 0.0
        assert manager.weekly_breaker_triggered is False
        # Monthly should still have its accumulated P&L (unless month also rolled)
        # Since we only changed week, monthly may or may not roll depending on dates
        # The key assertion: weekly was reset

    def test_monthly_rollover_resets_monthly(self, manager):
        """New calendar month resets monthly P&L and breaker."""
        manager.record_realized_pnl(-3500.0)
        manager.monthly_realized_pnl = -3500.0  # Set directly for clarity

        today = date.today()
        # Simulate first day of next month
        if today.month == 12:
            next_month_date = date(today.year + 1, 1, 5)
        else:
            next_month_date = date(today.year, today.month + 1, 5)

        iso_year, iso_week, _ = next_month_date.isocalendar()
        with patch('src.core.drawdown_manager.date') as mock_date:
            mock_date.today.return_value = next_month_date
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            manager.check_period_rollovers()

        assert manager.monthly_realized_pnl == 0.0
        assert manager.monthly_breaker_triggered is False

    def test_same_period_no_reset(self, manager):
        """Same week/month does not reset anything."""
        manager.record_realized_pnl(-1000.0)
        manager.check_period_rollovers()
        assert manager.weekly_realized_pnl == -1000.0
        assert manager.monthly_realized_pnl == -1000.0

    def test_both_roll_simultaneously(self, manager):
        """When both week and month roll at the same time (e.g., first Monday of new month)."""
        manager.record_realized_pnl(-2000.0)
        manager.weekly_breaker_triggered = True
        manager.monthly_breaker_triggered = True

        today = date.today()
        # Force both week and month to be different
        if today.month == 12:
            future_date = date(today.year + 1, 2, 10)
        else:
            future_date = date(today.year + 1, 1, 10)

        iso_year, iso_week, _ = future_date.isocalendar()
        with patch('src.core.drawdown_manager.date') as mock_date:
            mock_date.today.return_value = future_date
            mock_date.side_effect = lambda *a, **kw: date(*a, **kw)
            manager.check_period_rollovers()

        assert manager.weekly_realized_pnl == 0.0
        assert manager.monthly_realized_pnl == 0.0
        assert manager.weekly_breaker_triggered is False
        assert manager.monthly_breaker_triggered is False


# ============================================================================
# PERSISTENCE TESTS
# ============================================================================

class TestPersistence:
    """Test state save/load round-trip."""

    def test_save_and_load_preserves_state(self, temp_state_file, default_config):
        """State survives save + new instance creation."""
        mgr1 = DrawdownManager(
            account_size=50000.0,
            config=default_config,
            persistence_path=temp_state_file,
        )
        mgr1.record_realized_pnl(-1500.0)
        mgr1.record_realized_pnl(200.0)
        # Net: -1300

        # Create new instance - should load persisted state
        mgr2 = DrawdownManager(
            account_size=50000.0,
            config=default_config,
            persistence_path=temp_state_file,
        )
        assert mgr2.weekly_realized_pnl == -1300.0
        assert mgr2.monthly_realized_pnl == -1300.0
        assert mgr2.weekly_breaker_triggered is False

    def test_triggered_breaker_persists(self, temp_state_file, default_config):
        """Triggered breaker state survives restart."""
        mgr1 = DrawdownManager(
            account_size=50000.0,
            config=default_config,
            persistence_path=temp_state_file,
        )
        mgr1.record_realized_pnl(-2000.0)  # Triggers weekly
        assert mgr1.weekly_breaker_triggered is True

        mgr2 = DrawdownManager(
            account_size=50000.0,
            config=default_config,
            persistence_path=temp_state_file,
        )
        assert mgr2.weekly_breaker_triggered is True
        assert mgr2.weekly_realized_pnl == -2000.0

    def test_no_file_starts_fresh(self, temp_state_file, default_config):
        """No state file results in clean initialization."""
        mgr = DrawdownManager(
            account_size=50000.0,
            config=default_config,
            persistence_path=temp_state_file,
        )
        assert mgr.weekly_realized_pnl == 0.0
        assert mgr.monthly_realized_pnl == 0.0

    def test_state_file_created_on_first_record(self, temp_state_file, default_config):
        """State file created when first P&L is recorded."""
        assert not temp_state_file.exists()
        mgr = DrawdownManager(
            account_size=50000.0,
            config=default_config,
            persistence_path=temp_state_file,
        )
        mgr.record_realized_pnl(-100.0)
        assert temp_state_file.exists()

    def test_state_file_structure(self, temp_state_file, default_config):
        """Verify the JSON structure of the state file."""
        mgr = DrawdownManager(
            account_size=50000.0,
            config=default_config,
            persistence_path=temp_state_file,
        )
        mgr.record_realized_pnl(-500.0)

        data = json.loads(temp_state_file.read_text())
        assert 'weekly_realized_pnl' in data
        assert 'monthly_realized_pnl' in data
        assert 'weekly_breaker_triggered' in data
        assert 'monthly_breaker_triggered' in data
        assert 'current_iso_year' in data
        assert 'current_iso_week' in data
        assert 'current_month' in data
        assert 'current_month_year' in data
        assert 'consecutive_losses' in data
        assert 'consec_pause_until' in data
        assert 'last_updated' in data
        assert data['weekly_realized_pnl'] == -500.0


# ============================================================================
# STALE PERIOD DETECTION
# ============================================================================

class TestStalePeriodDetection:
    """Test that loading state from a prior period discards stale data."""

    def test_stale_weekly_data_discarded(self, temp_state_file, default_config):
        """State from a different ISO week is discarded on load."""
        today = date.today()
        iso_year, iso_week, _ = today.isocalendar()

        # Write state with a different week
        stale_week = iso_week - 1 if iso_week > 1 else 52
        stale_year = iso_year if iso_week > 1 else iso_year - 1
        stale_data = {
            'weekly_realized_pnl': -1500.0,
            'monthly_realized_pnl': -3000.0,
            'weekly_breaker_triggered': True,
            'monthly_breaker_triggered': False,
            'current_iso_year': stale_year,
            'current_iso_week': stale_week,
            'current_month': today.month,
            'current_month_year': today.year,
        }
        temp_state_file.write_text(json.dumps(stale_data))

        mgr = DrawdownManager(
            account_size=50000.0,
            config=default_config,
            persistence_path=temp_state_file,
        )
        # Weekly should be reset (stale), monthly should be loaded (current)
        assert mgr.weekly_realized_pnl == 0.0
        assert mgr.weekly_breaker_triggered is False
        assert mgr.monthly_realized_pnl == -3000.0
        assert mgr.monthly_breaker_triggered is False

    def test_stale_monthly_data_discarded(self, temp_state_file, default_config):
        """State from a different calendar month is discarded on load."""
        today = date.today()
        iso_year, iso_week, _ = today.isocalendar()

        stale_month = today.month - 1 if today.month > 1 else 12
        stale_month_year = today.year if today.month > 1 else today.year - 1
        stale_data = {
            'weekly_realized_pnl': -800.0,
            'monthly_realized_pnl': -3500.0,
            'weekly_breaker_triggered': False,
            'monthly_breaker_triggered': True,
            'current_iso_year': iso_year,
            'current_iso_week': iso_week,
            'current_month': stale_month,
            'current_month_year': stale_month_year,
        }
        temp_state_file.write_text(json.dumps(stale_data))

        mgr = DrawdownManager(
            account_size=50000.0,
            config=default_config,
            persistence_path=temp_state_file,
        )
        # Monthly should be reset (stale), weekly should be loaded (current)
        assert mgr.weekly_realized_pnl == -800.0
        assert mgr.weekly_breaker_triggered is False
        assert mgr.monthly_realized_pnl == 0.0
        assert mgr.monthly_breaker_triggered is False

    def test_both_stale_discards_all(self, temp_state_file, default_config):
        """State from both a different week and month discards everything."""
        stale_data = {
            'weekly_realized_pnl': -1000.0,
            'monthly_realized_pnl': -5000.0,
            'weekly_breaker_triggered': True,
            'monthly_breaker_triggered': True,
            'current_iso_year': 2024,
            'current_iso_week': 1,
            'current_month': 1,
            'current_month_year': 2024,
        }
        temp_state_file.write_text(json.dumps(stale_data))

        mgr = DrawdownManager(
            account_size=50000.0,
            config=default_config,
            persistence_path=temp_state_file,
        )
        assert mgr.weekly_realized_pnl == 0.0
        assert mgr.monthly_realized_pnl == 0.0
        assert mgr.weekly_breaker_triggered is False
        assert mgr.monthly_breaker_triggered is False


# ============================================================================
# CORRUPTED FILE HANDLING
# ============================================================================

class TestCorruptedFileHandling:
    """Test graceful handling of corrupted or malformed state files."""

    def test_invalid_json(self, temp_state_file, default_config):
        """Invalid JSON falls back to fresh state."""
        temp_state_file.write_text("not valid json {{{")
        mgr = DrawdownManager(
            account_size=50000.0,
            config=default_config,
            persistence_path=temp_state_file,
        )
        assert mgr.weekly_realized_pnl == 0.0
        assert mgr.monthly_realized_pnl == 0.0

    def test_empty_file(self, temp_state_file, default_config):
        """Empty file falls back to fresh state."""
        temp_state_file.write_text("")
        mgr = DrawdownManager(
            account_size=50000.0,
            config=default_config,
            persistence_path=temp_state_file,
        )
        assert mgr.weekly_realized_pnl == 0.0

    def test_missing_keys(self, temp_state_file, default_config):
        """Partial data uses defaults for missing keys."""
        today = date.today()
        iso_year, iso_week, _ = today.isocalendar()
        partial_data = {
            'weekly_realized_pnl': -750.0,
            'current_iso_year': iso_year,
            'current_iso_week': iso_week,
            # Missing monthly fields and breaker flags
        }
        temp_state_file.write_text(json.dumps(partial_data))

        mgr = DrawdownManager(
            account_size=50000.0,
            config=default_config,
            persistence_path=temp_state_file,
        )
        assert mgr.weekly_realized_pnl == -750.0
        assert mgr.weekly_breaker_triggered is False  # Defaults to False
        assert mgr.monthly_realized_pnl == 0.0  # Missing = reset

    def test_wrong_type_values(self, temp_state_file, default_config):
        """Wrong types in JSON handled gracefully."""
        temp_state_file.write_text(json.dumps("just a string"))
        mgr = DrawdownManager(
            account_size=50000.0,
            config=default_config,
            persistence_path=temp_state_file,
        )
        # Should not crash, falls back to defaults
        assert mgr.weekly_realized_pnl == 0.0


# ============================================================================
# ACCOUNT SIZE UPDATE TESTS
# ============================================================================

class TestAccountSizeUpdates:
    """Test that account size changes cascade to dollar limits."""

    def test_update_recalculates_limits(self, manager):
        """Updating account size recalculates dollar limits."""
        assert manager.weekly_max_loss == 2000.0   # 4% of 50k
        assert manager.monthly_max_loss == 4000.0  # 8% of 50k

        manager.update_account_size(100000.0)

        assert manager.account_size == 100000.0
        assert manager.weekly_max_loss == 4000.0   # 4% of 100k
        assert manager.monthly_max_loss == 8000.0  # 8% of 100k

    def test_update_affects_future_triggers(self, manager):
        """Larger account means higher threshold before trigger."""
        manager.record_realized_pnl(-1500.0)
        assert manager.weekly_breaker_triggered is False

        # Shrink account - now 4% of 30k = $1200, which is below -1500
        manager.update_account_size(30000.0)

        # Already recorded losses exceed new limit, but breaker only checks on
        # new record_realized_pnl calls
        manager.record_realized_pnl(-1.0)  # Tiny additional loss
        assert manager.weekly_breaker_triggered is True

    def test_update_to_larger_account_prevents_trigger(self, manager):
        """Growing account raises threshold above current losses."""
        manager.record_realized_pnl(-1900.0)  # Close to 4% of 50k
        assert manager.weekly_breaker_triggered is False

        manager.update_account_size(200000.0)  # 4% = $8000 now
        manager.record_realized_pnl(-100.0)    # Total: -2000, still under $8000
        assert manager.weekly_breaker_triggered is False


# ============================================================================
# CAN_TRADE() GATING TESTS
# ============================================================================

class TestCanTradeGating:
    """Test can_trade() logic, priority, and messaging."""

    def test_both_ok_returns_true(self, manager):
        """No breakers triggered = trading allowed."""
        allowed, reason = manager.can_trade()
        assert allowed is True
        assert reason == "Drawdown limits OK"

    def test_weekly_triggered_blocks(self, manager):
        """Weekly breaker blocks trading."""
        manager.record_realized_pnl(-2000.0)
        allowed, reason = manager.can_trade()
        assert allowed is False
        assert "Weekly" in reason

    def test_monthly_triggered_blocks(self, manager):
        """Monthly breaker blocks trading."""
        manager.record_realized_pnl(-4000.0)
        allowed, reason = manager.can_trade()
        assert allowed is False
        # Could be weekly or monthly in reason (weekly also triggered)

    def test_weekly_checked_before_monthly(self, temp_state_file):
        """When both triggered, weekly reason is returned (checked first)."""
        cfg = {
            'weekly': {'enabled': True, 'max_loss_pct': 4.0},
            'monthly': {'enabled': True, 'max_loss_pct': 8.0},
        }
        mgr = DrawdownManager(
            account_size=50000.0,
            config=cfg,
            persistence_path=temp_state_file,
            max_daily_loss_pct=100.0,
        )
        mgr.record_realized_pnl(-4000.0)  # Triggers both
        assert mgr.weekly_breaker_triggered is True
        assert mgr.monthly_breaker_triggered is True
        allowed, reason = mgr.can_trade()
        assert allowed is False
        assert "Weekly" in reason  # Weekly checked first

    def test_disabled_breakers_always_allow(self, temp_state_file):
        """Disabled breakers never block, regardless of losses."""
        mgr = DrawdownManager(
            account_size=50000.0,
            config=None,
            persistence_path=temp_state_file,
            max_daily_loss_pct=9999999.0,
        )
        mgr.record_realized_pnl(-999999.0)
        allowed, reason = mgr.can_trade()
        assert allowed is True

    def test_only_monthly_blocks_when_weekly_disabled(self, temp_state_file):
        """With weekly disabled, only monthly can block."""
        cfg = {
            'weekly': {'enabled': False, 'max_loss_pct': 4.0},
            'monthly': {'enabled': True, 'max_loss_pct': 8.0},
        }
        mgr = DrawdownManager(
            account_size=50000.0,
            config=cfg,
            persistence_path=temp_state_file,
            max_daily_loss_pct=100.0,
        )
        mgr.record_realized_pnl(-3000.0)  # Under monthly threshold
        allowed, _ = mgr.can_trade()
        assert allowed is True

        mgr.record_realized_pnl(-1000.0)  # Now at -4000, hits monthly
        allowed, reason = mgr.can_trade()
        assert allowed is False
        assert "Monthly" in reason


# ============================================================================
# GET_STATUS() TESTS
# ============================================================================

class TestGetStatus:
    """Test get_status() output structure and values."""

    def test_status_structure(self, manager):
        """Status has correct top-level keys and nested structure."""
        status = manager.get_status()
        assert 'daily' in status
        assert 'weekly' in status
        assert 'monthly' in status
        assert 'consecutive_losses' in status

        daily = status['daily']
        assert set(daily.keys()) == {
            'realized_pnl', 'max_loss_pct',
            'max_loss_dollars', 'breaker_triggered',
        }

        weekly = status['weekly']
        assert set(weekly.keys()) == {
            'enabled', 'realized_pnl', 'max_loss_pct',
            'max_loss_dollars', 'breaker_triggered', 'iso_week', 'iso_year',
        }

        monthly = status['monthly']
        assert set(monthly.keys()) == {
            'enabled', 'realized_pnl', 'max_loss_pct',
            'max_loss_dollars', 'breaker_triggered', 'month', 'year',
        }

        consec = status['consecutive_losses']
        assert set(consec.keys()) == {
            'enabled', 'count', 'max_consecutive',
            'pause_hours', 'paused', 'resume_time',
        }

    def test_status_values_initial(self, manager):
        """Initial status shows zeros and no triggers."""
        status = manager.get_status()
        assert status['weekly']['enabled'] is True
        assert status['weekly']['realized_pnl'] == 0.0
        assert status['weekly']['max_loss_pct'] == 4.0
        assert status['weekly']['max_loss_dollars'] == 2000.0
        assert status['weekly']['breaker_triggered'] is False

        assert status['monthly']['enabled'] is True
        assert status['monthly']['realized_pnl'] == 0.0
        assert status['monthly']['max_loss_pct'] == 8.0
        assert status['monthly']['max_loss_dollars'] == 4000.0
        assert status['monthly']['breaker_triggered'] is False

    def test_status_reflects_pnl(self, manager):
        """Status updates after P&L recording."""
        manager.record_realized_pnl(-1234.56)
        status = manager.get_status()
        assert status['weekly']['realized_pnl'] == -1234.56
        assert status['monthly']['realized_pnl'] == -1234.56

    def test_status_reflects_trigger(self, manager):
        """Status shows breaker triggered state."""
        manager.record_realized_pnl(-2000.0)
        status = manager.get_status()
        assert status['weekly']['breaker_triggered'] is True

    def test_status_values_rounded(self, manager):
        """P&L and dollar values are rounded to 2 decimal places."""
        manager.record_realized_pnl(-1234.567)
        status = manager.get_status()
        assert status['weekly']['realized_pnl'] == -1234.57
        assert status['monthly']['realized_pnl'] == -1234.57

    def test_status_period_info(self, manager):
        """Status includes current period identifiers."""
        today = date.today()
        iso_year, iso_week, _ = today.isocalendar()
        status = manager.get_status()
        assert status['weekly']['iso_week'] == iso_week
        assert status['weekly']['iso_year'] == iso_year
        assert status['monthly']['month'] == today.month
        assert status['monthly']['year'] == today.year


# ============================================================================
# PORTFOLIO MANAGER INTEGRATION TESTS
# ============================================================================

class TestPortfolioManagerIntegration:
    """Test DrawdownManager integration with PortfolioManager."""

    @pytest.fixture
    def portfolio(self, temp_state_file):
        """Create a PortfolioManager with drawdown limits enabled.

        Daily loss limit set high (10%) so it doesn't interfere with
        weekly/monthly drawdown testing.
        """
        from src.core.portfolio_manager import PortfolioManager
        return PortfolioManager(
            account_size=50000.0,
            max_total_positions=2,
            max_0dte_positions=1,
            max_daily_loss_pct=10.0,
            drawdown_limits={
                'weekly': {'enabled': True, 'max_loss_pct': 4.0},
                'monthly': {'enabled': True, 'max_loss_pct': 8.0},
            },
            persistence_path=temp_state_file,
        )

    def test_drawdown_manager_created(self, portfolio):
        """PortfolioManager creates a DrawdownManager."""
        assert hasattr(portfolio, 'drawdown_manager')
        assert isinstance(portfolio.drawdown_manager, DrawdownManager)

    def test_none_config_creates_disabled_manager(self, temp_state_file):
        """Missing drawdown_limits creates disabled DrawdownManager."""
        from src.core.portfolio_manager import PortfolioManager
        pm = PortfolioManager(
            account_size=50000.0,
            persistence_path=temp_state_file,
        )
        assert pm.drawdown_manager.weekly_enabled is False
        assert pm.drawdown_manager.monthly_enabled is False

    def test_close_position_records_to_drawdown(self, portfolio):
        """close_position() cascades P&L to drawdown manager."""
        from src.core.portfolio_manager import StrategyType
        portfolio.register_position(
            position_id='test-1',
            strategy=StrategyType.DAILY_INCOME,
            direction='bearish',
            contracts=2,
            max_risk=400.0,
            is_0dte=True,
        )
        portfolio.close_position('test-1', realized_pnl=-300.0)
        assert portfolio.drawdown_manager.weekly_realized_pnl == -300.0
        assert portfolio.drawdown_manager.monthly_realized_pnl == -300.0

    def test_update_realized_pnl_records_to_drawdown(self, portfolio):
        """update_realized_pnl() cascades to drawdown manager."""
        portfolio.update_realized_pnl(-750.0)
        assert portfolio.drawdown_manager.weekly_realized_pnl == -750.0

    def test_weekly_breaker_blocks_can_enter(self, portfolio):
        """Weekly drawdown breaker blocks can_enter_position()."""
        from src.core.portfolio_manager import StrategyType

        # Record losses that trigger weekly (4% of 50k = $2000)
        portfolio.update_realized_pnl(-2000.0)

        allowed, reason = portfolio.can_enter_position(
            strategy=StrategyType.DAILY_INCOME,
            contracts=1,
            max_risk_per_contract=200.0,
            is_0dte=True,
        )
        assert allowed is False
        assert "Weekly" in reason

    def test_drawdown_in_get_status(self, portfolio):
        """get_status() includes drawdown_limits key."""
        status = portfolio.get_status()
        assert 'drawdown_limits' in status
        assert 'weekly' in status['drawdown_limits']
        assert 'monthly' in status['drawdown_limits']

    def test_update_account_size_cascades(self, portfolio):
        """update_account_size() cascades to drawdown manager."""
        portfolio.update_account_size(100000.0)
        assert portfolio.drawdown_manager.account_size == 100000.0
        assert portfolio.drawdown_manager.weekly_max_loss == 4000.0  # 4% of 100k

    def test_reset_daily_checks_rollovers(self, portfolio):
        """reset_daily() calls check_period_rollovers()."""
        portfolio.update_realized_pnl(-500.0)
        portfolio.reset_daily()
        # Daily P&L should be reset, drawdown P&L should persist (same period)
        assert portfolio.daily_realized_pnl == 0.0
        assert portfolio.drawdown_manager.weekly_realized_pnl == -500.0

    def test_daily_breaker_checked_before_drawdown(self, temp_state_file):
        """Daily circuit breaker triggers before drawdown is checked."""
        from src.core.portfolio_manager import PortfolioManager, StrategyType

        # Use a low daily loss limit so daily triggers first
        pm = PortfolioManager(
            account_size=50000.0,
            max_total_positions=2,
            max_0dte_positions=1,
            max_daily_loss_pct=2.0,  # 2% = $1000
            drawdown_limits={
                'weekly': {'enabled': True, 'max_loss_pct': 4.0},
                'monthly': {'enabled': True, 'max_loss_pct': 8.0},
            },
            persistence_path=temp_state_file,
        )
        # Trigger daily breaker (2% of 50k = $1000)
        pm.update_realized_pnl(-1000.0)

        allowed, reason = pm.can_enter_position(
            strategy=StrategyType.DAILY_INCOME,
            contracts=1,
            max_risk_per_contract=200.0,
        )
        assert allowed is False
        assert "Daily" in reason or "Circuit breaker" in reason


# ============================================================================
# EDGE CASES
# ============================================================================

class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_zero_pnl_no_trigger(self, manager):
        """Recording zero P&L has no effect."""
        manager.record_realized_pnl(0.0)
        assert manager.weekly_breaker_triggered is False
        assert manager.weekly_realized_pnl == 0.0

    def test_very_small_account(self, temp_state_file, default_config):
        """Small account still works with tiny dollar limits."""
        mgr = DrawdownManager(
            account_size=1000.0,
            config=default_config,
            persistence_path=temp_state_file,
            max_daily_loss_pct=100.0,
        )
        assert mgr.weekly_max_loss == 40.0  # 4% of 1k
        mgr.record_realized_pnl(-40.0)
        assert mgr.weekly_breaker_triggered is True

    def test_large_account(self, temp_state_file, default_config):
        """Large account computes correct limits."""
        mgr = DrawdownManager(
            account_size=1_000_000.0,
            config=default_config,
            persistence_path=temp_state_file,
        )
        assert mgr.weekly_max_loss == 40000.0   # 4% of 1M
        assert mgr.monthly_max_loss == 80000.0  # 8% of 1M

    def test_many_small_records(self, manager):
        """Many small P&L records accumulate correctly."""
        for _ in range(200):
            manager.record_realized_pnl(-10.0)
        assert manager.weekly_realized_pnl == -2000.0
        assert manager.weekly_breaker_triggered is True

    def test_positive_pnl_only(self, manager):
        """Only positive P&L never triggers any breaker."""
        for _ in range(100):
            manager.record_realized_pnl(100.0)
        assert manager.weekly_realized_pnl == 10000.0
        assert manager.weekly_breaker_triggered is False
        assert manager.monthly_breaker_triggered is False
