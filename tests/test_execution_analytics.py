"""
Tests for execution quality analytics.

Covers:
- Slippage calculation (positive, negative, zero, total cost)
- Time bucket classification (all 5 buckets)
- Exit reason breakdown (grouping, win rate, avg P&L, empty)
- Slippage by VIX regime (low, high, missing)
- BacktestTrade dataclass fields (new execution fields)
- /api/analytics/execution endpoint (structure, empty, backtest)
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock
import json

import pytz
ET = pytz.timezone('America/New_York')

from src.backtest.engine import BacktestTrade, _classify_bt_time_bucket
from dashboard.app import _analytics_execution, app, API_TOKEN


def _make_trade(**overrides):
    """Build a trade dict with sensible defaults."""
    base = {
        'trade_id': 'test-001',
        'entry_time': '2025-06-15T10:15:00',
        'exit_time': '2025-06-15T12:30:00',
        'direction': 'bearish',
        'pnl': 50.0,
        'exit_reason': 'profit_target',
        'quantity': 1,
        'slippage': -0.04,
        'slippage_pct': -2.0,
        'vix_at_entry': 18.0,
        'entry_time_bucket': 'Mid-Morning (10-11)',
        'theoretical_credit': 2.00,
        'actual_credit': 1.96,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------
# Slippage Calculation
# ---------------------------------------------------------------

class TestSlippageCalculation:
    def test_negative_slippage(self):
        """Negative slippage = fill worse than theoretical (typical)."""
        trades = [_make_trade(slippage=-0.05, slippage_pct=-2.5)]
        result = _analytics_execution(trades)
        ss = result['slippage_summary']
        assert ss['avg_slippage'] == -0.05
        assert ss['avg_slippage_pct'] == -2.5
        assert ss['trades_with_data'] == 1

    def test_positive_slippage(self):
        """Positive slippage = fill better than theoretical (rare improvement)."""
        trades = [_make_trade(slippage=0.03, slippage_pct=1.5)]
        result = _analytics_execution(trades)
        ss = result['slippage_summary']
        assert ss['avg_slippage'] == 0.03
        assert ss['avg_slippage_pct'] == 1.5

    def test_zero_slippage_excluded(self):
        """Trades with zero slippage are excluded from slippage stats."""
        trades = [
            _make_trade(slippage=0, slippage_pct=0),
            _make_trade(slippage=-0.04, slippage_pct=-2.0),
        ]
        result = _analytics_execution(trades)
        ss = result['slippage_summary']
        assert ss['trades_with_data'] == 1
        assert ss['total_trades'] == 2
        assert ss['avg_slippage'] == -0.04

    def test_total_slippage_cost_with_quantity(self):
        """Total cost multiplies slippage * quantity * 100."""
        trades = [
            _make_trade(slippage=-0.04, quantity=3),
            _make_trade(slippage=-0.06, quantity=2),
        ]
        result = _analytics_execution(trades)
        ss = result['slippage_summary']
        # (-0.04 * 3 * 100) + (-0.06 * 2 * 100) = -12 + -12 = -24
        assert ss['total_slippage_cost'] == -24.0


# ---------------------------------------------------------------
# Time Bucket Classification
# ---------------------------------------------------------------

class TestTimeBucketClassification:
    def test_open_bucket(self):
        dt = ET.localize(datetime(2025, 6, 15, 9, 35))
        assert _classify_bt_time_bucket(dt) == 'Open (9:30-10)'

    def test_mid_morning_bucket(self):
        dt = ET.localize(datetime(2025, 6, 15, 10, 15))
        assert _classify_bt_time_bucket(dt) == 'Mid-Morning (10-11)'

    def test_midday_bucket(self):
        dt = ET.localize(datetime(2025, 6, 15, 11, 30))
        assert _classify_bt_time_bucket(dt) == 'Midday (11-1)'

    def test_afternoon_bucket(self):
        dt = ET.localize(datetime(2025, 6, 15, 14, 0))
        assert _classify_bt_time_bucket(dt) == 'Afternoon (1-3)'

    def test_close_bucket(self):
        dt = ET.localize(datetime(2025, 6, 15, 15, 30))
        assert _classify_bt_time_bucket(dt) == 'Close (3-4)'


# ---------------------------------------------------------------
# Exit Reason Breakdown
# ---------------------------------------------------------------

class TestExitReasonBreakdown:
    def test_groups_by_reason(self):
        trades = [
            _make_trade(exit_reason='profit_target', pnl=50),
            _make_trade(exit_reason='profit_target', pnl=30),
            _make_trade(exit_reason='stop_loss', pnl=-100),
        ]
        result = _analytics_execution(trades)
        reasons = {r['reason']: r for r in result['exit_reasons']}
        assert reasons['profit_target']['count'] == 2
        assert reasons['stop_loss']['count'] == 1

    def test_win_rate_calculation(self):
        trades = [
            _make_trade(exit_reason='profit_target', pnl=50),
            _make_trade(exit_reason='profit_target', pnl=-10),
            _make_trade(exit_reason='profit_target', pnl=30),
        ]
        result = _analytics_execution(trades)
        reasons = {r['reason']: r for r in result['exit_reasons']}
        assert reasons['profit_target']['win_rate'] == pytest.approx(66.7, abs=0.1)

    def test_avg_pnl_calculation(self):
        trades = [
            _make_trade(exit_reason='expiration', pnl=100),
            _make_trade(exit_reason='expiration', pnl=-50),
        ]
        result = _analytics_execution(trades)
        reasons = {r['reason']: r for r in result['exit_reasons']}
        assert reasons['expiration']['avg_pnl'] == 25.0

    def test_empty_trades(self):
        result = _analytics_execution([])
        assert result['exit_reasons'] == []
        assert result['slippage_summary']['total_trades'] == 0


# ---------------------------------------------------------------
# Slippage by VIX Regime
# ---------------------------------------------------------------

class TestSlippageByVixRegime:
    def test_low_vix(self):
        trades = [_make_trade(vix_at_entry=12.0, slippage=-0.02)]
        result = _analytics_execution(trades)
        regimes = {r['regime']: r for r in result['slippage_by_vix']}
        assert 'Low (<15)' in regimes
        assert regimes['Low (<15)']['count'] == 1
        assert regimes['Low (<15)']['avg_slippage'] == -0.02

    def test_high_vix(self):
        trades = [_make_trade(vix_at_entry=35.0, slippage=-0.15)]
        result = _analytics_execution(trades)
        regimes = {r['regime']: r for r in result['slippage_by_vix']}
        assert 'High (>30)' in regimes
        assert regimes['High (>30)']['avg_slippage'] == -0.15

    def test_missing_vix_excluded(self):
        trades = [
            _make_trade(vix_at_entry=0, slippage=-0.05),
            _make_trade(vix_at_entry=None, slippage=-0.05),
            _make_trade(vix_at_entry=22.0, slippage=-0.08),
        ]
        result = _analytics_execution(trades)
        total_vix_trades = sum(r['count'] for r in result['slippage_by_vix'])
        assert total_vix_trades == 1  # only the vix=22 trade


# ---------------------------------------------------------------
# BacktestTrade Fields
# ---------------------------------------------------------------

class TestBacktestTradeFields:
    def test_dataclass_has_execution_fields(self):
        bt = BacktestTrade(
            trade_id='bt-001',
            entry_time=datetime(2025, 6, 15, 10, 0),
            theoretical_credit=2.00,
            actual_credit=1.96,
            slippage=-0.04,
            slippage_pct=-2.0,
            entry_time_bucket='Mid-Morning (10-11)',
        )
        assert bt.theoretical_credit == 2.00
        assert bt.actual_credit == 1.96
        assert bt.slippage == -0.04
        assert bt.slippage_pct == -2.0
        assert bt.entry_time_bucket == 'Mid-Morning (10-11)'

    def test_to_dict_includes_execution_fields(self):
        bt = BacktestTrade(
            trade_id='bt-002',
            entry_time=datetime(2025, 6, 15, 10, 0),
            theoretical_credit=2.50,
            actual_credit=2.42,
            slippage=-0.08,
            slippage_pct=-3.2,
            entry_time_bucket='Open (9:30-10)',
        )
        d = bt.to_dict()
        assert d['theoretical_credit'] == 2.50
        assert d['actual_credit'] == 2.42
        assert d['slippage'] == -0.08
        assert d['slippage_pct'] == -3.2
        assert d['entry_time_bucket'] == 'Open (9:30-10)'

    def test_slippage_math(self):
        """actual - theoretical = slippage (negative = worse fill)."""
        theoretical = 2.00
        actual = 1.92
        slippage = round(actual - theoretical, 4)
        assert slippage == -0.08

    def test_default_values(self):
        bt = BacktestTrade(trade_id='bt-003', entry_time=datetime(2025, 1, 1))
        assert bt.theoretical_credit == 0.0
        assert bt.actual_credit == 0.0
        assert bt.slippage == 0.0
        assert bt.slippage_pct == 0.0
        assert bt.entry_time_bucket == ''


# ---------------------------------------------------------------
# Analytics Endpoint
# ---------------------------------------------------------------

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


class TestAnalyticsEndpoint:
    @patch('dashboard.app.classify_trades')
    @patch('dashboard.app.yahoo')
    def test_response_structure(self, mock_yahoo, mock_classify, client):
        mock_yahoo.get_spx_quote.return_value = {'price': 5800}
        mock_classify.return_value = ([], [
            _make_trade(slippage=-0.04, slippage_pct=-2.0, vix_at_entry=18.0),
        ])
        resp = client.get('/api/analytics/execution?source=live')
        data = resp.get_json()
        assert data['success'] is True
        assert 'slippage_summary' in data
        assert 'slippage_by_bucket' in data
        assert 'slippage_by_vix' in data
        assert 'exit_reasons' in data
        assert data['slippage_summary']['trades_with_data'] == 1

    @patch('dashboard.app.classify_trades')
    @patch('dashboard.app.yahoo')
    def test_empty_trades(self, mock_yahoo, mock_classify, client):
        mock_yahoo.get_spx_quote.return_value = {'price': 5800}
        mock_classify.return_value = ([], [])
        resp = client.get('/api/analytics/execution?source=live')
        data = resp.get_json()
        assert data['success'] is True
        assert data['slippage_summary']['total_trades'] == 0
        assert data['exit_reasons'] == []

    @patch('dashboard.app.sqlite3')
    def test_backtest_source(self, mock_sqlite, client):
        mock_conn = MagicMock()
        mock_sqlite.connect.return_value = mock_conn
        trades = [_make_trade(slippage=-0.03, slippage_pct=-1.5, vix_at_entry=16.0)]
        mock_conn.execute.return_value.fetchone.return_value = (
            json.dumps({'trades': trades}),
        )
        resp = client.get('/api/analytics/execution?source=backtest&run_id=42')
        data = resp.get_json()
        assert data['success'] is True
        assert data['slippage_summary']['trades_with_data'] == 1
