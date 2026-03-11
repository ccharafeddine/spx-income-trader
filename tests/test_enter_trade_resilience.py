"""
Tests that enter_trade() and the main loop survive exceptions in save_trade()
without silently killing the bot thread.

Covers:
- save_trade() raising Exception -> caught, returns trade (live on broker), sets _db_write_failed
- save_trade() raising SystemExit -> caught by BaseException handler, re-raised
- No non-ASCII characters in position_manager.py log messages (Windows cp1252 safety)
- Log handlers use utf-8 encoding
- Main loop catches BaseException when positions are open
"""

import ast
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime

import pytest
import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

ET = pytz.timezone('America/New_York')


def _make_pm_and_spread():
    """Create a PositionManager and CreditSpread for testing."""
    from src.core.position_manager import PositionManager
    from src.models.spread import CreditSpread, OptionLeg, TradeDirection

    broker = MagicMock()
    broker.place_spread_order.return_value = "order-resilience-1"
    broker.get_order_status.return_value = {'status': 'filled', 'fill_price': 2.50}

    strategy = MagicMock()
    strategy.classify_credit_quality.return_value = {
        'quality': 'GOOD', 'credit_received': 2.50, 'moneyness': 'ATM',
        'expected_range': '$2.40-$2.60', 'expected_min': 2.40,
        'expected_max': 2.60, 'pct_of_expected_min': 104.2,
        'is_simulated_concern': False,
    }

    db = MagicMock()

    pm = PositionManager(broker=broker, strategy=strategy, db_manager=db)

    now = ET.localize(datetime(2026, 3, 3, 10, 0))
    spread = CreditSpread(
        direction=TradeDirection.BEARISH,
        short_leg=OptionLeg(strike=6730, option_type='call', action='sell', price=5.00),
        long_leg=OptionLeg(strike=6735, option_type='call', action='buy', price=3.00),
        credit_received=2.00,
        entry_time=now,
        expiration=now.replace(hour=16, minute=0),
        underlying_price_at_entry=6732.0,
    )

    bar = MagicMock()
    bar.timestamp = now
    bar.open = 6740.0
    bar.high = 6750.0
    bar.low = 6720.0
    bar.close = 6732.0
    bar.range = 30.0
    bar.close_percentage_in_range.return_value = 40.0

    return pm, spread, bar, db


class TestSaveTradeExceptionHandling:
    """Verify enter_trade() survives save_trade() failures."""

    def test_save_trade_exception_returns_trade_and_halts(self):
        """When save_trade() raises, trade is returned (it's live on broker)
        and _db_write_failed is set to block future entries."""
        pm, spread, bar, db = _make_pm_and_spread()
        db.save_trade.side_effect = Exception("database is locked")

        with patch('time.sleep'):
            result = pm.enter_trade(spread, bar, quantity=1)

        # Trade is returned because it's already live on broker
        assert result is not None
        # Future trading is halted
        assert pm._db_write_failed is True

    def test_save_trade_exception_keeps_trade_in_open_trades(self):
        """Even when save_trade() fails, the trade is still in open_trades
        so it can be monitored (it was already appended at line 208)."""
        pm, spread, bar, db = _make_pm_and_spread()
        db.save_trade.side_effect = Exception("database is locked")

        with patch('time.sleep'):
            pm.enter_trade(spread, bar, quantity=1)

        # Trade was appended to open_trades before save_trade was called
        assert len(pm.open_trades) == 1

    def test_save_trade_system_exit_is_reraised(self):
        """When save_trade() raises SystemExit (BaseException), the new
        BaseException handler logs it and re-raises so the caller can handle
        cleanup. Previously this killed the bot thread silently."""
        pm, spread, bar, db = _make_pm_and_spread()
        db.save_trade.side_effect = SystemExit(1)

        with patch('time.sleep'), pytest.raises(SystemExit):
            pm.enter_trade(spread, bar, quantity=1)

    def test_save_trade_keyboard_interrupt_is_reraised(self):
        """KeyboardInterrupt from save_trade() must propagate for clean shutdown."""
        pm, spread, bar, db = _make_pm_and_spread()
        db.save_trade.side_effect = KeyboardInterrupt()

        with patch('time.sleep'), pytest.raises(KeyboardInterrupt):
            pm.enter_trade(spread, bar, quantity=1)


class TestNoUnicodeInLogMessages:
    """Verify position_manager.py has no non-ASCII characters in logger calls.

    On Windows with cp1252 encoding (PyInstaller windowed mode), non-ASCII
    characters like checkmarks cause UnicodeEncodeError in log handlers.
    This crashed the bot on 2026-03-03.
    """

    def test_no_non_ascii_in_logger_calls(self):
        """Scan position_manager.py for non-ASCII chars in logger.* calls."""
        pm_path = Path(__file__).parent.parent / 'src' / 'core' / 'position_manager.py'
        source = pm_path.read_text(encoding='utf-8')
        tree = ast.parse(source, filename=str(pm_path))

        violations = []
        for node in ast.walk(tree):
            # Find logger.info(...), logger.error(...), etc.
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == 'logger'):
                # Check all string arguments for non-ASCII
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        for i, ch in enumerate(arg.value):
                            if ord(ch) > 127:
                                violations.append(
                                    f"Line {node.lineno}: logger.{node.func.attr}() "
                                    f"contains non-ASCII char U+{ord(ch):04X} '{ch}'"
                                )
                    # Also check JoinedStr (f-strings)
                    if isinstance(arg, ast.JoinedStr):
                        for val in arg.values:
                            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                                for ch in val.value:
                                    if ord(ch) > 127:
                                        violations.append(
                                            f"Line {node.lineno}: logger.{node.func.attr}() "
                                            f"f-string contains non-ASCII char U+{ord(ch):04X} '{ch}'"
                                        )

        assert violations == [], (
            "Non-ASCII characters in logger calls will crash PyInstaller "
            "windowed builds on Windows (cp1252 encoding):\n"
            + "\n".join(violations)
        )


class TestLogHandlerEncoding:
    """Verify all RotatingFileHandler instances use utf-8 encoding."""

    def test_setup_logging_uses_utf8(self):
        """All RotatingFileHandler calls in setup_logging must specify encoding='utf-8'."""
        log_path = Path(__file__).parent.parent / 'src' / 'utils' / 'logging.py'
        source = log_path.read_text(encoding='utf-8')
        tree = ast.parse(source, filename=str(log_path))

        violations = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == 'RotatingFileHandler'):
                has_encoding = False
                for kw in node.keywords:
                    if kw.arg == 'encoding':
                        if isinstance(kw.value, ast.Constant) and kw.value.value == 'utf-8':
                            has_encoding = True
                if not has_encoding:
                    violations.append(f"Line {node.lineno}: RotatingFileHandler missing encoding='utf-8'")

        assert violations == [], (
            "RotatingFileHandler without encoding='utf-8' will crash on "
            "Windows when non-ASCII characters appear in log messages:\n"
            + "\n".join(violations)
        )


class TestMainLoopBaseExceptionHandling:
    """Verify the main loop catches BaseException when positions are open."""

    def test_main_loop_has_base_exception_handler(self):
        """_run_main_loop must catch BaseException to prevent silent thread death."""
        main_path = Path(__file__).parent.parent / 'src' / 'main.py'
        source = main_path.read_text(encoding='utf-8')

        # Check that 'except BaseException' appears in the file
        assert 'except BaseException' in source, (
            "src/main.py is missing 'except BaseException' handler. "
            "SystemExit can bypass 'except Exception' and silently kill "
            "the bot thread while positions are open."
        )

    def test_enter_trade_has_base_exception_handler(self):
        """enter_trade must catch BaseException and re-raise for visibility."""
        pm_path = Path(__file__).parent.parent / 'src' / 'core' / 'position_manager.py'
        source = pm_path.read_text(encoding='utf-8')

        assert 'except BaseException' in source, (
            "position_manager.py is missing 'except BaseException' handler. "
            "Unhandled BaseException in enter_trade() silently kills the bot thread."
        )

    def test_run_bot_has_base_exception_handler(self):
        """_run_bot must catch BaseException for post-mortem logging."""
        desktop_path = Path(__file__).parent.parent / 'app_desktop.py'
        source = desktop_path.read_text(encoding='utf-8')

        assert 'except BaseException' in source, (
            "app_desktop.py is missing 'except BaseException' handler in _run_bot(). "
            "Unhandled BaseException causes silent 'Bot thread exited' with no error logged."
        )


class TestDevnullEncoding:
    """Verify sys.stdout/stderr devnull redirect uses utf-8 encoding."""

    def test_devnull_redirect_uses_utf8(self):
        """app_desktop.py devnull redirect must use encoding='utf-8'."""
        desktop_path = Path(__file__).parent.parent / 'app_desktop.py'
        source = desktop_path.read_text(encoding='utf-8')

        # Find the devnull redirect lines
        for line in source.splitlines():
            if "open(os.devnull, 'w')" in line and 'sys.std' in line:
                assert "encoding='utf-8'" in line, (
                    f"devnull redirect missing encoding='utf-8': {line.strip()}\n"
                    "Without utf-8, non-ASCII log messages cause UnicodeEncodeError "
                    "in the StreamHandler that writes to sys.stdout."
                )
