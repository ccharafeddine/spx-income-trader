"""Tests for fill_quality_factor in BacktestBroker.

Covers:
1. Default 1.0 passthrough (credit unchanged after slippage)
2. 0.90 factor reduces entry credit by 10%
3. 0.50 factor reduces entry credit by 50%
4. Factor interacts correctly with BidAskModel slippage
5. close_spread is NOT affected by fill_quality_factor
"""

import sys
from datetime import datetime
from pathlib import Path

import pytz
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtest.sim_broker import BacktestBroker
from src.models.spread import CreditSpread, TradeDirection, OptionLeg

ET = pytz.timezone('US/Eastern')


def _make_spread(credit=2.50, direction=TradeDirection.BULLISH, spread_width=5.0):
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


class TestFillQualityFactor:
    """fill_quality_factor scales entry credit after slippage."""

    def test_default_passthrough(self):
        """Factor=1.0 (default) does not change fill price vs flat slippage."""
        broker = BacktestBroker(slippage=0.04)
        assert broker._fill_quality_factor == 1.0

        spread = _make_spread(credit=2.50)
        order_id = broker.place_spread_order(spread, 1, limit_price=2.50)
        status = broker.get_order_status(order_id)

        # 2.50 - 0.04 slippage = 2.46, * 1.0 = 2.46
        assert status['fill_price'] == pytest.approx(2.46, abs=0.001)

    def test_factor_090_reduces_credit(self):
        """Factor=0.90 reduces entry credit by 10% after slippage."""
        broker = BacktestBroker(slippage=0.04, fill_quality_factor=0.90)
        spread = _make_spread(credit=2.50)
        order_id = broker.place_spread_order(spread, 1, limit_price=2.50)
        status = broker.get_order_status(order_id)

        # 2.50 - 0.04 slippage = 2.46, * 0.90 = 2.214
        expected = (2.50 - 0.04) * 0.90
        assert status['fill_price'] == pytest.approx(expected, abs=0.001)

    def test_factor_050_reduces_credit(self):
        """Factor=0.50 reduces entry credit by 50% after slippage."""
        broker = BacktestBroker(slippage=0.04, fill_quality_factor=0.50)
        spread = _make_spread(credit=2.50)
        order_id = broker.place_spread_order(spread, 1, limit_price=2.50)
        status = broker.get_order_status(order_id)

        # 2.50 - 0.04 slippage = 2.46, * 0.50 = 1.23
        expected = (2.50 - 0.04) * 0.50
        assert status['fill_price'] == pytest.approx(expected, abs=0.001)

    def test_factor_with_bid_ask_model(self):
        """Factor applied on top of BidAskModel dynamic slippage (no flat override)."""
        broker = BacktestBroker(fill_quality_factor=0.90)  # slippage=None -> BidAskModel
        # Set market state so BidAskModel can compute slippage
        dt = ET.localize(datetime(2025, 1, 15, 10, 30))  # morning, normal hours
        broker.set_market_state(price=6000.0, bar_high=6010.0, bar_low=5990.0,
                                vix=15.0, dt=dt)

        spread = _make_spread(credit=2.50)
        order_id = broker.place_spread_order(spread, 1, limit_price=2.50)
        status = broker.get_order_status(order_id)

        # BidAskModel: VIX=15 (normal, 1.3x), 10:30 (morning, 1.0x)
        # per_leg = 0.02 * 1.3 * 1.0 = 0.026, total = 0.052
        # After slippage: 2.50 - 0.052 = 2.448
        # After factor: 2.448 * 0.90 = 2.2032
        after_slippage = 2.50 - broker._get_slippage()
        expected = after_slippage * 0.90
        assert status['fill_price'] == pytest.approx(expected, abs=0.001)
        # Verify it's actually less than the 1.0 factor case
        assert status['fill_price'] < after_slippage

    def test_close_spread_not_affected(self):
        """fill_quality_factor does NOT reduce exit debit (only entry credit)."""
        broker_fq = BacktestBroker(slippage=0.04, fill_quality_factor=0.50)
        broker_1x = BacktestBroker(slippage=0.04, fill_quality_factor=1.00)

        spread = _make_spread(credit=2.50)

        close_id_fq = broker_fq.close_spread(spread, 1, limit_price=0.20)
        close_id_1x = broker_1x.close_spread(spread, 1, limit_price=0.20)

        status_fq = broker_fq.get_order_status(close_id_fq)
        status_1x = broker_1x.get_order_status(close_id_1x)

        # Both should have identical close fill prices (0.20 + 0.04 = 0.24)
        assert status_fq['fill_price'] == pytest.approx(0.24, abs=0.001)
        assert status_1x['fill_price'] == pytest.approx(0.24, abs=0.001)
        assert status_fq['fill_price'] == status_1x['fill_price']
