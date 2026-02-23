"""
Tests that the main loop does not self-stop during market hours
unless a risk limit is explicitly triggered.

Regression test for: PositionManager.get_open_positions() AttributeError
causing consecutive error accumulation and bot shutdown.
"""

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestPositionManagerMethodCalls:
    """Verify main.py only calls methods that actually exist on PositionManager."""

    def test_no_get_open_positions_on_position_manager(self):
        """main.py must never call position_manager.get_open_positions().

        PositionManager exposes get_open_trades(), not get_open_positions().
        Calling the wrong name causes an AttributeError on every loop
        iteration, which accumulates to 5 consecutive errors and triggers
        an auto-shutdown during market hours.
        """
        main_path = Path(__file__).parent.parent / 'src' / 'main.py'
        source = main_path.read_text(encoding='utf-8')

        # Parse the AST and look for position_manager.get_open_positions calls
        tree = ast.parse(source, filename=str(main_path))
        bad_calls = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                # Match: self.position_manager.get_open_positions()
                if (isinstance(func, ast.Attribute)
                        and func.attr == 'get_open_positions'
                        and isinstance(func.value, ast.Attribute)
                        and func.value.attr == 'position_manager'):
                    bad_calls.append(node.lineno)

        assert bad_calls == [], (
            f"main.py calls position_manager.get_open_positions() on lines {bad_calls}. "
            f"PositionManager only has get_open_trades(). This will cause "
            f"AttributeError -> 5 consecutive errors -> bot auto-shutdown."
        )

    def test_get_open_trades_exists_on_position_manager(self):
        """PositionManager must expose get_open_trades()."""
        from src.core.position_manager import PositionManager
        assert hasattr(PositionManager, 'get_open_trades'), (
            "PositionManager.get_open_trades() is missing - main.py depends on it"
        )

    def test_position_manager_method_calls_in_main(self):
        """Every position_manager method *call* in main.py must exist on PositionManager."""
        from src.core.position_manager import PositionManager

        main_path = Path(__file__).parent.parent / 'src' / 'main.py'
        source = main_path.read_text(encoding='utf-8')
        tree = ast.parse(source, filename=str(main_path))

        pm_methods_called = set()
        for node in ast.walk(tree):
            # Only check Call nodes (method invocations), not plain attribute access
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                func = node.func
                if (isinstance(func.value, ast.Attribute)
                        and func.value.attr == 'position_manager'):
                    pm_methods_called.add(func.attr)

        pm_methods = {name for name in dir(PositionManager) if callable(getattr(PositionManager, name, None))}
        missing = pm_methods_called - pm_methods
        assert missing == set(), (
            f"main.py calls position_manager methods that don't exist: {missing}"
        )


class TestConsecutiveErrorShutdown:
    """Verify the consecutive error handler doesn't shut down for recoverable errors."""

    def test_max_consecutive_errors_is_reasonable(self):
        """The error threshold should be at least 5 to tolerate transient issues."""
        main_path = Path(__file__).parent.parent / 'src' / 'main.py'
        source = main_path.read_text(encoding='utf-8')
        assert 'max_consecutive_errors = 5' in source

    def test_error_counter_resets_on_success(self):
        """consecutive_errors must be reset to 0 after a successful loop iteration."""
        main_path = Path(__file__).parent.parent / 'src' / 'main.py'
        source = main_path.read_text(encoding='utf-8')
        assert 'consecutive_errors = 0' in source
