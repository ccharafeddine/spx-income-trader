"""
Tests for Trade Duration analytics computation.

Validates:
1. Duration bucketing with known times
2. Mixed winners/losers duration comparison
3. Trades with missing entry/exit times (skipped gracefully)
4. Single trade edge case
5. All winners or all losers
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Mirror the _compute_duration_analysis logic from dashboard/app.py
# ---------------------------------------------------------------------------

BUCKETS = [
    ('0-1h', 0, 60),
    ('1-2h', 60, 120),
    ('2-3h', 120, 180),
    ('3-4h', 180, 240),
    ('4-5h', 240, 300),
    ('5-6h', 300, 360),
    ('6h+', 360, float('inf')),
]


def _compute_duration_analysis(trades):
    valid = []
    for t in trades:
        mins = t.get('duration_minutes')
        if mins is None:
            mins = t.get('time_in_trade_minutes')
        if mins is None:
            continue
        mins = int(mins)
        pnl = t.get('pnl') or 0
        valid.append({'minutes': mins, 'pnl': float(pnl), 'win': float(pnl) > 0})

    if not valid:
        return {'total_trades': 0}

    winners = [t for t in valid if t['win']]
    losers = [t for t in valid if not t['win']]

    def _avg_mins(group):
        return round(sum(t['minutes'] for t in group) / len(group), 1) if group else 0

    avg_winner_mins = _avg_mins(winners)
    avg_loser_mins = _avg_mins(losers)
    fastest_winner = min((t['minutes'] for t in winners), default=0)
    longest_loser = max((t['minutes'] for t in losers), default=0)

    bucket_data = []
    for label, lo, hi in BUCKETS:
        in_bucket = [t for t in valid if lo <= t['minutes'] < hi]
        w = [t for t in in_bucket if t['win']]
        l = [t for t in in_bucket if not t['win']]
        pnls = [t['pnl'] for t in in_bucket]
        bucket_data.append({
            'bucket': label,
            'total': len(in_bucket),
            'winners': len(w),
            'losers': len(l),
            'win_rate': round(len(w) / len(in_bucket) * 100, 1) if in_bucket else 0,
            'avg_pnl': round(sum(pnls) / len(pnls), 2) if pnls else 0,
        })

    if len(winners) < 3 or len(losers) < 3:
        interpretation = 'Not enough data for meaningful comparison (need 3+ winners and 3+ losers).'
    elif avg_winner_mins < avg_loser_mins * 0.7:
        interpretation = 'Winners exit quickly via profit target. Losers tend to be held longer.'
    elif avg_loser_mins < avg_winner_mins * 0.7:
        interpretation = 'Losers exit quickly (stop loss). Winners tend to be held longer.'
    else:
        interpretation = 'No significant duration difference between winners and losers.'

    return {
        'total_trades': len(valid),
        'total_winners': len(winners),
        'total_losers': len(losers),
        'avg_winner_minutes': avg_winner_mins,
        'avg_loser_minutes': avg_loser_mins,
        'fastest_winner_minutes': fastest_winner,
        'longest_loser_minutes': longest_loser,
        'buckets': bucket_data,
        'interpretation': interpretation,
    }


def _make_trade(duration_minutes, pnl):
    """Create a minimal trade dict with duration."""
    return {'duration_minutes': duration_minutes, 'pnl': pnl}


# ---------------------------------------------------------------------------
# TestDurationBucketing
# ---------------------------------------------------------------------------

class TestDurationBucketing:
    """Duration bucketing assigns trades to correct time ranges."""

    def test_zero_minutes_in_first_bucket(self):
        trades = [_make_trade(0, 100)]
        result = _compute_duration_analysis(trades)
        assert result['buckets'][0]['bucket'] == '0-1h'
        assert result['buckets'][0]['total'] == 1

    def test_59_minutes_in_first_bucket(self):
        trades = [_make_trade(59, 100)]
        result = _compute_duration_analysis(trades)
        assert result['buckets'][0]['total'] == 1

    def test_60_minutes_in_second_bucket(self):
        trades = [_make_trade(60, 100)]
        result = _compute_duration_analysis(trades)
        assert result['buckets'][0]['total'] == 0
        assert result['buckets'][1]['bucket'] == '1-2h'
        assert result['buckets'][1]['total'] == 1

    def test_360_minutes_in_last_bucket(self):
        trades = [_make_trade(360, -50)]
        result = _compute_duration_analysis(trades)
        assert result['buckets'][6]['bucket'] == '6h+'
        assert result['buckets'][6]['total'] == 1

    def test_all_seven_buckets_exist(self):
        trades = [_make_trade(30, 100)]
        result = _compute_duration_analysis(trades)
        assert len(result['buckets']) == 7
        labels = [b['bucket'] for b in result['buckets']]
        assert labels == ['0-1h', '1-2h', '2-3h', '3-4h', '4-5h', '5-6h', '6h+']

    def test_spread_across_buckets(self):
        trades = [
            _make_trade(30, 100),   # 0-1h
            _make_trade(90, 80),    # 1-2h
            _make_trade(150, -50),  # 2-3h
            _make_trade(200, -30),  # 3-4h
            _make_trade(400, -100), # 6h+
        ]
        result = _compute_duration_analysis(trades)
        assert result['buckets'][0]['total'] == 1
        assert result['buckets'][1]['total'] == 1
        assert result['buckets'][2]['total'] == 1
        assert result['buckets'][3]['total'] == 1
        assert result['buckets'][4]['total'] == 0
        assert result['buckets'][5]['total'] == 0
        assert result['buckets'][6]['total'] == 1


# ---------------------------------------------------------------------------
# TestDurationMixedWinnersLosers
# ---------------------------------------------------------------------------

class TestDurationMixedWinnersLosers:
    """Duration comparison between winners and losers."""

    def test_winner_loser_averages(self):
        trades = [
            _make_trade(30, 100),   # winner, 30m
            _make_trade(60, 200),   # winner, 60m
            _make_trade(90, 150),   # winner, 90m
            _make_trade(300, -100), # loser, 300m
            _make_trade(330, -50),  # loser, 330m
            _make_trade(360, -80),  # loser, 360m
        ]
        result = _compute_duration_analysis(trades)
        assert result['avg_winner_minutes'] == 60.0   # (30+60+90)/3
        assert result['avg_loser_minutes'] == 330.0    # (300+330+360)/3

    def test_fastest_winner(self):
        trades = [
            _make_trade(15, 50),
            _make_trade(45, 100),
            _make_trade(200, -30),
        ]
        result = _compute_duration_analysis(trades)
        assert result['fastest_winner_minutes'] == 15

    def test_longest_loser(self):
        trades = [
            _make_trade(30, 100),
            _make_trade(300, -50),
            _make_trade(390, -100),
        ]
        result = _compute_duration_analysis(trades)
        assert result['longest_loser_minutes'] == 390

    def test_winners_faster_interpretation(self):
        """When winners avg < 70% of losers avg, interpretation says fast exits."""
        trades = [
            _make_trade(30, 100),
            _make_trade(40, 80),
            _make_trade(50, 60),
            _make_trade(300, -100),
            _make_trade(320, -50),
            _make_trade(340, -80),
        ]
        result = _compute_duration_analysis(trades)
        assert 'quickly' in result['interpretation'].lower()
        assert 'profit target' in result['interpretation'].lower()

    def test_similar_duration_interpretation(self):
        """When durations are similar, interpretation says no significant difference."""
        trades = [
            _make_trade(100, 100),
            _make_trade(110, 80),
            _make_trade(120, 60),
            _make_trade(105, -50),
            _make_trade(115, -30),
            _make_trade(125, -80),
        ]
        result = _compute_duration_analysis(trades)
        assert 'no significant' in result['interpretation'].lower()

    def test_win_rate_by_bucket(self):
        trades = [
            _make_trade(30, 100),   # 0-1h winner
            _make_trade(45, 50),    # 0-1h winner
            _make_trade(50, -20),   # 0-1h loser
            _make_trade(200, -100), # 3-4h loser
        ]
        result = _compute_duration_analysis(trades)
        bucket_0 = result['buckets'][0]
        assert bucket_0['win_rate'] == 66.7
        assert bucket_0['winners'] == 2
        assert bucket_0['losers'] == 1


# ---------------------------------------------------------------------------
# TestDurationMissingData
# ---------------------------------------------------------------------------

class TestDurationMissingData:
    """Trades with missing duration data are skipped gracefully."""

    def test_none_duration_skipped(self):
        trades = [
            _make_trade(None, 100),
            _make_trade(30, 50),
        ]
        result = _compute_duration_analysis(trades)
        assert result['total_trades'] == 1

    def test_all_none_returns_zero(self):
        trades = [
            _make_trade(None, 100),
            _make_trade(None, -50),
        ]
        result = _compute_duration_analysis(trades)
        assert result['total_trades'] == 0

    def test_empty_trades(self):
        result = _compute_duration_analysis([])
        assert result['total_trades'] == 0

    def test_live_trade_field_name(self):
        """Live trades use time_in_trade_minutes instead of duration_minutes."""
        trades = [{'time_in_trade_minutes': 45, 'pnl': 100}]
        result = _compute_duration_analysis(trades)
        assert result['total_trades'] == 1
        assert result['avg_winner_minutes'] == 45.0

    def test_zero_pnl_counted_as_loser(self):
        """Trade with exactly $0 P&L is a loser."""
        trades = [_make_trade(60, 0)]
        result = _compute_duration_analysis(trades)
        assert result['total_losers'] == 1
        assert result['total_winners'] == 0


# ---------------------------------------------------------------------------
# TestDurationSingleTrade
# ---------------------------------------------------------------------------

class TestDurationSingleTrade:
    """Edge case with a single trade."""

    def test_single_winner(self):
        trades = [_make_trade(45, 100)]
        result = _compute_duration_analysis(trades)
        assert result['total_trades'] == 1
        assert result['total_winners'] == 1
        assert result['total_losers'] == 0
        assert result['avg_winner_minutes'] == 45.0
        assert result['avg_loser_minutes'] == 0
        assert result['fastest_winner_minutes'] == 45

    def test_single_loser(self):
        trades = [_make_trade(300, -100)]
        result = _compute_duration_analysis(trades)
        assert result['total_trades'] == 1
        assert result['total_losers'] == 1
        assert result['total_winners'] == 0
        assert result['longest_loser_minutes'] == 300

    def test_single_trade_insufficient_data_interpretation(self):
        trades = [_make_trade(45, 100)]
        result = _compute_duration_analysis(trades)
        assert 'not enough' in result['interpretation'].lower()


# ---------------------------------------------------------------------------
# TestDurationAllSameOutcome
# ---------------------------------------------------------------------------

class TestDurationAllSameOutcome:
    """All winners or all losers."""

    def test_all_winners(self):
        trades = [
            _make_trade(30, 100),
            _make_trade(60, 50),
            _make_trade(90, 200),
        ]
        result = _compute_duration_analysis(trades)
        assert result['total_winners'] == 3
        assert result['total_losers'] == 0
        assert result['avg_loser_minutes'] == 0
        assert result['longest_loser_minutes'] == 0

    def test_all_losers(self):
        trades = [
            _make_trade(200, -50),
            _make_trade(300, -100),
            _make_trade(350, -80),
        ]
        result = _compute_duration_analysis(trades)
        assert result['total_losers'] == 3
        assert result['total_winners'] == 0
        assert result['avg_winner_minutes'] == 0
        assert result['fastest_winner_minutes'] == 0

    def test_all_same_interpretation(self):
        """Not enough of one side triggers the 'not enough data' message."""
        trades = [
            _make_trade(30, 100),
            _make_trade(60, 50),
            _make_trade(90, 200),
            _make_trade(100, 80),
        ]
        result = _compute_duration_analysis(trades)
        assert 'not enough' in result['interpretation'].lower()


# ---------------------------------------------------------------------------
# TestDurationResponseStructure
# ---------------------------------------------------------------------------

class TestDurationResponseStructure:
    """Response structure validation."""

    def test_all_keys_present(self):
        trades = [_make_trade(30, 100), _make_trade(300, -50)]
        result = _compute_duration_analysis(trades)
        expected_keys = {
            'total_trades', 'total_winners', 'total_losers',
            'avg_winner_minutes', 'avg_loser_minutes',
            'fastest_winner_minutes', 'longest_loser_minutes',
            'buckets', 'interpretation',
        }
        assert expected_keys.issubset(set(result.keys()))

    def test_bucket_keys(self):
        trades = [_make_trade(30, 100)]
        result = _compute_duration_analysis(trades)
        for b in result['buckets']:
            assert set(b.keys()) == {'bucket', 'total', 'winners', 'losers', 'win_rate', 'avg_pnl'}

    def test_empty_result_minimal(self):
        result = _compute_duration_analysis([])
        assert result == {'total_trades': 0}
