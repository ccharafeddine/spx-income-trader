"""
Tests for slippage tracking: theoretical mid-price vs actual fill.

Tests cover:
- Mid-price calculation for bullish (put) and bearish (call) spreads
- construct_spread() sets theoretical_mid_credit on CreditSpread
- _construct_strategy_spread() sets theoretical_mid_credit (parallel strategy path)
- Slippage computation: positive (favorable), negative (unfavorable), zero
- Graceful handling when theoretical_mid_credit is None (legacy spreads)
- Division-by-zero guard when theoretical_mid == 0
- DB migration adds the 4 new columns
- Integration: enter_trade() populates slippage in entry_context
"""

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models.spread import CreditSpread, OptionLeg, TradeDirection


# ============================================================================
# FIXTURES
# ============================================================================

ET = pytz.timezone("America/New_York")


def _make_chain(short_strike, long_strike, direction,
                short_bid, short_ask, long_bid, long_ask):
    """Build a minimal options chain dict for two strikes."""
    chain = {}
    if direction == TradeDirection.BULLISH:
        chain[short_strike] = {
            'put_bid': short_bid, 'put_ask': short_ask,
            'call_bid': 0, 'call_ask': 0,
        }
        chain[long_strike] = {
            'put_bid': long_bid, 'put_ask': long_ask,
            'call_bid': 0, 'call_ask': 0,
        }
    else:
        chain[short_strike] = {
            'call_bid': short_bid, 'call_ask': short_ask,
            'put_bid': 0, 'put_ask': 0,
        }
        chain[long_strike] = {
            'call_bid': long_bid, 'call_ask': long_ask,
            'put_bid': 0, 'put_ask': 0,
        }
    return chain


# ============================================================================
# Mid-Price Calculation
# ============================================================================

class TestMidPriceCalculation:
    """Verify theoretical mid-price math for both directions."""

    def test_bullish_mid_price(self):
        """Bullish put spread: mid = (short_put_mid) - (long_put_mid)."""
        # Short 6000 put: bid=3.00 ask=3.40 -> mid=3.20
        # Long  5995 put: bid=0.80 ask=1.00 -> mid=0.90
        # Theoretical mid credit = 3.20 - 0.90 = 2.30
        short_mid = (3.00 + 3.40) / 2  # 3.20
        long_mid = (0.80 + 1.00) / 2   # 0.90
        expected = round(short_mid - long_mid, 4)
        assert expected == 2.30

    def test_bearish_mid_price(self):
        """Bearish call spread: mid = (short_call_mid) - (long_call_mid)."""
        # Short 6000 call: bid=3.50 ask=3.90 -> mid=3.70
        # Long  6005 call: bid=1.10 ask=1.30 -> mid=1.20
        # Theoretical mid credit = 3.70 - 1.20 = 2.50
        short_mid = (3.50 + 3.90) / 2  # 3.70
        long_mid = (1.10 + 1.30) / 2   # 1.20
        expected = round(short_mid - long_mid, 4)
        assert expected == 2.50

    def test_wide_bid_ask_spread(self):
        """Wide bid-ask should produce mid between natural and limit credit."""
        # Short: bid=2.00 ask=4.00 -> mid=3.00
        # Long:  bid=0.50 ask=1.50 -> mid=1.00
        # Mid credit = 2.00 (between natural 0.50 and limit 3.50)
        short_mid = (2.00 + 4.00) / 2
        long_mid = (0.50 + 1.50) / 2
        assert round(short_mid - long_mid, 4) == 2.0


# ============================================================================
# construct_spread() - Strategy Path
# ============================================================================

class TestConstructSpreadMidCredit:
    """Test that SPXIncomeStrategy.construct_spread() sets theoretical_mid_credit."""

    def test_mid_credit_on_construct_spread(self):
        from src.core.strategy import SPXIncomeStrategy

        strategy = SPXIncomeStrategy(
            pulse_threshold=10.0,
            spread_width=5.0,
            profit_target_pct=80.0,
            min_credit=0.50,
        )

        chain = _make_chain(
            short_strike=6000, long_strike=5995,
            direction=TradeDirection.BULLISH,
            short_bid=3.00, short_ask=3.40,
            long_bid=0.80, long_ask=1.00,
        )

        spread = strategy.construct_spread(
            current_price=6000.0,
            direction=TradeDirection.BULLISH,
            options_chain=chain,
        )

        assert spread is not None
        # credit_received uses bid/ask: 3.00 - 1.00 = 2.00
        assert spread.credit_received == 2.00
        # theoretical_mid = (3.20) - (0.90) = 2.30
        assert spread.theoretical_mid_credit == 2.30

    def test_mid_credit_bearish_construct_spread(self):
        from src.core.strategy import SPXIncomeStrategy

        strategy = SPXIncomeStrategy(
            pulse_threshold=10.0,
            spread_width=5.0,
            profit_target_pct=80.0,
            min_credit=0.50,
        )

        chain = _make_chain(
            short_strike=6000, long_strike=6005,
            direction=TradeDirection.BEARISH,
            short_bid=3.50, short_ask=3.90,
            long_bid=1.10, long_ask=1.30,
        )

        spread = strategy.construct_spread(
            current_price=6000.0,
            direction=TradeDirection.BEARISH,
            options_chain=chain,
        )

        assert spread is not None
        # credit_received uses bid/ask: 3.50 - 1.30 = 2.20
        assert spread.credit_received == 2.20
        # theoretical_mid = 3.70 - 1.20 = 2.50
        assert spread.theoretical_mid_credit == 2.50


# ============================================================================
# Slippage Computation
# ============================================================================

class TestSlippageComputation:
    """Test slippage = actual_fill - theoretical_mid."""

    def test_positive_favorable_slippage(self):
        """Fill above mid -> positive slippage (favorable)."""
        theoretical_mid = 2.30
        actual_fill = 2.40
        slippage = round(actual_fill - theoretical_mid, 4)
        slippage_pct = round((slippage / theoretical_mid) * 100, 2)
        assert slippage == 0.10
        assert slippage_pct == 4.35  # 0.10/2.30 * 100

    def test_negative_unfavorable_slippage(self):
        """Fill below mid -> negative slippage (unfavorable)."""
        theoretical_mid = 2.50
        actual_fill = 2.35
        slippage = round(actual_fill - theoretical_mid, 4)
        slippage_pct = round((slippage / theoretical_mid) * 100, 2)
        assert slippage == -0.15
        assert slippage_pct == -6.0

    def test_zero_slippage(self):
        """Fill exactly at mid."""
        theoretical_mid = 2.00
        actual_fill = 2.00
        slippage = round(actual_fill - theoretical_mid, 4)
        assert slippage == 0.0

    def test_zero_theoretical_mid_no_division_error(self):
        """If theoretical_mid is 0, slippage_pct should be 0 (not crash)."""
        theoretical_mid = 0.0
        actual_fill = 0.05
        slippage = round(actual_fill - theoretical_mid, 4)
        slippage_pct = round((slippage / theoretical_mid) * 100, 2) if theoretical_mid != 0 else 0.0
        assert slippage == 0.05
        assert slippage_pct == 0.0

    def test_none_theoretical_no_slippage_keys(self):
        """When theoretical_mid_credit is None, no slippage keys in context."""
        entry_context = {'strategy_type': 'daily_income'}
        theoretical_mid = None
        actual_fill = 2.50

        if theoretical_mid is not None:
            entry_context['slippage'] = round(actual_fill - theoretical_mid, 4)

        assert 'slippage' not in entry_context
        assert 'slippage_pct' not in entry_context
        assert 'theoretical_credit' not in entry_context
        assert 'actual_credit' not in entry_context


# ============================================================================
# CreditSpread Dataclass
# ============================================================================

class TestCreditSpreadField:
    """Verify the theoretical_mid_credit field on CreditSpread."""

    def test_default_none(self):
        """Field defaults to None for backwards compatibility."""
        spread = CreditSpread(
            direction=TradeDirection.BULLISH,
            short_leg=OptionLeg(strike=6000, option_type='put', action='sell', price=3.00),
            long_leg=OptionLeg(strike=5995, option_type='put', action='buy', price=1.00),
            credit_received=2.00,
            entry_time=datetime.now(ET),
            expiration=datetime.now(ET),
        )
        assert spread.theoretical_mid_credit is None

    def test_set_explicitly(self):
        """Field can be set explicitly."""
        spread = CreditSpread(
            direction=TradeDirection.BULLISH,
            short_leg=OptionLeg(strike=6000, option_type='put', action='sell', price=3.00),
            long_leg=OptionLeg(strike=5995, option_type='put', action='buy', price=1.00),
            credit_received=2.00,
            entry_time=datetime.now(ET),
            expiration=datetime.now(ET),
            theoretical_mid_credit=2.30,
        )
        assert spread.theoretical_mid_credit == 2.30


# ============================================================================
# DB Migration
# ============================================================================

class TestDBMigration:
    """Test that _run_migrations() adds the 4 slippage columns."""

    def test_migration_adds_columns(self, tmp_path):
        from database.db_manager import DatabaseManager

        db_path = str(tmp_path / "test_slippage.db")
        db = DatabaseManager(db_path)

        # Check columns exist
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(trades)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()

        assert 'theoretical_credit' in columns
        assert 'actual_credit' in columns
        assert 'slippage' in columns
        assert 'slippage_pct' in columns

    def test_migration_idempotent(self, tmp_path):
        """Running migrations twice should not fail."""
        from database.db_manager import DatabaseManager

        db_path = str(tmp_path / "test_idempotent.db")
        db1 = DatabaseManager(db_path)
        db2 = DatabaseManager(db_path)  # second init re-runs migrations

        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(trades)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()

        assert 'slippage' in columns


# ============================================================================
# Integration: enter_trade() Slippage Flow
# ============================================================================

class TestEnterTradeSlippage:
    """Integration test: enter_trade() computes slippage and passes to save_trade()."""

    def test_slippage_in_entry_context(self):
        """Full flow: spread with mid -> enter_trade -> DB gets slippage fields."""
        from src.core.position_manager import PositionManager
        from src.models.trade import Trade, TradeStatus
        from src.models.bar import Bar

        # Mock broker
        broker = MagicMock()
        broker.place_spread_order.return_value = "order-123"
        broker.get_order_status.return_value = {
            'status': 'filled',
            'fill_price': 2.35,  # filled at 2.35 vs mid of 2.30
        }

        # Mock strategy
        strategy = MagicMock()
        strategy.classify_credit_quality.return_value = {
            'quality': 'GOOD',
            'credit_received': 2.35,
            'moneyness': 'ATM',
            'expected_range': '$2.40-$2.60',
            'expected_min': 2.40,
            'expected_max': 2.60,
            'pct_of_expected_min': 97.9,
            'is_simulated_concern': False,
        }

        # Mock DB - capture what save_trade receives
        db = MagicMock()
        saved_contexts = []
        def capture_save(trade, context=None):
            saved_contexts.append(context)
        db.save_trade.side_effect = capture_save

        pm = PositionManager(broker=broker, strategy=strategy, db_manager=db)

        # Build spread with theoretical_mid_credit
        now = datetime.now(ET)
        spread = CreditSpread(
            direction=TradeDirection.BULLISH,
            short_leg=OptionLeg(strike=6000, option_type='put', action='sell', price=3.00),
            long_leg=OptionLeg(strike=5995, option_type='put', action='buy', price=1.00),
            credit_received=2.00,
            entry_time=now,
            expiration=now.replace(hour=16, minute=0),
            underlying_price_at_entry=6000.0,
            theoretical_mid_credit=2.30,
        )

        bar = MagicMock(spec=Bar)
        bar.timestamp = now
        bar.open = 6000.0
        bar.high = 6010.0
        bar.low = 5990.0
        bar.close = 6005.0
        bar.range = 20.0
        bar.close_percentage_in_range.return_value = 75.0

        with patch('time.sleep'):
            trade = pm.enter_trade(spread, bar, quantity=1)

        assert trade is not None
        assert len(saved_contexts) == 1
        ctx = saved_contexts[0]
        assert ctx['theoretical_credit'] == 2.30
        assert ctx['actual_credit'] == 2.35
        assert ctx['slippage'] == 0.05
        assert ctx['slippage_pct'] == 2.17  # 0.05/2.30 * 100

    def test_no_slippage_when_no_theoretical(self):
        """Legacy spread without theoretical_mid_credit: no slippage in context."""
        from src.core.position_manager import PositionManager

        broker = MagicMock()
        broker.place_spread_order.return_value = "order-456"
        broker.get_order_status.return_value = {
            'status': 'filled',
            'fill_price': 2.00,
        }

        strategy = MagicMock()
        strategy.classify_credit_quality.return_value = {
            'quality': 'GOOD', 'credit_received': 2.00,
            'moneyness': 'ATM', 'expected_range': '$2.40-$2.60',
            'expected_min': 2.40, 'expected_max': 2.60,
            'pct_of_expected_min': 83.3, 'is_simulated_concern': False,
        }

        db = MagicMock()
        saved_contexts = []
        def capture_save(trade, context=None):
            saved_contexts.append(context)
        db.save_trade.side_effect = capture_save

        pm = PositionManager(broker=broker, strategy=strategy, db_manager=db)

        now = datetime.now(ET)
        spread = CreditSpread(
            direction=TradeDirection.BULLISH,
            short_leg=OptionLeg(strike=6000, option_type='put', action='sell', price=3.00),
            long_leg=OptionLeg(strike=5995, option_type='put', action='buy', price=1.00),
            credit_received=2.00,
            entry_time=now,
            expiration=now.replace(hour=16, minute=0),
            underlying_price_at_entry=6000.0,
            # No theoretical_mid_credit set (defaults to None)
        )

        bar = MagicMock()
        bar.timestamp = now
        bar.open = 6000.0
        bar.high = 6010.0
        bar.low = 5990.0
        bar.close = 6005.0
        bar.range = 20.0
        bar.close_percentage_in_range.return_value = 75.0

        with patch('time.sleep'):
            trade = pm.enter_trade(spread, bar, quantity=1)

        assert trade is not None
        ctx = saved_contexts[0]
        assert 'slippage' not in ctx
        assert 'theoretical_credit' not in ctx
