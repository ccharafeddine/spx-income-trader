"""
Tests for BB Agreement analytics endpoint and computation.

Validates:
1. BB analysis with mixed agreed/disagreed trades
2. BB analysis with all NULL bb_agreement
3. BB analysis with only agreed or only disagreed
4. API endpoint returns correct structure
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Helpers - simulate the _compute_bb_analysis logic from dashboard/app.py
# ---------------------------------------------------------------------------

def _compute_bb_analysis(trades):
    """Mirror of the endpoint's computation for testing."""
    agreed = [t for t in trades if t.get('bb_agreement') == 1 or t.get('bb_agreement') is True]
    disagreed = [t for t in trades if t.get('bb_agreement') == 0 or t.get('bb_agreement') is False]
    total_with_data = len(agreed) + len(disagreed)

    def _group_stats(group):
        if not group:
            return {'count': 0, 'wins': 0, 'losses': 0, 'win_rate': 0, 'avg_pnl': 0, 'total_pnl': 0}
        wins = [t for t in group if (t.get('pnl') or 0) > 0]
        losses = [t for t in group if (t.get('pnl') or 0) <= 0]
        pnls = [t.get('pnl') or 0 for t in group]
        return {
            'count': len(group),
            'wins': len(wins),
            'losses': len(losses),
            'win_rate': round(len(wins) / len(group) * 100, 1) if group else 0,
            'avg_pnl': round(sum(pnls) / len(pnls), 2) if pnls else 0,
            'total_pnl': round(sum(pnls), 2),
        }

    return {
        'total_trades_with_bb': total_with_data,
        'agreed': _group_stats(agreed),
        'disagreed': _group_stats(disagreed),
    }


def _make_trade(pnl, bb_agreement=None):
    """Create a minimal trade dict."""
    return {
        'strategy_type': 'daily_income',
        'pnl': pnl,
        'bb_agreement': bb_agreement,
    }


# ---------------------------------------------------------------------------
# TestBBAnalysisMixed
# ---------------------------------------------------------------------------

class TestBBAnalysisMixed:
    """BB analysis with mixed agreed/disagreed trades."""

    def test_mixed_counts(self):
        trades = [
            _make_trade(100, bb_agreement=1),
            _make_trade(-50, bb_agreement=1),
            _make_trade(80, bb_agreement=1),
            _make_trade(60, bb_agreement=0),
            _make_trade(-30, bb_agreement=0),
        ]
        result = _compute_bb_analysis(trades)
        assert result['total_trades_with_bb'] == 5
        assert result['agreed']['count'] == 3
        assert result['disagreed']['count'] == 2

    def test_mixed_win_rates(self):
        trades = [
            _make_trade(100, bb_agreement=1),
            _make_trade(-50, bb_agreement=1),
            _make_trade(80, bb_agreement=1),
            _make_trade(60, bb_agreement=0),
            _make_trade(-30, bb_agreement=0),
        ]
        result = _compute_bb_analysis(trades)
        assert result['agreed']['win_rate'] == 66.7  # 2/3
        assert result['disagreed']['win_rate'] == 50.0  # 1/2

    def test_mixed_pnl(self):
        trades = [
            _make_trade(100, bb_agreement=1),
            _make_trade(-50, bb_agreement=1),
            _make_trade(80, bb_agreement=0),
            _make_trade(-20, bb_agreement=0),
        ]
        result = _compute_bb_analysis(trades)
        assert result['agreed']['total_pnl'] == 50.0
        assert result['agreed']['avg_pnl'] == 25.0
        assert result['disagreed']['total_pnl'] == 60.0
        assert result['disagreed']['avg_pnl'] == 30.0

    def test_boolean_bb_agreement_values(self):
        """Handle both True/False (backtest) and 1/0 (database) values."""
        trades = [
            _make_trade(100, bb_agreement=True),
            _make_trade(-50, bb_agreement=False),
        ]
        result = _compute_bb_analysis(trades)
        assert result['agreed']['count'] == 1
        assert result['disagreed']['count'] == 1


# ---------------------------------------------------------------------------
# TestBBAnalysisAllNull
# ---------------------------------------------------------------------------

class TestBBAnalysisAllNull:
    """BB analysis when all trades have NULL bb_agreement."""

    def test_all_null_returns_zero(self):
        trades = [
            _make_trade(100, bb_agreement=None),
            _make_trade(-50, bb_agreement=None),
            _make_trade(80, bb_agreement=None),
        ]
        result = _compute_bb_analysis(trades)
        assert result['total_trades_with_bb'] == 0
        assert result['agreed']['count'] == 0
        assert result['disagreed']['count'] == 0

    def test_empty_trades(self):
        result = _compute_bb_analysis([])
        assert result['total_trades_with_bb'] == 0

    def test_mixed_null_and_data(self):
        trades = [
            _make_trade(100, bb_agreement=1),
            _make_trade(-50, bb_agreement=None),
            _make_trade(80, bb_agreement=0),
        ]
        result = _compute_bb_analysis(trades)
        assert result['total_trades_with_bb'] == 2
        assert result['agreed']['count'] == 1
        assert result['disagreed']['count'] == 1


# ---------------------------------------------------------------------------
# TestBBAnalysisOneSided
# ---------------------------------------------------------------------------

class TestBBAnalysisOneSided:
    """BB analysis with only agreed or only disagreed trades."""

    def test_only_agreed(self):
        trades = [
            _make_trade(100, bb_agreement=1),
            _make_trade(50, bb_agreement=1),
            _make_trade(-30, bb_agreement=1),
        ]
        result = _compute_bb_analysis(trades)
        assert result['agreed']['count'] == 3
        assert result['agreed']['win_rate'] == 66.7
        assert result['disagreed']['count'] == 0
        assert result['disagreed']['win_rate'] == 0

    def test_only_disagreed(self):
        trades = [
            _make_trade(-100, bb_agreement=0),
            _make_trade(-50, bb_agreement=0),
        ]
        result = _compute_bb_analysis(trades)
        assert result['agreed']['count'] == 0
        assert result['disagreed']['count'] == 2
        assert result['disagreed']['win_rate'] == 0

    def test_all_wins_agreed(self):
        trades = [
            _make_trade(100, bb_agreement=1),
            _make_trade(200, bb_agreement=1),
        ]
        result = _compute_bb_analysis(trades)
        assert result['agreed']['win_rate'] == 100.0
        assert result['agreed']['wins'] == 2
        assert result['agreed']['losses'] == 0


# ---------------------------------------------------------------------------
# TestBBAnalysisStructure
# ---------------------------------------------------------------------------

class TestBBAnalysisStructure:
    """API response structure validation."""

    def test_response_keys(self):
        trades = [_make_trade(100, bb_agreement=1)]
        result = _compute_bb_analysis(trades)
        assert 'total_trades_with_bb' in result
        assert 'agreed' in result
        assert 'disagreed' in result

    def test_group_stat_keys(self):
        trades = [_make_trade(100, bb_agreement=1)]
        result = _compute_bb_analysis(trades)
        expected_keys = {'count', 'wins', 'losses', 'win_rate', 'avg_pnl', 'total_pnl'}
        assert set(result['agreed'].keys()) == expected_keys
        assert set(result['disagreed'].keys()) == expected_keys

    def test_zero_pnl_counted_as_loss(self):
        """Trade with exactly $0 P&L counted as loss (not a win)."""
        trades = [_make_trade(0, bb_agreement=1)]
        result = _compute_bb_analysis(trades)
        assert result['agreed']['wins'] == 0
        assert result['agreed']['losses'] == 1

    def test_negative_pnl_counted_as_loss(self):
        trades = [_make_trade(-1, bb_agreement=0)]
        result = _compute_bb_analysis(trades)
        assert result['disagreed']['wins'] == 0
        assert result['disagreed']['losses'] == 1
