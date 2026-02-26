"""
Tests for the daily journal feature.

Covers:
- daily_journal table creation and CRUD
- /api/journal/calendar integration with daily_journal data
- /api/journal/daily/<date> endpoint
- TradingBot rejection tracking helpers
"""

import pytest
import sqlite3
import json
import tempfile
import os
import pytz
from datetime import date, datetime
from unittest.mock import patch, MagicMock

ET = pytz.timezone("America/New_York")

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from database.db_manager import DatabaseManager
from dashboard.app import app, API_TOKEN


# --------------------------------------------------------------------------
# DatabaseManager tests
# --------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    """Create a temporary DatabaseManager for testing."""
    db_path = str(tmp_path / 'test.db')
    dm = DatabaseManager(db_path)
    yield dm


def test_daily_journal_table_exists(db):
    """daily_journal table should be created by schema.sql."""
    conn = sqlite3.connect(db.db_path)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='daily_journal'"
    )
    assert cursor.fetchone() is not None
    conn.close()


def test_save_and_get_daily_journal(db):
    """Save a daily journal entry and retrieve it."""
    test_date = date(2026, 2, 17)
    journal_data = {
        'bars_built': 5,
        'pulse_bars_found': 2,
        'signals_evaluated': 4,
        'trades_entered': 0,
        'spx_open': 6050.0,
        'spx_close': 6025.0,
        'spx_change_pct': -0.41,
        'vix_level': 18.5,
        'vix_regime': 'normal',
        'rejection_reasons': [
            {'timestamp': '10:30:00', 'strategy': 'daily_income', 'reason': 'setup_expired',
             'detail': 'bearish setup trigger not hit'},
        ],
        'market_context': {'day_type': 'trending_down', 'range': 25.0},
        'no_trade_summary': '2 pulse bars detected. Setup expired without breakout.',
    }

    db.save_daily_journal(test_date, journal_data)

    result = db.get_daily_journal(test_date)
    assert result is not None
    assert result['bars_built'] == 5
    assert result['pulse_bars_found'] == 2
    assert result['signals_evaluated'] == 4
    assert result['trades_entered'] == 0
    assert result['spx_open'] == 6050.0
    assert result['spx_close'] == 6025.0
    assert result['vix_regime'] == 'normal'
    assert result['no_trade_summary'] == '2 pulse bars detected. Setup expired without breakout.'
    # JSON fields should be parsed
    assert isinstance(result['rejection_reasons'], list)
    assert len(result['rejection_reasons']) == 1
    assert result['rejection_reasons'][0]['reason'] == 'setup_expired'
    assert isinstance(result['market_context'], dict)
    assert result['market_context']['day_type'] == 'trending_down'


def test_get_daily_journal_missing(db):
    """Getting a non-existent journal entry returns None."""
    result = db.get_daily_journal(date(2020, 1, 1))
    assert result is None


def test_get_daily_journals_for_month(db):
    """Get all journal entries for a month."""
    db.save_daily_journal(date(2026, 2, 10), {
        'bars_built': 3, 'no_trade_summary': 'No pulse bars.',
    })
    db.save_daily_journal(date(2026, 2, 11), {
        'bars_built': 5, 'trades_entered': 1,
        'no_trade_summary': '1 trade entered.',
    })
    db.save_daily_journal(date(2026, 3, 1), {
        'bars_built': 4, 'no_trade_summary': 'Different month.',
    })

    result = db.get_daily_journals_for_month(2026, 2)
    assert len(result) == 2
    assert '2026-02-10' in result
    assert '2026-02-11' in result
    assert '2026-03-01' not in result


def test_save_daily_journal_upsert(db):
    """Saving twice for the same date should update (not duplicate)."""
    test_date = date(2026, 2, 17)
    db.save_daily_journal(test_date, {'bars_built': 3, 'no_trade_summary': 'First'})
    db.save_daily_journal(test_date, {'bars_built': 5, 'no_trade_summary': 'Updated'})

    result = db.get_daily_journal(test_date)
    assert result['bars_built'] == 5
    assert result['no_trade_summary'] == 'Updated'


# --------------------------------------------------------------------------
# Dashboard API tests
# --------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    """Flask test client with a temporary database containing the full schema."""
    db_file = str(tmp_path / 'test_app.db')

    # Initialize the temp DB with the full schema
    schema_path = Path(__file__).parent.parent / 'database' / 'schema.sql'
    conn = sqlite3.connect(db_file)
    conn.executescript(schema_path.read_text())
    conn.close()

    app.config['TESTING'] = True
    with patch('dashboard.app.DATABASE_PATH', db_file):
        with app.test_client() as c:
            yield c


def test_journal_daily_endpoint_not_found(client):
    """GET /api/journal/daily/<date> returns 404 for missing entry."""
    resp = client.get('/api/journal/daily/2020-01-01')
    assert resp.status_code == 404
    data = resp.get_json()
    assert 'error' in data


def test_journal_daily_endpoint_invalid_date(client):
    """GET /api/journal/daily/<date> returns 400 for bad format."""
    resp = client.get('/api/journal/daily/not-a-date')
    assert resp.status_code == 400


def test_journal_calendar_includes_journal_data(client):
    """Calendar endpoint should include daily_journal data when available."""
    # This test just verifies the endpoint doesn't crash with the new code
    resp = client.get('/api/journal/calendar?month=2&year=2026')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'days' in data
    assert 'summary' in data


# --------------------------------------------------------------------------
# TradingBot rejection tracking (unit test)
# --------------------------------------------------------------------------

def test_record_rejection():
    """_record_rejection accumulates events in _journal_rejections list."""
    # Create a minimal mock TradingBot with just the tracking attributes
    from unittest.mock import MagicMock
    import pytz

    class FakeBot:
        def __init__(self):
            self.tz = pytz.timezone("America/New_York")
            self._journal_rejections = []
            self._journal_bars_built = 0
            self._journal_pulse_bars = 0
            self._journal_signals_evaluated = 0
            self._journal_trades_entered = 0
            self._journal_finalized = False

    # Patch the method onto the fake class
    from src.main import TradingBot
    bot = FakeBot()
    bot._record_rejection = TradingBot._record_rejection.__get__(bot, FakeBot)

    bot._record_rejection('daily_income', 'circuit_breaker', 'Daily P&L -$500 hit limit')
    bot._record_rejection('tag_n_turn', 'tnt_limit_reached', 'Already traded 1 TNT')

    assert len(bot._journal_rejections) == 2
    assert bot._journal_rejections[0]['strategy'] == 'daily_income'
    assert bot._journal_rejections[0]['reason'] == 'circuit_breaker'
    assert bot._journal_rejections[1]['strategy'] == 'tag_n_turn'


# --------------------------------------------------------------------------
# Calendar refresh tests
# --------------------------------------------------------------------------

@pytest.fixture
def client_with_db(tmp_path):
    """Flask test client + db path for inserting test data."""
    db_file = str(tmp_path / 'test_app.db')
    schema_path = Path(__file__).parent.parent / 'database' / 'schema.sql'
    conn = sqlite3.connect(db_file)
    conn.executescript(schema_path.read_text())
    conn.close()

    app.config['TESTING'] = True
    with patch('dashboard.app.DATABASE_PATH', db_file):
        with app.test_client() as c:
            yield c, db_file


def _insert_trade(db_file, trade_id, entry_time, status, pnl=None,
                  exit_time=None, credit=1.50, expiration=None):
    """Helper to insert a trade row for testing."""
    if expiration is None:
        expiration = entry_time[:10] + ' 16:00:00'
    conn = sqlite3.connect(db_file)
    conn.execute(
        """INSERT INTO trades (id, entry_time, exit_time, direction, status,
              strategy_type, short_strike, long_strike, spread_width,
              credit_received, entry_price, quantity, expiration, pnl)
           VALUES (?, ?, ?, 'put', ?, 'daily_income', 6000, 5990, 10, ?, ?, 1, ?, ?)""",
        (trade_id, entry_time, exit_time, status, credit, credit,
         expiration, pnl),
    )
    conn.commit()
    conn.close()


def test_calendar_returns_closed_trade_pnl(client_with_db):
    """Calendar endpoint returns correct P&L immediately after a trade closes in DB."""
    client, db_file = client_with_db
    today = datetime.now(ET).strftime('%Y-%m-%d')
    month = datetime.now(ET).month
    year = datetime.now(ET).year

    # Insert a closed trade with $960 P&L (credit >= MIN_CREDIT_THRESHOLD)
    _insert_trade(db_file, 'cal-test-1',
                  entry_time=f'{today} 10:15:00',
                  exit_time=f'{today} 11:30:00',
                  status='closed', pnl=960.0, credit=1.50)

    resp = client.get(f'/api/journal/calendar?month={month}&year={year}')
    assert resp.status_code == 200
    data = resp.get_json()
    day_info = data['days'].get(today)
    assert day_info is not None, f"No calendar entry for {today}"
    assert day_info['pnl'] == 960.0


@patch('dashboard.app.yahoo')
def test_api_today_includes_closed_today_count(mock_yahoo, client_with_db):
    """GET /api/today includes closed_today_count reflecting closed trades."""
    client, db_file = client_with_db
    today = datetime.now(ET).strftime('%Y-%m-%d')

    mock_yahoo.get_spx_quote.return_value = {'price': 6000.0, 'previous_close': 5990.0}
    mock_yahoo.get_intraday_bars.return_value = []

    # Use a future expiration so the active trade isn't classified as expired
    from datetime import timedelta
    tomorrow = (datetime.now(ET) + timedelta(days=1)).strftime('%Y-%m-%d')
    future_exp = f'{tomorrow} 16:00:00'

    # Insert one open and one closed trade for today
    _insert_trade(db_file, 'cnt-open-1',
                  entry_time=f'{today} 10:00:00',
                  status='active', expiration=future_exp)
    _insert_trade(db_file, 'cnt-closed-1',
                  entry_time=f'{today} 10:15:00',
                  exit_time=f'{today} 11:00:00',
                  status='closed', pnl=480.0)

    resp = client.get('/api/today')
    assert resp.status_code == 200
    data = resp.get_json()
    assert 'closed_today_count' in data
    assert data['closed_today_count'] == 1

    # Close the second trade
    conn = sqlite3.connect(db_file)
    conn.execute(
        "UPDATE trades SET status='closed', exit_time=?, pnl=480 WHERE id='cnt-open-1'",
        (f'{today} 12:00:00',),
    )
    conn.commit()
    conn.close()

    resp2 = client.get('/api/today')
    data2 = resp2.get_json()
    assert data2['closed_today_count'] == 2


def test_calendar_attributes_pnl_to_exit_date(client_with_db):
    """Closed trade P&L appears on the exit date, not the entry date."""
    client, db_file = client_with_db
    now = datetime.now(ET)
    month = now.month
    year = now.year

    # Use mid-month dates to avoid month boundary issues
    entry_date = f'{year:04d}-{month:02d}-15'
    exit_date = f'{year:04d}-{month:02d}-16'

    _insert_trade(db_file, 'cross-day-1',
                  entry_time=f'{entry_date} 14:00:00',
                  exit_time=f'{exit_date} 10:00:00',
                  status='closed', pnl=960.0)

    resp = client.get(f'/api/journal/calendar?month={month}&year={year}')
    assert resp.status_code == 200
    data = resp.get_json()

    # P&L should be on the exit date (when realized), not entry date
    exit_info = data['days'].get(exit_date)
    assert exit_info is not None, f"No calendar entry for exit date {exit_date}"
    assert exit_info['pnl'] == 960.0

    # Entry date should have no P&L entry (trade attributed to exit)
    entry_info = data['days'].get(entry_date)
    assert entry_info is None or entry_info['pnl'] == 0.0
