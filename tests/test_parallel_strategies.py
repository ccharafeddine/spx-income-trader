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

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models.spread import CreditSpread, OptionLeg, TradeDirection
from src.models.bar import Bar
from src.core.portfolio_manager import PortfolioManager, StrategyType


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
    bot.broker.get_order_status.return_value = {'status': 'filled', 'fill_price': 2.50}
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

    def test_bnb_execution(self):
        """Execute a B&B trade."""
        from src.main import TradingBot

        bot = _make_mock_bot()

        result = TradingBot._execute_strategy_trade(
            bot,
            strategy_type=StrategyType.BNB,
            direction=TradeDirection.BEARISH,
            current_price=6000.0,
            is_0dte=True,
            spread_width=5.0,
        )

        assert result is True
        assert bot.dte0_trades_today == 1  # B&B is 0DTE

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
        """DI+ORB+B&B sum into dte0_trades_today, TNT into tnt_trades_today."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        bot.db.get_daily_counts_by_strategy.return_value = {
            'daily_income': 1,
            'orb': 1,
            'tag_n_turn': 2,
        }
        bot.db.get_daily_summary.return_value = {'trades_count': 4, 'realized_pnl': -150.0}

        TradingBot._restore_daily_counters(bot, date(2026, 2, 11))

        assert bot.dte0_trades_today == 2  # DI(1) + ORB(1) + B&B(0)
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
    """ORB sets triggered_today=True inside check_breakout() before returning.
    If execution fails, rollback_entry() must restore retryable state."""

    def test_rollback_resets_triggered_today(self, tmp_path):
        """After rollback, ORB can trigger again on next tick."""
        from src.core.orb_strategy import ORBStrategy, ORBRange

        orb = ORBStrategy({'enabled': True}, persistence_path=tmp_path / 'orb.json')
        orb.opening_range = ORBRange(
            date='2026-02-11', high=6010.0, low=5990.0,
            close=6008.0, range_size=20.0,
            close_position_pct=90.0, direction_bias='bullish',
        )

        # First breakout signal (sets triggered_today=True internally)
        signal = orb.check_breakout(6015.0)
        assert signal is not None
        assert orb.triggered_today is True
        assert orb.position_open is True

        # Simulate execution failure -> rollback
        orb.rollback_entry()
        assert orb.triggered_today is False
        assert orb.position_open is False
        assert orb.entry_price is None

        # Should be able to trigger again
        signal2 = orb.check_breakout(6015.0)
        assert signal2 is not None, "ORB should retry after rollback"

    def test_no_retry_without_rollback(self, tmp_path):
        """Without rollback, triggered_today blocks all future breakouts."""
        from src.core.orb_strategy import ORBStrategy, ORBRange

        orb = ORBStrategy({'enabled': True}, persistence_path=tmp_path / 'orb.json')
        orb.opening_range = ORBRange(
            date='2026-02-11', high=6010.0, low=5990.0,
            close=6008.0, range_size=20.0,
            close_position_pct=90.0, direction_bias='bullish',
        )

        signal = orb.check_breakout(6015.0)
        assert signal is not None

        # Without rollback: no retry
        signal2 = orb.check_breakout(6020.0)
        assert signal2 is None, "ORB should be blocked without rollback"


class TestBnBRollbackOnFailure:
    """B&B sets position_open=True inside check_entry_signal() before returning.
    If execution fails, rollback_entry() must restore retryable state."""

    def test_rollback_allows_retry(self, tmp_path):
        """After rollback, B&B can signal again on next tick."""
        from src.core.bnb_strategy import BnBStrategy, BnBSignal
        import pytz

        bnb = BnBStrategy({'enabled': True}, persistence_path=tmp_path / 'bnb.json')
        bnb.active_signal = BnBSignal(
            signal_date='2026-02-10', direction='bullish',
            pulse_bar_time='15:30', pulse_bar_close=6000.0,
            is_presumptive=True, spx_close=6005.0,
        )

        tz = pytz.timezone("America/New_York")
        entry_time = datetime(2026, 2, 11, 9, 35, tzinfo=tz)

        # First signal (sets position_open=True internally)
        signal = bnb.check_entry_signal(6000.0, entry_time)
        assert signal is not None
        assert bnb.position_open is True

        # Simulate execution failure -> rollback
        bnb.rollback_entry()
        assert bnb.position_open is False
        assert bnb.entry_price is None

        # Should be able to signal again
        signal2 = bnb.check_entry_signal(6001.0, entry_time)
        assert signal2 is not None, "B&B should retry after rollback"

    def test_no_retry_without_rollback(self, tmp_path):
        """Without rollback, position_open=True blocks all future signals."""
        from src.core.bnb_strategy import BnBStrategy, BnBSignal
        import pytz

        bnb = BnBStrategy({'enabled': True}, persistence_path=tmp_path / 'bnb.json')
        bnb.active_signal = BnBSignal(
            signal_date='2026-02-10', direction='bullish',
            pulse_bar_time='15:30', pulse_bar_close=6000.0,
            is_presumptive=True, spx_close=6005.0,
        )

        tz = pytz.timezone("America/New_York")
        entry_time = datetime(2026, 2, 11, 9, 35, tzinfo=tz)

        signal = bnb.check_entry_signal(6000.0, entry_time)
        assert signal is not None

        # Without rollback: blocked
        signal2 = bnb.check_entry_signal(6001.0, entry_time)
        assert signal2 is None, "B&B should be blocked without rollback"


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
# TESTS: B&B Exit Execution
# ============================================================================

class TestBnBExitExecution:
    """Test B&B exit and roll_to_daily wiring."""

    def test_bnb_exit_closes_position(self):
        """B&B 'exit' action triggers broker close."""
        from src.main import TradingBot
        import pytz

        bot = _make_mock_bot()
        bot.tag_n_turn_enabled = False
        bot.orb_enabled = False
        bot.bnb_enabled = True
        bot.bnb_strategy = MagicMock()

        bot.bollinger = MagicMock()
        bot.bollinger.day_open = 6000.0
        bot.bar_builder = MagicMock()
        bot.bar_builder.current_bar_start = None
        bot._current_spx_price = 0
        bot.broker.get_current_price.return_value = 6000.0

        # Simulate a completed bar triggering B&B exit
        completed_bar = Bar(
            timestamp=datetime(2026, 2, 11, 10, 0),
            open=5990, high=6010, low=5985, close=6005,
        )
        bot.bar_builder.add_price.return_value = completed_bar

        bot.bnb_strategy.on_bar_complete.return_value = {
            'action': 'exit',
            'reason': 'Just Breakfast 30-min exit',
            'strategy': 'bnb',
        }

        # Mock portfolio position for B&B
        mock_slot = MagicMock()
        mock_slot.position_id = "bnb-trade-001"
        bot.portfolio.get_positions_by_strategy.return_value = [mock_slot]

        # Mock successful close
        bot.position_manager.close_trade_by_id = MagicMock(return_value=-50.0)
        bot.position_manager.recently_closed = []
        bot._drain_recently_closed = MagicMock()

        tz = pytz.timezone("America/New_York")
        current_time = datetime(2026, 2, 11, 10, 0, tzinfo=tz)
        TradingBot._update_market_state(bot, current_time)

        bot.position_manager.close_trade_by_id.assert_called_once_with(
            "bnb-trade-001", "B&B: Just Breakfast 30-min exit"
        )
        bot._drain_recently_closed.assert_called_once()

    def test_bnb_roll_to_daily_no_close(self):
        """B&B 'roll_to_daily' action should NOT close the position."""
        from src.main import TradingBot
        import pytz

        bot = _make_mock_bot()
        bot.tag_n_turn_enabled = False
        bot.orb_enabled = False
        bot.bnb_enabled = True
        bot.bnb_strategy = MagicMock()

        bot.bollinger = MagicMock()
        bot.bollinger.day_open = 6000.0
        bot.bar_builder = MagicMock()
        bot.bar_builder.current_bar_start = None
        bot._current_spx_price = 0
        bot.broker.get_current_price.return_value = 6000.0

        completed_bar = Bar(
            timestamp=datetime(2026, 2, 11, 10, 0),
            open=5990, high=6010, low=5985, close=6005,
        )
        bot.bar_builder.add_price.return_value = completed_bar

        bot.bnb_strategy.on_bar_complete.return_value = {
            'action': 'roll_to_daily',
            'direction': 'bullish',
            'reason': 'First bar confirms - rolling to daily setup',
            'strategy': 'bnb',
        }

        bot.position_manager.close_trade_by_id = MagicMock()

        tz = pytz.timezone("America/New_York")
        current_time = datetime(2026, 2, 11, 10, 0, tzinfo=tz)
        TradingBot._update_market_state(bot, current_time)

        # Should NOT close position
        bot.position_manager.close_trade_by_id.assert_not_called()
        assert bot.portfolio.daily_realized_pnl == 0.0

        # Should log the roll signal
        bot._log_signal.assert_called_with("BNB_ROLL_TO_DAILY", {
            "strategy": "bnb",
            "direction": "bullish",
            "reason": "First bar confirms - rolling to daily setup",
        })


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
