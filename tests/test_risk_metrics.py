"""
Tests for risk-adjusted performance metrics.

Covers:
- VaR 95/99 calculation (historical percentile method)
- CVaR 95/99 (Expected Shortfall)
- Calmar Ratio passthrough
- Tail Ratio (symmetric, skewed right, skewed left)
- Streak detection (all wins, alternating, mixed)
- Insufficient data gate (<20 days)
- /api/analytics/risk endpoint (structure, empty trades)
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import math
from unittest.mock import patch, MagicMock
import json

from dashboard.app import _analytics_risk_metrics, app, API_TOKEN


def _build_equity_curve(daily_pnls, starting_equity=10000):
    """Build an equity curve list from daily P&L values."""
    curve = []
    equity = starting_equity
    for i, pnl in enumerate(daily_pnls):
        equity += pnl
        curve.append({
            'date': f'2025-01-{i+1:02d}',
            'equity': round(equity, 2),
            'daily_pnl': round(pnl, 2),
        })
    return curve


class TestVaRCalculation:
    """VaR 95/99 via sorted percentiles."""

    def test_var_95_known_data(self):
        """VaR 95% should be the value at floor(n*0.05) index in sorted P&Ls."""
        # 100 values: -50, -49, ..., 49 (sorted ascending)
        pnls = list(range(-50, 50))
        curve = _build_equity_curve(pnls)
        result = _analytics_risk_metrics(curve, 1.5)
        # floor(100 * 0.05) = 5 -> sorted_pnls[5] = -45
        assert result['var_95'] == -45.0

    def test_var_99_known_data(self):
        """VaR 99% should be the value at floor(n*0.01) index in sorted P&Ls."""
        pnls = list(range(-50, 50))
        curve = _build_equity_curve(pnls)
        result = _analytics_risk_metrics(curve, 1.5)
        # floor(100 * 0.01) = 1 -> sorted_pnls[1] = -49
        assert result['var_99'] == -49.0

    def test_var_with_all_positive(self):
        """When all P&Ls are positive, VaR should still be a positive number."""
        pnls = [10 + i for i in range(25)]
        curve = _build_equity_curve(pnls)
        result = _analytics_risk_metrics(curve, 1.0)
        # floor(25 * 0.05) = 1 -> sorted[1] = 11
        assert result['var_95'] == 11.0
        assert result['var_95'] > 0

    def test_var_with_all_negative(self):
        """When all P&Ls are negative, VaR captures worst losses."""
        pnls = [-100 + i for i in range(25)]
        curve = _build_equity_curve(pnls, starting_equity=50000)
        result = _analytics_risk_metrics(curve, 1.0)
        assert result['var_95'] < 0


class TestCVaRCalculation:
    """CVaR (Expected Shortfall) - mean of tail beyond VaR."""

    def test_cvar_95_average(self):
        """CVaR 95% = mean of all values <= VaR 95%."""
        pnls = list(range(-50, 50))
        curve = _build_equity_curve(pnls)
        result = _analytics_risk_metrics(curve, 1.0)
        # VaR 95 = -45, values <= -45: [-50, -49, -48, -47, -46, -45]
        expected = (-50 + -49 + -48 + -47 + -46 + -45) / 6
        assert result['cvar_95'] == round(expected, 2)

    def test_cvar_99_average(self):
        """CVaR 99% = mean of all values <= VaR 99%."""
        pnls = list(range(-50, 50))
        curve = _build_equity_curve(pnls)
        result = _analytics_risk_metrics(curve, 1.0)
        # VaR 99 = -49, values <= -49: [-50, -49]
        expected = (-50 + -49) / 2
        assert result['cvar_99'] == round(expected, 2)

    def test_cvar_worse_than_var(self):
        """CVaR should always be <= VaR (further into the tail)."""
        pnls = list(range(-50, 50))
        curve = _build_equity_curve(pnls)
        result = _analytics_risk_metrics(curve, 1.0)
        assert result['cvar_95'] <= result['var_95']
        assert result['cvar_99'] <= result['var_99']


class TestCalmarRatio:
    """Calmar ratio passthrough."""

    def test_calmar_passthrough(self):
        """Calmar ratio should be passed through from input."""
        pnls = [10] * 25
        curve = _build_equity_curve(pnls)
        result = _analytics_risk_metrics(curve, 2.345)
        assert result['calmar_ratio'] == 2.345

    def test_calmar_zero(self):
        """Zero calmar should pass through as 0."""
        pnls = [10] * 25
        curve = _build_equity_curve(pnls)
        result = _analytics_risk_metrics(curve, 0)
        assert result['calmar_ratio'] == 0.0


class TestTailRatio:
    """Tail Ratio = mean(top 5%) / abs(mean(bottom 5%))."""

    def test_symmetric_data(self):
        """Symmetric distribution should give tail ratio near 1.0."""
        # Symmetric around 0: -25..24 (100 values)
        pnls = list(range(-25, 75))  # need 100 values
        # Actually let's make it truly symmetric
        pnls = list(range(-50, 50))
        curve = _build_equity_curve(pnls)
        result = _analytics_risk_metrics(curve, 1.0)
        # top 5%: [45,46,47,48,49], mean=47
        # bottom 5%: [-50,-49,-48,-47,-46,-45], mean=-47.5
        # ratio = 47/47.5 ~ 0.989
        assert result['tail_ratio'] is not None
        assert 0.9 <= result['tail_ratio'] <= 1.1

    def test_skewed_right(self):
        """Right-skewed data (big wins) should give tail ratio > 1."""
        # Most values small, a few huge positive
        pnls = [-5] * 90 + [200] * 10
        curve = _build_equity_curve(pnls, starting_equity=50000)
        result = _analytics_risk_metrics(curve, 1.0)
        assert result['tail_ratio'] is not None
        assert result['tail_ratio'] > 1.0

    def test_skewed_left(self):
        """Left-skewed data (big losses) should give tail ratio < 1."""
        # Most values small positive, a few huge negative
        pnls = [5] * 90 + [-200] * 10
        curve = _build_equity_curve(pnls, starting_equity=50000)
        result = _analytics_risk_metrics(curve, 1.0)
        assert result['tail_ratio'] is not None
        assert result['tail_ratio'] < 1.0


class TestStreakDetection:
    """Win/loss streak detection from daily P&L."""

    def test_all_wins(self):
        """All positive days should produce one long win streak."""
        pnls = [10] * 25
        curve = _build_equity_curve(pnls)
        result = _analytics_risk_metrics(curve, 1.0)
        assert result['longest_win_streak'] == 25
        assert result['longest_loss_streak'] == 0
        assert result['avg_win_streak'] == 25.0
        assert result['avg_loss_streak'] == 0

    def test_alternating(self):
        """Alternating win/loss gives streak of 1 each."""
        pnls = [10, -10] * 15  # 30 days
        curve = _build_equity_curve(pnls)
        result = _analytics_risk_metrics(curve, 1.0)
        assert result['longest_win_streak'] == 1
        assert result['longest_loss_streak'] == 1
        assert result['avg_win_streak'] == 1.0
        assert result['avg_loss_streak'] == 1.0

    def test_mixed_streaks(self):
        """Known pattern with identifiable streak lengths."""
        # 3 wins, 2 losses, 5 wins, 1 loss, 4 wins = 15 + some padding
        pnls = [10, 10, 10, -5, -5, 10, 10, 10, 10, 10, -5, 10, 10, 10, 10] + [10] * 5
        curve = _build_equity_curve(pnls)
        result = _analytics_risk_metrics(curve, 1.0)
        # Win streaks: 3, 5, 9 (4+5 at end). Loss streaks: 2, 1
        assert result['longest_win_streak'] == 9
        assert result['longest_loss_streak'] == 2


class TestInsufficientData:
    """Data gate: <20 trading days returns None values."""

    def test_fewer_than_20_days(self):
        """With <20 data points, all metrics should be None."""
        pnls = [10] * 19
        curve = _build_equity_curve(pnls)
        result = _analytics_risk_metrics(curve, 1.5)
        assert result['sufficient_data'] is False
        assert result['var_95'] is None
        assert result['cvar_95'] is None
        assert result['tail_ratio'] is None
        assert result['calmar_ratio'] is None
        assert result['longest_win_streak'] is None
        assert result['trading_days'] == 19

    def test_exactly_20_days(self):
        """Exactly 20 data points should compute metrics."""
        pnls = list(range(-10, 10))  # 20 values
        curve = _build_equity_curve(pnls, starting_equity=50000)
        result = _analytics_risk_metrics(curve, 1.0)
        assert result['sufficient_data'] is True
        assert result['var_95'] is not None
        assert result['cvar_95'] is not None
        assert result['trading_days'] == 20


class TestRiskEndpoint:
    """Flask endpoint /api/analytics/risk."""

    def test_response_structure(self):
        """Endpoint returns expected keys in response."""
        fake_trades = [
            {'trade_id': f't{i}', 'pnl': 10 + i, 'exit_time': f'2025-01-{i+1:02d} 10:00:00',
             'entry_time': f'2025-01-{i+1:02d} 09:30:00', 'direction': 'bearish',
             'strategy_type': 'put_credit_spread', 'quantity': 1}
            for i in range(25)
        ]
        with patch('dashboard.app.yahoo') as mock_yahoo, \
             patch('dashboard.app.classify_trades') as mock_classify, \
             patch('dashboard.app._get_starting_capital', return_value=10000), \
             patch('dashboard.app.sqlite3') as mock_sql:
            mock_yahoo.get_spx_quote.return_value = {'price': 5800}
            mock_classify.return_value = ([], fake_trades)
            mock_conn = MagicMock()
            mock_sql.connect.return_value = mock_conn
            mock_conn.execute.return_value = None
            mock_conn.__enter__ = lambda s: s
            mock_conn.__exit__ = MagicMock(return_value=False)

            with app.test_client() as c:
                resp = c.get('/api/analytics/risk?source=live')
                data = resp.get_json()
                assert data['success'] is True
                assert 'var_95' in data
                assert 'cvar_95' in data
                assert 'tail_ratio' in data
                assert 'calmar_ratio' in data
                assert 'sufficient_data' in data
                assert 'trading_days' in data

    def test_empty_trades(self):
        """With no trades, endpoint should return insufficient data."""
        with patch('dashboard.app.yahoo') as mock_yahoo, \
             patch('dashboard.app.classify_trades') as mock_classify, \
             patch('dashboard.app._get_starting_capital', return_value=10000), \
             patch('dashboard.app.sqlite3') as mock_sql:
            mock_yahoo.get_spx_quote.return_value = {'price': 5800}
            mock_classify.return_value = ([], [])
            mock_conn = MagicMock()
            mock_sql.connect.return_value = mock_conn
            mock_conn.execute.return_value = None
            mock_conn.__enter__ = lambda s: s
            mock_conn.__exit__ = MagicMock(return_value=False)

            with app.test_client() as c:
                resp = c.get('/api/analytics/risk?source=live')
                data = resp.get_json()
                assert data['success'] is True
                assert data['sufficient_data'] is False
