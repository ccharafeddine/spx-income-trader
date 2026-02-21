"""
Tests for exit reason normalization.

Covers:
- All known exit reason patterns from DI, 1PM, TNT, and expiration strategies
- Unknown patterns pass through unchanged
- Dashboard normalization (_normalize_exit_reason in app.py)
- Backtest engine normalization (_normalize_exit_reason in engine.py)
- Label mapping for display
- Grouped aggregation produces correct counts and averages
- Date range computation
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import patch, MagicMock
from collections import defaultdict


# Import dashboard normalization
from dashboard.app import (
    _normalize_exit_reason as dash_normalize,
    _exit_reason_label,
    _exit_reason_date_range,
)

# Import backtest engine normalization
from src.backtest.engine import _normalize_exit_reason as bt_normalize


class TestDashboardNormalization:
    """Dashboard _normalize_exit_reason maps verbose strings to categories."""

    def test_profit_target_verbose(self):
        """Verbose profit target string normalizes."""
        assert dash_normalize('Profit target reached: $144.00 (target: $144.00)') == 'profit_target'

    def test_profit_target_already_normalized(self):
        """Already-normalized key passes through."""
        assert dash_normalize('profit_target') == 'profit_target'

    def test_1pm_non_trending_favorable(self):
        raw = (
            '1PM CHECK: NON-TRENDING FAVORABLE '
            '(SPX moved $+5.23, P&L 62.3%). '
            'Action: CLOSE for 62.3% profit (no momentum to reach target)'
        )
        assert dash_normalize(raw) == '1pm_close_non_trending'

    def test_1pm_non_trending_unfavorable(self):
        raw = (
            '1PM CHECK: NON-TRENDING UNFAVORABLE '
            '(SPX moved $-8.50, P&L -15.2%). '
            'Action: CLOSE to limit loss'
        )
        assert dash_normalize(raw) == '1pm_close_non_trending'

    def test_1pm_trending(self):
        raw = (
            '1PM CHECK: TRENDING UP FAVORABLE '
            '(SPX moved $+22.00, >$15 threshold). '
            'Action: HOLD TO EXPIRATION'
        )
        assert dash_normalize(raw) == '1pm_hold_trending'

    def test_1pm_trending_down(self):
        raw = (
            '1PM CHECK: TRENDING DOWN UNFAVORABLE '
            '(SPX moved $-18.50, >$15 threshold). '
            'Action: HOLD TO EXPIRATION'
        )
        assert dash_normalize(raw) == '1pm_hold_trending'

    def test_1pm_already_normalized(self):
        assert dash_normalize('1pm_close_non_trending') == '1pm_close_non_trending'
        assert dash_normalize('1pm_hold_trending') == '1pm_hold_trending'

    def test_expiration_4pm(self):
        assert dash_normalize('Expiration (4:00 PM)') == 'expiration'

    def test_expiration_reached(self):
        assert dash_normalize('Expiration reached (4:00 PM EST)') == 'expiration'

    def test_expiration_1pm(self):
        assert dash_normalize('Expiration (1:00 PM)') == 'expiration_1pm'

    def test_expiration_already_normalized(self):
        assert dash_normalize('expiration') == 'expiration'
        assert dash_normalize('expiration_1pm') == 'expiration_1pm'

    def test_tnt_target_hit_with_space(self):
        assert dash_normalize('TNT: target_hit') == 'tnt_target_hit'

    def test_tnt_target_hit_no_space(self):
        assert dash_normalize('TNT:target_hit') == 'tnt_target_hit'

    def test_tnt_stop_hit(self):
        assert dash_normalize('TNT: stop_hit') == 'tnt_stop_hit'

    def test_tnt_max_hold(self):
        assert dash_normalize('TNT: max_hold_exceeded') == 'tnt_max_hold'

    def test_tnt_already_normalized(self):
        assert dash_normalize('tnt_target_hit') == 'tnt_target_hit'
        assert dash_normalize('tnt_stop_hit') == 'tnt_stop_hit'
        assert dash_normalize('tnt_max_hold') == 'tnt_max_hold'

    def test_expired_at_startup(self):
        assert dash_normalize('Expired (resolved at startup, dashboard)') == 'expired_at_startup'
        assert dash_normalize('Expired (resolved at startup, broker)') == 'expired_at_startup'

    def test_unknown_passes_through(self):
        """Unknown reason strings pass through unchanged."""
        assert dash_normalize('some_custom_reason') == 'some_custom_reason'
        assert dash_normalize('manual_close') == 'manual_close'

    def test_none_becomes_unknown(self):
        assert dash_normalize(None) == 'unknown'

    def test_empty_becomes_unknown(self):
        assert dash_normalize('') == 'unknown'

    def test_whitespace_stripped(self):
        assert dash_normalize('  profit_target  ') == 'profit_target'


class TestBacktestNormalization:
    """Backtest engine _normalize_exit_reason returns (category, detail) tuple."""

    def test_returns_tuple(self):
        result = bt_normalize('Profit target reached: $100 (target: $100)')
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_profit_target(self):
        key, detail = bt_normalize('Profit target reached: $100 (target: $100)')
        assert key == 'profit_target'
        assert 'Profit target' in detail

    def test_expiration_4pm(self):
        key, detail = bt_normalize('Expiration (4:00 PM)')
        assert key == 'expiration'

    def test_expiration_1pm(self):
        key, detail = bt_normalize('Expiration (1:00 PM)')
        assert key == 'expiration_1pm'

    def test_1pm_non_trending(self):
        key, detail = bt_normalize('1PM CHECK: NON-TRENDING FAVORABLE (SPX moved $+5, P&L 50%). Action: CLOSE')
        assert key == '1pm_close_non_trending'
        assert '1PM CHECK' in detail

    def test_1pm_trending(self):
        key, detail = bt_normalize('1PM CHECK: TRENDING UP FAVORABLE (SPX moved $+20). Action: HOLD')
        assert key == '1pm_hold_trending'

    def test_tnt_target(self):
        key, detail = bt_normalize('TNT: target_hit')
        assert key == 'tnt_target_hit'

    def test_tnt_stop(self):
        key, detail = bt_normalize('TNT: stop_hit')
        assert key == 'tnt_stop_hit'

    def test_tnt_max_hold(self):
        key, detail = bt_normalize('TNT: max_hold_exceeded')
        assert key == 'tnt_max_hold'

    def test_unknown(self):
        key, detail = bt_normalize('some_random_reason')
        assert key == 'some_random_reason'
        assert detail == ''

    def test_empty(self):
        key, detail = bt_normalize('')
        assert key == 'unknown'

    def test_none_safe(self):
        """None input shouldn't crash (though signature expects str)."""
        key, detail = bt_normalize(None)
        assert key == 'unknown'


class TestExitReasonLabels:
    """Label mapping for display."""

    def test_profit_target_label(self):
        assert _exit_reason_label('profit_target') == 'Profit Target (80%)'

    def test_1pm_close_label(self):
        assert _exit_reason_label('1pm_close_non_trending') == '1PM Close (Non-Trending)'

    def test_1pm_hold_label(self):
        label = _exit_reason_label('1pm_hold_trending')
        assert '1PM Hold' in label
        assert 'Expiration' in label

    def test_expiration_label(self):
        assert _exit_reason_label('expiration') == 'Held to Expiration'

    def test_tnt_labels(self):
        assert _exit_reason_label('tnt_target_hit') == 'TNT Target Hit'
        assert _exit_reason_label('tnt_stop_hit') == 'TNT Stop Loss'
        assert 'TNT Max Hold' in _exit_reason_label('tnt_max_hold')

    def test_unknown_label(self):
        assert _exit_reason_label('unknown') == 'Unknown'

    def test_unmapped_returns_key(self):
        """Unmapped keys return the key itself."""
        assert _exit_reason_label('some_custom') == 'some_custom'


class TestDateRange:
    """Date range computation from trade lists."""

    def test_single_date(self):
        trades = [{'date': '2026-01-15'}]
        assert _exit_reason_date_range(trades) == '2026-01-15'

    def test_date_range(self):
        trades = [{'date': '2026-01-15'}, {'date': '2026-02-20'}, {'date': '2026-01-20'}]
        assert _exit_reason_date_range(trades) == '2026-01-15 to 2026-02-20'

    def test_empty(self):
        assert _exit_reason_date_range([]) == ''

    def test_no_dates(self):
        trades = [{'date': ''}, {'date': ''}]
        assert _exit_reason_date_range(trades) == ''


class TestGroupedAggregation:
    """Normalization produces correct grouped counts and averages."""

    def test_grouping_reduces_rows(self):
        """Multiple verbose reasons should group into one category."""
        from dashboard.app import _analytics_execution

        trades = [
            {'exit_reason': 'Profit target reached: $144.00 (target: $144.00)', 'pnl': 144.0,
             'entry_time': '2026-01-15T10:00:00', 'direction': 'bullish', 'credit_received': 1.80,
             'spx_at_entry': 5800.0, 'spx_at_exit': 5810.0, 'quantity': 1},
            {'exit_reason': 'Profit target reached: $120.00 (target: $120.00)', 'pnl': 120.0,
             'entry_time': '2026-01-16T10:00:00', 'direction': 'bullish', 'credit_received': 1.50,
             'spx_at_entry': 5800.0, 'spx_at_exit': 5815.0, 'quantity': 1},
            {'exit_reason': 'Profit target reached: $160.00 (target: $160.00)', 'pnl': 160.0,
             'entry_time': '2026-01-17T10:00:00', 'direction': 'bearish', 'credit_received': 2.00,
             'spx_at_entry': 5800.0, 'spx_at_exit': 5790.0, 'quantity': 1},
        ]
        result = _analytics_execution(trades)
        exit_reasons = result['exit_reasons']

        # All three should group into one "profit_target" row
        assert len(exit_reasons) == 1
        row = exit_reasons[0]
        assert row['reason'] == 'profit_target'
        assert row['count'] == 3
        assert row['win_rate'] == 100.0
        assert abs(row['avg_pnl'] - 141.33) < 0.1

    def test_mixed_reasons_group_correctly(self):
        """Different reason types should produce separate rows."""
        from dashboard.app import _analytics_execution

        trades = [
            {'exit_reason': 'Profit target reached: $100 (target: $100)', 'pnl': 100.0,
             'entry_time': '2026-01-15T10:00:00', 'direction': 'bullish',
             'spx_at_entry': 5800, 'spx_at_exit': 5810, 'quantity': 1},
            {'exit_reason': 'Expiration (4:00 PM)', 'pnl': -50.0,
             'entry_time': '2026-01-16T10:00:00', 'direction': 'bullish',
             'spx_at_entry': 5800, 'spx_at_exit': 5780, 'quantity': 1},
            {'exit_reason': 'Expiration reached (4:00 PM EST)', 'pnl': 180.0,
             'entry_time': '2026-01-17T10:00:00', 'direction': 'bearish',
             'spx_at_entry': 5800, 'spx_at_exit': 5780, 'quantity': 1},
            {'exit_reason': 'TNT: target_hit', 'pnl': 200.0,
             'entry_time': '2026-01-18T10:00:00', 'direction': 'bullish',
             'spx_at_entry': 5800, 'spx_at_exit': 5850, 'quantity': 1},
        ]
        result = _analytics_execution(trades)
        reasons = {r['reason']: r for r in result['exit_reasons']}

        assert 'profit_target' in reasons
        assert reasons['profit_target']['count'] == 1
        # Both expiration strings should merge
        assert 'expiration' in reasons
        assert reasons['expiration']['count'] == 2
        assert 'tnt_target_hit' in reasons
        assert reasons['tnt_target_hit']['count'] == 1

    def test_label_included_in_result(self):
        """Each result row should have a 'label' field."""
        from dashboard.app import _analytics_execution

        trades = [
            {'exit_reason': 'Profit target reached: $100 (target: $100)', 'pnl': 100.0,
             'entry_time': '2026-01-15T10:00:00', 'direction': 'bullish',
             'spx_at_entry': 5800, 'spx_at_exit': 5810, 'quantity': 1},
        ]
        result = _analytics_execution(trades)
        assert result['exit_reasons'][0]['label'] == 'Profit Target (80%)'

    def test_date_range_included(self):
        """Each result row should have a 'date_range' field."""
        from dashboard.app import _analytics_execution

        trades = [
            {'exit_reason': 'Expiration (4:00 PM)', 'pnl': 100.0,
             'entry_time': '2026-01-15T10:00:00', 'direction': 'bullish',
             'spx_at_entry': 5800, 'spx_at_exit': 5810, 'quantity': 1},
            {'exit_reason': 'Expiration (4:00 PM)', 'pnl': 50.0,
             'entry_time': '2026-02-20T10:00:00', 'direction': 'bullish',
             'spx_at_entry': 5800, 'spx_at_exit': 5820, 'quantity': 1},
        ]
        result = _analytics_execution(trades)
        assert result['exit_reasons'][0]['date_range'] == '2026-01-15 to 2026-02-20'

    def test_trades_included_for_drilldown(self):
        """Each result row should include individual trade details."""
        from dashboard.app import _analytics_execution

        trades = [
            {'exit_reason': 'profit_target', 'pnl': 100.0,
             'entry_time': '2026-01-15T10:00:00', 'direction': 'bullish',
             'credit_received': 1.80, 'spx_at_entry': 5800, 'spx_at_exit': 5810, 'quantity': 1},
        ]
        result = _analytics_execution(trades)
        row = result['exit_reasons'][0]
        assert 'trades' in row
        assert len(row['trades']) == 1
        t = row['trades'][0]
        assert t['date'] == '2026-01-15'
        assert t['direction'] == 'bullish'
        assert t['pnl'] == 100.0


class TestExecutionEndpoint:
    """/api/analytics/execution returns normalized exit reasons."""

    def test_normalized_reasons_in_response(self):
        from dashboard.app import app

        fake_trades = [
            {
                'trade_id': 't1', 'status': 'closed',
                'entry_time': '2026-01-15T10:00:00', 'exit_time': '2026-01-15T15:00:00',
                'direction': 'bullish', 'pnl': 144.0, 'quantity': 1,
                'credit_received': 1.80, 'slippage': -0.03, 'slippage_pct': -1.5,
                'exit_reason': 'Profit target reached: $144.00 (target: $144.00)',
                'spx_at_entry': 5800, 'spx_at_exit': 5810,
                'vix_at_entry': 18.0, 'strategy_type': 'daily_income',
            },
            {
                'trade_id': 't2', 'status': 'closed',
                'entry_time': '2026-01-16T10:00:00', 'exit_time': '2026-01-16T16:00:00',
                'direction': 'bearish', 'pnl': 180.0, 'quantity': 1,
                'credit_received': 2.00, 'slippage': 0, 'slippage_pct': 0,
                'exit_reason': 'Expiration (4:00 PM)',
                'spx_at_entry': 5800, 'spx_at_exit': 5780,
                'vix_at_entry': 20.0, 'strategy_type': 'daily_income',
            },
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
                resp = c.get('/api/analytics/execution?source=live')
                data = resp.get_json()
                assert data['success'] is True
                reasons = {r['reason']: r for r in data['exit_reasons']}
                assert 'profit_target' in reasons
                assert 'expiration' in reasons
                # Verbose strings should NOT appear as keys
                assert 'Profit target reached: $144.00 (target: $144.00)' not in reasons
