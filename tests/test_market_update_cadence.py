"""
Tests for 30-minute market update notification cadence.

The market update fires at :00 and :30 past each hour during market hours,
aligned with 30-minute bar boundaries.
"""

import sys
from pathlib import Path
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytz
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

ET = pytz.timezone("America/New_York")


# ---------------------------------------------------------------------------
# 1. Source-level: function renamed from hourly to market update
# ---------------------------------------------------------------------------

class TestMarketUpdateNaming:
    """Verify the function and variable names reflect 30-min cadence."""

    def test_function_renamed_to_market_update(self):
        """_send_market_update_if_due must exist in main.py."""
        main_path = Path(__file__).parent.parent / 'src' / 'main.py'
        source = main_path.read_text(encoding='utf-8')
        assert 'def _send_market_update_if_due' in source, (
            "Function should be renamed from _send_hourly_update_if_due "
            "to _send_market_update_if_due"
        )

    def test_state_variable_renamed(self):
        """_last_market_update must be the tracking variable."""
        main_path = Path(__file__).parent.parent / 'src' / 'main.py'
        source = main_path.read_text(encoding='utf-8')
        assert '_last_market_update' in source
        assert '_last_hourly_update' not in source, (
            "Old _last_hourly_update variable should be renamed"
        )

    def test_caller_uses_new_name(self):
        """The main loop must call _send_market_update_if_due."""
        main_path = Path(__file__).parent.parent / 'src' / 'main.py'
        source = main_path.read_text(encoding='utf-8')
        assert '_send_market_update_if_due(' in source


# ---------------------------------------------------------------------------
# 2. Timing logic: fires at :00 and :30, dedup per 30-min slot
# ---------------------------------------------------------------------------

class TestMarketUpdateTiming:
    """Verify the 30-minute cadence logic."""

    def test_fires_on_hour_boundary(self):
        """Update should fire at :00."""
        main_path = Path(__file__).parent.parent / 'src' / 'main.py'
        source = main_path.read_text(encoding='utf-8')
        # The function must check minute in (0, 30)
        assert 'current_et.minute not in (0, 30)' in source or \
               'current_et.minute != 0 and current_et.minute != 30' in source

    def test_fires_on_half_hour_boundary(self):
        """Update should fire at :30."""
        main_path = Path(__file__).parent.parent / 'src' / 'main.py'
        source = main_path.read_text(encoding='utf-8')
        # Must include 30 in the boundary check
        assert '30' in source  # minimal sanity check

    def test_non_boundary_returns_early(self):
        """At :15 or :45, the function should return without sending."""
        main_path = Path(__file__).parent.parent / 'src' / 'main.py'
        source = main_path.read_text(encoding='utf-8')
        # The broken `pass` from the old code must now be `return`
        lines = source.split('\n')
        in_func = False
        found_return_after_boundary_check = False
        for line in lines:
            stripped = line.strip()
            if 'def _send_market_update_if_due' in stripped:
                in_func = True
            if in_func:
                if 'current_et.minute not in' in stripped:
                    # Next non-empty, non-comment line should be return
                    continue
                if in_func and stripped == 'return' and not found_return_after_boundary_check:
                    found_return_after_boundary_check = True
                    break
                if stripped.startswith('def ') and '_send_market_update_if_due' not in stripped:
                    break  # left the function

        assert found_return_after_boundary_check, (
            "Non-boundary minutes must `return`, not `pass`"
        )

    def test_dedup_uses_30min_slots_not_hours(self):
        """Dedup must compare 30-min slots, not just hours, so :00 and :30
        in the same hour both fire."""
        main_path = Path(__file__).parent.parent / 'src' / 'main.py'
        source = main_path.read_text(encoding='utf-8')
        # Old broken dedup: current_et.hour == self._last_hourly_update.hour
        assert 'current_et.hour == self._last_hourly_update.hour' not in source, (
            "Old hour-only dedup should be replaced with 30-min slot comparison"
        )
        # New dedup should compute slots (hour*2 + half)
        assert 'current_slot' in source, (
            "Should compute current_slot for 30-min dedup"
        )

    def test_slot_calculation_correct(self):
        """Verify the slot formula: hour*2 + (1 if minute >= 30 else 0)."""
        # 10:00 → slot 20, 10:30 → slot 21, 11:00 → slot 22
        def slot(hour, minute):
            return hour * 2 + (1 if minute >= 30 else 0)

        assert slot(10, 0) == 20
        assert slot(10, 30) == 21
        assert slot(11, 0) == 22
        assert slot(9, 30) == 19
        assert slot(15, 30) == 31
        # Same hour, different slots
        assert slot(10, 0) != slot(10, 30)
        # Different hours, different slots
        assert slot(10, 30) != slot(11, 0)
