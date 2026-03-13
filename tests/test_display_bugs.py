"""
Regression tests for dashboard display bugs:
1. Calendar cell shows today's closed trades correctly (not as open)
2. window_closed strategy state renders grey LED, not red
"""

import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytz
import pytest

ET = pytz.timezone('US/Eastern')
TEMPLATE = (Path(__file__).parent.parent / "dashboard" / "templates" / "index.html").read_text(
    encoding="utf-8"
)


# ---------------------------------------------------------------------------
# BUG 1 — Calendar cell for today should show closed trades as closed
# ---------------------------------------------------------------------------

class TestCalendarClosedTrades:
    """Calendar must show trades closed today with closed status, not open."""

    @pytest.fixture
    def client_with_closed_trade(self, tmp_path):
        """Flask test client with a trade that closed today."""
        from dashboard.app import app, _open_db

        db_file = tmp_path / 'trades.db'
        schema = (Path(__file__).parent.parent / 'database' / 'schema.sql').read_text()
        conn = sqlite3.connect(str(db_file))
        conn.executescript(schema)

        today = datetime.now(ET).strftime('%Y-%m-%d')
        # Insert a closed trade for today
        conn.execute(
            "INSERT INTO trades (id, entry_time, exit_time, direction, status, "
            "short_strike, long_strike, spread_width, credit_received, entry_price, "
            "quantity, expiration, pnl, exit_price, exit_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ('t-closed', f'{today} 10:00:00', f'{today} 11:30:00', 'bearish',
             'closed', 6660.0, 6665.0, 5.0, 2.50, 2.50, 1,
             f'{today} 16:00:00', 210.0, 0.40, 'target'),
        )
        conn.commit()
        conn.close()

        app.config['TESTING'] = True
        with patch('dashboard.app.DATABASE_PATH', str(db_file)):
            with patch('dashboard.app.get_db_connection') as mock_conn:
                def _get_conn():
                    c = sqlite3.connect(str(db_file))
                    c.row_factory = sqlite3.Row
                    c.execute("PRAGMA journal_mode=WAL")
                    return c
                mock_conn.side_effect = _get_conn
                yield app.test_client(), db_file

    def test_calendar_shows_closed_trade_not_open(self, client_with_closed_trade):
        """A trade closed today should appear as a closed trade in the calendar."""
        client, db_file = client_with_closed_trade
        now = datetime.now(ET)

        resp = client.get(f'/api/journal/calendar?month={now.month}&year={now.year}')
        assert resp.status_code == 200
        data = resp.get_json()

        today_str = now.strftime('%Y-%m-%d')
        day = data.get('days', {}).get(today_str, {})

        # Should count as a closed trade, not open
        assert day.get('trades', 0) >= 1, "Closed trade should count in 'trades'"
        assert day.get('open_count', 0) == 0, "No open trades should be reported"

    def test_calendar_resolves_expired_active_trade(self, tmp_path):
        """An active trade past expiration should be resolved as closed in calendar."""
        from dashboard.app import app, _open_db

        db_file = tmp_path / 'trades2.db'
        schema = (Path(__file__).parent.parent / 'database' / 'schema.sql').read_text()
        conn = sqlite3.connect(str(db_file))
        conn.executescript(schema)

        today = datetime.now(ET).strftime('%Y-%m-%d')
        yesterday = (datetime.now(ET) - timedelta(days=1)).strftime('%Y-%m-%d')
        # Insert an active trade with past expiration (bot missed the close)
        conn.execute(
            "INSERT INTO trades (id, entry_time, direction, status, "
            "short_strike, long_strike, spread_width, credit_received, entry_price, "
            "quantity, expiration) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ('t-stale', f'{yesterday} 10:00:00', 'bearish',
             'active', 6660.0, 6665.0, 5.0, 2.50, 2.50, 1,
             f'{yesterday} 16:00:00'),
        )
        conn.commit()
        conn.close()

        app.config['TESTING'] = True
        now = datetime.now(ET)
        with patch('dashboard.app.DATABASE_PATH', str(db_file)):
            with patch('dashboard.app.get_db_connection') as mock_conn:
                def _get_conn():
                    c = sqlite3.connect(str(db_file))
                    c.row_factory = sqlite3.Row
                    c.execute("PRAGMA journal_mode=WAL")
                    return c
                mock_conn.side_effect = _get_conn
                client = app.test_client()
                resp = client.get(f'/api/journal/calendar?month={now.month}&year={now.year}')

        assert resp.status_code == 200
        data = resp.get_json()

        day = data.get('days', {}).get(yesterday, {})
        # Should have been resolved from active → expired
        assert day.get('open_count', 0) == 0, "Expired trade should not count as open"
        assert day.get('trades', 0) >= 1, "Expired trade should count as closed"


# ---------------------------------------------------------------------------
# BUG 2 — window_closed LED should be grey, not red
# ---------------------------------------------------------------------------

class TestWindowClosedLED:
    """window_closed strategy state must render with grey LED, not red."""

    def test_window_closed_css_exists(self):
        """CSS must have explicit window_closed LED styling."""
        assert 'data-state="window_closed"' in TEMPLATE, \
            "Expected explicit CSS rule for window_closed state"

    def test_window_closed_led_is_not_red(self):
        """window_closed LED must not use red gradient colors."""
        # Extract the window_closed CSS block
        m = re.search(
            r'\.status-light\[data-state="window_closed"\]\s*\.led\s*\{([^}]+)\}',
            TEMPLATE
        )
        assert m, "Could not find window_closed LED CSS rule"
        css_block = m.group(1)
        # Should NOT contain red colors (#dc2626, #f87171)
        assert '#dc2626' not in css_block, "window_closed should not use red"
        assert '#f87171' not in css_block, "window_closed should not use red"

    def test_window_closed_led_uses_grey(self):
        """window_closed LED should use grey tones."""
        m = re.search(
            r'\.status-light\[data-state="window_closed"\]\s*\.led\s*\{([^}]+)\}',
            TEMPLATE
        )
        assert m, "Could not find window_closed LED CSS rule"
        css_block = m.group(1)
        # Should contain grey colors (#6b7280 or #4b5563)
        assert '#6b7280' in css_block or '#4b5563' in css_block, \
            "window_closed LED should use grey gradient"

    def test_js_uses_window_closed_state_not_idle(self):
        """When market open but window passed, JS should set window_closed not idle."""
        # Find the "Window Closed" label assignment
        m = re.search(r"setLightState\([^)]*'([^']+)',\s*'Window Closed'\)", TEMPLATE)
        assert m, "Could not find setLightState call with 'Window Closed' label"
        state = m.group(1)
        assert state == 'window_closed', \
            f"Expected state 'window_closed' for 'Window Closed', got '{state}'"

    def test_idle_state_still_red(self):
        """Idle state should remain red (for active strategies not yet watching)."""
        m = re.search(
            r'\.status-light\[data-state="idle"\]\s*\.led\s*\{([^}]+)\}',
            TEMPLATE
        )
        assert m, "Could not find idle LED CSS rule"
        assert '#dc2626' in m.group(1), "idle should still use red"

    def test_window_closed_strat_state_class(self):
        """strat-state.window_closed should have grey styling like idle."""
        assert '.strat-state.window_closed' in TEMPLATE, \
            "Expected .strat-state.window_closed CSS class"
