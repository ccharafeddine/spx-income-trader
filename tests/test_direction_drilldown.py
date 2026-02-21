"""
Tests for Direction Drill-Down analytics (market direction x trade direction).

Validates:
1. Cross-tabulation with known market directions and trade directions
2. Up-day specific breakdown
3. Empty regime (no trades in a direction)
4. Interpretation text selection logic
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.analytics.regime_analysis import RegimeAnalyzer


def _make_trade(daily_move_pct, direction, pnl):
    """Create a minimal trade dict for drilldown testing."""
    return {
        'daily_move_pct': daily_move_pct,
        'direction': direction,
        'pnl': pnl,
    }


# ---------------------------------------------------------------------------
# TestCrossTabulation
# ---------------------------------------------------------------------------

class TestCrossTabulation:
    """Cross-tabulation correctly groups trades by market direction x trade direction."""

    def test_basic_counts(self):
        """Trades are counted in the correct cells."""
        trades = [
            _make_trade(0.5, 'bullish', 100),   # Up + bullish
            _make_trade(0.5, 'bearish', -50),    # Up + bearish
            _make_trade(-0.5, 'bullish', 80),    # Down + bullish
            _make_trade(-0.5, 'bearish', 120),   # Down + bearish
        ]
        analyzer = RegimeAnalyzer()
        result = analyzer.analyze_direction_drilldown(trades)
        rows = {r['regime']: r for r in result['rows']}

        assert rows['Up']['bull_count'] == 1
        assert rows['Up']['bear_count'] == 1
        assert rows['Down']['bull_count'] == 1
        assert rows['Down']['bear_count'] == 1
        assert rows['Flat']['bull_count'] == 0
        assert rows['Flat']['bear_count'] == 0

    def test_win_rate_calculation(self):
        """Win rates are calculated correctly per cell."""
        trades = [
            _make_trade(0.5, 'bullish', 100),    # Up bull win
            _make_trade(0.5, 'bullish', -50),     # Up bull loss
            _make_trade(0.5, 'bullish', 80),      # Up bull win
            _make_trade(0.5, 'bearish', -30),     # Up bear loss
            _make_trade(0.5, 'bearish', -40),     # Up bear loss
        ]
        analyzer = RegimeAnalyzer()
        result = analyzer.analyze_direction_drilldown(trades)
        up = next(r for r in result['rows'] if r['regime'] == 'Up')

        assert up['bull_win_rate'] == 66.7   # 2/3
        assert up['bear_win_rate'] == 0.0    # 0/2

    def test_avg_pnl_per_cell(self):
        """Average P&L is calculated correctly per cell."""
        trades = [
            _make_trade(0.5, 'bullish', 100),
            _make_trade(0.5, 'bullish', -50),
            _make_trade(-0.5, 'bearish', 200),
        ]
        analyzer = RegimeAnalyzer()
        result = analyzer.analyze_direction_drilldown(trades)
        rows = {r['regime']: r for r in result['rows']}

        assert rows['Up']['bull_avg_pnl'] == 25.0    # (100-50)/2
        assert rows['Down']['bear_avg_pnl'] == 200.0

    def test_all_five_regimes_present(self):
        """All five direction regimes appear in rows."""
        trades = [_make_trade(0.5, 'bullish', 100)]
        analyzer = RegimeAnalyzer()
        result = analyzer.analyze_direction_drilldown(trades)
        regimes = [r['regime'] for r in result['rows']]
        assert regimes == ['Strong Down', 'Down', 'Flat', 'Up', 'Strong Up']

    def test_regime_boundaries(self):
        """Boundary values are classified correctly."""
        trades = [
            _make_trade(-1.5, 'bullish', -100),  # Strong Down (< -1.0)
            _make_trade(-1.0, 'bullish', -80),    # Down (-1.0 to -0.25)
            _make_trade(-0.25, 'bullish', 50),    # Flat (-0.25 to 0.25)
            _make_trade(0.0, 'bearish', 60),      # Flat
            _make_trade(0.25, 'bearish', -30),    # Up (0.25 to 1.0)
            _make_trade(1.0, 'bearish', -50),     # Strong Up (>= 1.0)
        ]
        analyzer = RegimeAnalyzer()
        result = analyzer.analyze_direction_drilldown(trades)
        rows = {r['regime']: r for r in result['rows']}

        assert rows['Strong Down']['bull_count'] == 1
        assert rows['Down']['bull_count'] == 1
        assert rows['Flat']['bull_count'] == 1
        assert rows['Flat']['bear_count'] == 1
        assert rows['Up']['bear_count'] == 1
        assert rows['Strong Up']['bear_count'] == 1

    def test_none_daily_move_skipped(self):
        """Trades with no daily_move_pct are excluded."""
        trades = [
            _make_trade(None, 'bullish', 100),
            _make_trade(0.5, 'bullish', 80),
        ]
        analyzer = RegimeAnalyzer()
        result = analyzer.analyze_direction_drilldown(trades)
        total = sum(r['bull_count'] + r['bear_count'] for r in result['rows'])
        assert total == 1


# ---------------------------------------------------------------------------
# TestUpDayDetail
# ---------------------------------------------------------------------------

class TestUpDayDetail:
    """Up-day detail cards are populated correctly."""

    def test_up_detail_populated(self):
        """up_detail dict reflects the Up row stats."""
        trades = [
            _make_trade(0.5, 'bullish', 100),
            _make_trade(0.5, 'bullish', -50),
            _make_trade(0.5, 'bearish', -30),
        ]
        analyzer = RegimeAnalyzer()
        result = analyzer.analyze_direction_drilldown(trades)
        up = result['up_detail']

        assert up is not None
        assert up['bull_count'] == 2
        assert up['bull_win_rate'] == 50.0
        assert up['bear_count'] == 1
        assert up['bear_win_rate'] == 0.0

    def test_up_detail_none_when_no_up_trades(self):
        """up_detail is None when no trades fall in the Up regime."""
        trades = [
            _make_trade(-0.5, 'bullish', 100),   # Down
            _make_trade(0.0, 'bearish', 50),      # Flat
        ]
        analyzer = RegimeAnalyzer()
        result = analyzer.analyze_direction_drilldown(trades)
        assert result['up_detail'] is None

    def test_up_detail_avg_pnl(self):
        """up_detail correctly computes avg P&L per direction."""
        trades = [
            _make_trade(0.5, 'bullish', 100),
            _make_trade(0.5, 'bullish', 200),
            _make_trade(0.5, 'bearish', -80),
            _make_trade(0.5, 'bearish', -20),
        ]
        analyzer = RegimeAnalyzer()
        result = analyzer.analyze_direction_drilldown(trades)
        up = result['up_detail']

        assert up['bull_avg_pnl'] == 150.0   # (100+200)/2
        assert up['bear_avg_pnl'] == -50.0   # (-80-20)/2


# ---------------------------------------------------------------------------
# TestEmptyRegime
# ---------------------------------------------------------------------------

class TestEmptyRegime:
    """Regimes with no trades in a direction are handled gracefully."""

    def test_empty_trades(self):
        """No trades produces all-zero rows."""
        analyzer = RegimeAnalyzer()
        result = analyzer.analyze_direction_drilldown([])
        for r in result['rows']:
            assert r['bull_count'] == 0
            assert r['bear_count'] == 0
            assert r['bull_win_rate'] == 0
            assert r['bear_win_rate'] == 0

    def test_only_bullish_trades(self):
        """Bear columns are zero when only bullish trades exist."""
        trades = [
            _make_trade(0.5, 'bullish', 100),
            _make_trade(-0.5, 'bullish', -50),
        ]
        analyzer = RegimeAnalyzer()
        result = analyzer.analyze_direction_drilldown(trades)
        for r in result['rows']:
            assert r['bear_count'] == 0

    def test_only_bearish_trades(self):
        """Bull columns are zero when only bearish trades exist."""
        trades = [
            _make_trade(0.5, 'bearish', -30),
            _make_trade(-0.5, 'bearish', 100),
        ]
        analyzer = RegimeAnalyzer()
        result = analyzer.analyze_direction_drilldown(trades)
        for r in result['rows']:
            assert r['bull_count'] == 0

    def test_unknown_direction_ignored(self):
        """Trades with unknown direction are not counted."""
        trades = [
            _make_trade(0.5, 'unknown', 100),
            _make_trade(0.5, '', 50),
            _make_trade(0.5, 'bullish', 80),
        ]
        analyzer = RegimeAnalyzer()
        result = analyzer.analyze_direction_drilldown(trades)
        up = next(r for r in result['rows'] if r['regime'] == 'Up')
        assert up['bull_count'] == 1
        assert up['bear_count'] == 0


# ---------------------------------------------------------------------------
# TestInterpretation
# ---------------------------------------------------------------------------

class TestInterpretation:
    """Interpretation text correctly identifies the up-day problem source."""

    def test_bearish_underperforming(self):
        """When bearish trades have low WR on Up days, says bearish is the issue."""
        trades = [
            _make_trade(0.5, 'bullish', 100),
            _make_trade(0.5, 'bullish', 80),
            _make_trade(0.5, 'bearish', -50),
            _make_trade(0.5, 'bearish', -30),
        ]
        analyzer = RegimeAnalyzer()
        result = analyzer.analyze_direction_drilldown(trades)
        assert 'bearish' in result['interpretation'].lower()
        assert 'call spreads' in result['interpretation'].lower()

    def test_bullish_underperforming(self):
        """When bullish trades have low WR on Up days, says bullish is the issue."""
        trades = [
            _make_trade(0.5, 'bullish', -50),
            _make_trade(0.5, 'bullish', -30),
            _make_trade(0.5, 'bearish', 100),
            _make_trade(0.5, 'bearish', 80),
        ]
        analyzer = RegimeAnalyzer()
        result = analyzer.analyze_direction_drilldown(trades)
        assert 'bullish' in result['interpretation'].lower()
        assert 'put spreads' in result['interpretation'].lower()

    def test_both_underperforming(self):
        """When both directions have low WR, says both struggle."""
        trades = [
            _make_trade(0.5, 'bullish', -50),
            _make_trade(0.5, 'bullish', -30),
            _make_trade(0.5, 'bearish', -40),
            _make_trade(0.5, 'bearish', -20),
        ]
        analyzer = RegimeAnalyzer()
        result = analyzer.analyze_direction_drilldown(trades)
        assert 'both' in result['interpretation'].lower()

    def test_neither_underperforming(self):
        """When both directions perform well, says no significant issue."""
        trades = [
            _make_trade(0.5, 'bullish', 100),
            _make_trade(0.5, 'bullish', 80),
            _make_trade(0.5, 'bearish', 90),
            _make_trade(0.5, 'bearish', 70),
        ]
        analyzer = RegimeAnalyzer()
        result = analyzer.analyze_direction_drilldown(trades)
        assert 'no significant' in result['interpretation'].lower()

    def test_no_up_day_trades(self):
        """When no trades on Up days, says nothing to analyze."""
        trades = [
            _make_trade(-0.5, 'bullish', 100),
            _make_trade(0.0, 'bearish', 80),
        ]
        analyzer = RegimeAnalyzer()
        result = analyzer.analyze_direction_drilldown(trades)
        assert 'no trades' in result['interpretation'].lower()

    def test_insufficient_sample_not_flagged(self):
        """A single trade in a direction should not trigger low-WR flag."""
        trades = [
            _make_trade(0.5, 'bullish', 100),    # 1 bull win
            _make_trade(0.5, 'bearish', -50),     # 1 bear loss
        ]
        analyzer = RegimeAnalyzer()
        result = analyzer.analyze_direction_drilldown(trades)
        # With only 1 trade per direction, neither should be flagged as low
        assert 'no significant' in result['interpretation'].lower()


# ---------------------------------------------------------------------------
# TestAnalyzeIncludesDrilldown
# ---------------------------------------------------------------------------

class TestAnalyzeIncludesDrilldown:
    """The main analyze() method includes direction_drilldown."""

    def test_drilldown_key_present(self):
        trades = [_make_trade(0.5, 'bullish', 100)]
        analyzer = RegimeAnalyzer()
        result = analyzer.analyze(trades)
        assert 'direction_drilldown' in result

    def test_drilldown_has_rows(self):
        trades = [_make_trade(0.5, 'bullish', 100)]
        analyzer = RegimeAnalyzer()
        result = analyzer.analyze(trades)
        dd = result['direction_drilldown']
        assert 'rows' in dd
        assert 'up_detail' in dd
        assert 'interpretation' in dd
        assert len(dd['rows']) == 5
