"""
Tests for dashboard overview indicators: strategy status, risk bars, P&L summary.

Covers:
- Daily Income strategy status lifecycle (IDLE -> POSITION -> IDLE)
- TNT strategy status lifecycle (IDLE -> POSITION -> IDLE)
- B&B / ORB strategy status DB cross-check
- Risk bar drawdown values (winning day, losing day, circuit breaker)
- Daily P&L in summary header
- Open positions count
"""

import pytest
import sqlite3
import json
import os
import pytz
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock
from pathlib import Path

ET = pytz.timezone("America/New_York")

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from dashboard.app import app, API_TOKEN


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

@pytest.fixture
def client_with_db(tmp_path):
    """Flask test client + db path + tmp base dir for JSON state files."""
    db_file = str(tmp_path / 'test_app.db')
    schema_path = Path(__file__).parent.parent / 'database' / 'schema.sql'
    conn = sqlite3.connect(db_file)
    conn.executescript(schema_path.read_text())
    conn.close()

    # Create database subdir for JSON state files
    db_dir = tmp_path / 'database'
    db_dir.mkdir(exist_ok=True)

    app.config['TESTING'] = True
    with patch('dashboard.app.DATABASE_PATH', db_file), \
         patch('dashboard.app.BASE_DIR', tmp_path):
        with app.test_client() as c:
            yield c, db_file, tmp_path


def _insert_trade(db_file, trade_id, entry_time, status, pnl=None,
                  exit_time=None, credit=1.50, expiration=None,
                  strategy_type='daily_income', quantity=1):
    """Helper to insert a trade row for testing."""
    if expiration is None:
        expiration = entry_time[:10] + ' 16:00:00'
    conn = sqlite3.connect(db_file)
    conn.execute(
        """INSERT INTO trades (id, entry_time, exit_time, direction, status,
              strategy_type, short_strike, long_strike, spread_width,
              credit_received, entry_price, quantity, expiration, pnl)
           VALUES (?, ?, ?, 'put', ?, ?, 6000, 5990, 10, ?, ?, ?, ?, ?)""",
        (trade_id, entry_time, exit_time, status, strategy_type,
         credit, credit, quantity, expiration, pnl),
    )
    conn.commit()
    conn.close()


def _write_json(tmp_path, filename, data):
    """Write a JSON file into the database subdir."""
    db_dir = tmp_path / 'database'
    db_dir.mkdir(exist_ok=True)
    with open(db_dir / filename, 'w') as f:
        json.dump(data, f)


# --------------------------------------------------------------------------
# 1. TNT strategy status lifecycle
# --------------------------------------------------------------------------

def test_tnt_status_idle_when_no_trades(client_with_db):
    """TNT state is IDLE when JSON says POSITION but no open TNT trades in DB."""
    client, db_file, tmp_path = client_with_db

    # JSON claims POSITION but DB has no open TNT trades
    _write_json(tmp_path, 'tag_n_turn_positions.json', {
        'state': 'POSITION',
        'active_position': {'trade_id': 'ghost-1'},
    })

    with patch('dashboard.app.STRATEGY_PARAMS', {
        'tag_n_turn': {'enabled': True},
        'bnb': {'enabled': False},
        'orb': {'enabled': False},
        'strategy': {},
        'portfolio': {'account_size': 50000},
    }):
        resp = client.get('/api/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['tag_n_turn']['state'] == 'IDLE'
        assert data['tag_n_turn']['active_position'] is None


def test_tnt_status_position_when_active_trade_exists(client_with_db):
    """TNT state is POSITION when JSON says POSITION and DB confirms open trade."""
    client, db_file, tmp_path = client_with_db
    today = datetime.now(ET).strftime('%Y-%m-%d')
    tomorrow = (datetime.now(ET) + timedelta(days=2)).strftime('%Y-%m-%d')

    _insert_trade(db_file, 'tnt-active-1',
                  entry_time=f'{today} 14:00:00',
                  status='active',
                  strategy_type='tag_n_turn',
                  expiration=f'{tomorrow} 16:00:00')

    _write_json(tmp_path, 'tag_n_turn_positions.json', {
        'state': 'POSITION',
        'active_position': {'trade_id': 'tnt-active-1'},
    })

    with patch('dashboard.app.STRATEGY_PARAMS', {
        'tag_n_turn': {'enabled': True},
        'bnb': {'enabled': False},
        'orb': {'enabled': False},
        'strategy': {},
        'portfolio': {'account_size': 50000},
    }):
        resp = client.get('/api/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['tag_n_turn']['state'] == 'POSITION'
        assert data['tag_n_turn']['active_position'] is not None


def test_tnt_status_returns_idle_after_trade_closed(client_with_db):
    """TNT state returns to IDLE when trade is closed in DB even if JSON is stale."""
    client, db_file, tmp_path = client_with_db
    today = datetime.now(ET).strftime('%Y-%m-%d')

    # Insert trade as closed
    _insert_trade(db_file, 'tnt-closed-1',
                  entry_time=f'{today} 14:00:00',
                  exit_time=f'{today} 15:30:00',
                  status='closed',
                  pnl=500.0,
                  strategy_type='tag_n_turn')

    # JSON is stale - still says POSITION
    _write_json(tmp_path, 'tag_n_turn_positions.json', {
        'state': 'POSITION',
        'active_position': {'trade_id': 'tnt-closed-1'},
    })

    with patch('dashboard.app.STRATEGY_PARAMS', {
        'tag_n_turn': {'enabled': True},
        'bnb': {'enabled': False},
        'orb': {'enabled': False},
        'strategy': {},
        'portfolio': {'account_size': 50000},
    }):
        resp = client.get('/api/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['tag_n_turn']['state'] == 'IDLE'


# --------------------------------------------------------------------------
# 2. Daily Income strategy status lifecycle
# --------------------------------------------------------------------------

def test_di_status_idle_when_no_trades(client_with_db):
    """DI state is IDLE when no open daily_income trades exist in DB."""
    client, db_file, tmp_path = client_with_db

    with patch('dashboard.app.STRATEGY_PARAMS', {
        'tag_n_turn': {'enabled': False},
        'bnb': {'enabled': False},
        'orb': {'enabled': False},
        'strategy': {},
        'portfolio': {'account_size': 50000},
    }):
        resp = client.get('/api/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['daily_income']['state'] == 'IDLE'
        assert data['daily_income']['enabled'] is True


def test_di_status_position_when_active_trade_exists(client_with_db):
    """DI state is POSITION when an active daily_income trade exists in DB."""
    client, db_file, tmp_path = client_with_db
    today = datetime.now(ET).strftime('%Y-%m-%d')
    # Future expiration prevents classify_trades from resolving to 'expired'
    future = (datetime.now(ET) + timedelta(days=2)).strftime('%Y-%m-%d')

    _insert_trade(db_file, 'di-active-1',
                  entry_time=f'{today} 10:15:00',
                  status='active',
                  strategy_type='daily_income',
                  expiration=f'{future} 16:00:00')

    with patch('dashboard.app.STRATEGY_PARAMS', {
        'tag_n_turn': {'enabled': False},
        'bnb': {'enabled': False},
        'orb': {'enabled': False},
        'strategy': {},
        'portfolio': {'account_size': 50000},
    }):
        resp = client.get('/api/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['daily_income']['state'] == 'POSITION'


def test_di_status_returns_idle_after_trade_closed(client_with_db):
    """DI state returns to IDLE when the trade is closed in DB."""
    client, db_file, tmp_path = client_with_db
    today = datetime.now(ET).strftime('%Y-%m-%d')
    # Future expiration prevents classify_trades from resolving to 'expired'
    future = (datetime.now(ET) + timedelta(days=2)).strftime('%Y-%m-%d')

    # Insert as active first
    _insert_trade(db_file, 'di-lifecycle-1',
                  entry_time=f'{today} 10:15:00',
                  status='active',
                  strategy_type='daily_income',
                  expiration=f'{future} 16:00:00')

    with patch('dashboard.app.STRATEGY_PARAMS', {
        'tag_n_turn': {'enabled': False},
        'bnb': {'enabled': False},
        'orb': {'enabled': False},
        'strategy': {},
        'portfolio': {'account_size': 50000},
    }):
        # Verify POSITION first
        resp = client.get('/api/status')
        data = resp.get_json()
        assert data['daily_income']['state'] == 'POSITION'

        # Close the trade
        conn = sqlite3.connect(db_file)
        conn.execute(
            "UPDATE trades SET status='closed', exit_time=?, pnl=480 "
            "WHERE id='di-lifecycle-1'",
            (f'{today} 11:30:00',),
        )
        conn.commit()
        conn.close()

        # Verify IDLE after close
        resp2 = client.get('/api/status')
        data2 = resp2.get_json()
        assert data2['daily_income']['state'] == 'IDLE'


# --------------------------------------------------------------------------
# 3. B&B and ORB strategy status DB cross-check
# --------------------------------------------------------------------------

def test_bnb_position_open_overridden_when_no_db_trade(client_with_db):
    """B&B position_open is False when JSON says True but no open B&B trade in DB."""
    client, db_file, tmp_path = client_with_db

    _write_json(tmp_path, 'bnb_signals.json', {
        'position_open': True,
        'pending_signal': None,
        'active_signal': {'direction': 'put'},
    })

    with patch('dashboard.app.STRATEGY_PARAMS', {
        'tag_n_turn': {'enabled': False},
        'bnb': {'enabled': True},
        'orb': {'enabled': False},
        'strategy': {},
        'portfolio': {'account_size': 50000},
    }):
        resp = client.get('/api/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['bnb']['position_open'] is False


def test_bnb_position_open_confirmed_when_db_trade_exists(client_with_db):
    """B&B position_open stays True when DB confirms an open B&B trade."""
    client, db_file, tmp_path = client_with_db
    today = datetime.now(ET).strftime('%Y-%m-%d')
    future = (datetime.now(ET) + timedelta(days=2)).strftime('%Y-%m-%d')

    _insert_trade(db_file, 'bnb-active-1',
                  entry_time=f'{today} 10:30:00',
                  status='active',
                  strategy_type='bnb',
                  expiration=f'{future} 16:00:00')

    _write_json(tmp_path, 'bnb_signals.json', {
        'position_open': True,
        'pending_signal': None,
        'active_signal': {'direction': 'put'},
    })

    with patch('dashboard.app.STRATEGY_PARAMS', {
        'tag_n_turn': {'enabled': False},
        'bnb': {'enabled': True},
        'orb': {'enabled': False},
        'strategy': {},
        'portfolio': {'account_size': 50000},
    }):
        resp = client.get('/api/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['bnb']['position_open'] is True


def test_orb_position_open_overridden_when_no_db_trade(client_with_db):
    """ORB position_open is False when JSON says True but no open ORB trade in DB."""
    client, db_file, tmp_path = client_with_db

    _write_json(tmp_path, 'orb_state.json', {
        'position_open': True,
        'opening_range': {'high': 6010, 'low': 5990},
        'triggered_today': True,
    })

    with patch('dashboard.app.STRATEGY_PARAMS', {
        'tag_n_turn': {'enabled': False},
        'bnb': {'enabled': False},
        'orb': {'enabled': True},
        'strategy': {},
        'portfolio': {'account_size': 50000},
    }):
        resp = client.get('/api/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['orb']['position_open'] is False


def test_orb_position_open_confirmed_when_db_trade_exists(client_with_db):
    """ORB position_open stays True when DB confirms an open ORB trade."""
    client, db_file, tmp_path = client_with_db
    today = datetime.now(ET).strftime('%Y-%m-%d')
    future = (datetime.now(ET) + timedelta(days=2)).strftime('%Y-%m-%d')

    _insert_trade(db_file, 'orb-active-1',
                  entry_time=f'{today} 10:00:00',
                  status='active',
                  strategy_type='orb',
                  expiration=f'{future} 16:00:00')

    _write_json(tmp_path, 'orb_state.json', {
        'position_open': True,
        'opening_range': {'high': 6010, 'low': 5990},
        'triggered_today': True,
    })

    with patch('dashboard.app.STRATEGY_PARAMS', {
        'tag_n_turn': {'enabled': False},
        'bnb': {'enabled': False},
        'orb': {'enabled': True},
        'strategy': {},
        'portfolio': {'account_size': 50000},
    }):
        resp = client.get('/api/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['orb']['position_open'] is True


# --------------------------------------------------------------------------
# 4. Risk bar values on a winning day
# --------------------------------------------------------------------------

def test_risk_bars_winning_day_shows_zero_used(client_with_db):
    """Winning day: all drawdown used values should be zero."""
    client, db_file, tmp_path = client_with_db
    today = datetime.now(ET).strftime('%Y-%m-%d')

    _insert_trade(db_file, 'win-1',
                  entry_time=f'{today} 10:15:00',
                  exit_time=f'{today} 11:30:00',
                  status='closed', pnl=1302.0)

    # Write portfolio state with positive daily P&L
    _write_json(tmp_path, 'portfolio_state.json', {
        'daily_realized_pnl': 1302.0,
        'circuit_breaker_triggered': False,
    })

    with patch('dashboard.app._load_settings', return_value={
        'portfolio': {
            'account_size': 50000,
            'max_daily_loss_pct': 2.0,
            'drawdown_limits': {
                'weekly': {'enabled': True, 'max_loss_pct': 4.0},
                'monthly': {'enabled': True, 'max_loss_pct': 8.0},
            },
        },
    }):
        resp = client.get('/api/risk-status')
        assert resp.status_code == 200
        data = resp.get_json()

        # Daily: positive P&L means no drawdown consumed
        # The realized_pnl is positive, so max(0, -pnl) == 0
        assert data['daily']['realized_pnl'] == 1302.0
        # The frontend computes used = max(0, -realized_pnl) = 0
        assert max(0, -data['daily']['realized_pnl']) == 0
        assert data['daily']['breaker_triggered'] is False

        # Weekly/monthly DB fallback only sums negative P&L trades
        # Our trade is positive, so weekly/monthly realized_pnl should be 0
        assert data['weekly']['realized_pnl'] == 0.0
        assert data['monthly']['realized_pnl'] == 0.0


# --------------------------------------------------------------------------
# 5. Risk bar values on a losing day
# --------------------------------------------------------------------------

def test_risk_bars_losing_day_shows_loss_as_used(client_with_db):
    """Losing day: drawdown used equals the loss amount (positive number)."""
    client, db_file, tmp_path = client_with_db
    today = datetime.now(ET).strftime('%Y-%m-%d')

    _insert_trade(db_file, 'loss-1',
                  entry_time=f'{today} 10:15:00',
                  exit_time=f'{today} 11:30:00',
                  status='closed', pnl=-675.0)

    _write_json(tmp_path, 'portfolio_state.json', {
        'daily_realized_pnl': -675.0,
        'circuit_breaker_triggered': False,
    })

    with patch('dashboard.app._load_settings', return_value={
        'portfolio': {
            'account_size': 50000,
            'max_daily_loss_pct': 2.0,
            'drawdown_limits': {
                'weekly': {'enabled': True, 'max_loss_pct': 4.0},
                'monthly': {'enabled': True, 'max_loss_pct': 8.0},
            },
        },
    }):
        resp = client.get('/api/risk-status')
        assert resp.status_code == 200
        data = resp.get_json()

        # Daily realized_pnl is -675
        assert data['daily']['realized_pnl'] == -675.0
        # Frontend: max(0, -(-675)) = 675 shown as "used"
        assert max(0, -data['daily']['realized_pnl']) == 675.0
        # Loss is $675 vs limit of $1000 (2% of $50k) - breaker should NOT fire
        assert data['daily']['breaker_triggered'] is False
        assert data['daily']['max_loss_dollars'] == 1000.0


# --------------------------------------------------------------------------
# 6. Risk bar values when circuit breaker should fire
# --------------------------------------------------------------------------

def test_risk_bars_circuit_breaker_fires_on_large_loss(client_with_db):
    """Circuit breaker flag is True when daily loss exceeds the limit."""
    client, db_file, tmp_path = client_with_db

    _write_json(tmp_path, 'portfolio_state.json', {
        'daily_realized_pnl': -1200.0,
        'circuit_breaker_triggered': True,
    })

    with patch('dashboard.app._load_settings', return_value={
        'portfolio': {
            'account_size': 50000,
            'max_daily_loss_pct': 2.0,
            'drawdown_limits': {
                'weekly': {'enabled': False},
                'monthly': {'enabled': False},
            },
        },
    }):
        resp = client.get('/api/risk-status')
        assert resp.status_code == 200
        data = resp.get_json()

        assert data['daily']['breaker_triggered'] is True
        assert data['daily']['realized_pnl'] == -1200.0
        # Frontend: max(0, -(-1200)) = 1200 shown as "used"
        assert max(0, -data['daily']['realized_pnl']) == 1200.0


# --------------------------------------------------------------------------
# 7. Daily P&L in the summary header
# --------------------------------------------------------------------------

@patch('dashboard.app.yahoo')
def test_today_net_pnl_sums_correctly(mock_yahoo, client_with_db):
    """Two winning trades sum correctly in /api/today today_pnl."""
    client, db_file, tmp_path = client_with_db
    today = datetime.now(ET).strftime('%Y-%m-%d')

    mock_yahoo.get_spx_quote.return_value = {'price': 6000.0, 'previous_close': 5990.0}
    mock_yahoo.get_intraday_bars.return_value = []

    _insert_trade(db_file, 'pnl-1',
                  entry_time=f'{today} 10:00:00',
                  exit_time=f'{today} 10:45:00',
                  status='closed', pnl=616.0)
    _insert_trade(db_file, 'pnl-2',
                  entry_time=f'{today} 11:00:00',
                  exit_time=f'{today} 11:45:00',
                  status='closed', pnl=685.0)

    resp = client.get('/api/today')
    assert resp.status_code == 200
    data = resp.get_json()

    assert data['today_pnl'] == 1301.0
    assert data['closed_today_count'] == 2


# --------------------------------------------------------------------------
# 8. Open positions count
# --------------------------------------------------------------------------

@patch('dashboard.app.yahoo')
def test_open_positions_count_lifecycle(mock_yahoo, client_with_db):
    """Open position appears in /api/today then disappears after close."""
    client, db_file, tmp_path = client_with_db
    today = datetime.now(ET).strftime('%Y-%m-%d')
    future = (datetime.now(ET) + timedelta(days=2)).strftime('%Y-%m-%d')

    mock_yahoo.get_spx_quote.return_value = {'price': 6000.0, 'previous_close': 5990.0}
    mock_yahoo.get_intraday_bars.return_value = []

    _insert_trade(db_file, 'pos-1',
                  entry_time=f'{today} 10:00:00',
                  status='active',
                  expiration=f'{future} 16:00:00')

    resp = client.get('/api/today')
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data['open_positions']) == 1

    # Close the trade
    conn = sqlite3.connect(db_file)
    conn.execute(
        "UPDATE trades SET status='closed', exit_time=?, pnl=480 WHERE id='pos-1'",
        (f'{today} 12:00:00',),
    )
    conn.commit()
    conn.close()

    resp2 = client.get('/api/today')
    data2 = resp2.get_json()
    assert len(data2['open_positions']) == 0
    assert data2['closed_today_count'] == 1
    assert data2['today_pnl'] == 480.0
