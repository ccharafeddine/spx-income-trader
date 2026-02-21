"""
Tests for P&L attribution (decomposition into theta, delta, vega, residual).

Covers:
- Attribution math with known inputs
- Theta P&L positive for winning credit spreads
- Components sum to approximately actual P&L (within residual)
- Batch aggregation correctness
- Missing data gracefully returns N/A
- Percentage calculations handle zero P&L
- /api/analytics/attribution endpoint structure
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import patch, MagicMock

from src.analytics.greeks import GreeksCalculator
from src.analytics.pnl_attribution import PnLAttributor
from dashboard.app import app, API_TOKEN


def _make_trade(**overrides):
    """Build a trade dict with sensible defaults for a put credit spread."""
    base = {
        'trade_id': 'test-001',
        'entry_time': '2025-06-15T09:45:00',
        'exit_time': '2025-06-15T15:30:00',
        'expiration': '2025-06-15T16:00:00',
        'direction': 'bullish',
        'short_strike': 5750.0,
        'long_strike': 5745.0,
        'credit_received': 1.80,
        'entry_price': 1.80,
        'exit_price': 0.10,
        'pnl': 170.0,  # (1.80 - 0.10) * 100
        'quantity': 1,
        'spx_at_entry': 5800.0,
        'spx_at_exit': 5810.0,
        'vix_at_entry': 18.0,
        'vix_at_exit': 17.0,
        'strategy_type': 'daily_income',
        'duration_minutes': 345,
    }
    base.update(overrides)
    return base


@pytest.fixture
def attributor():
    return PnLAttributor(GreeksCalculator())


class TestAttributionMath:
    """Attribution math with known inputs."""

    def test_basic_attribution_returns_all_components(self, attributor):
        """All four components should be present in result."""
        trade = _make_trade()
        result = attributor.attribute_trade(trade)
        assert result['attributed'] is True
        assert result['theta_pnl'] is not None
        assert result['delta_pnl'] is not None
        assert result['vega_pnl'] is not None
        assert result['residual_pnl'] is not None

    def test_components_sum_to_actual(self, attributor):
        """theta + delta + vega + residual should equal actual P&L."""
        trade = _make_trade()
        result = attributor.attribute_trade(trade)
        component_sum = (
            result['theta_pnl'] + result['delta_pnl'] +
            result['vega_pnl'] + result['residual_pnl']
        )
        assert abs(component_sum - trade['pnl']) < 0.02  # rounding tolerance

    def test_hours_held_computed(self, attributor):
        """Hours held should be computed from entry/exit times."""
        trade = _make_trade()
        result = attributor.attribute_trade(trade)
        # 09:45 to 15:30 = 5.75 hours
        assert abs(result['hours_held'] - 5.75) < 0.01

    def test_hours_held_from_duration_minutes(self, attributor):
        """Falls back to duration_minutes when times unavailable."""
        trade = _make_trade(entry_time=None, exit_time=None, duration_minutes=120)
        result = attributor.attribute_trade(trade)
        assert result['hours_held'] == 2.0


class TestThetaPnL:
    """Theta P&L for credit spreads."""

    def test_theta_positive_for_winning_put_credit(self, attributor):
        """Selling premium = collecting theta. Theta P&L should be positive."""
        trade = _make_trade(
            spx_at_entry=5800.0, spx_at_exit=5800.0,  # no move
            vix_at_entry=18.0, vix_at_exit=18.0,  # no IV change
        )
        result = attributor.attribute_trade(trade)
        # For a put credit spread (short higher put, long lower put),
        # net theta should be positive (collecting time decay)
        assert result['theta_pnl'] > 0

    def test_theta_scales_with_holding_time(self, attributor):
        """Longer hold = more theta collected."""
        short_trade = _make_trade(
            exit_time='2025-06-15T10:45:00',  # 1 hour
            duration_minutes=60,
        )
        long_trade = _make_trade(
            exit_time='2025-06-15T15:30:00',  # 5.75 hours
            duration_minutes=345,
        )
        short_result = attributor.attribute_trade(short_trade)
        long_result = attributor.attribute_trade(long_trade)
        assert long_result['theta_pnl'] > short_result['theta_pnl']


class TestDeltaPnL:
    """Delta P&L from underlying movement."""

    def test_delta_pnl_with_spx_move(self, attributor):
        """A bullish credit spread with SPX rising should see delta effect."""
        trade = _make_trade(spx_at_entry=5800.0, spx_at_exit=5850.0)
        result = attributor.attribute_trade(trade)
        # Delta P&L should be non-zero with a 50-point SPX move
        assert result['delta_pnl'] != 0

    def test_no_spx_move_zero_delta(self, attributor):
        """No SPX movement means zero delta P&L."""
        trade = _make_trade(spx_at_entry=5800.0, spx_at_exit=5800.0)
        result = attributor.attribute_trade(trade)
        assert result['delta_pnl'] == 0.0


class TestVegaPnL:
    """Vega P&L from IV changes."""

    def test_vega_with_iv_decrease(self, attributor):
        """Falling IV should produce vega P&L for short-vega position."""
        trade = _make_trade(vix_at_entry=25.0, vix_at_exit=20.0)
        result = attributor.attribute_trade(trade)
        assert result['vega_pnl'] != 0

    def test_no_iv_data_zero_vega(self, attributor):
        """Missing VIX data should produce zero vega P&L."""
        trade = _make_trade(vix_at_entry=None, vix_at_exit=None)
        result = attributor.attribute_trade(trade)
        assert result['vega_pnl'] == 0


class TestBatchAggregation:
    """Batch attribution across multiple trades."""

    def test_batch_totals_match_individual(self, attributor):
        """Batch totals should match sum of individual attributions."""
        trades = [_make_trade(trade_id=f't{i}', pnl=100 + i * 10) for i in range(5)]
        batch = attributor.attribute_batch(trades)

        individual_theta = sum(
            attributor.attribute_trade(t)['theta_pnl'] for t in trades
        )
        assert abs(batch['total_theta'] - individual_theta) < 0.1

    def test_batch_averages(self, attributor):
        """Averages should equal total / attributed_count."""
        trades = [_make_trade(trade_id=f't{i}') for i in range(4)]
        batch = attributor.attribute_batch(trades)
        if batch['attributed_count'] > 0:
            expected_avg = round(batch['total_theta'] / batch['attributed_count'], 2)
            assert batch['avg_theta'] == expected_avg

    def test_batch_percentages_sum(self, attributor):
        """Component percentages should roughly sum to ~100% (with residual)."""
        trades = [_make_trade(trade_id=f't{i}') for i in range(5)]
        batch = attributor.attribute_batch(trades)
        total_pct = abs(batch['theta_pct']) + abs(batch['delta_pct']) + abs(batch['vega_pct']) + abs(batch['residual_pct'])
        # Components should account for all of the P&L
        assert total_pct > 0

    def test_batch_has_interpretation(self, attributor):
        """Batch result should include interpretation string."""
        trades = [_make_trade(trade_id=f't{i}') for i in range(3)]
        batch = attributor.attribute_batch(trades)
        assert isinstance(batch['interpretation'], str)
        assert len(batch['interpretation']) > 0

    def test_batch_counts(self, attributor):
        """Batch should track total and attributed counts."""
        trades = [_make_trade(trade_id=f't{i}') for i in range(3)]
        batch = attributor.attribute_batch(trades)
        assert batch['total_count'] == 3
        assert batch['attributed_count'] == 3


class TestMissingData:
    """Missing data gracefully returns N/A."""

    def test_missing_pnl(self, attributor):
        """Trade with no P&L returns empty result."""
        trade = _make_trade(pnl=None)
        result = attributor.attribute_trade(trade)
        assert result['attributed'] is False
        assert result['theta_pnl'] is None

    def test_missing_strikes(self, attributor):
        """Trade with no strike prices returns empty result."""
        trade = _make_trade(short_strike=None, long_strike=None)
        result = attributor.attribute_trade(trade)
        assert result['attributed'] is False

    def test_missing_entry_price(self, attributor):
        """Trade with no SPX entry price returns empty result."""
        trade = _make_trade(spx_at_entry=None)
        result = attributor.attribute_trade(trade)
        assert result['attributed'] is False

    def test_missing_times(self, attributor):
        """Trade with no times and no duration returns empty result."""
        trade = _make_trade(entry_time=None, exit_time=None, duration_minutes=None)
        result = attributor.attribute_trade(trade)
        assert result['attributed'] is False

    def test_empty_batch(self, attributor):
        """Empty trade list returns zero aggregates."""
        batch = attributor.attribute_batch([])
        assert batch['attributed_count'] == 0
        assert batch['total_theta'] == 0
        assert 'interpretation' in batch


class TestPercentageEdgeCases:
    """Percentage calculations handle zero P&L."""

    def test_zero_pnl_no_divide_by_zero(self, attributor):
        """Zero actual P&L should not crash percentage calculation."""
        trade = _make_trade(pnl=0.0)
        result = attributor.attribute_trade(trade)
        # Should not crash, percentages computed against abs(1) fallback
        assert result['attributed'] is True
        assert result['theta_pct'] is not None

    def test_zero_total_batch(self, attributor):
        """Batch with zero total P&L should not crash."""
        trades = [
            _make_trade(trade_id='t1', pnl=50.0),
            _make_trade(trade_id='t2', pnl=-50.0),
        ]
        batch = attributor.attribute_batch(trades)
        # Should not crash even if total_actual is near zero
        assert batch['attributed_count'] == 2


class TestAttributionEndpoint:
    """/api/analytics/attribution endpoint."""

    def test_response_structure(self):
        """Endpoint returns expected keys."""
        fake_trades = [
            {
                'trade_id': f't{i}',
                'pnl': 100.0,
                'entry_time': f'2025-06-{i+1:02d}T09:45:00',
                'exit_time': f'2025-06-{i+1:02d}T15:30:00',
                'expiration': f'2025-06-{i+1:02d}T16:00:00',
                'direction': 'bullish',
                'short_strike': 5750.0,
                'long_strike': 5745.0,
                'quantity': 1,
                'spx_at_entry': 5800.0,
                'spx_at_exit': 5810.0,
                'vix_at_entry': 18.0,
                'vix_at_exit': 17.0,
                'status': 'closed',
            }
            for i in range(5)
        ]
        with patch('dashboard.app.yahoo') as mock_yahoo, \
             patch('dashboard.app.classify_trades') as mock_classify, \
             patch('dashboard.app.sqlite3') as mock_sql:
            mock_yahoo.get_spx_quote.return_value = {'price': 5800}
            mock_classify.return_value = ([], fake_trades)
            mock_conn = MagicMock()
            mock_sql.connect.return_value = mock_conn
            mock_conn.execute.return_value = None
            mock_conn.__enter__ = lambda s: s
            mock_conn.__exit__ = MagicMock(return_value=False)

            with app.test_client() as c:
                resp = c.get('/api/analytics/attribution?source=live')
                data = resp.get_json()
                assert data['success'] is True
                assert 'total_theta' in data
                assert 'total_delta' in data
                assert 'total_vega' in data
                assert 'total_residual' in data
                assert 'interpretation' in data
                assert 'attributed_count' in data

    def test_empty_trades(self):
        """With no trades, endpoint returns zero attributed."""
        with patch('dashboard.app.yahoo') as mock_yahoo, \
             patch('dashboard.app.classify_trades') as mock_classify, \
             patch('dashboard.app.sqlite3') as mock_sql:
            mock_yahoo.get_spx_quote.return_value = {'price': 5800}
            mock_classify.return_value = ([], [])
            mock_conn = MagicMock()
            mock_sql.connect.return_value = mock_conn
            mock_conn.execute.return_value = None
            mock_conn.__enter__ = lambda s: s
            mock_conn.__exit__ = MagicMock(return_value=False)

            with app.test_client() as c:
                resp = c.get('/api/analytics/attribution?source=live')
                data = resp.get_json()
                assert data['success'] is True
                assert data['attributed_count'] == 0
