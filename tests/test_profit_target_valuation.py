"""
Tests for false profit target trigger prevention.

Covers:
- Ask-side pricing for unrealized P&L (not mid-price)
- Max-profit cap prevents absurd P&L values
- Spread that just opened cannot immediately trigger profit target
- Schwab fill_price uses net spread price, not averaged leg prices
- Dry-run/backtest use options chain (with extrinsic value), not intrinsic-only
"""

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models.spread import CreditSpread, OptionLeg, TradeDirection
from src.models.trade import Trade, TradeStatus

ET = pytz.timezone('America/New_York')


def _make_spread(direction=TradeDirection.BEARISH, credit=2.05, width=5.0,
                 short_strike=5750.0, long_strike=5755.0):
    """Create a test credit spread."""
    now = ET.localize(datetime(2026, 3, 11, 10, 0))
    opt_type = 'call' if direction == TradeDirection.BEARISH else 'put'
    return CreditSpread(
        direction=direction,
        short_leg=OptionLeg(strike=short_strike, option_type=opt_type, action='sell', price=5.00),
        long_leg=OptionLeg(strike=long_strike, option_type=opt_type, action='buy', price=2.95),
        credit_received=credit,
        entry_time=now,
        expiration=now.replace(hour=16, minute=0),
        underlying_price_at_entry=5740.0,
    )


def _make_trade(spread=None, entry_price=None, quantity=3):
    """Create a test trade."""
    spread = spread or _make_spread()
    return Trade(
        id='test-trade-001',
        spread=spread,
        status=TradeStatus.ACTIVE,
        setup_bar=MagicMock(),
        entry_price=entry_price or spread.credit_received,
        entry_time=spread.entry_time,
        quantity=quantity,
    )


# ============================================================================
# Max-profit cap prevents absurd P&L
# ============================================================================

class TestMaxProfitCap:
    """Unrealized P&L must never exceed the credit received."""

    def test_pnl_capped_at_max_profit(self):
        """If current_price=0 (spread worthless), P&L = credit * 100 * qty, not more."""
        trade = _make_trade(entry_price=2.05, quantity=3)
        trade.update_pnl(0.0)
        max_profit = 2.05 * 100 * 3  # $615
        assert trade.pnl == max_profit

    def test_inflated_entry_price_still_capped(self):
        """Even if entry_price is wrong (bug), P&L is capped at credit_received."""
        spread = _make_spread(credit=2.05)
        # Simulate the old averaging bug: entry_price = avg of legs = $13.025
        trade = _make_trade(spread=spread, entry_price=13.025, quantity=3)
        trade.update_pnl(0.0)
        max_profit = 2.05 * 100 * 3  # $615
        assert trade.pnl == max_profit
        # Without cap, this would be $13.025 * 300 = $3,907.50

    def test_normal_profit_not_capped(self):
        """P&L below max profit is not affected by the cap."""
        trade = _make_trade(entry_price=2.05, quantity=3)
        trade.update_pnl(1.00)  # spread costs $1.00 to close
        expected = (2.05 - 1.00) * 100 * 3  # $315
        assert trade.pnl == expected

    def test_loss_not_affected_by_cap(self):
        """Negative P&L (loss) passes through uncapped."""
        trade = _make_trade(entry_price=2.05, quantity=3)
        trade.update_pnl(4.00)  # spread costs $4.00 to close (loss)
        expected = (2.05 - 4.00) * 100 * 3  # -$585
        assert trade.pnl == expected

    def test_pnl_percent_uses_capped_value(self):
        """P&L percent is based on the capped P&L, not raw."""
        trade = _make_trade(entry_price=2.05, quantity=3)
        trade.update_pnl(0.0)
        # P&L capped at $615, percent = 615 / (2.05 * 100 * 3) * 100 = 100%
        assert trade.pnl_percent == 100.0


# ============================================================================
# Strategy should_exit caps profit at max_profit
# ============================================================================

class TestShouldExitProfitCap:
    """Profit target in should_exit() must use capped profit."""

    def test_should_exit_caps_profit_at_max(self):
        """Even with broken entry_price, profit target uses capped value."""
        from src.core.strategy import SPXIncomeStrategy
        strategy = MagicMock(spec=SPXIncomeStrategy)
        strategy.profit_target = 0.80
        strategy.enable_1pm_check = False
        strategy.should_exit = SPXIncomeStrategy.should_exit.__get__(strategy)

        spread = _make_spread(credit=2.05)
        trade = _make_trade(spread=spread, entry_price=13.025, quantity=3)

        current_time = spread.entry_time.replace(minute=5)
        should_exit, reason = strategy.should_exit(trade, 0.0, current_time)

        if should_exit:
            # Extract the dollar amount from the reason
            import re
            match = re.search(r'\$(\d+\.\d+)', reason)
            if match:
                reported_profit = float(match.group(1))
                max_profit = 615.0
                assert reported_profit <= max_profit + 0.01, (
                    f"Reported profit ${reported_profit} exceeds max profit ${max_profit}"
                )


# ============================================================================
# Ask-side pricing (live brokers)
# ============================================================================

class TestAskSidePricing:
    """Live brokers must use ask-side pricing for position valuation."""

    def _make_chain(self, short_strike, long_strike, opt_type='call',
                    short_bid=0.05, short_ask=0.40, long_bid=0.00, long_ask=0.10):
        """Create a mock options chain with specific bid/ask."""
        chain = {
            short_strike: {
                f'{opt_type}_bid': short_bid,
                f'{opt_type}_ask': short_ask,
                f'{opt_type}_last': (short_bid + short_ask) / 2,
                f'{opt_type}_volume': 100,
                f'{opt_type}_oi': 500,
            },
            long_strike: {
                f'{opt_type}_bid': long_bid,
                f'{opt_type}_ask': long_ask,
                f'{opt_type}_last': (long_bid + long_ask) / 2,
                f'{opt_type}_volume': 100,
                f'{opt_type}_oi': 500,
            }
        }
        # Add the other option type too (needed by some chain lookups)
        other_type = 'put' if opt_type == 'call' else 'call'
        for strike in chain:
            chain[strike][f'{other_type}_bid'] = 0.05
            chain[strike][f'{other_type}_ask'] = 0.10
            chain[strike][f'{other_type}_last'] = 0.075
            chain[strike][f'{other_type}_volume'] = 50
            chain[strike][f'{other_type}_oi'] = 200
        return chain

    def test_schwab_uses_ask_side(self):
        """Schwab get_position_value must use short_ask - long_bid."""
        from src.brokers.schwab_broker import SchwabBroker
        spread = _make_spread()
        chain = self._make_chain(5750.0, 5755.0, 'call',
                                 short_bid=0.05, short_ask=0.40,
                                 long_bid=0.00, long_ask=0.10)

        broker = MagicMock(spec=SchwabBroker)
        broker.get_options_chain.return_value = chain
        broker.get_position_value = SchwabBroker.get_position_value.__get__(broker)

        value = broker.get_position_value(spread)

        # Ask-side: short_ask(0.40) - long_bid(0.00) = 0.40
        assert value == 0.40
        # Mid would have been: short_mid(0.225) - long_mid(0.05) = 0.175

    def test_schwab_mid_would_overstate_profit(self):
        """Demonstrate that mid-price overstates profit vs ask-side."""
        chain = self._make_chain(5750.0, 5755.0, 'call',
                                 short_bid=0.05, short_ask=0.40,
                                 long_bid=0.00, long_ask=0.10)

        short_mid = (0.05 + 0.40) / 2  # 0.225
        long_mid = (0.00 + 0.10) / 2   # 0.05
        mid_value = short_mid - long_mid  # 0.175

        ask_value = 0.40 - 0.00  # 0.40

        # Mid says spread costs $0.175 to close (profit = $1.875/share)
        # Ask says spread costs $0.40 to close (profit = $1.65/share)
        # The $0.225 difference × 100 × 3 = $67.50 per monitoring cycle
        assert ask_value > mid_value, "Ask-side is always more conservative"

    def test_etrade_uses_ask_side(self):
        """E*TRADE get_position_value must use short_ask - long_bid."""
        from src.brokers.etrade_broker import ETradeBroker
        spread = _make_spread()
        chain = self._make_chain(5750.0, 5755.0, 'call',
                                 short_bid=0.10, short_ask=0.50,
                                 long_bid=0.05, long_ask=0.15)

        broker = MagicMock(spec=ETradeBroker)
        broker.get_options_chain.return_value = chain
        broker.get_position_value = ETradeBroker.get_position_value.__get__(broker)

        value = broker.get_position_value(spread)
        assert value == 0.45  # short_ask(0.50) - long_bid(0.05)

    def test_bullish_spread_uses_put_ask_side(self):
        """Bullish (put) spread uses put ask-side pricing."""
        from src.brokers.schwab_broker import SchwabBroker
        spread = _make_spread(direction=TradeDirection.BULLISH,
                              short_strike=5740.0, long_strike=5735.0)
        chain = self._make_chain(5740.0, 5735.0, 'put',
                                 short_bid=0.15, short_ask=0.55,
                                 long_bid=0.05, long_ask=0.20)

        broker = MagicMock(spec=SchwabBroker)
        broker.get_options_chain.return_value = chain
        broker.get_position_value = SchwabBroker.get_position_value.__get__(broker)

        value = broker.get_position_value(spread)
        assert value == 0.50  # put_ask(0.55) - put_bid(0.05)


# ============================================================================
# Newly-opened spread cannot immediately trigger profit target
# ============================================================================

class TestNoImmediateProfitTarget:
    """A spread that just opened should not trigger profit target in 1 minute."""

    def test_otm_spread_with_extrinsic_value_no_false_trigger(self):
        """OTM spread with realistic ask-side pricing should NOT fire immediately."""
        from src.core.strategy import SPXIncomeStrategy
        strategy = MagicMock(spec=SPXIncomeStrategy)
        strategy.profit_target = 0.80  # 80%
        strategy.enable_1pm_check = False
        strategy.should_exit = SPXIncomeStrategy.should_exit.__get__(strategy)

        spread = _make_spread(credit=2.05)
        trade = _make_trade(spread=spread, entry_price=2.05, quantity=3)

        # Realistic 1-minute-after-entry ask-side value for OTM 0DTE spread:
        # Even if intrinsic = 0, the ask is still ~$0.40-0.60 due to extrinsic
        current_value = 0.50  # realistic ask-side cost to close
        current_time = spread.entry_time.replace(minute=1)

        should_exit, reason = strategy.should_exit(trade, current_value, current_time)

        # current_profit = (2.05 - 0.50) * 100 * 3 = $465
        # profit_target = 2.05 * 100 * 3 * 0.80 = $492
        # $465 < $492 → should NOT trigger
        assert not should_exit, (
            f"Profit target should NOT fire 1 min after entry with "
            f"realistic ask-side pricing. Reason: {reason}"
        )

    def test_intrinsic_zero_with_old_code_would_false_trigger(self):
        """Demonstrate that intrinsic=0 valuation would falsely trigger."""
        from src.core.strategy import SPXIncomeStrategy
        strategy = MagicMock(spec=SPXIncomeStrategy)
        strategy.profit_target = 0.80
        strategy.enable_1pm_check = False
        strategy.should_exit = SPXIncomeStrategy.should_exit.__get__(strategy)

        spread = _make_spread(credit=2.05)
        trade = _make_trade(spread=spread, entry_price=2.05, quantity=3)

        # Old code: intrinsic = 0 for OTM spread
        current_value = 0.0
        current_time = spread.entry_time.replace(minute=1)

        should_exit, reason = strategy.should_exit(trade, current_value, current_time)

        # current_profit = min((2.05-0)*300, 615) = $615
        # profit_target = $492
        # $615 >= $492 → triggers (but P&L is now capped at $615, not $3907)
        assert should_exit, "With current_value=0, profit target should fire"
        # But the reported profit is capped at max, not inflated
        assert "$615.00" in reason


# ============================================================================
# Schwab fill_price parsing
# ============================================================================

class TestSchwabFillPriceParsing:
    """Verify _parse_order_response uses net spread price, not averaged legs."""

    def _parse(self, order_data):
        from src.brokers.schwab_broker import SchwabBroker
        broker = SchwabBroker.__new__(SchwabBroker)
        return broker._parse_order_response(order_data)

    def test_spread_order_uses_net_price(self):
        """Multi-leg order should use order-level price, not avg of legs."""
        order_data = {
            'status': 'FILLED',
            'filledQuantity': 3,
            'price': 2.05,  # Net credit of the spread
            'orderLegCollection': [
                {'instrument': {'symbol': 'SPXW_030726C5750'}, 'instruction': 'SELL_TO_OPEN'},
                {'instrument': {'symbol': 'SPXW_030726C5755'}, 'instruction': 'BUY_TO_OPEN'},
            ],
            'orderActivityCollection': [{
                'executionLegs': [
                    {'price': 12.50},  # Short leg fill
                    {'price': 10.45},  # Long leg fill
                ]
            }],
        }
        result = self._parse(order_data)
        # Should be 2.05 (net price), NOT (12.50+10.45)/2 = 11.475
        assert result['fill_price'] == 2.05
        assert result['filled_quantity'] == 3

    def test_single_leg_order_uses_execution_price(self):
        """Single-leg order should still use execution leg price."""
        order_data = {
            'status': 'FILLED',
            'filledQuantity': 1,
            'price': 5.00,  # Limit price
            'orderLegCollection': [
                {'instrument': {'symbol': 'SPXW_030726C5750'}, 'instruction': 'SELL_TO_OPEN'},
            ],
            'orderActivityCollection': [{
                'executionLegs': [
                    {'price': 5.10},  # Actual fill (better than limit)
                ]
            }],
        }
        result = self._parse(order_data)
        assert result['fill_price'] == 5.10  # Uses execution leg, not order price

    def test_spread_fallback_if_no_order_legs(self):
        """If orderLegCollection is missing, fall back to order price."""
        order_data = {
            'status': 'FILLED',
            'filledQuantity': 3,
            'price': 2.05,
            'orderActivityCollection': [{
                'executionLegs': [
                    {'price': 12.50},
                    {'price': 10.45},
                ]
            }],
        }
        result = self._parse(order_data)
        # No orderLegCollection → treats as single leg → averages legs
        # This is the fallback behavior
        assert result['fill_price'] == pytest.approx(11.475)

    def test_old_averaged_legs_would_inflate_pnl(self):
        """Demonstrate the bug: averaging legs would produce $3,907.50 P&L."""
        avg_price = (12.50 + 10.45) / 2  # 11.475
        net_price = 2.05
        # With 3 contracts, cost-to-close = 0:
        old_pnl = (avg_price - 0) * 100 * 3  # $3,442.50
        new_pnl = (net_price - 0) * 100 * 3  # $615
        assert old_pnl > 3000, "Old bug would show >$3,000 profit"
        assert new_pnl == pytest.approx(615), "Correct net price gives $615 max"


# ============================================================================
# Dry-run/backtest use chain pricing (not intrinsic-only)
# ============================================================================

class TestDryRunChainPricing:
    """Dry-run and backtest should use options chain, not intrinsic-only."""

    def test_dry_run_otm_spread_has_extrinsic_value(self):
        """OTM spread in dry-run should NOT return 0 (has extrinsic value)."""
        from src.brokers.dry_run_broker import DryRunBroker

        spread = _make_spread(direction=TradeDirection.BEARISH,
                              short_strike=5750.0, long_strike=5755.0)

        broker = MagicMock(spec=DryRunBroker)
        # Simulate chain with extrinsic value on OTM options
        chain = {
            5750.0: {
                'call_bid': 0.05, 'call_ask': 0.40,
                'call_last': 0.225, 'call_volume': 100, 'call_oi': 500,
                'put_bid': 5.00, 'put_ask': 5.50,
                'put_last': 5.25, 'put_volume': 100, 'put_oi': 500,
            },
            5755.0: {
                'call_bid': 0.00, 'call_ask': 0.10,
                'call_last': 0.05, 'call_volume': 100, 'call_oi': 500,
                'put_bid': 10.0, 'put_ask': 10.50,
                'put_last': 10.25, 'put_volume': 100, 'put_oi': 500,
            },
        }
        broker.get_options_chain.return_value = chain
        broker.get_current_price.return_value = 5740.0
        broker.get_position_value = DryRunBroker.get_position_value.__get__(broker)

        value = broker.get_position_value(spread)

        # ask-side: short_ask(0.40) - long_bid(0.00) = 0.40
        assert value == 0.40
        # Old intrinsic-only would return 0 (both calls OTM when SPX=5740 < 5750)

    def test_backtest_otm_spread_has_extrinsic_value(self):
        """Backtest spread should use chain pricing, not intrinsic-only."""
        from src.backtest.sim_broker import BacktestBroker

        spread = _make_spread(direction=TradeDirection.BEARISH,
                              short_strike=5750.0, long_strike=5755.0)

        broker = MagicMock(spec=BacktestBroker)
        chain = {
            5750.0: {
                'call_bid': 0.10, 'call_ask': 0.35,
                'call_last': 0.225, 'call_volume': 100, 'call_oi': 500,
                'put_bid': 5.00, 'put_ask': 5.50,
                'put_last': 5.25, 'put_volume': 100, 'put_oi': 500,
            },
            5755.0: {
                'call_bid': 0.02, 'call_ask': 0.12,
                'call_last': 0.07, 'call_volume': 100, 'call_oi': 500,
                'put_bid': 10.0, 'put_ask': 10.50,
                'put_last': 10.25, 'put_volume': 100, 'put_oi': 500,
            },
        }
        broker.get_options_chain.return_value = chain
        broker._current_price = 5740.0
        broker.get_position_value = BacktestBroker.get_position_value.__get__(broker)

        value = broker.get_position_value(spread)

        # mid-price: short_last(0.225) - long_last(0.07) = 0.155
        # (backtest uses mid-prices to avoid double-counting BidAskModel slippage)
        assert value == pytest.approx(0.155)


# ============================================================================
# Unrealized P&L never exceeds max profit
# ============================================================================

class TestPnlNeverExceedsMaxProfit:
    """Integration test: full chain from valuation to P&L."""

    def test_full_chain_pnl_capped(self):
        """Even with current_value=0, P&L equals max profit, not more."""
        spread = _make_spread(credit=2.05)
        trade = _make_trade(spread=spread, entry_price=2.05, quantity=3)

        trade.update_pnl(0.0)  # Spread supposedly worthless

        max_profit = 2.05 * 100 * 3
        assert trade.pnl == max_profit
        assert trade.pnl <= max_profit

    @pytest.mark.parametrize("entry_price,current_value,credit,qty", [
        (2.05, 0.0, 2.05, 3),    # Normal: value=0
        (2.05, -0.50, 2.05, 1),  # Impossible negative value
        (13.0, 0.0, 2.05, 3),    # Inflated entry (old bug)
        (5.00, 0.0, 3.00, 2),    # Another inflated entry
    ])
    def test_pnl_never_exceeds_max_profit(self, entry_price, current_value, credit, qty):
        """P&L must never exceed credit_received * 100 * quantity."""
        spread = _make_spread(credit=credit)
        trade = _make_trade(spread=spread, entry_price=entry_price, quantity=qty)
        trade.update_pnl(current_value)

        max_profit = credit * 100 * qty
        assert trade.pnl <= max_profit, (
            f"P&L ${trade.pnl:.2f} exceeds max profit ${max_profit:.2f}"
        )
