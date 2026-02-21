"""
Tests for market regime analysis.

Covers:
- VIX regime classification
- Direction regime classification
- Correlation calculation with known data
- Beta calculation
- Edge case: all trades in one regime
- Edge case: fewer than 5 trades returns N/A
- Regime transitions
- Endpoint structure
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import patch, MagicMock

from src.analytics.regime_analysis import (
    RegimeAnalyzer, _classify_vix_regime, _classify_direction_regime,
)


def _make_trade(vix=18.0, daily_move_pct=0.5, pnl=100.0, **overrides):
    """Build a trade dict with sensible defaults."""
    base = {
        'trade_id': 'test-001',
        'entry_time': '2025-06-15T09:45:00',
        'exit_time': '2025-06-15T15:30:00',
        'direction': 'bullish',
        'pnl': pnl,
        'quantity': 1,
        'vix_at_entry': vix,
        'vix_at_exit': vix,
        'spx_at_entry': 5800.0,
        'spx_at_exit': 5800.0 * (1 + daily_move_pct / 100),
        'daily_move_pct': daily_move_pct,
        'strategy_type': 'daily_income',
    }
    base.update(overrides)
    return base


@pytest.fixture
def analyzer():
    return RegimeAnalyzer()


class TestVixRegimeClassification:
    """VIX regime classification."""

    def test_calm(self):
        assert _classify_vix_regime(12.0) == 'Calm'

    def test_calm_boundary(self):
        assert _classify_vix_regime(0.0) == 'Calm'

    def test_normal(self):
        assert _classify_vix_regime(17.5) == 'Normal'

    def test_normal_boundary(self):
        assert _classify_vix_regime(15.0) == 'Normal'

    def test_elevated(self):
        assert _classify_vix_regime(25.0) == 'Elevated'

    def test_crisis(self):
        assert _classify_vix_regime(35.0) == 'Crisis'

    def test_none_vix(self):
        assert _classify_vix_regime(None) is None


class TestDirectionRegimeClassification:
    """SPX daily return regime classification."""

    def test_strong_down(self):
        assert _classify_direction_regime(-2.0) == 'Strong Down'

    def test_down(self):
        assert _classify_direction_regime(-0.5) == 'Down'

    def test_flat(self):
        assert _classify_direction_regime(0.0) == 'Flat'

    def test_up(self):
        assert _classify_direction_regime(0.5) == 'Up'

    def test_strong_up(self):
        assert _classify_direction_regime(1.5) == 'Strong Up'

    def test_boundary_flat_upper(self):
        """0.25 is classified as Up, not Flat."""
        assert _classify_direction_regime(0.25) == 'Up'

    def test_boundary_flat_lower(self):
        """-0.25 is classified as Flat (>= -0.25)."""
        assert _classify_direction_regime(-0.25) == 'Flat'

    def test_none_return(self):
        assert _classify_direction_regime(None) is None


class TestCorrelationCalculation:
    """Correlation with known data."""

    def test_perfect_positive_correlation(self, analyzer):
        """Trades where P&L perfectly tracks SPX return."""
        trades = [
            _make_trade(daily_move_pct=i * 0.5, pnl=i * 50, trade_id=f't{i}')
            for i in range(1, 11)
        ]
        result = analyzer.market_neutrality_score(trades)
        assert result['sufficient_data'] is True
        assert result['correlation'] > 0.99

    def test_perfect_negative_correlation(self, analyzer):
        """Trades where P&L inversely tracks SPX return."""
        trades = [
            _make_trade(daily_move_pct=i * 0.5, pnl=-i * 50, trade_id=f't{i}')
            for i in range(1, 11)
        ]
        result = analyzer.market_neutrality_score(trades)
        assert result['sufficient_data'] is True
        assert result['correlation'] < -0.99

    def test_zero_correlation(self, analyzer):
        """Random P&L unrelated to SPX return should show low correlation."""
        # Alternating pattern that decorrelates P&L from SPX return
        trades = [
            _make_trade(daily_move_pct=i * 0.3, pnl=100 if i % 2 == 0 else -100, trade_id=f't{i}')
            for i in range(1, 21)
        ]
        result = analyzer.market_neutrality_score(trades)
        assert result['sufficient_data'] is True
        assert abs(result['correlation']) < 0.5


class TestBetaCalculation:
    """Beta to SPX calculation."""

    def test_beta_positive(self, analyzer):
        """Positive beta when P&L moves with SPX."""
        trades = [
            _make_trade(daily_move_pct=i * 0.5, pnl=i * 30, trade_id=f't{i}')
            for i in range(1, 11)
        ]
        result = analyzer.market_neutrality_score(trades)
        assert result['beta'] > 0

    def test_beta_near_zero_for_constant_pnl(self, analyzer):
        """Constant P&L regardless of SPX move should give near-zero beta."""
        trades = [
            _make_trade(daily_move_pct=i * 0.5 - 2.5, pnl=100.0, trade_id=f't{i}')
            for i in range(1, 11)
        ]
        result = analyzer.market_neutrality_score(trades)
        assert abs(result['beta']) < 1.0


class TestAllTradesOneRegime:
    """Edge case: all trades fall in a single regime."""

    def test_all_calm_vix(self, analyzer):
        """All trades with VIX < 15 should populate only Calm bucket."""
        trades = [_make_trade(vix=12.0, trade_id=f't{i}') for i in range(10)]
        result = analyzer.analyze_by_vix_regime(trades)
        calm = next(r for r in result if r['regime'] == 'Calm')
        assert calm['count'] == 10
        for r in result:
            if r['regime'] != 'Calm':
                assert r['count'] == 0

    def test_all_flat_direction(self, analyzer):
        """All trades with flat daily return should populate only Flat."""
        trades = [_make_trade(daily_move_pct=0.1, trade_id=f't{i}') for i in range(10)]
        result = analyzer.analyze_by_direction_regime(trades)
        flat = next(r for r in result if r['regime'] == 'Flat')
        assert flat['count'] == 10


class TestInsufficientData:
    """Fewer than 5 trades returns insufficient data for neutrality."""

    def test_fewer_than_5_trades(self, analyzer):
        """With <5 trades, neutrality should report insufficient data."""
        trades = [_make_trade(trade_id=f't{i}') for i in range(4)]
        result = analyzer.market_neutrality_score(trades)
        assert result['sufficient_data'] is False
        assert result['correlation'] is None
        assert result['beta'] is None

    def test_exactly_5_trades(self, analyzer):
        """5 trades should be sufficient."""
        trades = [
            _make_trade(daily_move_pct=i * 0.5, pnl=i * 20, trade_id=f't{i}')
            for i in range(5)
        ]
        result = analyzer.market_neutrality_score(trades)
        assert result['sufficient_data'] is True
        assert result['correlation'] is not None


class TestRegimeTransitions:
    """VIX transition analysis."""

    def test_rising_vix(self, analyzer):
        """Trades with exit VIX > entry VIX classified as rising."""
        trades = [
            _make_trade(vix=15.0, trade_id=f't{i}',
                        vix_at_entry=15.0, vix_at_exit=20.0)
            for i in range(5)
        ]
        result = analyzer.analyze_regime_transitions(trades)
        assert result['rising_vix']['count'] == 5
        assert result['falling_vix']['count'] == 0

    def test_falling_vix(self, analyzer):
        """Trades with exit VIX < entry VIX classified as falling."""
        trades = [
            _make_trade(vix=20.0, trade_id=f't{i}',
                        vix_at_entry=20.0, vix_at_exit=15.0)
            for i in range(5)
        ]
        result = analyzer.analyze_regime_transitions(trades)
        assert result['falling_vix']['count'] == 5


class TestFullAnalysis:
    """Full analyze() method."""

    def test_full_analysis_structure(self, analyzer):
        """Full analysis returns all expected keys."""
        trades = [
            _make_trade(vix=14 + i, daily_move_pct=-1 + i * 0.5,
                        pnl=50 + i * 10, trade_id=f't{i}')
            for i in range(10)
        ]
        result = analyzer.analyze(trades)
        assert 'vix_regimes' in result
        assert 'direction_regimes' in result
        assert 'transitions' in result
        assert 'neutrality' in result
        assert result['trade_count'] == 10
        assert len(result['vix_regimes']) == 4
        assert len(result['direction_regimes']) == 5


class TestRegimeEndpoint:
    """/api/analytics/regime endpoint."""

    def test_response_structure(self):
        """Endpoint returns expected keys."""
        from dashboard.app import app

        fake_trades = [
            _make_trade(vix=18.0, daily_move_pct=0.5, pnl=100, trade_id=f't{i}',
                        status='closed')
            for i in range(10)
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
                resp = c.get('/api/analytics/regime?source=live')
                data = resp.get_json()
                assert data['success'] is True
                assert 'vix_regimes' in data
                assert 'direction_regimes' in data
                assert 'neutrality' in data
                assert 'transitions' in data

    def test_empty_trades(self):
        """With no trades, endpoint returns zero counts."""
        from dashboard.app import app

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
                resp = c.get('/api/analytics/regime?source=live')
                data = resp.get_json()
                assert data['success'] is True
                assert data['trade_count'] == 0
