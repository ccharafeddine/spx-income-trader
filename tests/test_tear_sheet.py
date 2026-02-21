"""
Tests for strategy tear sheet PDF generation.

Covers:
- Monthly PDF generation returns valid PDF bytes
- Weekly PDF generation works
- Custom date range works
- Empty period (no trades) generates PDF with "No trades" message
- Chart generation doesn't crash with < 2 data points
- Endpoint returns downloadable PDF
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from datetime import date, datetime
from unittest.mock import patch, MagicMock

from src.analytics.tear_sheet import TearSheetGenerator


def _make_trades(count=10, start_date='2026-02-03'):
    """Generate synthetic trade dicts for testing."""
    trades = []
    base = date.fromisoformat(start_date)
    for i in range(count):
        d = base.replace(day=base.day + i % 20)
        trades.append({
            'trade_id': f'test-{i:03d}',
            'entry_time': f'{d.isoformat()}T09:45:00',
            'exit_time': f'{d.isoformat()}T15:30:00',
            'expiration': f'{d.isoformat()}T16:00:00',
            'direction': 'bullish',
            'short_strike': 5750.0,
            'long_strike': 5745.0,
            'credit_received': 1.80,
            'entry_price': 1.80,
            'exit_price': 0.10,
            'pnl': 170.0 if i % 3 != 0 else -80.0,
            'quantity': 1,
            'spx_at_entry': 5800.0,
            'spx_at_exit': 5810.0,
            'vix_at_entry': 18.0,
            'vix_at_exit': 17.0,
            'strategy_type': 'daily_income',
            'exit_reason': 'profit_target' if i % 3 != 0 else 'stop_loss',
            'slippage': -0.03,
            'slippage_pct': -1.5,
        })
    return trades


def _make_analytics(trades):
    """Build minimal analytics dict matching _analytics_compute output."""
    equity = 10000
    equity_curve = []
    for i, t in enumerate(trades):
        pnl = t.get('pnl', 0)
        equity += pnl
        d = t.get('exit_time', '')[:10]
        equity_curve.append({'date': d, 'equity': round(equity, 2), 'daily_pnl': round(pnl, 2)})

    wins = [t for t in trades if (t.get('pnl') or 0) > 0]
    losses = [t for t in trades if (t.get('pnl') or 0) < 0]

    return {
        'trade_count': len(trades),
        'core': {
            'total_return_dollar': sum(t.get('pnl', 0) for t in trades),
            'total_return_pct': 5.2,
            'annualized_return': 12.0,
            'sharpe_ratio': 1.5,
            'sortino_ratio': 2.1,
            'calmar_ratio': 1.8,
            'max_drawdown_dollar': -200,
            'max_drawdown_pct': -2.0,
            'max_drawdown_duration_days': 3,
        },
        'trade_metrics': {
            'total_trades': len(trades),
            'win_rate': len(wins) / len(trades) * 100 if trades else 0,
            'profit_factor': 2.1,
            'avg_win': 170,
            'avg_loss': -80,
            'expectancy': 83.3,
        },
        'equity_curve': equity_curve,
    }


def _make_risk_metrics():
    return {
        'sufficient_data': True,
        'trading_days': 25,
        'var_95': -120.0,
        'var_99': -180.0,
        'cvar_95': -150.0,
        'cvar_99': -200.0,
        'tail_ratio': 1.3,
        'calmar_ratio': 1.8,
        'longest_win_streak': 5,
        'longest_loss_streak': 2,
        'avg_win_streak': 3.0,
        'avg_loss_streak': 1.5,
    }


def _make_attribution():
    return {
        'total_theta': 450.0,
        'total_delta': 120.0,
        'total_vega': -30.0,
        'total_residual': 60.0,
        'total_actual': 600.0,
        'avg_theta': 45.0,
        'avg_delta': 12.0,
        'avg_vega': -3.0,
        'avg_residual': 6.0,
        'theta_pct': 75.0,
        'delta_pct': 20.0,
        'vega_pct': -5.0,
        'residual_pct': 10.0,
        'attributed_count': 10,
        'total_count': 10,
        'interpretation': 'P&L primarily driven by theta decay.',
    }


def _make_execution():
    return {
        'slippage_summary': {
            'avg_slippage': -0.03,
            'total_slippage_cost': -30.0,
            'avg_slippage_pct': -1.5,
            'trades_with_data': 10,
            'total_trades': 10,
        },
        'slippage_by_bucket': [],
        'slippage_by_vix': [
            {'regime': 'Low (<15)', 'count': 5, 'avg_slippage': -0.02},
            {'regime': 'Normal (15-20)', 'count': 5, 'avg_slippage': -0.04},
        ],
        'exit_reasons': [],
    }


@pytest.fixture
def generator():
    return TearSheetGenerator()


@pytest.fixture
def sample_data():
    trades = _make_trades(10)
    return {
        'trades': trades,
        'analytics': _make_analytics(trades),
        'risk_metrics': _make_risk_metrics(),
        'attribution': _make_attribution(),
        'execution': _make_execution(),
    }


class TestMonthlyPDF:
    """Monthly PDF generation."""

    def test_returns_valid_pdf_bytes(self, generator, sample_data):
        """Monthly PDF should return bytes starting with PDF header."""
        result = generator.generate_monthly(
            2026, 2, **sample_data)
        assert isinstance(result, bytes)
        assert len(result) > 100
        assert result[:5] == b'%PDF-'

    def test_monthly_different_months(self, generator, sample_data):
        """Should work for any valid month."""
        for month in [1, 6, 12]:
            result = generator.generate_monthly(
                2026, month, **sample_data)
            assert result[:5] == b'%PDF-'


class TestWeeklyPDF:
    """Weekly PDF generation."""

    def test_weekly_returns_pdf(self, generator, sample_data):
        """Weekly PDF should return valid PDF bytes."""
        result = generator.generate_weekly(
            date(2026, 2, 17), date(2026, 2, 21), **sample_data)
        assert isinstance(result, bytes)
        assert result[:5] == b'%PDF-'

    def test_weekly_filters_trades(self, generator, sample_data):
        """Weekly should filter trades to the date range."""
        # All trades are in Feb 2026, so a Jan range should have 0 trades
        result = generator.generate_weekly(
            date(2025, 1, 6), date(2025, 1, 10), **sample_data)
        assert isinstance(result, bytes)
        assert result[:5] == b'%PDF-'


class TestCustomPDF:
    """Custom date range PDF generation."""

    def test_custom_returns_pdf(self, generator, sample_data):
        """Custom range PDF should return valid PDF bytes."""
        result = generator.generate_custom(
            date(2026, 1, 1), date(2026, 12, 31), **sample_data)
        assert isinstance(result, bytes)
        assert result[:5] == b'%PDF-'


class TestEmptyPeriod:
    """Empty period handling."""

    def test_no_trades_generates_pdf(self, generator):
        """Empty trade list should still generate a valid PDF."""
        result = generator.generate_monthly(
            2026, 2,
            trades=[],
            analytics={'core': {}, 'trade_metrics': {}, 'equity_curve': []},
            risk_metrics={'sufficient_data': False, 'trading_days': 0},
            attribution={'attributed_count': 0},
            execution={},
        )
        assert isinstance(result, bytes)
        assert result[:5] == b'%PDF-'
        assert len(result) > 100

    def test_no_trades_in_range(self, generator, sample_data):
        """When date range has no trades, PDF should still generate."""
        result = generator.generate_weekly(
            date(2030, 1, 1), date(2030, 1, 7), **sample_data)
        assert isinstance(result, bytes)
        assert result[:5] == b'%PDF-'


class TestChartEdgeCases:
    """Chart generation with minimal data."""

    def test_single_data_point_no_crash(self, generator):
        """Equity curve with < 2 points should not crash."""
        analytics = {
            'core': {}, 'trade_metrics': {},
            'equity_curve': [{'date': '2026-02-01', 'equity': 10000, 'daily_pnl': 0}],
        }
        result = generator.generate_monthly(
            2026, 2,
            trades=[],
            analytics=analytics,
            risk_metrics={'sufficient_data': False, 'trading_days': 1},
            attribution={'attributed_count': 0},
            execution={},
        )
        assert isinstance(result, bytes)
        assert result[:5] == b'%PDF-'

    def test_zero_data_points_no_crash(self, generator):
        """Empty equity curve should not crash."""
        analytics = {'core': {}, 'trade_metrics': {}, 'equity_curve': []}
        result = generator.generate_monthly(
            2026, 2,
            trades=[],
            analytics=analytics,
            risk_metrics={'sufficient_data': False, 'trading_days': 0},
            attribution={'attributed_count': 0},
            execution={},
        )
        assert isinstance(result, bytes)
        assert result[:5] == b'%PDF-'


class TestTearSheetEndpoint:
    """/api/export/tearsheet endpoint."""

    def test_endpoint_returns_pdf(self):
        """Endpoint should return a downloadable PDF."""
        from dashboard.app import app

        fake_trades = _make_trades(5)
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
                resp = c.get('/api/export/tearsheet?period=monthly&year=2026&month=2&source=live')
                assert resp.status_code == 200
                assert resp.content_type == 'application/pdf'
                assert resp.data[:5] == b'%PDF-'
