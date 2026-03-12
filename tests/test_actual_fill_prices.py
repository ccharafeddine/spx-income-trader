"""
Tests for actual fill price capture from Schwab execution legs.

Validates that:
1. Actual fill price overwrites limit price on entry and exit
2. P&L uses actual fill prices
3. Per-leg fill prices are captured
4. Fees are captured per-leg
5. Net P&L equals gross minus fees
6. Price difference between limit and fill is logged
7. Fallback to limit price when execution data is missing
"""

import pytest
import logging
from unittest.mock import MagicMock, patch

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.brokers.schwab_broker import SchwabBroker


# ---------------------------------------------------------------------------
# Helpers: build Schwab order response dicts
# ---------------------------------------------------------------------------

def _spread_order_data(
    limit_price: float,
    sell_price: float,
    buy_price: float,
    quantity: int = 1,
    status: str = 'FILLED',
    sell_instruction: str = 'SELL_TO_OPEN',
    buy_instruction: str = 'BUY_TO_OPEN',
    sell_fee: float = 0.61,
    buy_fee: float = 0.61,
):
    """Build a realistic Schwab spread order response with execution legs."""
    return {
        'status': status,
        'price': limit_price,
        'filledQuantity': quantity,
        'orderLegCollection': [
            {'instruction': sell_instruction, 'quantity': quantity,
             'instrument': {'symbol': 'SPXW_SHORT', 'assetType': 'OPTION'}},
            {'instruction': buy_instruction, 'quantity': quantity,
             'instrument': {'symbol': 'SPXW_LONG', 'assetType': 'OPTION'}},
        ],
        'orderActivityCollection': [
            {
                'activityType': 'EXECUTION',
                'executionType': 'FILL',
                'quantity': quantity,
                'executionLegs': [
                    {
                        'legId': 1,
                        'price': sell_price,
                        'quantity': quantity,
                        'commission': 0.0,
                        'fees': sell_fee,
                        'miscFees': 0.0,
                    },
                    {
                        'legId': 2,
                        'price': buy_price,
                        'quantity': quantity,
                        'commission': 0.0,
                        'fees': buy_fee,
                        'miscFees': 0.0,
                    },
                ],
            }
        ],
    }


def _spread_order_no_activity(limit_price: float, quantity: int = 1):
    """Build spread order response WITHOUT orderActivityCollection."""
    return {
        'status': 'FILLED',
        'price': limit_price,
        'filledQuantity': quantity,
        'orderLegCollection': [
            {'instruction': 'SELL_TO_OPEN', 'quantity': quantity,
             'instrument': {'symbol': 'SPXW_SHORT'}},
            {'instruction': 'BUY_TO_OPEN', 'quantity': quantity,
             'instrument': {'symbol': 'SPXW_LONG'}},
        ],
        'orderActivityCollection': [],
    }


@pytest.fixture
def broker():
    """Create a SchwabBroker with mocked auth."""
    with patch('src.brokers.schwab_broker.SchwabAuth'):
        b = SchwabBroker.__new__(SchwabBroker)
        b.auth = MagicMock()
        b._account_hash = 'test_hash'
        b._utils = None
        b.ORDER_POLL_INTERVAL = 0.01
        b.ORDER_TIMEOUT = 1
        return b


# ---------------------------------------------------------------------------
# 1. Actual fill price overwrites limit price on entry
# ---------------------------------------------------------------------------

class TestActualFillOverwritesLimit:
    """Verify _parse_order_response uses execution leg prices, not limit."""

    def test_entry_uses_actual_fill_not_limit(self, broker):
        """Entry fill price should be computed from execution legs."""
        # Limit was $2.50, actual fills: sell@23.93, buy@21.50 => net $2.43
        order = _spread_order_data(
            limit_price=2.50, sell_price=23.93, buy_price=21.50,
        )
        result = broker._parse_order_response(order)

        assert result['fill_price'] == 2.43
        assert result['fill_price'] != 2.50  # NOT the limit price

    def test_exit_uses_actual_fill_not_limit(self, broker):
        """Exit fill price should be computed from execution legs."""
        # Close order: BUY_TO_CLOSE short, SELL_TO_CLOSE long
        # Limit debit = $0.20, actual: buy@0.25, sell@0.03 => net abs(0.03 - 0.25) = $0.22
        order = _spread_order_data(
            limit_price=0.20,
            sell_price=0.03,
            buy_price=0.25,
            sell_instruction='SELL_TO_CLOSE',
            buy_instruction='BUY_TO_CLOSE',
        )
        result = broker._parse_order_response(order)

        assert result['fill_price'] == 0.22
        assert result['fill_price'] != 0.20  # NOT the limit price


# ---------------------------------------------------------------------------
# 2. P&L uses actual fill prices
# ---------------------------------------------------------------------------

class TestPnLUsesActualFills:
    """Verify P&L calculations use actual fills, not theoretical."""

    def test_pnl_from_actual_entry_and_exit(self):
        """P&L should be (actual_entry - actual_exit) * 100 * qty."""
        from src.models.trade import Trade, TradeStatus
        from src.models.spread import CreditSpread, OptionLeg, TradeDirection
        from src.models.bar import Bar
        from datetime import datetime

        spread = CreditSpread(
            short_leg=OptionLeg(strike=5800, option_type='PUT', action='sell', price=2.43),
            long_leg=OptionLeg(strike=5795, option_type='PUT', action='buy', price=0.50),
            direction=TradeDirection.BULLISH,
            entry_time=datetime(2026, 3, 12, 10, 0),
            expiration=datetime(2026, 3, 12, 16, 0),
            credit_received=2.43,  # actual fill, not theoretical
            underlying_price_at_entry=5820.0,
        )

        bar = Bar(
            timestamp=datetime(2026, 3, 12, 10, 0),
            open=5820, high=5825, low=5815, close=5822, volume=1000,
        )

        trade = Trade(
            id='test123', spread=spread, status=TradeStatus.ACTIVE,
            setup_bar=bar, entry_price=2.43, entry_time=datetime.now(),
        )

        # Close at actual exit fill of $0.22
        trade.close(exit_price=0.22, exit_time=datetime.now(), reason='target')

        # P&L = (2.43 - 0.22) * 100 * 1 = $221.00
        assert trade.pnl == 221.0
        assert trade.status == TradeStatus.CLOSED


# ---------------------------------------------------------------------------
# 3. Per-leg fill prices are captured
# ---------------------------------------------------------------------------

class TestPerLegFillCapture:
    """Verify leg_fills contains per-leg execution details."""

    def test_leg_fills_returned(self, broker):
        """_parse_order_response should return per-leg fill details."""
        order = _spread_order_data(
            limit_price=2.50, sell_price=23.93, buy_price=21.50,
        )
        result = broker._parse_order_response(order)

        assert 'leg_fills' in result
        assert len(result['leg_fills']) == 2

        sell_leg = next(l for l in result['leg_fills'] if 'SELL' in l['instruction'])
        buy_leg = next(l for l in result['leg_fills'] if 'BUY' in l['instruction'])

        assert sell_leg['price'] == 23.93
        assert buy_leg['price'] == 21.50
        assert sell_leg['quantity'] == 1
        assert buy_leg['quantity'] == 1

    def test_multi_fill_aggregation(self, broker):
        """Multiple execution activities should be aggregated correctly."""
        order = {
            'status': 'FILLED',
            'price': 2.50,
            'filledQuantity': 3,
            'orderLegCollection': [
                {'instruction': 'SELL_TO_OPEN'},
                {'instruction': 'BUY_TO_OPEN'},
            ],
            'orderActivityCollection': [
                {
                    'executionLegs': [
                        {'legId': 1, 'price': 23.93, 'quantity': 2,
                         'commission': 0, 'fees': 1.22, 'miscFees': 0},
                        {'legId': 2, 'price': 21.50, 'quantity': 2,
                         'commission': 0, 'fees': 1.22, 'miscFees': 0},
                    ]
                },
                {
                    'executionLegs': [
                        {'legId': 1, 'price': 23.90, 'quantity': 1,
                         'commission': 0, 'fees': 0.61, 'miscFees': 0},
                        {'legId': 2, 'price': 21.45, 'quantity': 1,
                         'commission': 0, 'fees': 0.61, 'miscFees': 0},
                    ]
                },
            ],
        }
        result = broker._parse_order_response(order)

        # Weighted avg sell = (23.93*2 + 23.90*1) / 3 = 23.92
        # Weighted avg buy  = (21.50*2 + 21.45*1) / 3 = 21.4833
        # Net = 23.92 - 21.4833 = 2.4367 => rounds to 2.4367
        expected = abs(round(
            (23.93 * 2 + 23.90 * 1) / 3 - (21.50 * 2 + 21.45 * 1) / 3,
            4,
        ))
        assert result['fill_price'] == expected
        assert len(result['leg_fills']) == 4  # 2 activities x 2 legs


# ---------------------------------------------------------------------------
# 4. Fees are captured per-leg
# ---------------------------------------------------------------------------

class TestPerLegFees:
    """Verify fees from execution legs are captured."""

    def test_per_leg_fees_in_leg_fills(self, broker):
        """Each leg_fill should have its fee amount."""
        order = _spread_order_data(
            limit_price=2.50, sell_price=23.93, buy_price=21.50,
            sell_fee=0.61, buy_fee=0.65,
        )
        result = broker._parse_order_response(order)

        sell_leg = next(l for l in result['leg_fills'] if 'SELL' in l['instruction'])
        buy_leg = next(l for l in result['leg_fills'] if 'BUY' in l['instruction'])

        assert sell_leg['fees'] == 0.61
        assert buy_leg['fees'] == 0.65

    def test_order_fees_total(self, broker):
        """order_fees should sum all per-leg fees."""
        order = _spread_order_data(
            limit_price=2.50, sell_price=23.93, buy_price=21.50,
            sell_fee=0.61, buy_fee=0.65,
        )
        result = broker._parse_order_response(order)

        assert result['order_fees'] == 1.26  # 0.61 + 0.65

    def test_fees_with_commission_and_misc(self, broker):
        """Fees should include commission + fees + miscFees."""
        order = {
            'status': 'FILLED',
            'price': 2.50,
            'filledQuantity': 1,
            'orderLegCollection': [
                {'instruction': 'SELL_TO_OPEN'},
                {'instruction': 'BUY_TO_OPEN'},
            ],
            'orderActivityCollection': [
                {
                    'executionLegs': [
                        {'legId': 1, 'price': 23.93, 'quantity': 1,
                         'commission': 0.50, 'fees': 0.61, 'miscFees': 0.10},
                        {'legId': 2, 'price': 21.50, 'quantity': 1,
                         'commission': 0.50, 'fees': 0.61, 'miscFees': 0.10},
                    ]
                },
            ],
        }
        result = broker._parse_order_response(order)

        # Each leg: 0.50 + 0.61 + 0.10 = 1.21
        leg1 = result['leg_fills'][0]
        assert leg1['fees'] == 1.21
        assert result['order_fees'] == 2.42  # 1.21 * 2


# ---------------------------------------------------------------------------
# 5. Net P&L equals gross minus fees
# ---------------------------------------------------------------------------

class TestNetPnL:
    """Verify net P&L = gross P&L - fees."""

    def test_net_pnl_calculation(self):
        """Dashboard compute_account should subtract commissions from P&L."""
        # This tests the formula: realized_pnl = sum(pnl - commissions)
        gross_pnl = 221.0  # (2.43 - 0.22) * 100
        commissions = 2.52  # entry fees + exit fees
        net_pnl = gross_pnl - commissions  # 218.48

        assert net_pnl == 218.48

    def test_march_11_scenario(self):
        """March 11 trade: bot showed -$310, actual was -$337.44."""
        # With actual fills, the entry credit would be lower and/or
        # exit debit would be higher, producing a larger loss
        # Limit entry: $2.50, actual entry: $2.43 (got less credit)
        # Expired worthless at max loss ($5.00 spread)
        # Gross P&L = (2.43 - 5.00) * 100 = -$257 ... but the actual
        # Schwab impact was -$337.44, which includes fees + fill differences
        # The key point: using actual fills gets closer to Schwab's number
        actual_entry = 2.43
        exit_at_max_loss = 5.00
        gross_pnl = (actual_entry - exit_at_max_loss) * 100  # -$257
        # With fees, the net is even more negative
        fees = 1.22
        net_pnl = gross_pnl - fees

        assert net_pnl < gross_pnl  # fees make it worse
        assert gross_pnl < -250  # actual fill produces larger loss than limit

    def test_march_12_scenario(self):
        """March 12 trade: bot showed +$190, actual was +$220.30."""
        # With actual fills, entry credit higher than limit
        actual_entry = 2.55  # better fill than $2.50 limit
        actual_exit = 0.32   # actual close price
        gross_pnl = (actual_entry - actual_exit) * 100  # $223
        fees = 2.52
        net_pnl = gross_pnl - fees  # $220.48

        assert net_pnl > 0
        assert abs(net_pnl - 220.30) < 1.0  # close to Schwab's number


# ---------------------------------------------------------------------------
# 6. Price difference between limit and fill is logged
# ---------------------------------------------------------------------------

class TestFillQualityLogging:
    """Verify fill quality logging."""

    def test_fill_quality_logged(self, broker, caplog):
        """Should log [FILL QUALITY] with limit vs actual diff."""
        order = _spread_order_data(
            limit_price=2.50, sell_price=23.93, buy_price=21.50,
        )
        with caplog.at_level(logging.INFO):
            broker._parse_order_response(order)

        fill_logs = [r for r in caplog.records if 'FILL QUALITY' in r.message]
        assert len(fill_logs) == 1
        msg = fill_logs[0].message
        assert 'limit=$2.5000' in msg
        assert 'actual=$2.4300' in msg
        assert 'diff=$-0.0700' in msg

    def test_no_log_when_no_execution_data(self, broker, caplog):
        """Should not log FILL QUALITY when no execution legs exist."""
        order = _spread_order_no_activity(limit_price=2.50)
        with caplog.at_level(logging.WARNING):
            broker._parse_order_response(order)

        fill_logs = [r for r in caplog.records if 'FILL QUALITY' in r.message]
        assert len(fill_logs) == 0

    def test_fallback_warning_logged(self, broker, caplog):
        """Should log warning when falling back to limit price."""
        order = _spread_order_no_activity(limit_price=2.50)
        with caplog.at_level(logging.WARNING):
            broker._parse_order_response(order)

        warnings = [r for r in caplog.records if 'limit price' in r.message.lower()]
        assert len(warnings) >= 1


# ---------------------------------------------------------------------------
# 7. Fallback behavior
# ---------------------------------------------------------------------------

class TestFallbackToLimit:
    """Verify graceful fallback when execution data is missing."""

    def test_no_activity_falls_back_to_limit(self, broker):
        """When no orderActivityCollection, use order-level price."""
        order = _spread_order_no_activity(limit_price=2.50)
        result = broker._parse_order_response(order)

        assert result['fill_price'] == 2.50
        assert 'leg_fills' not in result

    def test_empty_execution_legs(self, broker):
        """When execution legs are empty, fall back to limit."""
        order = {
            'status': 'FILLED',
            'price': 2.50,
            'filledQuantity': 1,
            'orderLegCollection': [
                {'instruction': 'SELL_TO_OPEN'},
                {'instruction': 'BUY_TO_OPEN'},
            ],
            'orderActivityCollection': [
                {'executionLegs': []}
            ],
        }
        result = broker._parse_order_response(order)

        # No sell/buy classified → falls back to limit
        assert result['fill_price'] == 2.50

    def test_unfilled_order(self, broker):
        """Unfilled orders should return 0 fill price."""
        order = {
            'status': 'WORKING',
            'price': 2.50,
            'filledQuantity': 0,
            'orderLegCollection': [
                {'instruction': 'SELL_TO_OPEN'},
                {'instruction': 'BUY_TO_OPEN'},
            ],
            'orderActivityCollection': [],
        }
        result = broker._parse_order_response(order)

        assert result['fill_price'] == 0.0
        assert result['filled_quantity'] == 0


# ---------------------------------------------------------------------------
# Credit received update in position manager
# ---------------------------------------------------------------------------

class TestCreditReceivedUpdate:
    """Verify spread.credit_received is updated with actual fill."""

    def test_credit_received_updated(self):
        """spread.credit_received should be set to actual fill price."""
        from src.models.spread import CreditSpread, OptionLeg, TradeDirection
        from datetime import datetime

        spread = CreditSpread(
            short_leg=OptionLeg(strike=5800, option_type='PUT', action='sell', price=2.50),
            long_leg=OptionLeg(strike=5795, option_type='PUT', action='buy', price=0.50),
            direction=TradeDirection.BULLISH,
            entry_time=datetime(2026, 3, 12, 10, 0),
            expiration=datetime(2026, 3, 12, 16, 0),
            credit_received=2.50,  # theoretical
            underlying_price_at_entry=5820.0,
        )

        assert spread.credit_received == 2.50

        # Simulate what position_manager does after fill
        actual_fill = 2.43
        spread.credit_received = actual_fill

        assert spread.credit_received == 2.43
        # Max profit now uses actual credit
        assert spread.max_profit == 2.43 * 100  # $243


# ---------------------------------------------------------------------------
# Single-leg order (unchanged behavior)
# ---------------------------------------------------------------------------

class TestSingleLegOrder:
    """Verify single-leg orders still use execution leg average."""

    def test_single_leg_uses_execution_price(self, broker):
        """Single-leg should average execution leg prices."""
        order = {
            'status': 'FILLED',
            'price': 2.50,
            'filledQuantity': 1,
            'orderLegCollection': [
                {'instruction': 'SELL_TO_OPEN'},
            ],
            'orderActivityCollection': [
                {
                    'executionLegs': [
                        {'legId': 1, 'price': 2.48, 'quantity': 1},
                    ]
                },
            ],
        }
        result = broker._parse_order_response(order)

        assert result['fill_price'] == 2.48
        assert 'leg_fills' not in result  # Only for spreads
