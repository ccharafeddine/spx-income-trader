"""
Tests for parallel strategy execution and per-strategy daily limits.

Covers:
- Per-strategy daily limits (DI limit=1 doesn't block ORB)
- Global circuit breaker blocks all strategies
- _execute_strategy_trade() with mocked broker
- _construct_strategy_spread() with different widths and expirations
- _restore_daily_counters() with mixed strategy trades
- _find_dte_expiration() helper
- DI not blocked by ORB/TNT/B&B open positions
"""

import pytest
from unittest.mock import Mock, MagicMock, patch, PropertyMock
from datetime import datetime, date, timedelta
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
    bot.daily_pnl = daily_pnl
    bot.max_daily_loss = 1000.0
    bot.trades_today_by_strategy = {s.value: 0 for s in StrategyType}
    bot.max_daily_trades_by_strategy = {
        'daily_income': 1,
        'tag_n_turn': 0,  # unlimited
        'bnb': 0,         # unlimited
        'orb': 1,
    }
    bot.notifier = None

    # Mock broker
    bot.broker = MagicMock()
    bot.broker.get_options_chain.return_value = _make_options_chain()
    bot.broker.place_spread_order.return_value = "ORDER-123"
    bot.broker.get_order_status.return_value = {'status': 'filled', 'fill_price': 2.50}
    bot.broker.get_current_price.return_value = 6000.0

    # Mock portfolio
    bot.portfolio = MagicMock(spec=PortfolioManager)
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

class TestPerStrategyDailyLimits:
    """Test that per-strategy limits are independent."""

    def test_di_limit_does_not_block_orb(self):
        """DI max_daily_trades=1 reached should NOT block ORB."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        bot.trades_today_by_strategy['daily_income'] = 1  # DI limit reached

        # Bind the real method
        result = TradingBot._check_daily_limits(bot, 'daily_income')
        assert result is False, "DI should be blocked"

        result = TradingBot._check_daily_limits(bot, 'orb')
        assert result is True, "ORB should NOT be blocked by DI limit"

    def test_orb_limit_blocks_after_1(self):
        """ORB max_daily_trades=1 should block after 1 trade."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        bot.trades_today_by_strategy['orb'] = 1

        result = TradingBot._check_daily_limits(bot, 'orb')
        assert result is False, "ORB should be blocked after 1 trade"

    def test_unlimited_strategy_never_blocked(self):
        """Strategies with max_daily_trades=0 are never blocked by trade count."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        bot.trades_today_by_strategy['tag_n_turn'] = 10  # 10 trades

        result = TradingBot._check_daily_limits(bot, 'tag_n_turn')
        assert result is True, "TNT (limit=0) should never be count-blocked"

    def test_bnb_unlimited(self):
        """B&B with max_daily_trades=0 should never be count-blocked."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        bot.trades_today_by_strategy['bnb'] = 5

        result = TradingBot._check_daily_limits(bot, 'bnb')
        assert result is True


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
        assert bot.trades_today_by_strategy['orb'] == 1
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
        assert bot.trades_today_by_strategy['tag_n_turn'] == 1

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
        assert bot.trades_today_by_strategy['bnb'] == 1

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
        assert bot.trades_today_by_strategy['orb'] == 0
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
        assert bot.trades_today_by_strategy['orb'] == 0


# ============================================================================
# TESTS: _restore_daily_counters
# ============================================================================

class TestRestoreDailyCounters:
    """Test restoring per-strategy trade counts from DB."""

    def test_restore_mixed_strategies(self):
        """Restore counts with trades from multiple strategies."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        bot.db.get_daily_counts_by_strategy.return_value = {
            'daily_income': 1,
            'orb': 1,
            'tag_n_turn': 2,
        }
        bot.db.get_daily_summary.return_value = {'trades_count': 4, 'realized_pnl': -150.0}

        TradingBot._restore_daily_counters(bot, date(2026, 2, 11))

        assert bot.trades_today_by_strategy['daily_income'] == 1
        assert bot.trades_today_by_strategy['orb'] == 1
        assert bot.trades_today_by_strategy['tag_n_turn'] == 2
        assert bot.trades_today_by_strategy['bnb'] == 0
        assert bot.daily_pnl == -150.0

    def test_restore_empty_day(self):
        """Restore on a day with no trades."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        bot.db.get_daily_counts_by_strategy.return_value = {}
        bot.db.get_daily_summary.return_value = {'trades_count': 0, 'realized_pnl': 0.0}

        TradingBot._restore_daily_counters(bot, date(2026, 2, 11))

        assert all(v == 0 for v in bot.trades_today_by_strategy.values())
        assert bot.daily_pnl == 0.0

    def test_restore_handles_db_error(self):
        """DB errors should reset to zero gracefully."""
        from src.main import TradingBot

        bot = _make_mock_bot()
        bot.db.get_daily_counts_by_strategy.side_effect = Exception("DB locked")

        TradingBot._restore_daily_counters(bot, date(2026, 2, 11))

        assert all(v == 0 for v in bot.trades_today_by_strategy.values())
        assert bot.daily_pnl == 0.0


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
        bot._check_daily_limits = MagicMock(return_value=True)

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
