"""
Tests for budget-based position sizing.

Verifies:
- DI: floor(daily_loss_budget / max_risk), capped by daily_contracts then max_contracts
- Swing: fixed swing_contracts, capped by max_contracts
- min_contracts floor
- get_calculated_di_contracts() helper
- get_position_sizing_summary() display format
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.portfolio_manager import PortfolioManager, StrategyType


# ============================================================================
# DI Budget Sizing
# ============================================================================

class TestDIBudgetSizing:

    def test_scenario_a_small_account(self):
        """$50k, 3%, $5 spread, daily_cap=7, max=10 -> 3."""
        pm = PortfolioManager(account_size=50000, max_daily_loss_pct=3.0,
                              spread_width=5.0, daily_contracts=7, max_contracts=10)
        assert pm.calculate_position_size(StrategyType.DAILY_INCOME, 500) == 3

    def test_scenario_b_capped_by_daily(self):
        """$200k -> 12 raw -> capped at daily_contracts=7."""
        pm = PortfolioManager(account_size=200000, max_daily_loss_pct=3.0,
                              spread_width=5.0, daily_contracts=7, max_contracts=10)
        assert pm.calculate_position_size(StrategyType.DAILY_INCOME, 500) == 7

    def test_scenario_c_capped_by_max(self):
        """$500k, daily_contracts=50 -> 30 raw -> capped at max_contracts=10."""
        pm = PortfolioManager(account_size=500000, max_daily_loss_pct=3.0,
                              spread_width=5.0, daily_contracts=50, max_contracts=10)
        assert pm.calculate_position_size(StrategyType.DAILY_INCOME, 500) == 10

    def test_daily_cap_tighter_than_max(self):
        """$500k -> 30 raw, daily_contracts=7, max=10 -> 7 (daily wins)."""
        pm = PortfolioManager(account_size=500000, max_daily_loss_pct=3.0,
                              spread_width=5.0, daily_contracts=7, max_contracts=10)
        assert pm.calculate_position_size(StrategyType.DAILY_INCOME, 500) == 7

    def test_min_contracts_floor(self):
        """Tiny budget should still return min_contracts."""
        pm = PortfolioManager(account_size=1000, max_daily_loss_pct=1.0,
                              daily_contracts=7, max_contracts=10, min_contracts=1)
        result = pm.calculate_position_size(StrategyType.DAILY_INCOME, 500)
        assert result >= 1

    def test_invalid_risk_returns_min(self):
        """Zero or negative risk per contract returns min_contracts."""
        pm = PortfolioManager(daily_contracts=7, max_contracts=10)
        assert pm.calculate_position_size(StrategyType.DAILY_INCOME, 0) == 1
        assert pm.calculate_position_size(StrategyType.DAILY_INCOME, -100) == 1


# ============================================================================
# Swing Sizing
# ============================================================================

class TestSwingSizing:

    def test_tnt_uses_swing_contracts(self):
        pm = PortfolioManager(swing_contracts=2, max_contracts=10)
        assert pm.calculate_position_size(StrategyType.TAG_N_TURN, 500) == 2

    def test_tnt_respects_global_max(self):
        pm = PortfolioManager(swing_contracts=15, max_contracts=10)
        assert pm.calculate_position_size(StrategyType.TAG_N_TURN, 500) == 10

    def test_bnb_uses_swing_contracts(self):
        pm = PortfolioManager(swing_contracts=3, max_contracts=20)
        assert pm.calculate_position_size(StrategyType.BNB, 500) == 3

    def test_orb_uses_swing_contracts(self):
        pm = PortfolioManager(swing_contracts=3, max_contracts=20)
        assert pm.calculate_position_size(StrategyType.ORB, 500) == 3

    def test_swing_min_contracts_floor(self):
        """swing_contracts=0 still gets clamped to min_contracts=1."""
        pm = PortfolioManager(swing_contracts=0, min_contracts=1, max_contracts=20)
        assert pm.calculate_position_size(StrategyType.TAG_N_TURN, 500) == 1


# ============================================================================
# Display Helpers
# ============================================================================

class TestDIDisplayHelper:

    def test_di_display_format(self):
        pm = PortfolioManager(account_size=50000, max_daily_loss_pct=3.0,
                              spread_width=5.0, daily_contracts=7, max_contracts=10)
        calculated, cap = pm.get_calculated_di_contracts()
        assert calculated == 3
        assert cap == 7
        summary = pm.get_position_sizing_summary()
        assert summary['di_contracts_display'] == "3 / 7"

    def test_large_account_display(self):
        """$200k, 3% -> 12 raw -> capped at daily=7 -> '7 / 7'."""
        pm = PortfolioManager(account_size=200000, max_daily_loss_pct=3.0,
                              spread_width=5.0, daily_contracts=7, max_contracts=10)
        calculated, cap = pm.get_calculated_di_contracts()
        assert calculated == 7
        assert cap == 7

    def test_max_contracts_caps_display(self):
        """max_contracts=3 caps below daily_contracts=7 -> '3 / 7'."""
        pm = PortfolioManager(account_size=200000, max_daily_loss_pct=3.0,
                              spread_width=5.0, daily_contracts=7, max_contracts=3)
        calculated, cap = pm.get_calculated_di_contracts()
        assert calculated == 3
        assert cap == 7


# ============================================================================
# Summary Dict
# ============================================================================

class TestPositionSizingSummary:

    def test_summary_keys(self):
        pm = PortfolioManager(account_size=50000, max_daily_loss_pct=2.0,
                              daily_contracts=7, swing_contracts=2,
                              spread_width=5.0, min_contracts=1, max_contracts=20)
        s = pm.get_position_sizing_summary()
        assert s['daily_contracts'] == 7
        assert s['swing_contracts'] == 2
        assert s['spread_width'] == 5.0
        assert s['max_contracts'] == 20
        assert s['min_contracts'] == 1
        assert 'calculated_di_contracts' in s
        assert 'di_contracts_display' in s

    def test_summary_display_value(self):
        pm = PortfolioManager(account_size=50000, max_daily_loss_pct=2.0,
                              spread_width=5.0, daily_contracts=7, max_contracts=20)
        s = pm.get_position_sizing_summary()
        # $50k * 2% = $1000 / ($5*100) = 2 contracts
        assert s['di_contracts_display'] == "2 / 7"
