"""Tests for Merton jump-diffusion option pricing model.

Covers:
- Jump price >= BS price for OTM options (tail risk premium)
- Jump price ~ BS for deep ITM (jumps matter less)
- T=0 returns intrinsic value
- Higher lambda increases put premium
- Negative jump skew boosts OTM puts more than calls
- Put-call parity holds approximately
- Chain generation produces valid bid/ask
- Performance: chain generation under 2 seconds
"""

import math
import sys
import time
from datetime import datetime, date
from pathlib import Path

import pytz
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.brokers.dry_run_broker import (
    _black_scholes_price,
    _merton_jump_price,
)
from src.backtest.sim_broker import BacktestBroker

ET = pytz.timezone('America/New_York')

# Common test parameters: SPX ~6900, 0DTE, moderate vol
S = 6900.0
T_0DTE = 1 / 252       # 1 trading day in years
R = 0.05
SIGMA = 0.15


# ---------------------------------------------------------------
# Core pricing properties
# ---------------------------------------------------------------

class TestMertonVsBlackScholes:
    """Merton prices should exceed BS for OTM options (tail premium)."""

    def test_otm_put_jump_premium(self):
        """OTM put 1% below spot: Merton > BS due to negative jump skew."""
        K = round(S * 0.99)
        bs = _black_scholes_price(S, K, T_0DTE, R, SIGMA, 'put')
        mj = _merton_jump_price(S, K, T_0DTE, R, SIGMA, 'put')
        assert mj > bs, f"Merton {mj:.4f} should exceed BS {bs:.4f} for OTM put"

    def test_otm_call_jump_premium(self):
        """OTM call 1% above spot: Merton > BS due to jump vol."""
        K = round(S * 1.01)
        bs = _black_scholes_price(S, K, T_0DTE, R, SIGMA, 'call')
        mj = _merton_jump_price(S, K, T_0DTE, R, SIGMA, 'call')
        assert mj > bs, f"Merton {mj:.4f} should exceed BS {bs:.4f} for OTM call"

    def test_deep_otm_put_meaningful_premium(self):
        """Deep OTM put 2% below: jump premium should be meaningful."""
        K = round(S * 0.98)
        bs = _black_scholes_price(S, K, T_0DTE, R, SIGMA, 'put')
        mj = _merton_jump_price(S, K, T_0DTE, R, SIGMA, 'put')
        premium_pct = (mj - bs) / bs * 100
        # With conservative params (lam=0.3), 2% OTM gets ~14% premium
        assert premium_pct > 5, f"Deep OTM put jump premium should be >5%, got {premium_pct:.1f}%"
        assert premium_pct < 70, f"Deep OTM put jump premium should be <70%, got {premium_pct:.1f}%"

    def test_atm_prices_close(self):
        """ATM: Merton and BS should be within ~10% (jumps matter less ATM)."""
        K = S
        bs = _black_scholes_price(S, K, T_0DTE, R, SIGMA, 'put')
        mj = _merton_jump_price(S, K, T_0DTE, R, SIGMA, 'put')
        diff_pct = abs(mj - bs) / bs * 100
        assert diff_pct < 10, f"ATM diff should be <10%, got {diff_pct:.1f}%"

    def test_deep_itm_prices_converge(self):
        """Deep ITM: dominated by intrinsic, so both models converge."""
        K_put = round(S * 1.05)  # deep ITM put
        bs = _black_scholes_price(S, K_put, T_0DTE, R, SIGMA, 'put')
        mj = _merton_jump_price(S, K_put, T_0DTE, R, SIGMA, 'put')
        diff_pct = abs(mj - bs) / bs * 100
        assert diff_pct < 5, f"Deep ITM diff should be <5%, got {diff_pct:.1f}%"

    def test_negative_skew_puts_vs_calls(self):
        """OTM puts get more jump premium than OTM calls (negative mu_j)."""
        K_put = round(S * 0.99)  # 1% OTM put
        K_call = round(S * 1.01)  # 1% OTM call

        bs_put = _black_scholes_price(S, K_put, T_0DTE, R, SIGMA, 'put')
        mj_put = _merton_jump_price(S, K_put, T_0DTE, R, SIGMA, 'put')
        put_premium = (mj_put - bs_put) / bs_put * 100

        bs_call = _black_scholes_price(S, K_call, T_0DTE, R, SIGMA, 'call')
        mj_call = _merton_jump_price(S, K_call, T_0DTE, R, SIGMA, 'call')
        call_premium = (mj_call - bs_call) / bs_call * 100

        assert put_premium > call_premium, (
            f"OTM put premium ({put_premium:.1f}%) should exceed "
            f"OTM call premium ({call_premium:.1f}%) due to negative jump skew"
        )


# ---------------------------------------------------------------
# Edge cases and robustness
# ---------------------------------------------------------------

class TestEdgeCases:
    """Boundary conditions that must not crash."""

    def test_t_zero_returns_intrinsic_call(self):
        """T=0 call should return max(S-K, 0)."""
        assert _merton_jump_price(100, 95, 0, R, SIGMA, 'call') == 5.0
        assert _merton_jump_price(100, 105, 0, R, SIGMA, 'call') == 0.0

    def test_t_zero_returns_intrinsic_put(self):
        """T=0 put should return max(K-S, 0)."""
        assert _merton_jump_price(100, 105, 0, R, SIGMA, 'put') == 5.0
        assert _merton_jump_price(100, 95, 0, R, SIGMA, 'put') == 0.0

    def test_negative_t_returns_intrinsic(self):
        """Negative T should not crash."""
        result = _merton_jump_price(100, 95, -0.01, R, SIGMA, 'call')
        assert result == 5.0

    def test_very_small_t(self):
        """Very small T (seconds before expiry) should not crash."""
        T_tiny = 1e-6
        result = _merton_jump_price(S, S, T_tiny, R, SIGMA, 'put')
        assert result >= 0

    def test_very_high_vix(self):
        """VIX=80 (sigma=0.80) should produce valid prices."""
        result = _merton_jump_price(S, S, T_0DTE, R, 0.80, 'put')
        assert result > 0
        assert result < S  # Can't exceed underlying price

    def test_zero_vol(self):
        """sigma=0 should still return a valid price."""
        result = _merton_jump_price(S, S - 50, T_0DTE, R, 0.0, 'call')
        assert result >= 0

    def test_price_always_non_negative(self):
        """Price must never be negative for any reasonable inputs."""
        for K in [S * 0.9, S, S * 1.1]:
            for opt in ['call', 'put']:
                price = _merton_jump_price(S, K, T_0DTE, R, SIGMA, opt)
                assert price >= 0, f"{opt} at K={K} returned {price}"


# ---------------------------------------------------------------
# Parameter sensitivity
# ---------------------------------------------------------------

class TestParameterSensitivity:
    """Verify price responds correctly to jump parameter changes."""

    def test_higher_lambda_increases_otm_put(self):
        """More frequent jumps -> higher OTM put premium."""
        K = round(S * 0.99)
        low_lam = _merton_jump_price(S, K, T_0DTE, R, SIGMA, 'put', lam=0.25)
        high_lam = _merton_jump_price(S, K, T_0DTE, R, SIGMA, 'put', lam=2.0)
        assert high_lam > low_lam

    def test_larger_negative_mu_j_increases_otm_put(self):
        """Bigger average jump down -> higher OTM put premium."""
        K = round(S * 0.99)
        small_jump = _merton_jump_price(S, K, T_0DTE, R, SIGMA, 'put', mu_j=-0.01)
        big_jump = _merton_jump_price(S, K, T_0DTE, R, SIGMA, 'put', mu_j=-0.05)
        assert big_jump > small_jump

    def test_higher_sigma_j_increases_otm_prices(self):
        """More volatile jumps -> higher OTM option prices."""
        K = round(S * 0.98)
        low_vol = _merton_jump_price(S, K, T_0DTE, R, SIGMA, 'put', sigma_j=0.01)
        high_vol = _merton_jump_price(S, K, T_0DTE, R, SIGMA, 'put', sigma_j=0.10)
        assert high_vol > low_vol

    def test_zero_lambda_equals_bs(self):
        """With no jumps (lam=0), Merton should equal BS."""
        K = round(S * 0.99)
        bs = _black_scholes_price(S, K, T_0DTE, R, SIGMA, 'put')
        mj = _merton_jump_price(S, K, T_0DTE, R, SIGMA, 'put', lam=0.0)
        assert abs(mj - bs) < 0.01, f"lam=0 should give BS price: {mj:.4f} vs {bs:.4f}"


# ---------------------------------------------------------------
# Put-call parity
# ---------------------------------------------------------------

class TestPutCallParity:
    """Put-call parity: C - P ~ S - K*exp(-rT) (approximately)."""

    def test_parity_atm(self):
        K = S
        c = _merton_jump_price(S, K, T_0DTE, R, SIGMA, 'call')
        p = _merton_jump_price(S, K, T_0DTE, R, SIGMA, 'put')
        parity = (c - p) - (S - K * math.exp(-R * T_0DTE))
        assert abs(parity) < 1.0, f"Put-call parity violation: {parity:.4f}"

    def test_parity_otm(self):
        K = round(S * 0.98)
        c = _merton_jump_price(S, K, T_0DTE, R, SIGMA, 'call')
        p = _merton_jump_price(S, K, T_0DTE, R, SIGMA, 'put')
        parity = (c - p) - (S - K * math.exp(-R * T_0DTE))
        assert abs(parity) < 1.0, f"Put-call parity violation: {parity:.4f}"


# ---------------------------------------------------------------
# Chain generation integration
# ---------------------------------------------------------------

class TestChainGeneration:
    """Verify BacktestBroker chain uses Merton and produces valid output."""

    def test_chain_has_valid_prices(self):
        """All strikes should have positive bid/ask."""
        broker = BacktestBroker(initial_capital=50000.0)
        broker.set_market_state(
            price=6900.0, bar_high=6910.0, bar_low=6890.0,
            vix=15.0, dt=ET.localize(datetime(2026, 2, 24, 10, 30)),
        )
        chain = broker.get_options_chain('SPX', '2026-02-24')
        assert len(chain) == 41  # 20 below + ATM + 20 above

        for strike, data in chain.items():
            assert data['call_bid'] > 0, f"call_bid at {strike} is {data['call_bid']}"
            assert data['call_ask'] > data['call_bid'], f"ask <= bid for call at {strike}"
            assert data['put_bid'] > 0, f"put_bid at {strike} is {data['put_bid']}"
            assert data['put_ask'] > data['put_bid'], f"ask <= bid for put at {strike}"

    def test_otm_put_has_jump_premium_in_chain(self):
        """OTM put in chain should price higher than pure intrinsic."""
        broker = BacktestBroker(initial_capital=50000.0)
        broker.set_market_state(
            price=6900.0, bar_high=6910.0, bar_low=6890.0,
            vix=15.0, dt=ET.localize(datetime(2026, 2, 24, 10, 30)),
        )
        chain = broker.get_options_chain('SPX', '2026-02-24')
        # 0.5% OTM put: strike = 6865 (below 6900)
        otm_strike = 6865.0
        if otm_strike in chain:
            put_mid = chain[otm_strike]['put_last']
            # OTM put intrinsic is 0, but should have extrinsic value from jumps
            assert put_mid > 0.05, f"OTM put at {otm_strike} should have extrinsic, got {put_mid}"

    def test_chain_performance(self):
        """Chain generation should complete in under 2 seconds."""
        broker = BacktestBroker(initial_capital=50000.0)
        broker.set_market_state(
            price=6900.0, bar_high=6910.0, bar_low=6890.0,
            vix=20.0, dt=ET.localize(datetime(2026, 2, 24, 10, 30)),
        )
        start = time.time()
        for _ in range(10):  # 10 chain generations
            broker.get_options_chain('SPX', '2026-02-24')
        elapsed = time.time() - start
        assert elapsed < 2.0, f"10 chain generations took {elapsed:.2f}s (should be <2s)"
