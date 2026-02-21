"""Tests for Black-Scholes Greeks calculator."""

import sys
import math
from pathlib import Path
from datetime import datetime, timedelta

import pytest
import pytz

# Project root setup
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.analytics.greeks import GreeksCalculator

ET = pytz.timezone('America/New_York')


@pytest.fixture
def calc():
    return GreeksCalculator(risk_free_rate=0.05)


# ---------------------------------------------------------------
# Test Delta
# ---------------------------------------------------------------
class TestDelta:
    def test_atm_call_delta_near_half(self, calc):
        """ATM call delta should be approximately 0.5."""
        g = calc.calculate_greeks(spot=6000, strike=6000, dte_years=30/365, iv=0.20, option_type='call')
        assert 0.45 < g['delta'] < 0.60

    def test_atm_put_delta_near_neg_half(self, calc):
        """ATM put delta should be approximately -0.5."""
        g = calc.calculate_greeks(spot=6000, strike=6000, dte_years=30/365, iv=0.20, option_type='put')
        assert -0.60 < g['delta'] < -0.45

    def test_otm_put_delta_near_zero(self, calc):
        """Deep OTM put delta should be near zero."""
        g = calc.calculate_greeks(spot=6000, strike=5700, dte_years=1/365, iv=0.15, option_type='put')
        assert -0.10 < g['delta'] < 0.0

    def test_itm_put_delta_near_neg_one(self, calc):
        """Deep ITM put delta should be near -1."""
        g = calc.calculate_greeks(spot=5700, strike=6000, dte_years=30/365, iv=0.15, option_type='put')
        assert -1.0 < g['delta'] < -0.85

    def test_call_put_delta_parity(self, calc):
        """Call delta - Put delta should equal 1 (put-call parity)."""
        call = calc.calculate_greeks(spot=6000, strike=6000, dte_years=30/365, iv=0.20, option_type='call')
        put = calc.calculate_greeks(spot=6000, strike=6000, dte_years=30/365, iv=0.20, option_type='put')
        assert abs((call['delta'] - put['delta']) - 1.0) < 0.001


# ---------------------------------------------------------------
# Test Theta
# ---------------------------------------------------------------
class TestTheta:
    def test_long_put_theta_negative(self, calc):
        """Long put theta should be negative (time decay hurts long positions)."""
        g = calc.calculate_greeks(spot=6000, strike=5950, dte_years=5/365, iv=0.18, option_type='put')
        assert g['theta'] < 0

    def test_long_call_theta_negative(self, calc):
        """Long call theta should be negative."""
        g = calc.calculate_greeks(spot=6000, strike=6050, dte_years=5/365, iv=0.18, option_type='call')
        assert g['theta'] < 0

    def test_credit_spread_net_theta_positive(self, calc):
        """Short credit spread should have positive net theta (time decay helps)."""
        spread = calc.calculate_spread_greeks(
            spot=6000, short_strike=5950, long_strike=5900,
            dte_years=5/365, iv=0.18, spread_type='put_credit', quantity=1
        )
        assert spread['theta'] > 0


# ---------------------------------------------------------------
# Test Gamma & Vega
# ---------------------------------------------------------------
class TestGammaVega:
    def test_long_option_gamma_positive(self, calc):
        """Long option gamma should be positive."""
        g = calc.calculate_greeks(spot=6000, strike=6000, dte_years=10/365, iv=0.20, option_type='put')
        assert g['gamma'] > 0

    def test_long_option_vega_positive(self, calc):
        """Long option vega should be positive."""
        g = calc.calculate_greeks(spot=6000, strike=6000, dte_years=10/365, iv=0.20, option_type='call')
        assert g['vega'] > 0

    def test_short_spread_gamma_negative(self, calc):
        """Short credit spread should have negative net gamma."""
        spread = calc.calculate_spread_greeks(
            spot=6000, short_strike=5950, long_strike=5900,
            dte_years=5/365, iv=0.18, spread_type='put_credit', quantity=1
        )
        assert spread['gamma'] < 0

    def test_short_spread_vega_negative(self, calc):
        """Short credit spread should have negative net vega."""
        spread = calc.calculate_spread_greeks(
            spot=6000, short_strike=5950, long_strike=5900,
            dte_years=5/365, iv=0.18, spread_type='put_credit', quantity=1
        )
        assert spread['vega'] < 0


# ---------------------------------------------------------------
# Test Spread Greeks
# ---------------------------------------------------------------
class TestSpreadGreeks:
    def test_quantity_scaling(self, calc):
        """Spread Greeks should scale linearly with quantity."""
        s1 = calc.calculate_spread_greeks(
            spot=6000, short_strike=5950, long_strike=5900,
            dte_years=10/365, iv=0.20, spread_type='put_credit', quantity=1
        )
        s3 = calc.calculate_spread_greeks(
            spot=6000, short_strike=5950, long_strike=5900,
            dte_years=10/365, iv=0.20, spread_type='put_credit', quantity=3
        )
        assert abs(s3['delta'] - 3 * s1['delta']) < 0.001
        assert abs(s3['theta'] - 3 * s1['theta']) < 0.001

    def test_bearish_call_spread_negative_delta(self, calc):
        """Bearish call credit spread should have negative net delta."""
        spread = calc.calculate_spread_greeks(
            spot=6000, short_strike=6050, long_strike=6100,
            dte_years=10/365, iv=0.20, spread_type='call_credit', quantity=1
        )
        assert spread['delta'] < 0

    def test_net_equals_long_minus_short_times_multiplier(self, calc):
        """Net Greek = (long - short) * quantity * 100 (short the short leg, long the long leg)."""
        short_g = calc.calculate_greeks(spot=6000, strike=5950, dte_years=10/365, iv=0.20, option_type='put')
        long_g = calc.calculate_greeks(spot=6000, strike=5900, dte_years=10/365, iv=0.20, option_type='put')
        spread = calc.calculate_spread_greeks(
            spot=6000, short_strike=5950, long_strike=5900,
            dte_years=10/365, iv=0.20, spread_type='put_credit', quantity=2
        )
        expected_delta = (long_g['delta'] - short_g['delta']) * 200
        assert abs(spread['delta'] - expected_delta) < 0.0001


# ---------------------------------------------------------------
# Test Portfolio Aggregation
# ---------------------------------------------------------------
class TestPortfolioAggregation:
    def _make_position(self, short_strike, long_strike, direction='bullish',
                       quantity=1, hours_from_now=4):
        """Helper to create a mock position dict."""
        exp = datetime.now(ET) + timedelta(hours=hours_from_now)
        return {
            'short_strike': short_strike,
            'long_strike': long_strike,
            'direction': direction,
            'quantity': quantity,
            'expiration': exp.isoformat(),
        }

    def test_empty_positions_returns_zeros(self, calc):
        """Empty positions list returns has_positions=False with zeroed values."""
        result = calc.calculate_portfolio_greeks([], 6000, 18.0)
        assert result['has_positions'] is False
        assert result['net_delta'] == 0.0
        assert result['net_theta'] == 0.0

    def test_single_position_counted(self, calc):
        """Single position should show position_count=1."""
        pos = self._make_position(5950, 5900)
        result = calc.calculate_portfolio_greeks([pos], 6000, 18.0)
        assert result['has_positions'] is True
        assert result['position_count'] == 1

    def test_two_positions_sum(self, calc):
        """Two positions' Greeks should add up."""
        pos1 = self._make_position(5950, 5900, direction='bullish')
        pos2 = self._make_position(6050, 6100, direction='bearish')

        r1 = calc.calculate_portfolio_greeks([pos1], 6000, 18.0)
        r2 = calc.calculate_portfolio_greeks([pos2], 6000, 18.0)
        combined = calc.calculate_portfolio_greeks([pos1, pos2], 6000, 18.0)

        assert combined['position_count'] == 2
        assert abs(combined['net_delta'] - (r1['net_delta'] + r2['net_delta'])) < 0.001

    def test_theta_per_hour_calculation(self, calc):
        """theta_per_hour should equal net_theta / 6.5."""
        pos = self._make_position(5950, 5900)
        result = calc.calculate_portfolio_greeks([pos], 6000, 18.0)
        expected = result['net_theta'] / 6.5
        assert abs(result['theta_per_hour'] - round(expected, 4)) < 0.0001

    def test_iv_derived_from_vix(self, calc):
        """IV used should be VIX / 100."""
        pos = self._make_position(5950, 5900)
        result = calc.calculate_portfolio_greeks([pos], 6000, 19.06)
        assert abs(result['iv_used'] - 0.1906) < 0.001


# ---------------------------------------------------------------
# Test Edge Cases
# ---------------------------------------------------------------
class TestEdgeCases:
    def test_zero_dte_epsilon(self, calc):
        """0DTE with near-zero time should not crash (epsilon floor)."""
        g = calc.calculate_greeks(spot=6000, strike=5950, dte_years=1e-8, iv=0.20, option_type='put')
        assert math.isfinite(g['delta'])
        assert math.isfinite(g['gamma'])
        assert math.isfinite(g['theta'])

    def test_high_iv(self, calc):
        """High IV (80%) should produce valid Greeks."""
        g = calc.calculate_greeks(spot=6000, strike=6000, dte_years=30/365, iv=0.80, option_type='put')
        assert -1.0 <= g['delta'] <= 0.0
        assert g['gamma'] > 0
        assert g['vega'] > 0

    def test_low_iv(self, calc):
        """Low IV (5%) should produce valid Greeks."""
        g = calc.calculate_greeks(spot=6000, strike=6000, dte_years=30/365, iv=0.05, option_type='call')
        assert 0.0 <= g['delta'] <= 1.0
        assert g['gamma'] > 0

    def test_interpretation_bearish(self, calc):
        """Bearish position should generate appropriate interpretation text."""
        interpretation = calc._build_interpretation(net_delta=-15.0, theta_per_hour=1.50, has_positions=True)
        assert 'bearish' in interpretation.lower()
        assert '$1.50' in interpretation

    def test_interpretation_no_positions(self, calc):
        """No positions should return neutral interpretation."""
        interpretation = calc._build_interpretation(net_delta=0, theta_per_hour=0, has_positions=False)
        assert 'no open positions' in interpretation.lower()
