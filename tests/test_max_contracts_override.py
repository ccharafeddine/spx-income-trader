"""
Tests for max_contracts_override in position sizing.

Tests cover:
- Override caps calculated size when lower
- Override higher than global max (global max wins)
- Override lower than calculated (override wins)
- No override provided (no effect on result)
- Override with different sizing methods (percent_risk, kelly)
- Zero and negative override values are ignored
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.portfolio_manager import (
    PortfolioManager,
    PositionSizingMethod,
    StrategyType,
)


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def portfolio():
    """Create a PortfolioManager with percent_risk sizing and known parameters."""
    return PortfolioManager(
        account_size=50000.0,
        sizing_method="percent_risk",
        risk_per_trade_pct=2.0,   # $1000 risk budget
        min_contracts=1,
        max_contracts=20,
    )


# ============================================================================
# Override Capping
# ============================================================================

class TestOverrideCapping:
    """Test that max_contracts_override caps calculated position size."""

    def test_override_caps_calculated_size(self, portfolio):
        """Calculated=5 contracts, override=3 -> capped to 3."""
        # $1000 budget / $200 risk = 5 contracts
        result = portfolio.calculate_position_size(
            strategy=StrategyType.DAILY_INCOME,
            max_risk_per_contract=200.0,
            max_contracts_override=3,
        )
        assert result == 3

    def test_override_equal_to_calculated(self, portfolio):
        """Calculated=5, override=5 -> no cap needed, returns 5."""
        result = portfolio.calculate_position_size(
            strategy=StrategyType.DAILY_INCOME,
            max_risk_per_contract=200.0,
            max_contracts_override=5,
        )
        assert result == 5

    def test_override_higher_than_calculated(self, portfolio):
        """Calculated=5, override=10 -> override has no effect, returns 5."""
        result = portfolio.calculate_position_size(
            strategy=StrategyType.DAILY_INCOME,
            max_risk_per_contract=200.0,
            max_contracts_override=10,
        )
        assert result == 5


class TestOverrideVsGlobalMax:
    """Test interaction between override and global max_contracts."""

    def test_global_max_wins_when_lower_than_override(self):
        """Global max=3, override=10 -> global max wins at 3."""
        pm = PortfolioManager(
            account_size=100000.0,   # Large account
            sizing_method="percent_risk",
            risk_per_trade_pct=2.0,  # $2000 risk budget
            min_contracts=1,
            max_contracts=3,         # Global ceiling at 3
        )
        # $2000 / $100 risk = 20 contracts -> clamped to global 3
        result = pm.calculate_position_size(
            strategy=StrategyType.DAILY_INCOME,
            max_risk_per_contract=100.0,
            max_contracts_override=10,
        )
        assert result == 3

    def test_override_wins_when_lower_than_global_max(self):
        """Global max=20, override=2 -> override wins at 2."""
        pm = PortfolioManager(
            account_size=100000.0,
            sizing_method="percent_risk",
            risk_per_trade_pct=2.0,
            min_contracts=1,
            max_contracts=20,
        )
        # $2000 / $100 = 20 contracts -> clamped to global 20 -> then override 2
        result = pm.calculate_position_size(
            strategy=StrategyType.DAILY_INCOME,
            max_risk_per_contract=100.0,
            max_contracts_override=2,
        )
        assert result == 2

    def test_both_constraints_applied_in_order(self):
        """Global clamp first, then override. Global=5, override=3."""
        pm = PortfolioManager(
            account_size=100000.0,
            sizing_method="percent_risk",
            risk_per_trade_pct=2.0,
            min_contracts=1,
            max_contracts=5,
        )
        # $2000 / $100 = 20 -> clamped to 5 -> then capped to 3
        result = pm.calculate_position_size(
            strategy=StrategyType.DAILY_INCOME,
            max_risk_per_contract=100.0,
            max_contracts_override=3,
        )
        assert result == 3


class TestNoOverride:
    """Test behavior when no override is provided."""

    def test_none_override_has_no_effect(self, portfolio):
        """Default None override does not change result."""
        result = portfolio.calculate_position_size(
            strategy=StrategyType.DAILY_INCOME,
            max_risk_per_contract=200.0,
        )
        # $1000 / $200 = 5 contracts
        assert result == 5

    def test_explicit_none_override(self, portfolio):
        """Explicitly passing None has no effect."""
        result = portfolio.calculate_position_size(
            strategy=StrategyType.DAILY_INCOME,
            max_risk_per_contract=200.0,
            max_contracts_override=None,
        )
        assert result == 5

    def test_zero_override_ignored(self, portfolio):
        """Override of 0 is treated as not provided."""
        result = portfolio.calculate_position_size(
            strategy=StrategyType.DAILY_INCOME,
            max_risk_per_contract=200.0,
            max_contracts_override=0,
        )
        assert result == 5

    def test_negative_override_ignored(self, portfolio):
        """Negative override is treated as not provided."""
        result = portfolio.calculate_position_size(
            strategy=StrategyType.DAILY_INCOME,
            max_risk_per_contract=200.0,
            max_contracts_override=-1,
        )
        assert result == 5


class TestOverrideWithKelly:
    """Test override works with kelly sizing method too."""

    def test_kelly_result_capped_by_override(self):
        pm = PortfolioManager(
            account_size=50000.0,
            sizing_method="kelly",
            risk_per_trade_pct=2.0,
            min_contracts=1,
            max_contracts=20,
        )
        # Mock historical stats to return known values
        # Kelly formula with these values produces a specific contract count
        with patch.object(pm, '_get_historical_stats',
                          return_value=(0.65, 150.0, 100.0, 50)):
            result = pm.calculate_position_size(
                strategy=StrategyType.DAILY_INCOME,
                max_risk_per_contract=200.0,
                max_contracts_override=2,
            )
        assert result <= 2


class TestPerStrategyOverrides:
    """Test that different strategies can have different overrides."""

    def test_different_overrides_per_strategy(self, portfolio):
        """Daily income capped at 5, TNT capped at 2."""
        di_result = portfolio.calculate_position_size(
            strategy=StrategyType.DAILY_INCOME,
            max_risk_per_contract=100.0,  # $1000/$100 = 10 -> cap 5
            max_contracts_override=5,
        )
        tnt_result = portfolio.calculate_position_size(
            strategy=StrategyType.TAG_N_TURN,
            max_risk_per_contract=100.0,  # $1000/$100 = 10 -> cap 2
            max_contracts_override=2,
        )
        assert di_result == 5
        assert tnt_result == 2


class TestFixedContractsRemoved:
    """Test that fixed_contracts sizing method is properly removed."""

    def test_fixed_contracts_method_not_in_enum(self):
        """fixed_contracts should not be a valid sizing method."""
        with pytest.raises(ValueError):
            PositionSizingMethod("fixed_contracts")

    def test_stale_config_falls_back_to_percent_risk(self):
        """Old config with fixed_contracts falls back gracefully."""
        pm = PortfolioManager(
            account_size=50000.0,
            sizing_method="fixed_contracts",  # Old value
            min_contracts=1,
            max_contracts=20,
        )
        assert pm.sizing_method == PositionSizingMethod.PERCENT_RISK
