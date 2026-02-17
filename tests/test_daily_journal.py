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
from datetime import date
from unittest.mock import patch, MagicMock

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
def client():
    """Flask test client."""
    app.config['TESTING'] = True
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
