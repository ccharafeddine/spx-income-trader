"""
Tests for Enhanced Market Context Metadata.

Tests cover:
- SMA distance calculation (points and percentage)
- Prior-range classification (above, below, within)
- Entry time bucket classification (early, mid, late)
- SMA provider daily cache
- Database migration adds all new columns
- Integration: enter_trade populates metadata in entry_context
"""

import sqlite3
import sys
from datetime import datetime, date, time as dt_time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytz

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.data.sma_provider import (
    compute_sma_distance,
    classify_vs_prior_range,
    classify_entry_time_bucket,
    invalidate_cache,
)


# ============================================================================
# FIXTURES
# ============================================================================

ET = pytz.timezone("America/New_York")


@pytest.fixture(autouse=True)
def clear_sma_cache():
    """Clear SMA cache before and after each test."""
    invalidate_cache()
    yield
    invalidate_cache()


# ============================================================================
# SMA Distance Calculation
# ============================================================================

class TestComputeSmaDistance:
    """Tests for compute_sma_distance()."""

    def test_above_sma(self):
        pts, pct = compute_sma_distance(6100.0, 6000.0)
        assert pts == 100.0
        assert pct == round((100.0 / 6000.0) * 100, 2)

    def test_below_sma(self):
        pts, pct = compute_sma_distance(5900.0, 6000.0)
        assert pts == -100.0
        assert pct == round((-100.0 / 6000.0) * 100, 2)

    def test_at_sma(self):
        pts, pct = compute_sma_distance(6000.0, 6000.0)
        assert pts == 0.0
        assert pct == 0.0

    def test_none_sma_returns_none(self):
        pts, pct = compute_sma_distance(6000.0, None)
        assert pts is None
        assert pct is None

    def test_zero_sma_returns_none(self):
        pts, pct = compute_sma_distance(6000.0, 0)
        assert pts is None
        assert pct is None

    def test_fractional_values(self):
        pts, pct = compute_sma_distance(6050.55, 6000.25)
        assert pts == round(6050.55 - 6000.25, 2)
        expected_pct = round(((6050.55 - 6000.25) / 6000.25) * 100, 2)
        assert pct == expected_pct


# ============================================================================
# Prior-Range Classification
# ============================================================================

class TestClassifyVsPriorRange:
    """Tests for classify_vs_prior_range()."""

    def test_above_range(self):
        assert classify_vs_prior_range(6100.0, 6050.0, 5950.0) == 'above'

    def test_below_range(self):
        assert classify_vs_prior_range(5900.0, 6050.0, 5950.0) == 'below'

    def test_within_range(self):
        assert classify_vs_prior_range(6000.0, 6050.0, 5950.0) == 'within'

    def test_at_high_is_within(self):
        assert classify_vs_prior_range(6050.0, 6050.0, 5950.0) == 'within'

    def test_at_low_is_within(self):
        assert classify_vs_prior_range(5950.0, 6050.0, 5950.0) == 'within'

    def test_none_high_returns_none(self):
        assert classify_vs_prior_range(6000.0, None, 5950.0) is None

    def test_none_low_returns_none(self):
        assert classify_vs_prior_range(6000.0, 6050.0, None) is None

    def test_both_none_returns_none(self):
        assert classify_vs_prior_range(6000.0, None, None) is None


# ============================================================================
# Entry Time Bucket Classification
# ============================================================================

class TestClassifyEntryTimeBucket:
    """Tests for classify_entry_time_bucket()."""

    def test_early_930(self):
        dt = datetime(2026, 2, 13, 9, 30, tzinfo=ET)
        assert classify_entry_time_bucket(dt) == 'early'

    def test_early_959(self):
        dt = datetime(2026, 2, 13, 9, 59, tzinfo=ET)
        assert classify_entry_time_bucket(dt) == 'early'

    def test_mid_1000(self):
        dt = datetime(2026, 2, 13, 10, 0, tzinfo=ET)
        assert classify_entry_time_bucket(dt) == 'mid'

    def test_mid_1029(self):
        dt = datetime(2026, 2, 13, 10, 29, tzinfo=ET)
        assert classify_entry_time_bucket(dt) == 'mid'

    def test_late_1030(self):
        dt = datetime(2026, 2, 13, 10, 30, tzinfo=ET)
        assert classify_entry_time_bucket(dt) == 'late'

    def test_late_afternoon(self):
        dt = datetime(2026, 2, 13, 14, 0, tzinfo=ET)
        assert classify_entry_time_bucket(dt) == 'late'

    def test_naive_datetime(self):
        """Naive datetime treated as-is (no timezone conversion)."""
        dt = datetime(2026, 2, 13, 9, 45)
        assert classify_entry_time_bucket(dt) == 'early'

    def test_utc_converts_to_et(self):
        """UTC 15:15 = ET 10:15 = mid bucket."""
        utc = pytz.utc.localize(datetime(2026, 2, 13, 15, 15))
        assert classify_entry_time_bucket(utc) == 'mid'


# ============================================================================
# DB Migration
# ============================================================================

class TestDBMetadataMigration:
    """Test that migration adds all new metadata columns."""

    def test_all_metadata_columns_exist(self, tmp_path):
        from database.db_manager import DatabaseManager
        db_path = str(tmp_path / "test_metadata.db")
        db = DatabaseManager(db_path)

        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(trades)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()

        expected = {
            'day_of_week', 'day_of_week_name', 'entry_time_bucket',
            'sma50', 'sma200',
            'spx_vs_sma50', 'spx_vs_sma50_pct',
            'spx_vs_sma200', 'spx_vs_sma200_pct',
            'prior_day_high', 'prior_day_low', 'spx_vs_prior_range',
        }
        assert expected.issubset(columns), f"Missing columns: {expected - columns}"

    def test_migration_is_idempotent(self, tmp_path):
        from database.db_manager import DatabaseManager
        db_path = str(tmp_path / "test_idem.db")
        db1 = DatabaseManager(db_path)
        db2 = DatabaseManager(db_path)  # second init should not fail

        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(trades)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()

        assert 'day_of_week' in columns
        assert 'spx_vs_prior_range' in columns


# ============================================================================
# Integration: enter_trade populates metadata
# ============================================================================

class TestEnterTradeMetadata:
    """Integration test: enter_trade() populates metadata in entry_context."""

    def _make_pm_and_spread(self):
        from src.core.position_manager import PositionManager
        from src.models.spread import CreditSpread, OptionLeg, TradeDirection

        broker = MagicMock()
        broker.place_spread_order.return_value = "order-meta-1"
        broker.get_order_status.return_value = {'status': 'filled', 'fill_price': 2.50}

        strategy = MagicMock()
        strategy.classify_credit_quality.return_value = {
            'quality': 'GOOD', 'credit_received': 2.50, 'moneyness': 'ATM',
            'expected_range': '$2.40-$2.60', 'expected_min': 2.40,
            'expected_max': 2.60, 'pct_of_expected_min': 104.2,
            'is_simulated_concern': False,
        }

        saved_contexts = []
        db = MagicMock()
        db.save_trade.side_effect = lambda trade, context=None: saved_contexts.append(context)

        pm = PositionManager(broker=broker, strategy=strategy, db_manager=db)

        now = ET.localize(datetime(2026, 2, 13, 9, 45))  # Friday, early bucket
        spread = CreditSpread(
            direction=TradeDirection.BULLISH,
            short_leg=OptionLeg(strike=6000, option_type='put', action='sell', price=3.00),
            long_leg=OptionLeg(strike=5995, option_type='put', action='buy', price=1.00),
            credit_received=2.00,
            entry_time=now,
            expiration=now.replace(hour=16, minute=0),
            underlying_price_at_entry=6050.0,
        )

        bar = MagicMock()
        bar.timestamp = now
        bar.open = 6040.0
        bar.high = 6060.0
        bar.low = 5990.0
        bar.close = 6050.0
        bar.range = 70.0
        bar.close_percentage_in_range.return_value = 85.7

        return pm, spread, bar, saved_contexts

    def test_day_of_week_populated(self):
        pm, spread, bar, saved_contexts = self._make_pm_and_spread()

        with patch('time.sleep'), \
             patch('src.data.sma_provider.get_sma50', return_value=None), \
             patch('src.data.sma_provider.get_sma200', return_value=None), \
             patch('src.data.sma_provider.get_prior_day_range', return_value=(None, None)), \
             patch('src.data.economic_calendar.get_today_events', return_value=[]):
            pm.enter_trade(spread, bar, quantity=1)

        ctx = saved_contexts[0]
        # day_of_week uses datetime.now(ET) inside enter_trade
        today_et = datetime.now(ET)
        assert ctx['day_of_week'] == today_et.weekday()
        assert ctx['day_of_week_name'] == today_et.strftime('%A')

    def test_entry_time_bucket_populated(self):
        pm, spread, bar, saved_contexts = self._make_pm_and_spread()

        with patch('time.sleep'), \
             patch('src.data.sma_provider.get_sma50', return_value=None), \
             patch('src.data.sma_provider.get_sma200', return_value=None), \
             patch('src.data.sma_provider.get_prior_day_range', return_value=(None, None)), \
             patch('src.data.sma_provider.classify_entry_time_bucket', return_value='early'), \
             patch('src.data.economic_calendar.get_today_events', return_value=[]):
            pm.enter_trade(spread, bar, quantity=1)

        ctx = saved_contexts[0]
        assert ctx['entry_time_bucket'] == 'early'

    def test_sma_distances_populated(self):
        pm, spread, bar, saved_contexts = self._make_pm_and_spread()

        with patch('time.sleep'), \
             patch('src.data.sma_provider.get_sma50', return_value=6000.0), \
             patch('src.data.sma_provider.get_sma200', return_value=5800.0), \
             patch('src.data.sma_provider.get_prior_day_range', return_value=(None, None)), \
             patch('src.data.economic_calendar.get_today_events', return_value=[]):
            pm.enter_trade(spread, bar, quantity=1)

        ctx = saved_contexts[0]
        # SPX = 6050, SMA50 = 6000 -> +50 pts, +0.83%
        assert ctx['sma50'] == 6000.0
        assert ctx['spx_vs_sma50'] == 50.0
        assert ctx['spx_vs_sma50_pct'] == round((50.0 / 6000.0) * 100, 2)
        # SPX = 6050, SMA200 = 5800 -> +250 pts, +4.31%
        assert ctx['sma200'] == 5800.0
        assert ctx['spx_vs_sma200'] == 250.0
        assert ctx['spx_vs_sma200_pct'] == round((250.0 / 5800.0) * 100, 2)

    def test_prior_range_populated(self):
        pm, spread, bar, saved_contexts = self._make_pm_and_spread()

        with patch('time.sleep'), \
             patch('src.data.sma_provider.get_sma50', return_value=None), \
             patch('src.data.sma_provider.get_sma200', return_value=None), \
             patch('src.data.sma_provider.get_prior_day_range', return_value=(6040.0, 5980.0)), \
             patch('src.data.economic_calendar.get_today_events', return_value=[]):
            pm.enter_trade(spread, bar, quantity=1)

        ctx = saved_contexts[0]
        # SPX = 6050 > prior_high = 6040 -> 'above'
        assert ctx['prior_day_high'] == 6040.0
        assert ctx['prior_day_low'] == 5980.0
        assert ctx['spx_vs_prior_range'] == 'above'

    def test_no_sma_keys_when_provider_returns_none(self):
        pm, spread, bar, saved_contexts = self._make_pm_and_spread()

        with patch('time.sleep'), \
             patch('src.data.sma_provider.get_sma50', return_value=None), \
             patch('src.data.sma_provider.get_sma200', return_value=None), \
             patch('src.data.sma_provider.get_prior_day_range', return_value=(None, None)), \
             patch('src.data.economic_calendar.get_today_events', return_value=[]):
            pm.enter_trade(spread, bar, quantity=1)

        ctx = saved_contexts[0]
        assert 'sma50' not in ctx
        assert 'sma200' not in ctx
        assert 'spx_vs_sma50' not in ctx
        assert 'prior_day_high' not in ctx
        assert 'spx_vs_prior_range' not in ctx

    def test_all_metadata_populated_together(self):
        """Full integration: all metadata fields populated in a single trade."""
        pm, spread, bar, saved_contexts = self._make_pm_and_spread()

        with patch('time.sleep'), \
             patch('src.data.sma_provider.get_sma50', return_value=6000.0), \
             patch('src.data.sma_provider.get_sma200', return_value=5800.0), \
             patch('src.data.sma_provider.get_prior_day_range', return_value=(6040.0, 5980.0)), \
             patch('src.data.sma_provider.classify_entry_time_bucket', return_value='early'), \
             patch('src.data.economic_calendar.get_today_events', return_value=[]):
            pm.enter_trade(spread, bar, quantity=1)

        ctx = saved_contexts[0]
        # Day of week (uses datetime.now(ET))
        today_et = datetime.now(ET)
        assert ctx['day_of_week'] == today_et.weekday()
        assert ctx['day_of_week_name'] == today_et.strftime('%A')
        # Time bucket
        assert ctx['entry_time_bucket'] == 'early'
        # SMA50
        assert ctx['sma50'] == 6000.0
        assert ctx['spx_vs_sma50'] == 50.0
        assert ctx['spx_vs_sma50_pct'] == round((50.0 / 6000.0) * 100, 2)
        # SMA200
        assert ctx['sma200'] == 5800.0
        assert ctx['spx_vs_sma200'] == 250.0
        assert ctx['spx_vs_sma200_pct'] == round((250.0 / 5800.0) * 100, 2)
        # Prior range
        assert ctx['prior_day_high'] == 6040.0
        assert ctx['prior_day_low'] == 5980.0
        assert ctx['spx_vs_prior_range'] == 'above'
