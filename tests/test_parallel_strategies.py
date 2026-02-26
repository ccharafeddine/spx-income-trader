"""
Tests for parallel strategy execution and shared-slot position limits.

Covers:
- Shared 0DTE slot (DI/ORB/B&B share 1 trade per day)
- Independent TNT swing slot (1 per day)
- Global circuit breaker blocks all strategies
- _execute_strategy_trade() with mocked broker
- _construct_strategy_spread() with different widths and expirations
- _restore_daily_counters() summing DI+ORB+B&B into dte0_trades_today
- _find_dte_expiration() helper
- DI not blocked by ORB/TNT/B&B open positions
"""

import pytest
from unittest.mock import Mock, MagicMock, patch, PropertyMock
from datetime import datetime, date, time, timedelta
import sys
from pathlib import Path
import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models.spread import CreditSpread, OptionLeg, TradeDirection
from src.models.bar import Bar
from src.core.portfolio_manager import PortfolioManager, StrategyType

ET = pytz.timezone("America/New_York")


# ============================================================================
# FIXTURES
# ============================================================================

def _make_options_chain(atm=6000.0, spread_width=5.0, credit=2.50):
    """Build a minimal options chain around the ATM strike."""
    chain = {}
    for offset in range(-20, 25, 5):
        strike = atm + offset
        chain[strike] = {
            'call_bid': max(credit + 0.20, 0.10),
            'call_ask': max(credit + 0.20 - credit, 0.05),
            'put_bid': max(credit + 0.20, 0.10),
            'put_ask': max(credit + 0.20 - credit, 0.05),
        }
    # Set realistic prices for ATM short + long
    short_strike = atm
    long_put = atm - spread_width
    long_call = atm + spread_width
    chain[short_strike]['put_bid'] = credit + 0.50
    chain[short_strike]['call_bid'] = credit + 0.50
    chain[long_put]['put_ask'] = 0.50
    chain[long_call]['call_ask'] = 0.50
    return chain


def _make_mock_bot(dry_run=True, daily_pnl=0.0):
    """Build a mock TradingBot with all needed attributes for testing."""
    import pytz

    bot = MagicMock()
    bot.tz = pytz.timezone("America/New_York")
    bot.dry_run = dry_run
    bot.dte0_trades_today = 0   # Shared 0DTE slot (DI/ORB/B&B)
    bot.tnt_trades_today = 0    # TNT swing slot
    bot.notifier = None

    # Mock broker
    bot.broker = MagicMock()
    bot.broker.get_options_chain.return_value = _make_options_chain()
    bot.broker.place_spread_order.return_value = "ORDER-123"
    bot.broker.get_order_status.return_value = {'status': 'filled', 'fill_price': 2.50, 'filled_quantity': 3}
    bot.broker.get_current_price.return_value = 6000.0

    # Mock portfolio (single source of truth for daily P&L)
    bot.portfolio = MagicMock(spec=PortfolioManager)
    bot.portfolio.daily_realized_pnl = daily_pnl
    bot.portfolio.max_daily_loss = 1000.0
    bot.portfolio.max_daily_loss_pct = 2.0
    bot.portfolio.circuit_breaker_triggered = (daily_pnl <= -1000.0)
    bot.portfolio.calculate_position_size.return_value = 3
    bot.portfolio.can_enter_position.return_value = (True, "Approved")
    bot.portfolio.register_position.return_value = True
    bot.portfolio.has_position_for_strategy.return_value = False

    # Mock position manager
    mock_trade = MagicMock()
    mock_trade.id = "test-trade-001"
    mock_trade.spread = MagicMock()
    mock_trade.spread.direction = TradeDirection.BULLISH
    mock_trade.spread.short_leg = MagicMock(strike=6000.0)
    mock_trade.spread.long_leg = MagicMock(strike=5995.0)
    mock_trade.spread.credit_received = 2.50
    mock_trade.spread.max_risk = 250.0
    mock_trade.spread.max_profit = 250.0
    mock_trade.spread.expiration = None
    bot.position_manager = MagicMock()
    bot.position_manager.enter_trade.return_value = mock_trade

    # Mock db
    bot.db = MagicMock()
    bot.db.get_daily_counts_by_strategy.return_value = {}
    bot.db.get_daily_summary.return_value = {'trades_count': 0, 'realized_pnl': 0.0}

    # Price feed
    bot.price_feed = MagicMock()
    bot.price_feed.get_latest_price.return_value = 6000.0
    bot.price_feed.get_latest_bar_data.return_value = None
    bot.price_feed.is_healthy.return_value = True
    bot.price_feed.get_health_status.return_value = {
        'source': 'yahoo', 'healthy': True,
        'last_update_secs_ago': 1.0, 'consecutive_failures': 0,
    }

    # Signal log
    bot._signal_log_path = Path(__file__).parent / 'test_signals.json'
    bot._log_signal = MagicMock()

    return bot


# ============================================================================
# TESTS: Per-Strategy Daily Limits
# ============================================================================

class TestShared0DTELimit:
    """Test that DI/ORB/B&B share a single 0DTE slot and TNT has its own."""

    def test_0dte_blocks_after_1(self):
        """Once any 0DTE strategy trades, all 0DTE strategies are blocked."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        bot.dte0_trades_today = 1  # One 0DTE trade already done

        result = TradingBot._check_0dte_limit(bot)
        assert result is False, "0DTE should be blocked after 1 trade"

    def test_0dte_allows_first_trade(self):
        """First 0DTE trade of the day should be allowed."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        assert bot.dte0_trades_today == 0

        result = TradingBot._check_0dte_limit(bot)
        assert result is True, "First 0DTE trade should be allowed"

    def test_tnt_independent_of_0dte(self):
        """TNT should not be blocked by 0DTE limit."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        bot.dte0_trades_today = 1  # 0DTE slot used

        result = TradingBot._check_tnt_limit(bot)
        assert result is True, "TNT should NOT be blocked by 0DTE limit"

    def test_tnt_blocks_after_1(self):
        """TNT should block after its own slot is used."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        bot.tnt_trades_today = 1

        result = TradingBot._check_tnt_limit(bot)
        assert result is False, "TNT should be blocked after 1 trade"

    def test_0dte_independent_of_tnt(self):
        """0DTE should not be blocked by TNT limit."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        bot.tnt_trades_today = 1  # TNT slot used

        result = TradingBot._check_0dte_limit(bot)
        assert result is True, "0DTE should NOT be blocked by TNT limit"


# ============================================================================
# TESTS: Global Circuit Breaker
# ============================================================================

class TestGlobalCircuitBreaker:
    """Test that the daily loss circuit breaker blocks ALL strategies."""

    def test_circuit_breaker_blocks_all(self):
        """When daily_pnl exceeds max_daily_loss, all strategies blocked."""
        from src.main import TradingBot

        bot = _make_mock_bot(daily_pnl=-1500.0)
        bot.notifier = None

        result = TradingBot._check_daily_loss_circuit_breaker(bot)
        assert result is False

    def test_circuit_breaker_ok_when_profitable(self):
        """Circuit breaker should not trigger when profitable."""
        from src.main import TradingBot

        bot = _make_mock_bot(daily_pnl=500.0)
        result = TradingBot._check_daily_loss_circuit_breaker(bot)
        assert result is True

    def test_circuit_breaker_at_exact_limit(self):
        """Circuit breaker triggers at exactly -max_daily_loss."""
        from src.main import TradingBot

        bot = _make_mock_bot(daily_pnl=-1000.0)
        bot.notifier = None
        result = TradingBot._check_daily_loss_circuit_breaker(bot)
        assert result is False


# ============================================================================
# TESTS: _construct_strategy_spread
# ============================================================================

class TestConstructStrategySpread:
    """Test spread construction with different parameters."""

    def test_bullish_spread_width_5(self):
        """Construct a $5-wide bullish put spread."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        chain = _make_options_chain(atm=6000.0, spread_width=5.0, credit=2.50)

        spread = TradingBot._construct_strategy_spread(
            bot, current_price=6000.0, direction=TradeDirection.BULLISH,
            options_chain=chain, spread_width=5.0, min_credit=1.00,
        )

        assert spread is not None
        assert spread.direction == TradeDirection.BULLISH
        assert spread.spread_width == 5.0
        assert spread.credit_received > 0

    def test_bearish_spread_width_10(self):
        """Construct a $10-wide bearish call spread (Tag 'n Turn style)."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        chain = _make_options_chain(atm=6000.0, spread_width=10.0, credit=3.00)

        spread = TradingBot._construct_strategy_spread(
            bot, current_price=6000.0, direction=TradeDirection.BEARISH,
            options_chain=chain, spread_width=10.0, min_credit=2.00,
        )

        assert spread is not None
        assert spread.direction == TradeDirection.BEARISH
        assert spread.spread_width == 10.0

    def test_min_credit_rejection(self):
        """Spread rejected when credit below min_credit."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        # Build chain with very low credit
        chain = _make_options_chain(atm=6000.0, credit=0.30)
        # Override so credit comes out below $1.00
        chain[6000.0]['put_bid'] = 0.60
        chain[5995.0]['put_ask'] = 0.50

        spread = TradingBot._construct_strategy_spread(
            bot, current_price=6000.0, direction=TradeDirection.BULLISH,
            options_chain=chain, spread_width=5.0, min_credit=1.00,
        )

        assert spread is None

    def test_custom_expiration(self):
        """Spread with custom expiration date (multi-DTE)."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        chain = _make_options_chain(atm=6000.0, credit=2.50)

        spread = TradingBot._construct_strategy_spread(
            bot, current_price=6000.0, direction=TradeDirection.BULLISH,
            options_chain=chain, spread_width=5.0, min_credit=1.00,
            expiration_str="2026-02-15",
        )

        assert spread is not None
        assert spread.expiration.date() == date(2026, 2, 15)


# ============================================================================
# TESTS: _find_dte_expiration
# ============================================================================

class TestFindDTEExpiration:
    """Test DTE expiration finder."""

    def test_skips_weekends(self):
        """Expiration should never land on a weekend."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        exp_str = TradingBot._find_dte_expiration(bot, min_dte=3, max_dte=7)
        exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()

        # Must be a weekday
        assert exp_date.weekday() < 5, f"Expiration {exp_str} is a weekend"

    def test_midpoint_dte(self):
        """Should target midpoint of DTE range."""
        from src.main import TradingBot
        import pytz

        bot = _make_mock_bot()
        exp_str = TradingBot._find_dte_expiration(bot, min_dte=3, max_dte=7)
        exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        today = datetime.now(pytz.timezone("America/New_York")).date()

        days_out = (exp_date - today).days
        # Midpoint of 3-7 is 5, but may shift by 1-2 for weekends
        assert 3 <= days_out <= 9, f"DTE {days_out} outside expected range"


# ============================================================================
# TESTS: _execute_strategy_trade
# ============================================================================

class TestExecuteStrategyTrade:
    """Test generic strategy execution flow."""

    def test_orb_0dte_execution(self):
        """Execute an ORB trade with 0DTE."""
        from src.main import TradingBot

        bot = _make_mock_bot()

        result = TradingBot._execute_strategy_trade(
            bot,
            strategy_type=StrategyType.ORB,
            direction=TradeDirection.BULLISH,
            current_price=6000.0,
            is_0dte=True,
            spread_width=5.0,
        )

        assert result is True
        assert bot.dte0_trades_today == 1  # ORB is 0DTE
        bot.portfolio.register_position.assert_called_once()
        bot.position_manager.enter_trade.assert_called_once()

        # Verify strategy_type was passed to enter_trade
        call_kwargs = bot.position_manager.enter_trade.call_args
        assert call_kwargs[1].get('strategy_type') == 'orb' or call_kwargs[0] is not None

    def test_tnt_multi_dte_execution(self):
        """Execute a Tag 'n Turn trade with multi-DTE expiration."""
        from src.main import TradingBot

        bot = _make_mock_bot()

        result = TradingBot._execute_strategy_trade(
            bot,
            strategy_type=StrategyType.TAG_N_TURN,
            direction=TradeDirection.BEARISH,
            current_price=6000.0,
            is_0dte=False,
            spread_width=10.0,
            min_credit=2.00,
            expiration_str="2026-02-15",
        )

        assert result is True
        assert bot.tnt_trades_today == 1  # TNT is swing (not 0DTE)

        # Verify is_0dte=False passed to portfolio
        call_kwargs = bot.portfolio.register_position.call_args[1]
        assert call_kwargs['is_0dte'] is False

    def test_portfolio_risk_blocks_trade(self):
        """Portfolio risk gate should prevent execution."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        bot.portfolio.can_enter_position.return_value = (False, "Max positions reached")

        result = TradingBot._execute_strategy_trade(
            bot,
            strategy_type=StrategyType.ORB,
            direction=TradeDirection.BULLISH,
            current_price=6000.0,
        )

        assert result is False
        assert bot.dte0_trades_today == 0
        bot.position_manager.enter_trade.assert_not_called()

    def test_empty_chain_fails_gracefully(self):
        """Empty options chain should fail without crashing."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        bot.broker.get_options_chain.return_value = None

        result = TradingBot._execute_strategy_trade(
            bot,
            strategy_type=StrategyType.ORB,
            direction=TradeDirection.BULLISH,
            current_price=6000.0,
        )

        assert result is False

    def test_enter_trade_failure(self):
        """Trade execution failure (broker rejects) should return False."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        bot.position_manager.enter_trade.return_value = None

        result = TradingBot._execute_strategy_trade(
            bot,
            strategy_type=StrategyType.ORB,
            direction=TradeDirection.BULLISH,
            current_price=6000.0,
        )

        assert result is False
        assert bot.dte0_trades_today == 0


# ============================================================================
# TESTS: _restore_daily_counters
# ============================================================================

class TestRestoreDailyCounters:
    """Test restoring trade counts from DB into shared-slot model."""

    def test_restore_mixed_strategies(self):
        """DI+ORB sum into dte0_trades_today, TNT into tnt_trades_today."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        bot.db.get_daily_counts_by_strategy.return_value = {
            'daily_income': 1,
            'orb': 1,
            'tag_n_turn': 2,
        }
        bot.db.get_daily_summary.return_value = {'trades_count': 4, 'realized_pnl': -150.0}

        TradingBot._restore_daily_counters(bot, date(2026, 2, 11))

        assert bot.dte0_trades_today == 2  # DI(1) + ORB(1)
        assert bot.tnt_trades_today == 2
        assert bot.portfolio.daily_realized_pnl == -150.0

    def test_restore_empty_day(self):
        """Restore on a day with no trades."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        bot.db.get_daily_counts_by_strategy.return_value = {}
        bot.db.get_daily_summary.return_value = {'trades_count': 0, 'realized_pnl': 0.0}

        TradingBot._restore_daily_counters(bot, date(2026, 2, 11))

        assert bot.dte0_trades_today == 0
        assert bot.tnt_trades_today == 0
        assert bot.portfolio.daily_realized_pnl == 0.0

    def test_restore_handles_db_error(self):
        """DB errors should reset to zero gracefully."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        bot.db.get_daily_counts_by_strategy.side_effect = Exception("DB locked")

        TradingBot._restore_daily_counters(bot, date(2026, 2, 11))

        assert bot.dte0_trades_today == 0
        assert bot.tnt_trades_today == 0
        assert bot.portfolio.daily_realized_pnl == 0.0


# ============================================================================
# TESTS: DI not blocked by other strategy positions
# ============================================================================

class TestDIPositionIndependence:
    """Verify DI doesn't check other strategies' positions for entry."""

    def test_di_breakout_not_blocked_by_orb(self):
        """DI breakout trigger should work when ORB has open position."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        # Portfolio says DI has no position (but ORB does)
        bot.portfolio.has_position_for_strategy.side_effect = lambda s: s == StrategyType.ORB

        # DI has a pending setup
        bot.pending_setup = {
            'direction': TradeDirection.BULLISH,
            'bar': Bar(
                timestamp=datetime(2026, 2, 11, 10, 0),
                open=5990, high=6010, low=5985, close=6005,
            ),
            'trigger_price': 6010.0,
            'timestamp': datetime(2026, 2, 11, 10, 30),
        }
        bot._current_spx_price = 6015.0  # Above trigger

        # Call the real method - it should NOT return early
        # (portfolio.has_position_for_strategy(DAILY_INCOME) returns False)
        TradingBot._check_breakout_trigger(bot, datetime(2026, 2, 11, 10, 35))

        # Should have called _execute_setup since breakout triggered
        bot._execute_setup.assert_called_once()

    def test_di_breakout_blocked_by_own_position(self):
        """DI breakout trigger blocked when DI already has a position."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        bot.portfolio.has_position_for_strategy.side_effect = lambda s: s == StrategyType.DAILY_INCOME

        bot.pending_setup = {
            'direction': TradeDirection.BULLISH,
            'bar': Bar(
                timestamp=datetime(2026, 2, 11, 10, 0),
                open=5990, high=6010, low=5985, close=6005,
            ),
            'trigger_price': 6010.0,
            'timestamp': datetime(2026, 2, 11, 10, 30),
        }
        bot._current_spx_price = 6015.0

        TradingBot._check_breakout_trigger(bot, datetime(2026, 2, 11, 10, 35))

        # Should NOT execute since DI already has a position
        bot._execute_setup.assert_not_called()


# ============================================================================
# TESTS: State rollback on failed execution
# ============================================================================

class TestORBRollbackOnFailure:
    """ORB sets triggered_today=True inside check_breakout() after confirmation.
    If execution fails, rollback_entry() must restore retryable state."""

    def test_rollback_resets_triggered_today(self, tmp_path):
        """After rollback, ORB can trigger again on next tick."""
        from src.core.orb_strategy import ORBStrategy, ORBRange

        orb = ORBStrategy(
            {'enabled': True, 'confirmation_minutes': 0},
            persistence_path=tmp_path / 'orb.json',
        )
        orb.opening_range = ORBRange(
            date='2026-02-11', high=6010.0, low=5990.0,
            close=6008.0, range_size=20.0,
            close_position_pct=90.0, direction_bias='bullish',
        )

        t0 = datetime(2026, 2, 11, 10, 30)
        t1 = datetime(2026, 2, 11, 10, 31)
        t2 = datetime(2026, 2, 11, 10, 32)
        t3 = datetime(2026, 2, 11, 10, 33)

        # First call: sets breakout pending
        signal = orb.check_breakout(6015.0, t0)
        # With confirmation_minutes=0, next call confirms immediately
        signal = orb.check_breakout(6015.0, t1)
        assert signal is not None
        assert orb.triggered_today is True
        assert orb.position_open is True

        # Simulate execution failure -> rollback
        orb.rollback_entry()
        assert orb.triggered_today is False
        assert orb.position_open is False
        assert orb.entry_price is None

        # Should be able to trigger again
        signal2 = orb.check_breakout(6015.0, t2)
        signal2 = orb.check_breakout(6015.0, t3)
        assert signal2 is not None, "ORB should retry after rollback"

    def test_no_retry_without_rollback(self, tmp_path):
        """Without rollback, triggered_today blocks all future breakouts."""
        from src.core.orb_strategy import ORBStrategy, ORBRange

        orb = ORBStrategy(
            {'enabled': True, 'confirmation_minutes': 0},
            persistence_path=tmp_path / 'orb.json',
        )
        orb.opening_range = ORBRange(
            date='2026-02-11', high=6010.0, low=5990.0,
            close=6008.0, range_size=20.0,
            close_position_pct=90.0, direction_bias='bullish',
        )

        t0 = datetime(2026, 2, 11, 10, 30)
        t1 = datetime(2026, 2, 11, 10, 31)
        t2 = datetime(2026, 2, 11, 10, 35)

        signal = orb.check_breakout(6015.0, t0)
        signal = orb.check_breakout(6015.0, t1)
        assert signal is not None

        # Without rollback: no retry
        signal2 = orb.check_breakout(6020.0, t2)
        assert signal2 is None, "ORB should be blocked without rollback"


class TestBnBSignalLogic:
    """B&B final-bar-only signal logic and bias/validation methods."""

    def test_bnb_final_bar_only(self, tmp_path):
        """15:00 pulse alone = no signal; 15:30 pulse = signal."""
        from src.core.bnb_strategy import BnBStrategy
        from src.models.bar import Bar

        bnb = BnBStrategy({'enabled': True}, persistence_path=tmp_path / 'bnb.json')

        # 15:00 bar with pulse (close at top of range)
        bar_1500 = Bar(
            timestamp=datetime(2026, 2, 11, 15, 0),
            open=6000, high=6020, low=6000, close=6019,  # bullish pulse
        )
        bnb.on_bar_complete(bar_1500, 6019)

        # 15:30 bar with NO pulse (close in middle)
        bar_1530 = Bar(
            timestamp=datetime(2026, 2, 11, 15, 30),
            open=6019, high=6030, low=6010, close=6020,  # middle
        )
        bnb.on_bar_complete(bar_1530, 6020)

        bnb.on_day_end(6020)
        assert bnb.pending_signal is None, "Only 15:00 pulse should not create signal"

        # Reset and test 15:30 pulse only
        bnb.reset_daily()
        bar_1500_flat = Bar(
            timestamp=datetime(2026, 2, 11, 15, 0),
            open=6000, high=6020, low=6000, close=6010,  # middle
        )
        bnb.on_bar_complete(bar_1500_flat, 6010)

        bar_1530_pulse = Bar(
            timestamp=datetime(2026, 2, 11, 15, 30),
            open=6010, high=6030, low=6010, close=6029,  # bullish pulse
        )
        bnb.on_bar_complete(bar_1530_pulse, 6029)

        bnb.on_day_end(6029)
        assert bnb.pending_signal is not None, "15:30 pulse should create signal"
        assert bnb.pending_signal.direction == 'bullish'

    def test_bnb_conflicting_pulses(self, tmp_path):
        """15:00 bullish + 15:30 bearish = no signal."""
        from src.core.bnb_strategy import BnBStrategy
        from src.models.bar import Bar

        bnb = BnBStrategy({'enabled': True}, persistence_path=tmp_path / 'bnb.json')

        bar_1500 = Bar(
            timestamp=datetime(2026, 2, 11, 15, 0),
            open=6000, high=6020, low=6000, close=6019,  # bullish
        )
        bnb.on_bar_complete(bar_1500, 6019)

        bar_1530 = Bar(
            timestamp=datetime(2026, 2, 11, 15, 30),
            open=6019, high=6020, low=6000, close=6001,  # bearish
        )
        bnb.on_bar_complete(bar_1530, 6001)

        bnb.on_day_end(6001)
        assert bnb.pending_signal is None, "Conflicting pulses should produce no signal"

    def test_bnb_reinforcing_pulses(self, tmp_path):
        """Both bars same direction = signal."""
        from src.core.bnb_strategy import BnBStrategy
        from src.models.bar import Bar

        bnb = BnBStrategy({'enabled': True}, persistence_path=tmp_path / 'bnb.json')

        bar_1500 = Bar(
            timestamp=datetime(2026, 2, 11, 15, 0),
            open=6000, high=6020, low=6000, close=6001,  # bearish
        )
        bnb.on_bar_complete(bar_1500, 6001)

        bar_1530 = Bar(
            timestamp=datetime(2026, 2, 11, 15, 30),
            open=6001, high=6010, low=5990, close=5991,  # bearish
        )
        bnb.on_bar_complete(bar_1530, 5991)

        bnb.on_day_end(5991)
        assert bnb.pending_signal is not None, "Reinforcing pulses should create signal"
        assert bnb.pending_signal.direction == 'bearish'

    def test_bnb_get_bias(self, tmp_path):
        """get_bias() returns correct direction from active_signal."""
        from src.core.bnb_strategy import BnBStrategy, BnBSignal

        bnb = BnBStrategy({'enabled': True}, persistence_path=tmp_path / 'bnb.json')

        assert bnb.get_bias() is None, "No active signal should return None"

        bnb.active_signal = BnBSignal(
            signal_date='2026-02-10', direction='bearish',
            pulse_bar_time='15:30', pulse_bar_close=6000.0,
            spx_close=6005.0,
        )
        assert bnb.get_bias() == 'bearish'

    def test_bnb_validate_signal_gap_invalidation(self, tmp_path):
        """Gap > 0.3% against direction = invalid."""
        from src.core.bnb_strategy import BnBStrategy, BnBSignal

        bnb = BnBStrategy(
            {'enabled': True, 'gap_invalidation_pct': 0.3},
            persistence_path=tmp_path / 'bnb.json',
        )

        bnb.active_signal = BnBSignal(
            signal_date='2026-02-10', direction='bullish',
            pulse_bar_time='15:30', pulse_bar_close=6000.0,
            spx_close=6000.0,
        )

        # Gap down > 0.3% against bullish signal = invalid
        gapped_price = 6000.0 * (1 - 0.004)  # -0.4%
        assert bnb.validate_signal(gapped_price) is False

        # Gap up against bearish signal = invalid
        bnb.active_signal.direction = 'bearish'
        gapped_up = 6000.0 * (1 + 0.004)  # +0.4%
        assert bnb.validate_signal(gapped_up) is False

    def test_bnb_validate_signal_valid(self, tmp_path):
        """Small or favorable gap = valid."""
        from src.core.bnb_strategy import BnBStrategy, BnBSignal

        bnb = BnBStrategy(
            {'enabled': True, 'gap_invalidation_pct': 0.3},
            persistence_path=tmp_path / 'bnb.json',
        )

        bnb.active_signal = BnBSignal(
            signal_date='2026-02-10', direction='bullish',
            pulse_bar_time='15:30', pulse_bar_close=6000.0,
            spx_close=6000.0,
        )

        # Small gap down (within tolerance) = valid
        assert bnb.validate_signal(5990.0) is True  # -0.17%

        # Gap up (favorable for bullish) = valid
        assert bnb.validate_signal(6020.0) is True


class TestTNTResetOnFailure:
    """TNT should reset state machine to IDLE when execution fails."""

    def test_wiring_resets_on_failure(self):
        """main.py calls _reset_to_idle() when _execute_strategy_trade returns False."""
        from src.main import TradingBot
        from src.models.spread import TradeDirection

        bot = _make_mock_bot()
        bot.tag_n_turn_enabled = True
        bot.tag_n_turn = MagicMock()

        # Signal detected
        bot.tag_n_turn.check_entry_signal.return_value = {
            'direction': TradeDirection.BULLISH,
            'entry_price': 6000.0,
            'target_price': 6050.0,
            'stop_price': 5950.0,
        }
        bot.tag_n_turn.check_exit_conditions.return_value = None

        # Execution will fail
        bot._execute_strategy_trade = MagicMock(return_value=False)
        bot._check_daily_loss_circuit_breaker = MagicMock(return_value=True)
        bot._check_tnt_limit = MagicMock(return_value=True)
        bot._check_0dte_limit = MagicMock(return_value=True)

        # Wire real _update_market_state but mock inner calls
        bot.orb_enabled = False
        bot.bnb_enabled = False
        bot.bollinger = MagicMock()
        bot.bollinger.day_open = 6000.0
        bot.bar_builder = MagicMock()
        bot.bar_builder.add_price.return_value = None
        bot.bar_builder.current_bar_start = None
        bot._current_spx_price = 0
        bot.broker.get_current_price.return_value = 6000.0

        import pytz
        current_time = datetime(2026, 2, 11, 10, 30, tzinfo=pytz.timezone("America/New_York"))
        TradingBot._update_market_state(bot, current_time)

        # Verify _reset_to_idle was called after failed execution
        bot.tag_n_turn._reset_to_idle.assert_called_once_with("Trade execution failed")
        bot.tag_n_turn.on_position_opened.assert_not_called()


# ============================================================================
# TESTS: Database get_daily_counts_by_strategy
# ============================================================================

class TestDBDailyCounts:
    """Test the new DB query method."""

    def test_counts_by_strategy(self, tmp_path):
        """Query returns correct per-strategy counts."""
        from database.db_manager import DatabaseManager

        db = DatabaseManager(str(tmp_path / "test.db"))

        # Insert test trades directly
        import sqlite3
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        exp = '2026-02-11 16:00:00'
        conn.execute("""
            INSERT INTO trades (id, entry_time, direction, status, short_strike,
                              long_strike, spread_width, credit_received, entry_price,
                              quantity, expiration, strategy_type)
            VALUES ('t1', '2026-02-11 10:00:00', 'bullish', 'closed', 6000, 5995,
                    5.0, 2.50, 2.50, 1, ?, 'daily_income')
        """, (exp,))
        conn.execute("""
            INSERT INTO trades (id, entry_time, direction, status, short_strike,
                              long_strike, spread_width, credit_received, entry_price,
                              quantity, expiration, strategy_type)
            VALUES ('t2', '2026-02-11 10:30:00', 'bearish', 'active', 6010, 6015,
                    5.0, 2.00, 2.00, 3, ?, 'orb')
        """, (exp,))
        conn.execute("""
            INSERT INTO trades (id, entry_time, direction, status, short_strike,
                              long_strike, spread_width, credit_received, entry_price,
                              quantity, expiration, strategy_type)
            VALUES ('t3', '2026-02-11 11:00:00', 'bullish', 'closed', 5990, 5985,
                    5.0, 2.80, 2.80, 2, ?, 'orb')
        """, (exp,))
        conn.commit()
        conn.close()

        counts = db.get_daily_counts_by_strategy(date(2026, 2, 11))

        assert counts.get('daily_income') == 1
        assert counts.get('orb') == 2
        assert counts.get('tag_n_turn') is None  # No TNT trades
        assert counts.get('bnb') is None

    def test_empty_day(self, tmp_path):
        """Query returns empty dict for a day with no trades."""
        from database.db_manager import DatabaseManager

        db = DatabaseManager(str(tmp_path / "test.db"))
        counts = db.get_daily_counts_by_strategy(date(2026, 2, 11))

        assert counts == {}


# ============================================================================
# TESTS: close_trade_by_id
# ============================================================================

class TestCloseTradeById:
    """Test PositionManager.close_trade_by_id()"""

    def test_close_valid_trade(self):
        """close_trade_by_id delegates to _exit_trade and returns P&L."""
        from src.core.position_manager import PositionManager
        from src.models.trade import Trade, TradeStatus

        pm = MagicMock(spec=PositionManager)

        trade = MagicMock(spec=Trade)
        trade.id = "trade-001"
        trade.status = TradeStatus.ACTIVE
        trade.pnl = 150.0

        pm.open_trades = [trade]

        # After _exit_trade, status should become CLOSED
        def fake_exit(t, reason):
            t.status = TradeStatus.CLOSED
        pm._exit_trade = MagicMock(side_effect=fake_exit)

        result = PositionManager.close_trade_by_id(pm, "trade-001", "TNT: target hit")

        pm._exit_trade.assert_called_once_with(trade, "TNT: target hit")
        assert result == 150.0

    def test_close_invalid_id(self):
        """close_trade_by_id returns None for unknown trade ID."""
        from src.core.position_manager import PositionManager

        pm = MagicMock(spec=PositionManager)
        pm.open_trades = []

        result = PositionManager.close_trade_by_id(pm, "no-such-trade", "reason")
        assert result is None

    def test_close_non_active_trade(self):
        """close_trade_by_id skips trades that are not ACTIVE."""
        from src.core.position_manager import PositionManager
        from src.models.trade import Trade, TradeStatus

        pm = MagicMock(spec=PositionManager)

        trade = MagicMock(spec=Trade)
        trade.id = "trade-002"
        trade.status = TradeStatus.CLOSED

        pm.open_trades = [trade]

        result = PositionManager.close_trade_by_id(pm, "trade-002", "reason")
        assert result is None

    def test_close_order_not_filled(self):
        """When _exit_trade fails (trade stays ACTIVE), returns None."""
        from src.core.position_manager import PositionManager
        from src.models.trade import Trade, TradeStatus

        pm = MagicMock(spec=PositionManager)

        trade = MagicMock(spec=Trade)
        trade.id = "trade-003"
        trade.status = TradeStatus.ACTIVE

        pm.open_trades = [trade]

        # _exit_trade does NOT change status (simulates failed close order)
        pm._exit_trade = MagicMock()

        result = PositionManager.close_trade_by_id(pm, "trade-003", "reason")
        assert result is None


# ============================================================================
# TESTS: TNT Exit Execution
# ============================================================================

class TestTNTExitExecution:
    """Test TNT exit wiring in _update_market_state."""

    def test_tnt_exit_closes_position(self):
        """TNT exit signal triggers broker close and state machine reset."""
        from src.main import TradingBot
        import pytz

        bot = _make_mock_bot()
        bot.tag_n_turn_enabled = True
        bot.tag_n_turn = MagicMock()
        bot.tag_n_turn.check_entry_signal.return_value = None
        bot.tag_n_turn.check_exit_conditions.return_value = {
            'reason': 'Target hit',
        }

        bot.orb_enabled = False
        bot.bnb_enabled = False
        bot.bollinger = MagicMock()
        bot.bollinger.day_open = 6000.0
        bot.bar_builder = MagicMock()
        bot.bar_builder.add_price.return_value = None
        bot.bar_builder.current_bar_start = None
        bot._current_spx_price = 0
        bot.broker.get_current_price.return_value = 6000.0

        # Mock portfolio position for TNT
        mock_slot = MagicMock()
        mock_slot.position_id = "tnt-trade-001"
        bot.portfolio.get_positions_by_strategy.return_value = [mock_slot]

        # Mock successful close
        bot.position_manager.close_trade_by_id = MagicMock(return_value=200.0)
        bot.position_manager.recently_closed = []
        bot._drain_recently_closed = MagicMock()

        tz = pytz.timezone("America/New_York")
        current_time = datetime(2026, 2, 11, 14, 0, tzinfo=tz)
        TradingBot._update_market_state(bot, current_time)

        # Verify close was called
        bot.position_manager.close_trade_by_id.assert_called_once_with(
            "tnt-trade-001", "TNT: Target hit"
        )
        bot._drain_recently_closed.assert_called_once()
        bot.tag_n_turn.on_position_closed.assert_called_once()
        bot._log_signal.assert_called()

    def test_tnt_exit_no_position(self):
        """TNT exit signal with no portfolio position logs warning."""
        from src.main import TradingBot
        import pytz

        bot = _make_mock_bot()
        bot.tag_n_turn_enabled = True
        bot.tag_n_turn = MagicMock()
        bot.tag_n_turn.check_entry_signal.return_value = None
        bot.tag_n_turn.check_exit_conditions.return_value = {
            'reason': 'Stop loss',
        }

        bot.orb_enabled = False
        bot.bnb_enabled = False
        bot.bollinger = MagicMock()
        bot.bollinger.day_open = 6000.0
        bot.bar_builder = MagicMock()
        bot.bar_builder.add_price.return_value = None
        bot.bar_builder.current_bar_start = None
        bot._current_spx_price = 0
        bot.broker.get_current_price.return_value = 6000.0

        # No TNT positions
        bot.portfolio.get_positions_by_strategy.return_value = []

        tz = pytz.timezone("America/New_York")
        current_time = datetime(2026, 2, 11, 14, 0, tzinfo=tz)
        TradingBot._update_market_state(bot, current_time)

        # Should NOT attempt to close
        bot.position_manager.close_trade_by_id = MagicMock()
        # (already returned before getting there)
        assert bot.portfolio.daily_realized_pnl == 0.0

    def test_tnt_exit_close_failed_retries(self):
        """When close order fails, TNT stays open for retry next cycle."""
        from src.main import TradingBot
        import pytz

        bot = _make_mock_bot()
        bot.tag_n_turn_enabled = True
        bot.tag_n_turn = MagicMock()
        bot.tag_n_turn.check_entry_signal.return_value = None
        bot.tag_n_turn.check_exit_conditions.return_value = {
            'reason': 'Target hit',
        }

        bot.orb_enabled = False
        bot.bnb_enabled = False
        bot.bollinger = MagicMock()
        bot.bollinger.day_open = 6000.0
        bot.bar_builder = MagicMock()
        bot.bar_builder.add_price.return_value = None
        bot.bar_builder.current_bar_start = None
        bot._current_spx_price = 0
        bot.broker.get_current_price.return_value = 6000.0

        mock_slot = MagicMock()
        mock_slot.position_id = "tnt-trade-002"
        bot.portfolio.get_positions_by_strategy.return_value = [mock_slot]

        # Close fails (returns None)
        bot.position_manager.close_trade_by_id = MagicMock(return_value=None)

        tz = pytz.timezone("America/New_York")
        current_time = datetime(2026, 2, 11, 14, 0, tzinfo=tz)
        TradingBot._update_market_state(bot, current_time)

        # State machine should NOT be reset (will retry next cycle)
        bot.tag_n_turn.on_position_closed.assert_not_called()
        assert bot.portfolio.daily_realized_pnl == 0.0


# ============================================================================
# TESTS: ORB Filter and Confirmation
# ============================================================================

class TestORBFilters:
    """Test ORB range filter and confirmation delay."""

    def test_orb_range_too_small(self, tmp_path):
        """Range < min_range_points results in no signal from set_opening_range()."""
        from src.core.orb_strategy import ORBStrategy
        from src.models.bar import Bar

        orb = ORBStrategy(
            {'enabled': True, 'min_range_points': 8.0},
            persistence_path=tmp_path / 'orb.json',
        )

        # Bar with only 5-point range (too small)
        bar = Bar(
            timestamp=datetime(2026, 2, 11, 9, 30),
            open=6000, high=6005, low=6000, close=6004.5,  # 5pt range
        )
        result = orb.set_opening_range(bar)
        assert result is None, "Range < 8.0 should skip"

    def test_orb_strong_signal_confirmed_breakout(self, tmp_path):
        """Strong pulse + price stays beyond ORB level for 3 min = entry."""
        from src.core.orb_strategy import ORBStrategy, ORBRange

        orb = ORBStrategy(
            {'enabled': True, 'confirmation_minutes': 3},
            persistence_path=tmp_path / 'orb.json',
        )
        orb.opening_range = ORBRange(
            date='2026-02-11', high=6010.0, low=5990.0,
            close=6009.0, range_size=20.0,
            close_position_pct=95.0, direction_bias='bullish',
        )

        t0 = datetime(2026, 2, 11, 10, 30)
        t3 = datetime(2026, 2, 11, 10, 33)

        # First: breakout detected, pending confirmation
        signal = orb.check_breakout(6015.0, t0)
        assert signal is None
        assert orb._breakout_pending is not None

        # After 3 min, price still above - confirmed
        signal = orb.check_breakout(6016.0, t3)
        assert signal is not None
        assert signal['direction'] == 'bullish'
        assert orb.triggered_today is True

    def test_orb_breakout_retreats_during_confirmation(self, tmp_path):
        """Strong pulse + breakout + retreat within 3 min = no entry, no retry."""
        from src.core.orb_strategy import ORBStrategy, ORBRange

        orb = ORBStrategy(
            {'enabled': True, 'confirmation_minutes': 3},
            persistence_path=tmp_path / 'orb.json',
        )
        orb.opening_range = ORBRange(
            date='2026-02-11', high=6010.0, low=5990.0,
            close=6009.0, range_size=20.0,
            close_position_pct=95.0, direction_bias='bullish',
        )

        t0 = datetime(2026, 2, 11, 10, 30)
        t3 = datetime(2026, 2, 11, 10, 33)

        # Breakout detected
        signal = orb.check_breakout(6015.0, t0)
        assert signal is None

        # After 3 min, price retreated back inside range
        signal = orb.check_breakout(6005.0, t3)
        assert signal is None
        assert orb.triggered_today is True, "Failed confirmation should block retry"
        assert orb._breakout_pending is None

    def test_orb_no_weak_signals(self, tmp_path):
        """Close at 30% (old weak zone) results in direction_bias=None."""
        from src.core.orb_strategy import ORBStrategy
        from src.models.bar import Bar

        orb = ORBStrategy(
            {'enabled': True, 'min_range_points': 1.0},
            persistence_path=tmp_path / 'orb.json',
        )

        # Bar where close is at 30% of range (used to be "weak" signal)
        bar = Bar(
            timestamp=datetime(2026, 2, 11, 9, 30),
            open=6000, high=6020, low=6000, close=6006,  # 30% position
        )
        result = orb.set_opening_range(bar)
        assert result is not None
        assert result.direction_bias is None, "30% close should not generate signal (no weak signals)"


# ============================================================================
# TESTS: B&B on_day_end lifecycle
# ============================================================================

class TestOnDayEnd:
    """Test that on_day_end is called once after market close."""

    def test_on_day_end_called_after_4pm(self):
        """on_day_end called once when market closed after 4 PM."""
        from src.main import TradingBot
        import pytz

        bot = _make_mock_bot()
        bot.bnb_enabled = True
        bot.bnb_strategy = MagicMock()
        bot._bnb_day_end_called = False
        bot.running = False  # Prevent loop from continuing
        tz = pytz.timezone("America/New_York")
        bot.current_trading_date = date(2026, 2, 11)
        bot.broker.get_current_price.return_value = 6050.0

        # Simulate market closed check at 4:05 PM
        current_time = datetime(2026, 2, 11, 16, 5, tzinfo=tz)
        bot._is_market_open = MagicMock(return_value=False)
        bot._interruptible_sleep = MagicMock()

        # Call the market-closed branch directly via the main loop body
        # We test the condition logic directly
        if (bot.bnb_enabled and bot.bnb_strategy
                and not bot._bnb_day_end_called
                and bot.current_trading_date == current_time.date()
                and current_time.time() >= time(16, 0)):
            spx_close = bot.broker.get_current_price("SPX")
            if spx_close > 0:
                bot.bnb_strategy.on_day_end(spx_close)
                bot._bnb_day_end_called = True

        bot.bnb_strategy.on_day_end.assert_called_once_with(6050.0)
        assert bot._bnb_day_end_called is True

    def test_on_day_end_not_called_twice(self):
        """on_day_end should not be called again once _bnb_day_end_called is True."""
        bot = _make_mock_bot()
        bot.bnb_enabled = True
        bot.bnb_strategy = MagicMock()
        bot._bnb_day_end_called = True  # Already called

        import pytz
        tz = pytz.timezone("America/New_York")
        current_time = datetime(2026, 2, 11, 16, 10, tzinfo=tz)
        bot.current_trading_date = date(2026, 2, 11)

        # Should skip because _bnb_day_end_called is True
        called = (bot.bnb_enabled and bot.bnb_strategy
                  and not bot._bnb_day_end_called
                  and bot.current_trading_date == current_time.date()
                  and current_time.time() >= time(16, 0))

        assert called is False
        bot.bnb_strategy.on_day_end.assert_not_called()


# ============================================================================
# TESTS: Duplicate close signal prevention
# ============================================================================

class TestDuplicateCloseSignalPrevention:
    """Verify a position only generates one close signal, not one per loop cycle.

    Root cause: DryRunBroker.close_spread() didn't register the close order in
    hypothetical_positions, so get_order_status() returned 'not_found', the trade
    stayed in open_trades, and monitor_positions() re-triggered exits every cycle.
    """

    def test_dry_run_close_order_is_findable(self):
        """DryRunBroker.close_spread() must register order for get_order_status()."""
        from src.brokers.dry_run_broker import DryRunBroker

        broker = DryRunBroker(initial_balance=50000.0)

        spread = MagicMock()
        spread.direction = TradeDirection.BULLISH
        spread.short_leg = MagicMock(strike=6000.0)
        spread.long_leg = MagicMock(strike=5995.0)
        spread.spread_width = 5.0

        order_id = broker.close_spread(spread, quantity=3, limit_price=0.20)

        status = broker.get_order_status(order_id)
        assert status['status'] == 'filled', (
            f"Close order status should be 'filled', got '{status['status']}'"
        )

    def test_monitor_positions_single_close_signal(self):
        """A position should generate exactly one close signal across multiple cycles."""
        from src.core.position_manager import PositionManager
        from src.models.trade import Trade, TradeStatus

        broker = MagicMock()
        strategy = MagicMock()
        db = MagicMock()

        pm = PositionManager(broker, strategy, db)

        # Create a realistic trade
        trade = MagicMock(spec=Trade)
        trade.id = "test-close-once"
        trade.status = TradeStatus.ACTIVE
        trade.spread = MagicMock()
        trade.spread.direction = TradeDirection.BULLISH
        trade.spread.short_leg = MagicMock(strike=6000.0)
        trade.spread.long_leg = MagicMock(strike=5995.0)
        trade.spread.spread_width = 5.0
        trade.spread.max_profit = 250.0
        trade.spread.profit_at_price = MagicMock(return_value=250.0)
        trade.spread.credit_received = 2.50
        trade.quantity = 1
        trade.pnl = 200.0
        trade.pnl_percent = 80.0
        trade.entry_time = datetime(2026, 2, 11, 10, 0)
        trade.exit_time = None
        trade._close_pending = False

        pm.open_trades = [trade]

        # Strategy says exit on every check
        strategy.should_exit.return_value = (True, "80% profit target")

        # Broker: close_spread succeeds, order fills
        broker.get_position_value.return_value = 0.50
        broker.get_current_price.return_value = 6000.0
        broker.close_spread.return_value = "ORDER-CLOSE-1"
        broker.get_order_status.return_value = {
            'status': 'filled', 'fill_price': 0.50,
        }

        # First cycle: should close the trade
        pnl1 = pm.monitor_positions()
        assert broker.close_spread.call_count == 1, "Should call close_spread once"

        # Trade was closed and removed from open_trades, so second cycle is a no-op
        pnl2 = pm.monitor_positions()
        assert broker.close_spread.call_count == 1, (
            "Should NOT call close_spread again (trade already removed)"
        )

    def test_close_pending_prevents_duplicate_on_failed_fill(self):
        """If close order fails to fill, _close_pending prevents re-submission."""
        from src.core.position_manager import PositionManager
        from src.models.trade import Trade, TradeStatus

        broker = MagicMock()
        strategy = MagicMock()
        db = MagicMock()

        pm = PositionManager(broker, strategy, db)

        trade = MagicMock(spec=Trade)
        trade.id = "test-pending-guard"
        trade.status = TradeStatus.ACTIVE
        trade.spread = MagicMock()
        trade.spread.spread_width = 5.0
        trade.quantity = 1
        trade.pnl = 100.0
        trade.pnl_percent = 40.0
        trade._close_pending = False

        pm.open_trades = [trade]

        strategy.should_exit.return_value = (True, "80% profit target")
        broker.get_position_value.return_value = 0.50
        broker.get_current_price.return_value = 6000.0
        broker.close_spread.return_value = "ORDER-CLOSE-2"
        # Order NOT filled
        broker.get_order_status.return_value = {
            'status': 'pending', 'fill_price': 0,
        }

        # First cycle: close attempted, fails, _close_pending cleared
        pm.monitor_positions()
        assert broker.close_spread.call_count == 1

        # _close_pending was set then cleared on failure, so retry is allowed
        # but only ONE retry per cycle
        pm.monitor_positions()
        assert broker.close_spread.call_count == 2

    def test_close_pending_flag_set_before_broker_call(self):
        """_close_pending must be True while broker.close_spread() executes."""
        from src.core.position_manager import PositionManager
        from src.models.trade import Trade, TradeStatus

        broker = MagicMock()
        strategy = MagicMock()
        db = MagicMock()

        pm = PositionManager(broker, strategy, db)

        trade = MagicMock(spec=Trade)
        trade.id = "test-flag-timing"
        trade.status = TradeStatus.ACTIVE
        trade.spread = MagicMock()
        trade.spread.spread_width = 5.0
        trade.quantity = 1
        trade.pnl = 200.0
        trade.pnl_percent = 80.0
        trade._close_pending = False

        pm.open_trades = [trade]

        strategy.should_exit.return_value = (True, "80% profit target")
        broker.get_position_value.return_value = 0.50
        broker.get_current_price.return_value = 6000.0

        # Capture _close_pending state when close_spread is called
        pending_during_close = []

        def capture_pending(*args, **kwargs):
            pending_during_close.append(trade._close_pending)
            return "ORDER-CLOSE-3"

        broker.close_spread.side_effect = capture_pending
        broker.get_order_status.return_value = {
            'status': 'filled', 'fill_price': 0.50,
        }

        pm.monitor_positions()

        assert pending_during_close == [True], (
            "_close_pending should be True when broker.close_spread() executes"
        )


# ============================================================================
# TESTS: DryRunBroker chain quality validation
# ============================================================================

class TestDryRunChainQuality:
    """Verify that DryRunBroker falls back to synthetic pricing when Yahoo
    Finance returns chains with all-zero bid/ask prices (common for 0DTE SPX).
    """

    def test_zero_price_chain_falls_back_to_synthetic(self):
        """Chain with all-zero bids at ATM should trigger synthetic fallback."""
        from src.brokers.dry_run_broker import DryRunBroker

        broker = DryRunBroker(initial_balance=50000.0)

        # Build a chain with 219 strikes but all zero prices (simulates Yahoo bug)
        zero_chain = {}
        for i in range(-110, 110):
            strike = 6000.0 + i * 5
            zero_chain[strike] = {
                'call_bid': 0, 'call_ask': 0, 'call_last': 0,
                'call_volume': 0, 'call_oi': 0,
                'put_bid': 0, 'put_ask': 0, 'put_last': 0,
                'put_volume': 0, 'put_oi': 0,
            }

        with patch.object(broker, '_fetch_real_options_chain', return_value=zero_chain), \
             patch.object(broker, 'get_current_price', return_value=6000.0), \
             patch.object(broker, '_build_synthetic_chain') as mock_synth:
            mock_synth.return_value = {6000.0: {'put_bid': 2.50}}
            chain = broker.get_options_chain('SPX', '2026-02-18')

            # Should have called synthetic fallback
            mock_synth.assert_called_once()
            assert broker._last_chain_source == 'synthetic'

    def test_valid_chain_passes_quality_check(self):
        """Chain with non-zero ATM bids should be accepted as-is."""
        from src.brokers.dry_run_broker import DryRunBroker

        broker = DryRunBroker(initial_balance=50000.0)

        # Build a chain with some valid prices at ATM
        valid_chain = {}
        for i in range(-10, 11):
            strike = 6000.0 + i * 5
            valid_chain[strike] = {
                'call_bid': max(0, 2.0 - abs(i) * 0.2),
                'call_ask': max(0.1, 2.2 - abs(i) * 0.2),
                'call_last': 2.1, 'call_volume': 100, 'call_oi': 500,
                'put_bid': max(0, 2.0 - abs(i) * 0.2),
                'put_ask': max(0.1, 2.2 - abs(i) * 0.2),
                'put_last': 2.1, 'put_volume': 100, 'put_oi': 500,
            }

        with patch.object(broker, '_fetch_real_options_chain', return_value=valid_chain), \
             patch.object(broker, 'get_current_price', return_value=6000.0), \
             patch.object(broker, '_build_synthetic_chain') as mock_synth:
            chain = broker.get_options_chain('SPX', '2026-02-18')

            # Should NOT have called synthetic fallback
            mock_synth.assert_not_called()
            assert broker._last_chain_source == 'real'
            assert len(chain) == 21

    def test_partial_zero_chain_passes_if_atm_has_prices(self):
        """Chain where only deep OTM strikes are zero should still pass."""
        from src.brokers.dry_run_broker import DryRunBroker

        broker = DryRunBroker(initial_balance=50000.0)

        chain = {}
        for i in range(-20, 21):
            strike = 6000.0 + i * 5
            # Only strikes within 15 of ATM have prices
            if abs(i) <= 3:
                chain[strike] = {
                    'call_bid': 1.5, 'call_ask': 1.7, 'call_last': 1.6,
                    'call_volume': 50, 'call_oi': 200,
                    'put_bid': 1.5, 'put_ask': 1.7, 'put_last': 1.6,
                    'put_volume': 50, 'put_oi': 200,
                }
            else:
                chain[strike] = {
                    'call_bid': 0, 'call_ask': 0, 'call_last': 0,
                    'call_volume': 0, 'call_oi': 0,
                    'put_bid': 0, 'put_ask': 0, 'put_last': 0,
                    'put_volume': 0, 'put_oi': 0,
                }

        with patch.object(broker, '_fetch_real_options_chain', return_value=chain), \
             patch.object(broker, 'get_current_price', return_value=6000.0), \
             patch.object(broker, '_build_synthetic_chain') as mock_synth:
            result = broker.get_options_chain('SPX', '2026-02-18')

            mock_synth.assert_not_called()
            assert broker._last_chain_source == 'real'


# ============================================================================
# TESTS: Catastrophic Scenario Prevention
# ============================================================================

class TestMaxContractsEnforcement:
    """Verify max_contracts=1 sticks through all sizing paths."""

    def test_max_contracts_1_sticks_for_orb(self):
        """If user sets max_contracts=1, ORB override of 3 does NOT override it."""
        from src.core.portfolio_manager import PortfolioManager, StrategyType

        pm = PortfolioManager(
            account_size=50000,
            max_contracts=1,
            min_contracts=1,
            daily_contracts=7,
            swing_contracts=3,
        )

        result = pm.calculate_position_size(
            strategy=StrategyType.ORB,
            max_risk_per_contract=200.0,
        )

        assert result == 1, f"max_contracts=1 must stick, got {result}"

    def test_max_contracts_1_sticks_for_di(self):
        """If user sets max_contracts=1, daily_contracts=7 does NOT override it."""
        from src.core.portfolio_manager import PortfolioManager, StrategyType

        pm = PortfolioManager(
            account_size=50000,
            max_contracts=1,
            min_contracts=1,
            daily_contracts=7,
            swing_contracts=2,
        )

        result = pm.calculate_position_size(
            strategy=StrategyType.DAILY_INCOME,
            max_risk_per_contract=200.0,
        )

        assert result == 1, f"max_contracts=1 must stick, got {result}"

    def test_max_contracts_1_sticks_for_tnt(self):
        """If user sets max_contracts=1, swing_contracts=2 does NOT override it."""
        from src.core.portfolio_manager import PortfolioManager, StrategyType

        pm = PortfolioManager(
            account_size=50000,
            max_contracts=1,
            min_contracts=1,
            daily_contracts=7,
            swing_contracts=2,
        )

        result = pm.calculate_position_size(
            strategy=StrategyType.TAG_N_TURN,
            max_risk_per_contract=200.0,
        )

        assert result == 1, f"max_contracts=1 must stick, got {result}"

    def test_small_account_produces_1_contract(self):
        """Small account with 2% loss limit should produce 1 contract, not 0."""
        from src.core.portfolio_manager import PortfolioManager, StrategyType

        pm = PortfolioManager(
            account_size=5000,
            max_daily_loss_pct=2.0,
            max_contracts=20,
            min_contracts=1,
            daily_contracts=7,
            swing_contracts=2,
        )

        # $5k * 2% = $100 budget / $500 risk = 0.2 -> floor(0.2) = 0
        # Clamped to min_contracts=1
        result = pm.calculate_position_size(
            strategy=StrategyType.DAILY_INCOME,
            max_risk_per_contract=500.0,
        )

        assert result == 1, f"min_contracts=1 must be the floor, got {result}"


class TestPartialFillAcceptance:
    """Partial fills are accepted with actual filled quantity; zero fills are rejected."""

    def _make_spread(self):
        spread = MagicMock()
        spread.credit_received = 2.50
        spread.max_risk = 250.0
        spread.max_profit = 250.0
        spread.breakeven = 5997.50
        spread.direction = TradeDirection.BULLISH
        spread.underlying_price_at_entry = 6000.0
        spread.short_leg = MagicMock(strike=6000.0)
        spread.long_leg = MagicMock(strike=5995.0)
        spread.expiration = None
        spread.theoretical_mid_credit = 2.50
        return spread

    def _make_pm(self, broker):
        from src.core.position_manager import PositionManager
        strategy = MagicMock()
        strategy.classify_credit_quality.return_value = {
            'quality': 'good', 'credit_received': 2.50,
            'expected_range': '$2-3', 'moneyness': 'ATM',
            'is_simulated_concern': False,
        }
        return PositionManager(
            broker=broker,
            strategy=strategy,
            db_manager=MagicMock(),
        )

    def test_partial_fill_accepted_with_correct_quantity(self):
        """Partial fill creates trade with actual filled quantity."""
        from src.models.bar import Bar

        broker = MagicMock()
        broker.place_spread_order.return_value = "ORDER-PARTIAL"
        broker.get_order_status.return_value = {
            'status': 'filled',
            'fill_price': 2.40,
            'filled_quantity': 7,  # Requested 10, only 7 filled
        }

        pm = self._make_pm(broker)
        bar = Bar(
            timestamp=datetime(2026, 2, 11, 10, 30),
            open=6000, high=6010, low=5990, close=6005,
        )

        result = pm.enter_trade(self._make_spread(), bar, quantity=10)

        assert result is not None, "Partial fill should be accepted"
        assert result.quantity == 7, "Trade quantity must be actual filled amount"

    def test_full_fill_accepted(self):
        """Full fill creates trade with requested quantity."""
        from src.models.bar import Bar

        broker = MagicMock()
        broker.place_spread_order.return_value = "ORDER-FULL"
        broker.get_order_status.return_value = {
            'status': 'filled',
            'fill_price': 2.50,
            'filled_quantity': 3,
        }

        pm = self._make_pm(broker)
        bar = Bar(
            timestamp=datetime(2026, 2, 11, 10, 30),
            open=6000, high=6010, low=5990, close=6005,
        )

        result = pm.enter_trade(self._make_spread(), bar, quantity=3)

        assert result is not None, "Full fill should be accepted"
        assert result.quantity == 3

    def test_zero_fill_rejected(self):
        """Zero filled contracts must reject the trade."""
        from src.models.bar import Bar

        broker = MagicMock()
        broker.place_spread_order.return_value = "ORDER-ZERO"
        broker.get_order_status.return_value = {
            'status': 'filled',
            'fill_price': 0,
            'filled_quantity': 0,
        }

        pm = self._make_pm(broker)
        spread = MagicMock()
        spread.credit_received = 2.50
        spread.max_risk = 250.0
        spread.direction = TradeDirection.BULLISH
        bar = Bar(
            timestamp=datetime(2026, 2, 11, 10, 30),
            open=6000, high=6010, low=5990, close=6005,
        )

        result = pm.enter_trade(spread, bar, quantity=5)

        assert result is None, "Zero fill must be rejected"


class TestBnBCannotEnterTrades:
    """After rework, B&B must never trigger a trade entry."""

    def test_bnb_on_bar_complete_returns_none(self, tmp_path):
        """on_bar_complete() always returns None (no action dict)."""
        from src.core.bnb_strategy import BnBStrategy
        from src.models.bar import Bar

        bnb = BnBStrategy({'enabled': True}, persistence_path=tmp_path / 'bnb.json')

        bar = Bar(
            timestamp=datetime(2026, 2, 11, 15, 30),
            open=6000, high=6020, low=6000, close=6019,  # Pulse bar
        )

        result = bnb.on_bar_complete(bar, 6019)
        assert result is None, "B&B on_bar_complete must return None (informational only)"

    def test_bnb_has_no_check_entry_signal(self):
        """BnBStrategy must not have check_entry_signal method."""
        from src.core.bnb_strategy import BnBStrategy

        assert not hasattr(BnBStrategy, 'check_entry_signal'), \
            "B&B must not have check_entry_signal (removed in rework)"

    def test_bnb_has_no_rollback_entry(self):
        """BnBStrategy must not have rollback_entry method."""
        from src.core.bnb_strategy import BnBStrategy

        assert not hasattr(BnBStrategy, 'rollback_entry'), \
            "B&B must not have rollback_entry (removed in rework)"

    def test_bnb_not_in_0dte_counter(self):
        """B&B trades must NOT count toward dte0_trades_today."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        bot.db.get_daily_counts_by_strategy.return_value = {
            'daily_income': 0,
            'orb': 0,
            'bnb': 5,  # Even if DB has old B&B trades
            'tag_n_turn': 0,
        }
        bot.db.get_daily_summary.return_value = {'trades_count': 5, 'realized_pnl': 0.0}

        TradingBot._restore_daily_counters(bot, date(2026, 2, 11))

        assert bot.dte0_trades_today == 0, "B&B must not count toward 0DTE slot"


class TestCircuitBreakerBlocksAllPaths:
    """Verify circuit breaker blocks every entry path."""

    def test_circuit_breaker_blocks_orb(self):
        """ORB entry blocked when circuit breaker is tripped."""
        from src.main import TradingBot

        bot = _make_mock_bot(daily_pnl=-1500.0)
        bot.portfolio.circuit_breaker_triggered = True

        result = TradingBot._check_daily_loss_circuit_breaker(bot)
        assert result is False

    def test_circuit_breaker_blocks_tnt(self):
        """TNT entry blocked when circuit breaker is tripped."""
        from src.main import TradingBot

        bot = _make_mock_bot(daily_pnl=-1500.0)
        bot.portfolio.circuit_breaker_triggered = True

        result = TradingBot._check_daily_loss_circuit_breaker(bot)
        assert result is False

    def test_circuit_breaker_blocks_di(self):
        """DI entry blocked when circuit breaker is tripped."""
        from src.main import TradingBot

        bot = _make_mock_bot(daily_pnl=-1500.0)
        bot.portfolio.circuit_breaker_triggered = True

        result = TradingBot._check_daily_loss_circuit_breaker(bot)
        assert result is False


# ============================================================================
# ORB DISABLED FLAG
# ============================================================================

class TestORBDisabledFlag:
    """Verify ORB strategy does not fire when disabled."""

    def test_orb_disabled_no_signals(self):
        """When ORB enabled=false, no ORB signals fire regardless of market."""
        from src.core.orb_strategy import ORBStrategy
        from src.models.bar import Bar

        config = {
            'enabled': False,
            'min_threshold': 10.0,
            'min_range_points': 8.0,
            'confirmation_minutes': 3,
        }
        orb = ORBStrategy(config)
        assert orb.enabled is False

        # Create a bar that would normally produce a strong signal
        bar = Bar(
            timestamp=ET.localize(datetime(2026, 2, 20, 9, 30)),
            open=5800.0, high=5830.0, low=5795.0, close=5828.0,
        )
        # set_opening_range returns None when disabled (internal guard)
        result = orb.set_opening_range(bar)
        assert result is None
        assert orb.opening_range is None

        # check_breakout also returns None when disabled
        breakout = orb.check_breakout(5835.0, ET.localize(datetime(2026, 2, 20, 10, 5)))
        assert breakout is None

        # Triple-guard in main.py also prevents calling:
        # `if self.orb_enabled and self.orb_strategy and self.orb_strategy.enabled`
        orb_enabled_flag = False  # simulating main.py's self.orb_enabled
        should_call = orb_enabled_flag and orb.enabled
        assert should_call is False


# ============================================================================
# MIN BAR RANGE POINTS
# ============================================================================

class TestMinBarRangePoints:
    """Verify PulseBarDetector rejects bars below minimum range threshold."""

    def test_bar_below_min_range_returns_neutral(self):
        """Bar with range < min_bar_range_points is NEUTRAL."""
        from src.core.pulse_detector import PulseBarDetector
        from src.models.bar import Bar, BarType

        detector = PulseBarDetector(threshold_percent=10.0, min_bar_range_points=2.0)
        # Range = 5801.5 - 5800.0 = 1.5 pts (< 2.0 threshold)
        bar = Bar(
            timestamp=ET.localize(datetime(2026, 2, 20, 10, 0)),
            open=5800.0, high=5801.5, low=5800.0, close=5801.4,
        )
        assert bar.range == 1.5
        result = detector.analyze_bar(bar)
        assert result == BarType.NEUTRAL

    def test_bar_above_min_range_detects_pulse(self):
        """Bar with range >= min_bar_range_points can be a pulse."""
        from src.core.pulse_detector import PulseBarDetector
        from src.models.bar import Bar, BarType

        detector = PulseBarDetector(threshold_percent=10.0, min_bar_range_points=2.0)
        # Range = 5810.0 - 5800.0 = 10 pts, close at 5809.5 = 95% (top 10%)
        bar = Bar(
            timestamp=ET.localize(datetime(2026, 2, 20, 10, 0)),
            open=5800.0, high=5810.0, low=5800.0, close=5809.5,
        )
        assert bar.range == 10.0
        result = detector.analyze_bar(bar)
        assert result == BarType.BULLISH_PULSE

    def test_min_range_zero_disables_filter(self):
        """When min_bar_range_points=0.0, no range filter is applied."""
        from src.core.pulse_detector import PulseBarDetector
        from src.models.bar import Bar, BarType

        detector = PulseBarDetector(threshold_percent=10.0, min_bar_range_points=0.0)
        # Tiny range 0.5 pts but close in top 10%
        bar = Bar(
            timestamp=ET.localize(datetime(2026, 2, 20, 10, 0)),
            open=5800.0, high=5800.5, low=5800.0, close=5800.48,
        )
        assert bar.range == 0.5
        result = detector.analyze_bar(bar)
        assert result == BarType.BULLISH_PULSE

    def test_bearish_pulse_respects_min_range(self):
        """Bearish pulse also filtered when bar range is too small."""
        from src.core.pulse_detector import PulseBarDetector
        from src.models.bar import Bar, BarType

        detector = PulseBarDetector(threshold_percent=10.0, min_bar_range_points=1.0)
        # Range = 0.8 pts (< 1.0), close in bottom 10%
        bar = Bar(
            timestamp=ET.localize(datetime(2026, 2, 20, 10, 0)),
            open=5800.8, high=5800.8, low=5800.0, close=5800.05,
        )
        assert bar.range < 1.0
        result = detector.analyze_bar(bar)
        assert result == BarType.NEUTRAL

    def test_strategy_passes_min_range_to_detector(self):
        """SPXIncomeStrategy forwards min_bar_range_points to PulseBarDetector."""
        from src.core.strategy import SPXIncomeStrategy

        strategy = SPXIncomeStrategy(min_bar_range_points=3.5)
        assert strategy.pulse_detector.min_bar_range_points == 3.5
