"""Tests for VIX + time-of-day bid-ask spread model.

Covers:
- BidAskModel arithmetic across VIX regimes and time buckets
- Fallback behavior when VIX unavailable
- Integration with DryRunBroker (dynamic slippage on fills)
- Integration with BacktestBroker / sim_broker (dynamic slippage on fills)
"""

import sys
from datetime import datetime, date, time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytz
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.brokers.dry_run_broker import BidAskModel, DryRunBroker
from src.backtest.sim_broker import BacktestBroker
from src.models.spread import CreditSpread, TradeDirection, OptionLeg

ET = pytz.timezone('US/Eastern')


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------

def _make_spread(direction=TradeDirection.BULLISH, credit=2.50, spread_width=5.0):
    """Build a minimal CreditSpread for testing fills."""
    if direction == TradeDirection.BULLISH:
        short = OptionLeg(strike=6000.0, option_type='put', action='sell', price=credit)
        long = OptionLeg(strike=6000.0 - spread_width, option_type='put', action='buy', price=0.30)
    else:
        short = OptionLeg(strike=6000.0, option_type='call', action='sell', price=credit)
        long = OptionLeg(strike=6000.0 + spread_width, option_type='call', action='buy', price=0.30)
    return CreditSpread(
        short_leg=short,
        long_leg=long,
        direction=direction,
        credit_received=credit,
        underlying_price_at_entry=6000.0,
        entry_time=datetime.now(ET),
        expiration=datetime.now(ET),
    )


# ---------------------------------------------------------------
# BidAskModel unit tests
# ---------------------------------------------------------------

class TestBidAskModel:
    """Pure arithmetic tests for the model."""

    def setup_method(self):
        self.model = BidAskModel()

    def test_low_vix_morning_near_baseline(self):
        """VIX=12, 10:30 (morning) -> baseline * 1.0 * 1.0 = $0.02."""
        spread = self.model.get_spread(vix=12.0, hour=10, minute=30)
        assert spread == 0.02

    def test_normal_vix_morning(self):
        """VIX=18, 10:30 -> $0.02 * 1.3 * 1.0 = $0.026."""
        spread = self.model.get_spread(vix=18.0, hour=10, minute=30)
        assert spread == 0.026

    def test_elevated_vix_close(self):
        """VIX=25, 15:30 -> $0.02 * 1.8 * 1.6 = $0.0576."""
        spread = self.model.get_spread(vix=25.0, hour=15, minute=30)
        assert spread == 0.0576

    def test_high_vix_open(self):
        """VIX=35, 9:45 -> $0.02 * 2.5 * 1.4 = $0.07."""
        spread = self.model.get_spread(vix=35.0, hour=9, minute=45)
        assert spread == 0.07

    def test_high_vix_close_max_scenario(self):
        """VIX=40, 15:30 -> $0.02 * 2.5 * 1.6 = $0.08."""
        spread = self.model.get_spread(vix=40.0, hour=15, minute=30)
        assert spread == 0.08

    def test_spread_always_positive(self):
        """Spread must be positive for any valid VIX/time combination."""
        for vix in [5, 10, 15, 20, 25, 30, 40, 80]:
            for hour in range(9, 17):
                for minute in [0, 15, 30, 45]:
                    spread = self.model.get_spread(vix, hour, minute)
                    assert spread > 0, f"Spread was {spread} for VIX={vix} {hour}:{minute}"

    def test_spread_never_exceeds_half_dollar(self):
        """Even at extreme VIX=80, 15:59, spread should stay under $0.50."""
        spread = self.model.get_spread(vix=80.0, hour=15, minute=59)
        assert spread <= 0.50

    def test_midday_bucket(self):
        """VIX=12, 12:00 (midday) -> $0.02 * 1.0 * 1.1 = $0.022."""
        spread = self.model.get_spread(vix=12.0, hour=12, minute=0)
        assert spread == 0.022

    def test_afternoon_bucket(self):
        """VIX=12, 14:00 (afternoon) -> $0.02 * 1.0 * 1.2 = $0.024."""
        spread = self.model.get_spread(vix=12.0, hour=14, minute=0)
        assert spread == 0.024

    def test_before_market_open_uses_open_multiplier(self):
        """VIX=12, 9:00 (before 9:30) -> $0.02 * 1.0 * 1.4 = $0.028."""
        spread = self.model.get_spread(vix=12.0, hour=9, minute=0)
        assert spread == 0.028

    def test_vix_boundary_15(self):
        """VIX=14.99 should use 'low' tier, VIX=15.0 should use 'normal'."""
        low = self.model.get_spread(vix=14.99, hour=10, minute=30)
        normal = self.model.get_spread(vix=15.0, hour=10, minute=30)
        assert low < normal

    def test_vix_boundary_20(self):
        """VIX=19.99 should use 'normal', VIX=20.0 should use 'elevated'."""
        normal = self.model.get_spread(vix=19.99, hour=10, minute=30)
        elevated = self.model.get_spread(vix=20.0, hour=10, minute=30)
        assert normal < elevated

    def test_vix_boundary_30(self):
        """VIX=29.99 should use 'elevated', VIX=30.0 should use 'high'."""
        elevated = self.model.get_spread(vix=29.99, hour=10, minute=30)
        high = self.model.get_spread(vix=30.0, hour=10, minute=30)
        assert elevated < high


# ---------------------------------------------------------------
# DryRunBroker integration
# ---------------------------------------------------------------

class TestDryRunBrokerDynamicSlippage:
    """Verify DryRunBroker.place_spread_order and close_spread use dynamic slippage."""

    @patch('src.brokers.dry_run_broker.YahooFinanceProvider')
    def test_place_order_deducts_slippage(self, mock_yf_cls):
        """Opening credit should be reduced by 2 * per_leg slippage."""
        mock_provider = MagicMock()
        mock_provider.get_spx_quote.return_value = {'price': 6000.0, 'market_time_epoch': 0}
        # VIX=12 -> low tier, we'll mock time to morning -> per_leg = $0.02
        mock_provider.get_vix_quote.return_value = {'price': 12.0}
        mock_yf_cls.return_value = mock_provider

        broker = DryRunBroker(initial_balance=50000.0)
        spread = _make_spread(credit=2.50)

        # Mock datetime.now to return 10:30 ET (morning bucket)
        mock_now = datetime(2026, 2, 24, 10, 30, 0, tzinfo=ET)
        with patch('src.brokers.dry_run_broker.datetime') as mock_dt:
            mock_dt.now.return_value = mock_now
            mock_dt.strptime = datetime.strptime
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            order_id = broker.place_spread_order(spread, quantity=1, limit_price=2.50)

        status = broker.get_order_status(order_id)
        # At VIX=12, 10:30 -> per_leg=$0.02, total=$0.04
        # fill = 2.50 - 0.04 = 2.46
        assert status['fill_price'] == pytest.approx(2.46, abs=0.01)

    @patch('src.brokers.dry_run_broker.YahooFinanceProvider')
    def test_close_spread_adds_slippage(self, mock_yf_cls):
        """Closing debit should be increased by 2 * per_leg slippage."""
        mock_provider = MagicMock()
        mock_provider.get_spx_quote.return_value = {'price': 6000.0, 'market_time_epoch': 0}
        mock_provider.get_vix_quote.return_value = {'price': 25.0}  # elevated
        mock_yf_cls.return_value = mock_provider

        broker = DryRunBroker(initial_balance=50000.0)
        spread = _make_spread(credit=2.50)

        mock_now = datetime(2026, 2, 24, 15, 30, 0, tzinfo=ET)
        with patch('src.brokers.dry_run_broker.datetime') as mock_dt:
            mock_dt.now.return_value = mock_now
            mock_dt.strptime = datetime.strptime
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            order_id = broker.close_spread(spread, quantity=1, limit_price=0.20)

        status = broker.get_order_status(order_id)
        # VIX=25, 15:30 -> per_leg=$0.0576, total=$0.1152
        # close fill = 0.20 + 0.1152 = 0.3152
        assert status['fill_price'] == pytest.approx(0.3152, abs=0.01)

    @patch('src.brokers.dry_run_broker.YahooFinanceProvider')
    def test_vix_unavailable_falls_back_to_baseline(self, mock_yf_cls):
        """When VIX quote returns None, should use BASE_SPREAD=$0.02."""
        mock_provider = MagicMock()
        mock_provider.get_spx_quote.return_value = {'price': 6000.0, 'market_time_epoch': 0}
        mock_provider.get_vix_quote.return_value = None  # VIX unavailable
        mock_yf_cls.return_value = mock_provider

        broker = DryRunBroker(initial_balance=50000.0)
        spread = _make_spread(credit=2.50)

        mock_now = datetime(2026, 2, 24, 10, 30, 0, tzinfo=ET)
        with patch('src.brokers.dry_run_broker.datetime') as mock_dt:
            mock_dt.now.return_value = mock_now
            mock_dt.strptime = datetime.strptime
            mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
            order_id = broker.place_spread_order(spread, quantity=1, limit_price=2.50)

        status = broker.get_order_status(order_id)
        # Fallback = $0.02/leg, total=$0.04, fill = 2.50 - 0.04 = 2.46
        assert status['fill_price'] == pytest.approx(2.46, abs=0.01)


# ---------------------------------------------------------------
# BacktestBroker (sim_broker) integration
# ---------------------------------------------------------------

class TestBacktestBrokerDynamicSlippage:
    """Verify BacktestBroker uses BidAskModel with VIX + time-of-day."""

    def test_open_order_deducts_slippage_low_vix_morning(self):
        """Low VIX morning: minimal slippage."""
        broker = BacktestBroker(initial_capital=50000.0)
        broker.set_market_state(
            price=6000.0, bar_high=6005.0, bar_low=5995.0,
            vix=12.0, dt=ET.localize(datetime(2026, 2, 24, 10, 30)),
        )
        spread = _make_spread(credit=2.50)
        order_id = broker.place_spread_order(spread, quantity=1, limit_price=2.50)
        status = broker.get_order_status(order_id)
        # VIX=12, 10:30 -> per_leg=$0.02, total=$0.04
        assert status['fill_price'] == pytest.approx(2.46, abs=0.001)

    def test_open_order_elevated_vix_close(self):
        """Elevated VIX at close: wider slippage."""
        broker = BacktestBroker(initial_capital=50000.0)
        broker.set_market_state(
            price=6000.0, bar_high=6005.0, bar_low=5995.0,
            vix=25.0, dt=ET.localize(datetime(2026, 2, 24, 15, 30)),
        )
        spread = _make_spread(credit=2.50)
        order_id = broker.place_spread_order(spread, quantity=1, limit_price=2.50)
        status = broker.get_order_status(order_id)
        # VIX=25, 15:30 -> per_leg=$0.0576, total=$0.1152
        assert status['fill_price'] == pytest.approx(2.3848, abs=0.001)

    def test_close_order_adds_slippage(self):
        """Closing debit increases by total slippage."""
        broker = BacktestBroker(initial_capital=50000.0)
        broker.set_market_state(
            price=6000.0, bar_high=6005.0, bar_low=5995.0,
            vix=18.0, dt=ET.localize(datetime(2026, 2, 24, 12, 0)),
        )
        spread = _make_spread(credit=2.50)
        order_id = broker.close_spread(spread, quantity=1, limit_price=0.20)
        status = broker.get_order_status(order_id)
        # VIX=18, 12:00 -> per_leg=$0.02*1.3*1.1=$0.0286, total=$0.0572
        assert status['fill_price'] == pytest.approx(0.2572, abs=0.001)

    def test_flat_slippage_override_ignores_model(self):
        """When flat slippage is set, BidAskModel should be bypassed."""
        broker = BacktestBroker(initial_capital=50000.0, slippage=0.10)
        broker.set_market_state(
            price=6000.0, bar_high=6005.0, bar_low=5995.0,
            vix=35.0, dt=ET.localize(datetime(2026, 2, 24, 15, 30)),
        )
        spread = _make_spread(credit=2.50)
        order_id = broker.place_spread_order(spread, quantity=1, limit_price=2.50)
        status = broker.get_order_status(order_id)
        # Flat override = $0.10 total, fill = 2.50 - 0.10 = 2.40
        assert status['fill_price'] == pytest.approx(2.40, abs=0.001)

    def test_high_vix_open_slippage(self):
        """VIX=35 at open (9:45): max stress scenario."""
        broker = BacktestBroker(initial_capital=50000.0)
        broker.set_market_state(
            price=6000.0, bar_high=6005.0, bar_low=5995.0,
            vix=35.0, dt=ET.localize(datetime(2026, 2, 24, 9, 45)),
        )
        spread = _make_spread(credit=2.50)
        order_id = broker.place_spread_order(spread, quantity=1, limit_price=2.50)
        status = broker.get_order_status(order_id)
        # VIX=35, 9:45 -> per_leg=$0.07, total=$0.14
        assert status['fill_price'] == pytest.approx(2.36, abs=0.001)

    def test_no_datetime_defaults_to_morning(self):
        """When _current_dt is None, should default to morning time."""
        broker = BacktestBroker(initial_capital=50000.0)
        # Only set price and VIX, no dt
        broker._current_price = 6000.0
        broker._current_vix = 12.0
        broker._current_dt = None

        spread = _make_spread(credit=2.50)
        order_id = broker.place_spread_order(spread, quantity=1, limit_price=2.50)
        status = broker.get_order_status(order_id)
        # Default to 10:30 (morning), VIX=12 -> per_leg=$0.02, total=$0.04
        assert status['fill_price'] == pytest.approx(2.46, abs=0.001)
