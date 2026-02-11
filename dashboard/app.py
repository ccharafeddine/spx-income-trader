"""
SPX Income Trader - Web Dashboard

Read-only monitoring dashboard that displays bot status, signals,
trades, and market data. Runs as a separate process from the trading bot.
"""

import sys
import os
import re
import json
import logging
import sqlite3
import ctypes
import time
import yaml
from pathlib import Path
from datetime import datetime, date, timedelta
from flask import Flask, render_template, jsonify, request, redirect, url_for

# Project root setup
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Settings files
SETTINGS_FILE = PROJECT_ROOT / 'config' / 'strategy_params.yaml'
RUNTIME_SETTINGS_FILE = PROJECT_ROOT / 'database' / 'runtime_settings.json'
SETTINGS_CHANGED_FILE = PROJECT_ROOT / 'database' / '.settings_changed'

from config.settings import (
    BASE_DIR, DATABASE_PATH, LOG_FILE,
    DASHBOARD_PORT, DASHBOARD_HOST, STRATEGY_PARAMS,
    ETRADE_CONFIG, is_etrade_configured, save_etrade_credentials, clear_etrade_credentials,
    get_trading_mode, save_trading_mode
)
from src.data.yahoo_finance import YahooFinanceProvider
from src.utils.version import APP_VERSION

import pytz

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(32)
ET = pytz.timezone('US/Eastern')


# ---------------------------------------------------------------------------
# Middleware: Redirect to setup if not configured
# ---------------------------------------------------------------------------

@app.before_request
def check_setup_required():
    """Redirect to setup if E*TRADE credentials are not configured."""
    # Allow setup, static, and auth routes without credentials
    allowed_paths = ['/setup', '/static', '/api/setup/status', '/auth/etrade/']
    if any(request.path.startswith(p) for p in allowed_paths):
        return None

    # Check if credentials are configured
    if not is_etrade_configured():
        return redirect(url_for('setup'))

# Shared Yahoo Finance provider (has its own 60s cache)
yahoo = YahooFinanceProvider()

LOCKFILE = BASE_DIR / 'bot.lock'
SIGNAL_LOG = BASE_DIR / 'logs' / 'signals.json'
# Legacy path for backward compatibility with older dry-run logs
_LEGACY_SIGNAL_LOG = BASE_DIR / 'logs' / 'dry_run_signals.json'
STARTING_CAPITAL = 50000.00
MIN_CREDIT_THRESHOLD = 1.00  # Signals below this were before safeguards

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def check_bot_status():
    """Check if the trading bot process is running.

    In desktop mode the bot runs as a thread in the same process, so
    the lockfile/PID check is unreliable (PID is always alive).  Use
    the authoritative desktop status function when available.

    In standalone mode, falls back to lockfile + PID + heartbeat checks.
    """
    # Desktop mode: use the in-process bot status (authoritative)
    desktop_fn = getattr(app, '_desktop_get_bot_status', None)
    if desktop_fn is not None:
        return desktop_fn()

    if not LOCKFILE.exists():
        return {'running': False, 'pid': None, 'status': 'stopped'}

    try:
        pid = int(LOCKFILE.read_text().strip())
    except (ValueError, OSError):
        return {'running': False, 'pid': None, 'status': 'stopped'}

    # Check if process is alive (cross-platform)
    process_alive = False
    if sys.platform == 'win32':
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            process_alive = True
    else:
        try:
            os.kill(pid, 0)
            process_alive = True
        except PermissionError:
            process_alive = True
        except OSError:
            process_alive = False

    if not process_alive:
        return {'running': False, 'pid': pid, 'status': 'stale'}

    # Process is alive — check heartbeat freshness as secondary signal
    # Use different thresholds based on market hours
    heartbeat_warning = None
    try:
        now_et = datetime.now(ET)
        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        is_market_hours = (now_et.weekday() < 5 and market_open <= now_et <= market_close)

        # Thresholds: stricter during market hours, lenient when closed
        if is_market_hours:
            warning_threshold = 300   # 5 minutes
            stale_threshold = 600     # 10 minutes
        else:
            warning_threshold = 900   # 15 minutes
            stale_threshold = 1800    # 30 minutes

        log_data = parse_todays_log()
        if log_data.get('last_heartbeat'):
            hb_time_str = log_data['last_heartbeat']['time']
            hb_time = now_et.replace(
                hour=int(hb_time_str[:2]),
                minute=int(hb_time_str[3:5]),
                second=int(hb_time_str[6:8]),
                microsecond=0
            )
            secs = (now_et - hb_time).total_seconds()

            # Handle edge case: heartbeat from previous day (negative or very large diff)
            # If heartbeat appears to be in the future or >12 hours old, ignore it
            if secs < 0 or secs > 43200:
                pass  # Don't warn, likely a date boundary issue
            elif secs > stale_threshold:
                heartbeat_warning = f'Last heartbeat {int(secs // 60)}m ago (stale)'
            elif secs > warning_threshold:
                heartbeat_warning = f'Last heartbeat {int(secs // 60)}m ago'
    except Exception:
        pass

    result = {'running': True, 'pid': pid, 'status': 'running'}
    if heartbeat_warning:
        result['heartbeat_warning'] = heartbeat_warning
    return result


def is_market_open():
    """Check if US equity market is open and compute next bar boundary."""
    now = datetime.now(ET)
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)

    is_open = (now.weekday() < 5 and market_open <= now <= market_close)

    # Next 30-min bar boundary
    minutes = now.hour * 60 + now.minute
    next_bar_min = ((minutes // 30) + 1) * 30
    next_bar_hour = next_bar_min // 60
    next_bar_minute = next_bar_min % 60

    if next_bar_hour < 24:
        next_bar = now.replace(hour=next_bar_hour, minute=next_bar_minute, second=0, microsecond=0)
        secs_remaining = max(0, int((next_bar - now).total_seconds()))
        next_bar_str = next_bar.strftime('%H:%M')
    else:
        secs_remaining = 0
        next_bar_str = None

    return {
        'is_open': is_open,
        'time_et': now.strftime('%H:%M:%S'),
        'day': now.strftime('%A'),
        'next_bar': next_bar_str,
        'next_bar_secs': secs_remaining,
    }


def _is_during_market_hours(timestamp_str):
    """Check if a timestamp string falls within market hours (9:30-16:00 ET, weekdays)."""
    try:
        dt = datetime.fromisoformat(timestamp_str)
        if dt.tzinfo is None:
            dt = ET.localize(dt)
        else:
            dt = dt.astimezone(ET)
        if dt.weekday() >= 5:
            return False
        market_open = dt.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = dt.replace(hour=16, minute=0, second=0, microsecond=0)
        return market_open <= dt <= market_close
    except (ValueError, TypeError):
        return True  # If we can't parse, don't filter


def _parse_expiration(exp_str):
    """Parse an expiration string into a timezone-aware datetime.

    For date-only strings ('2026-02-07'), returns 4:00 PM ET (market close).
    Handles: ISO format, 'YYYY-MM-DD HH:MM:SS', 'YYYY-MM-DD', and
    timezone-aware strings like '2026-02-07 00:00:00-05:00'.
    """
    if not exp_str:
        return None
    try:
        exp_str = str(exp_str).strip()
        dt = None

        # Try fromisoformat first (handles 'T' separator AND timezone offsets)
        try:
            dt = datetime.fromisoformat(exp_str)
        except ValueError:
            pass

        # Try date-only: "2026-02-07"
        if dt is None and len(exp_str) >= 10:
            try:
                dt = datetime.strptime(exp_str[:10], '%Y-%m-%d')
            except ValueError:
                pass

        if dt is None:
            return None

        # Ensure timezone-aware (localize to ET if naive)
        if dt.tzinfo is None:
            dt = ET.localize(dt)

        # If time is midnight (date-only input), set to 4 PM ET (market close)
        if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
            dt = dt.replace(hour=16, minute=0, second=0)
        return dt
    except (ValueError, TypeError):
        return None


def parse_todays_log():
    """Parse today's entries from the trading log file."""
    today_prefix = datetime.now(ET).date().strftime('%Y-%m-%d')
    bars = []
    pulses = []
    last_heartbeat = None
    building_bar = None

    bar_re = re.compile(
        r'Bar\(time=(\d{2}:\d{2}), '
        r'O=([\d.]+), H=([\d.]+), L=([\d.]+), C=([\d.]+)\)'
    )
    pulse_re = re.compile(r'(BULLISH|BEARISH) PULSE detected at')
    heartbeat_re = re.compile(r'\[Heartbeat\] Loop #(\d+) at ([\d:]+) ET')
    building_re = re.compile(
        r'Building bar (\d{2}:\d{2}): '
        r'O=\$([\d.]+) H=\$([\d.]+) L=\$([\d.]+) C=\$([\d.]+) '
        r'\((\d+) ticks\)'
    )

    try:
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                if today_prefix not in line:
                    continue

                m = bar_re.search(line)
                if m:
                    bars.append({
                        'time': m.group(1),
                        'open': float(m.group(2)),
                        'high': float(m.group(3)),
                        'low': float(m.group(4)),
                        'close': float(m.group(5)),
                    })

                m = pulse_re.search(line)
                if m:
                    ts_match = re.search(r'(\d{2}:\d{2}:\d{2})', line)
                    pulses.append({
                        'direction': m.group(1),
                        'time': ts_match.group(1) if ts_match else '',
                        'line': line.strip(),
                    })

                m = heartbeat_re.search(line)
                if m:
                    last_heartbeat = {
                        'loop': int(m.group(1)),
                        'time': m.group(2),
                    }

                m = building_re.search(line)
                if m:
                    building_bar = {
                        'time': m.group(1),
                        'open': float(m.group(2)),
                        'high': float(m.group(3)),
                        'low': float(m.group(4)),
                        'close': float(m.group(5)),
                        'ticks': int(m.group(6)),
                    }
    except FileNotFoundError:
        pass

    return {
        'bars': bars,
        'pulses': pulses,
        'last_heartbeat': last_heartbeat,
        'building_bar': building_bar,
    }


def detect_trading_mode():
    """Return the configured trading mode from keyring / env."""
    mode = get_trading_mode()  # from config.settings (keyring -> env -> 'dry-run')
    if mode == 'live':
        return 'LIVE'
    if mode == 'dry-run':
        return 'DRY RUN'
    return 'UNKNOWN'


def get_db_connection():
    """Get a SQLite connection with WAL mode and timeout for concurrent access."""
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_journal_notes_table(conn):
    """Create journal_notes table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS journal_notes (
            trade_id TEXT PRIMARY KEY,
            rating INTEGER DEFAULT NULL,
            what_differently TEXT DEFAULT NULL,
            review_notes TEXT DEFAULT NULL,
            news_catalyst TEXT DEFAULT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (trade_id) REFERENCES trades(id)
        )
    """)
    conn.commit()


def load_signals():
    """Load signals from both the unified log and the legacy dry-run log."""
    signals = []
    for path in (SIGNAL_LOG, _LEGACY_SIGNAL_LOG):
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            signals.extend(data.get('signals', []))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    return signals


def get_all_trades(conn):
    """Get all trades from the DB."""
    rows = conn.execute("SELECT * FROM trades ORDER BY entry_time").fetchall()
    return [dict(r) for r in rows]


def classify_trades(conn, spx_price):
    """
    Classify all trades into open vs effectively-closed.

    Trades still marked 'active' in DB but past expiration are treated
    as expired (max profit if OTM, loss if ITM) since the bot didn't
    run at close to update them.
    """
    now = datetime.now(ET)
    all_trades = get_all_trades(conn)

    open_positions = []
    closed_trades = []

    for trade in all_trades:
        t = dict(trade)

        # Already closed in DB
        if t['status'] in ('closed', 'expired'):
            _annotate_trade(t)
            closed_trades.append(t)
            continue

        # Check if this active trade has expired
        exp_dt = _parse_expiration(t.get('expiration'))
        if exp_dt and now > exp_dt:
            # Expired but DB wasn't updated (bot wasn't running at close)
            # Calculate final P&L based on SPX price at expiration
            _resolve_expired_trade(t, spx_price)
            _annotate_trade(t)
            closed_trades.append(t)
        else:
            # Genuinely still open
            open_positions.append(t)

    return open_positions, closed_trades


def _resolve_expired_trade(trade, spx_price):
    """Calculate P&L for an expired trade and mark it as expired."""
    short_strike = trade['short_strike']
    long_strike = trade['long_strike']
    direction = trade['direction']
    credit = trade['credit_received']
    quantity = trade['quantity']
    spread_width = abs(long_strike - short_strike)

    if spx_price and short_strike:
        if direction == 'bullish':
            intrinsic = max(0, short_strike - spx_price)
        else:
            intrinsic = max(0, spx_price - short_strike)
        intrinsic = min(intrinsic, spread_width)
        pnl = (credit - intrinsic) * 100 * quantity
    else:
        # Can't determine — assume max profit (expired worthless)
        pnl = credit * 100 * quantity

    trade['status'] = 'expired'
    trade['exit_reason'] = 'Expired at close (dashboard-resolved)'
    trade['exit_price'] = 0.0
    trade['pnl'] = round(pnl, 2)
    trade['exit_time'] = trade.get('expiration', '')


def _annotate_trade(trade):
    """Add flags/notes to a trade for display purposes."""
    credit = trade.get('credit_received', 0)
    # Flag trades with credit below the min threshold (pre-safeguard)
    if credit < MIN_CREDIT_THRESHOLD:
        trade['flag'] = 'LOW_CREDIT'
        trade['flag_note'] = (
            f'Credit ${credit:.2f} < ${MIN_CREDIT_THRESHOLD:.2f} min — '
            'taken before safeguards were added'
        )
    else:
        trade['flag'] = None
        trade['flag_note'] = None


def compute_account(conn, spx_price):
    """Compute account balance, realized/unrealized P&L."""
    open_positions, closed_trades = classify_trades(conn, spx_price)
    now = datetime.now(ET)

    # Realized P&L from all closed/expired trades
    realized_pnl = sum(t.get('pnl') or 0 for t in closed_trades)

    # Today's realized P&L
    today_str = datetime.now(ET).date().isoformat()
    daily_realized = 0.0
    for t in closed_trades:
        exit_t = t.get('exit_time', '') or ''
        entry_t = t.get('entry_time', '') or ''
        if exit_t.startswith(today_str) or entry_t.startswith(today_str):
            daily_realized += t.get('pnl') or 0

    # Open positions and unrealized P&L
    unrealized_pnl = 0.0
    position_details = []

    for pos in open_positions:
        detail = dict(pos)
        short_strike = pos['short_strike']
        long_strike = pos['long_strike']
        direction = pos['direction']
        credit = pos['credit_received']
        quantity = pos['quantity']

        if spx_price and short_strike:
            if direction == 'bullish':
                intrinsic = max(0, short_strike - spx_price)
            else:
                intrinsic = max(0, spx_price - short_strike)

            spread_width = abs(long_strike - short_strike)
            intrinsic = min(intrinsic, spread_width)
            est_close_price = intrinsic
            est_pnl = (credit - est_close_price) * 100 * quantity
            unrealized_pnl += est_pnl

            distance = spx_price - short_strike
            if direction == 'bullish':
                distance_safe = distance
            else:
                distance_safe = -distance

            if distance_safe > 10:
                status = 'SAFE'
            elif distance_safe > 0:
                status = 'WARNING'
            else:
                status = 'DANGER'

            detail['est_pnl'] = round(est_pnl, 2)
            detail['distance_to_short'] = round(distance, 2)
            detail['position_status'] = status
        else:
            detail['est_pnl'] = 0
            detail['distance_to_short'] = 0
            detail['position_status'] = 'UNKNOWN'

        # Time remaining
        exp_dt = _parse_expiration(pos.get('expiration'))
        if exp_dt:
            remaining = exp_dt - now
            detail['time_remaining_secs'] = max(0, int(remaining.total_seconds()))
        else:
            detail['time_remaining_secs'] = 0

        _annotate_trade(detail)
        position_details.append(detail)

    current_balance = STARTING_CAPITAL + realized_pnl
    total_return_pct = ((current_balance + unrealized_pnl - STARTING_CAPITAL)
                        / STARTING_CAPITAL * 100)

    return {
        'starting_capital': STARTING_CAPITAL,
        'realized_pnl': round(realized_pnl, 2),
        'unrealized_pnl': round(unrealized_pnl, 2),
        'current_balance': round(current_balance, 2),
        'total_equity': round(current_balance + unrealized_pnl, 2),
        'total_return_pct': round(total_return_pct, 4),
        'daily_pnl': round(daily_realized, 2),
        'positions': position_details,
        'closed_trades': closed_trades,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    """Setup/onboarding page for E*TRADE credentials."""
    error = None
    success = None

    if request.method == 'POST':
        consumer_key = request.form.get('consumer_key', '').strip()
        consumer_secret = request.form.get('consumer_secret', '').strip()
        account_id = request.form.get('account_id', '').strip()
        environment = request.form.get('environment', 'sandbox')

        # Validate fields
        if not consumer_key or not consumer_secret or not account_id:
            error = 'All fields are required.'
        else:
            # Save to keychain
            sandbox = environment != 'production'
            if save_etrade_credentials(consumer_key, consumer_secret, account_id, sandbox):
                return redirect(url_for('index'))
            else:
                error = 'Failed to save credentials. Make sure keyring is installed.'

        masked_key = ('****' + consumer_key[-4:]) if len(consumer_key) > 4 else '****'
        return render_template('setup.html',
            error=error,
            consumer_key=masked_key,
            account_id=account_id,
            environment=environment,
            is_configured=is_etrade_configured()
        )

    # GET request
    return render_template('setup.html',
        is_configured=is_etrade_configured(),
        environment='sandbox' if ETRADE_CONFIG.get('sandbox', True) else 'production'
    )


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    """Settings page for managing credentials and viewing PDT status."""
    message = None
    message_type = None

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'update_credentials':
            consumer_key = request.form.get('consumer_key', '').strip()
            consumer_secret = request.form.get('consumer_secret', '').strip()
            account_id = request.form.get('account_id', '').strip()
            environment = request.form.get('environment', 'sandbox')

            # Only update if new values provided (not masked placeholders)
            if consumer_key and not consumer_key.startswith('****'):
                # Get existing values if partial update
                existing_key = ETRADE_CONFIG.get('consumer_key') or ''
                existing_secret = ETRADE_CONFIG.get('consumer_secret') or ''
                existing_account = ETRADE_CONFIG.get('account_id') or ''

                new_key = consumer_key if consumer_key else existing_key
                new_secret = consumer_secret if consumer_secret else existing_secret
                new_account = account_id if (account_id and not account_id.startswith('****')) else existing_account

                sandbox = environment != 'production'
                if save_etrade_credentials(new_key, new_secret, new_account, sandbox):
                    message = 'Credentials updated successfully.'
                    message_type = 'success'
                else:
                    message = 'Failed to save credentials.'
                    message_type = 'error'
            else:
                message = 'No changes detected.'
                message_type = 'success'

    # Prepare masked values for display
    key = ETRADE_CONFIG.get('consumer_key') or ''
    account = ETRADE_CONFIG.get('account_id') or ''
    masked_key = '****' + key[-4:] if len(key) > 4 else key
    masked_account = '****' + account[-4:] if len(account) > 4 else account

    return render_template('settings.html',
        message=message,
        message_type=message_type,
        masked_key=masked_key,
        masked_account=masked_account,
        is_production=not ETRADE_CONFIG.get('sandbox', True),
        credential_source=ETRADE_CONFIG.get('credential_source', 'none').title()
    )


@app.route('/api/setup/status')
def api_setup_status():
    """Check if credentials are configured."""
    return jsonify({'configured': is_etrade_configured()})


@app.route('/api/test-connection')
def api_test_connection():
    """Test E*TRADE API connection."""
    if not is_etrade_configured():
        return jsonify({'success': False, 'error': 'Credentials not configured'})

    try:
        # Try to get SPX quote via Yahoo (always works)
        spx = yahoo.get_spx_quote() or {}
        spx_price = spx.get('price')

        if spx_price:
            return jsonify({
                'success': True,
                'spx_price': spx_price,
                'message': 'Connection successful'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Could not fetch SPX price'
            })
    except Exception as e:
        logger.error(f"Test connection failed: {e}")
        return jsonify({'success': False, 'error': 'Connection test failed'})


@app.route('/api/clear-credentials', methods=['POST'])
def api_clear_credentials():
    """Clear stored credentials from keychain."""
    try:
        if clear_etrade_credentials():
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Failed to clear credentials'})
    except Exception as e:
        logger.error(f"Failed to clear credentials: {e}")
        return jsonify({'success': False, 'error': 'Failed to clear credentials'})


# ---------------------------------------------------------------------------
# E*TRADE OAuth flow (web-based, non-blocking)
# ---------------------------------------------------------------------------

# Temporary storage for pending OAuth request tokens (single-user app)
_pending_oauth = {}


def _get_etrade_auth():
    """Create an ETradeAuth instance from current config."""
    from src.brokers.etrade_auth import ETradeAuth
    return ETradeAuth(
        consumer_key=ETRADE_CONFIG.get('consumer_key'),
        consumer_secret=ETRADE_CONFIG.get('consumer_secret'),
        sandbox=ETRADE_CONFIG.get('sandbox', True),
        token_file=ETRADE_CONFIG.get('token_file'),
    )


@app.route('/auth/etrade/start')
def auth_etrade_start():
    """Initiate E*TRADE OAuth flow. Returns the authorization URL as JSON."""
    if not is_etrade_configured():
        return jsonify({'error': 'E*TRADE credentials not configured. Complete setup first.'}), 400

    try:
        auth = _get_etrade_auth()
        request_token, request_token_secret = auth._get_request_token()
        auth_url = auth._get_authorization_url(request_token)

        # Store pending tokens for the callback
        _pending_oauth['request_token'] = request_token
        _pending_oauth['request_token_secret'] = request_token_secret

        return jsonify({'auth_url': auth_url})
    except Exception as e:
        logger.error(f"OAuth start failed: {e}")
        return jsonify({'error': 'Failed to start OAuth flow. Check credentials and try again.'}), 500


@app.route('/auth/etrade/callback', methods=['POST'])
def auth_etrade_callback():
    """Complete OAuth token exchange with the verifier code."""
    data = request.get_json()
    if not data or not data.get('verifier'):
        return jsonify({'error': 'Verifier code is required'}), 400

    if not _pending_oauth.get('request_token'):
        return jsonify({'error': 'No pending OAuth flow. Start the flow first.'}), 400

    verifier = data['verifier'].strip()
    if not verifier:
        return jsonify({'error': 'Verifier code cannot be empty'}), 400

    try:
        auth = _get_etrade_auth()
        access_token, access_token_secret = auth._get_access_token(
            _pending_oauth['request_token'],
            _pending_oauth['request_token_secret'],
            verifier,
        )

        # Store tokens via the auth instance
        auth.access_token = access_token
        auth.access_token_secret = access_token_secret
        auth.token_timestamp = time.time()
        auth._save_tokens()

        # Clear pending state
        _pending_oauth.clear()

        return jsonify({
            'success': True,
            'message': 'E*TRADE connected successfully',
            'token_age_hours': 0.0,
        })
    except Exception as e:
        # Clear pending state on failure too
        _pending_oauth.clear()
        logger.error(f"OAuth token exchange failed: {e}")
        return jsonify({'error': 'Token exchange failed. Please try again.'}), 500


@app.route('/auth/etrade/status')
def auth_etrade_status():
    """Check whether valid OAuth tokens exist and their age."""
    if not is_etrade_configured():
        return jsonify({'connected': False, 'reason': 'Credentials not configured'})

    token_file = ETRADE_CONFIG.get('token_file')
    if not token_file or not os.path.exists(token_file):
        return jsonify({'connected': False, 'reason': 'No tokens found'})

    try:
        with open(token_file, 'r') as f:
            token_data = json.load(f)

        timestamp = token_data.get('timestamp', 0)
        age_hours = (time.time() - timestamp) / 3600

        # Tokens are invalid if older than 2 hours or environment mismatch
        sandbox = ETRADE_CONFIG.get('sandbox', True)
        if token_data.get('sandbox') != sandbox:
            return jsonify({
                'connected': False,
                'reason': 'Token environment mismatch',
            })

        if age_hours > 2.0:
            return jsonify({
                'connected': False,
                'reason': 'Tokens expired (older than 2 hours)',
                'token_age_hours': round(age_hours, 2),
            })

        return jsonify({
            'connected': True,
            'token_age_hours': round(age_hours, 2),
            'expires_in_minutes': max(0, round((2.0 - age_hours) * 60)),
            'needs_renewal': age_hours > 1.5,
        })

    except Exception as e:
        logger.error(f"Error reading token status: {e}")
        return jsonify({'connected': False, 'reason': 'Error reading token status'})


@app.route('/auth/etrade/disconnect', methods=['POST'])
def auth_etrade_disconnect():
    """Revoke and delete E*TRADE OAuth tokens."""
    token_file = ETRADE_CONFIG.get('token_file')

    # Try to revoke the token on E*TRADE's side first
    if token_file and os.path.exists(token_file):
        try:
            auth = _get_etrade_auth()
            auth._load_tokens()
            auth.revoke_token()
        except Exception:
            pass  # Best effort - still delete locally

        # Delete the token file
        try:
            os.remove(token_file)
        except OSError:
            pass

    return jsonify({'success': True, 'message': 'Disconnected from E*TRADE'})


def _try_renew_etrade_token():
    """Attempt background token renewal. Returns status dict."""
    token_file = ETRADE_CONFIG.get('token_file')
    if not token_file or not os.path.exists(token_file):
        return None

    try:
        with open(token_file, 'r') as f:
            token_data = json.load(f)

        timestamp = token_data.get('timestamp', 0)
        age_hours = (time.time() - timestamp) / 3600

        # Only renew if tokens are between 1.5 and 2.0 hours old
        if age_hours < 1.5 or age_hours > 2.0:
            if age_hours <= 1.5:
                return {'status': 'fresh', 'age_hours': round(age_hours, 2)}
            return {'status': 'expired', 'age_hours': round(age_hours, 2)}

        # Attempt renewal
        auth = _get_etrade_auth()
        auth.access_token = token_data['access_token']
        auth.access_token_secret = token_data['access_token_secret']
        auth.token_timestamp = timestamp

        if auth._renew_token():
            return {'status': 'renewed', 'age_hours': 0.0}
        else:
            return {'status': 'renewal_failed', 'age_hours': round(age_hours, 2)}

    except Exception as e:
        logger.error(f"Token renewal check failed: {e}")
        return {'status': 'error', 'error': 'Token renewal check failed'}


@app.route('/api/pdt/status')
def api_pdt_status():
    """Get PDT tracker status."""
    pdt_cfg = STRATEGY_PARAMS.get('pdt', {})

    # If PDT tracking is disabled, return minimal status
    if not pdt_cfg.get('pdt_protection', True):
        return jsonify({
            'enabled': False,
            'is_restricted': False,
            'day_trades_used': 0,
            'day_trades_remaining': 999,
            'max_day_trades': 3,
            'account_value': None,
            'threshold': 25000,
            'next_slot_frees_on': None,
        })

    # Try to load PDT status from tracker
    try:
        from src.core.pdt_tracker import PDTTracker

        tracker = PDTTracker(
            db_path=DATABASE_PATH,
            enabled=pdt_cfg.get('pdt_protection', True),
            threshold=pdt_cfg.get('pdt_threshold', 25000),
            max_day_trades=pdt_cfg.get('pdt_max_day_trades', 3),
            window_days=pdt_cfg.get('pdt_window_days', 5),
        )

        status = tracker.get_pdt_status()
        return jsonify(status)

    except Exception as e:
        logger.error(f"PDT status check failed: {e}")
        return jsonify({
            'enabled': True,
            'is_restricted': False,
            'day_trades_used': 0,
            'day_trades_remaining': 3,
            'max_day_trades': 3,
            'account_value': None,
            'threshold': 25000,
            'next_slot_frees_on': None,
            'error': 'Failed to load PDT status',
        })


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    """Status panel data: bot state, market, SPX quote, heartbeat, account."""
    bot = check_bot_status()
    market = is_market_open()
    log_data = parse_todays_log()
    mode = detect_trading_mode()

    # SPX quote
    spx = yahoo.get_spx_quote() or {}
    spx_price = spx.get('price')

    # Account data
    try:
        conn = get_db_connection()
        account = compute_account(conn, spx_price)
        conn.close()
    except Exception:
        account = {
            'starting_capital': STARTING_CAPITAL,
            'realized_pnl': 0, 'unrealized_pnl': 0,
            'current_balance': STARTING_CAPITAL, 'total_equity': STARTING_CAPITAL,
            'total_return_pct': 0, 'daily_pnl': 0, 'positions': [],
            'closed_trades': [],
        }

    # Today's summary (after market close)
    today_summary = None
    if not market['is_open']:
        today_str = datetime.now(ET).date().isoformat()
        today_closed = [
            t for t in account.get('closed_trades', [])
            if (t.get('entry_time', '') or '').startswith(today_str)
        ]
        flagged = [t for t in today_closed if t.get('flag') == 'LOW_CREDIT']
        valid = [t for t in today_closed if t.get('flag') != 'LOW_CREDIT']
        total_pnl = sum(t.get('pnl') or 0 for t in valid)
        wins = sum(1 for t in valid if (t.get('pnl') or 0) > 0)
        losses = sum(1 for t in valid if (t.get('pnl') or 0) < 0)

        today_summary = {
            'bars_built': len(log_data['bars']),
            'pulse_bars': len(log_data['pulses']),
            'signals_generated': len([
                s for s in load_signals()
                if s.get('timestamp', '').startswith(today_str)
                and _is_during_market_hours(s.get('timestamp', ''))
            ]),
            'trades_taken': len(today_closed),
            'valid_trades': len(valid),
            'flagged_trades': len(flagged),
            'wins': wins,
            'losses': losses,
            'total_pnl': round(total_pnl, 2),
            'result': 'WIN' if total_pnl > 0 else ('LOSS' if total_pnl < 0 else 'FLAT'),
        }

    # Strategy params summary
    strat = STRATEGY_PARAMS.get('strategy', {})
    tnt_cfg = STRATEGY_PARAMS.get('tag_n_turn', {})
    bnb_cfg = STRATEGY_PARAMS.get('bnb', {})
    orb_cfg = STRATEGY_PARAMS.get('orb', {})
    params_summary = {
        'pulse_threshold': strat.get('pulse_threshold', 10),
        'spread_width': strat.get('spread_width', 5),
        'profit_target_pct': strat.get('profit_target_pct', 80),
        'contracts': strat.get('contracts_per_trade', 5),
    }

    # Tag 'n Turn status (read from persistence file if enabled)
    tag_n_turn_status = {'enabled': tnt_cfg.get('enabled', False)}
    if tnt_cfg.get('enabled', False):
        try:
            tnt_path = BASE_DIR / 'database' / 'tag_n_turn_positions.json'
            if tnt_path.exists():
                import json
                with open(tnt_path, 'r') as f:
                    tnt_data = json.load(f)
                tag_n_turn_status = {
                    'enabled': True,
                    'state': tnt_data.get('state', 'IDLE'),
                    'tag_info': tnt_data.get('tag_info'),
                    'awaiting_breakout': {
                        'direction': tnt_data.get('pulse_info', {}).get('direction', '').upper() if tnt_data.get('pulse_info') else None,
                        'breakout_level': tnt_data.get('breakout_level'),
                    } if tnt_data.get('state') == 'AWAITING_BREAKOUT' else None,
                    'active_position': tnt_data.get('active_position'),
                }
        except Exception:
            pass

    # B&B status (read from persistence file if enabled)
    bnb_status = {'enabled': bnb_cfg.get('enabled', False)}
    if bnb_cfg.get('enabled', False):
        try:
            bnb_path = BASE_DIR / 'database' / 'bnb_signals.json'
            if bnb_path.exists():
                import json
                with open(bnb_path, 'r') as f:
                    bnb_data = json.load(f)
                bnb_status = {
                    'enabled': True,
                    'pending_signal': bnb_data.get('pending_signal'),
                    'active_signal': bnb_data.get('active_signal'),
                    'position_open': bnb_data.get('position_open', False),
                }
        except Exception:
            pass

    # ORB status (read from persistence file if enabled)
    orb_status = {'enabled': orb_cfg.get('enabled', False)}
    if orb_cfg.get('enabled', False):
        try:
            orb_path = BASE_DIR / 'database' / 'orb_state.json'
            if orb_path.exists():
                import json
                with open(orb_path, 'r') as f:
                    orb_data = json.load(f)
                orb_status = {
                    'enabled': True,
                    'opening_range': orb_data.get('opening_range'),
                    'triggered_today': orb_data.get('triggered_today', False),
                    'position_open': orb_data.get('position_open', False),
                }
        except Exception:
            pass

    # Portfolio status (read from persistence file)
    portfolio_status = None
    try:
        portfolio_path = BASE_DIR / 'database' / 'portfolio_state.json'
        if portfolio_path.exists():
            import json
            with open(portfolio_path, 'r') as f:
                port_data = json.load(f)
            portfolio_cfg = STRATEGY_PARAMS.get('portfolio', {})
            account_size = portfolio_cfg.get('account_size', 50000)
            max_daily_risk = account_size * (portfolio_cfg.get('max_daily_risk_pct', 5.0) / 100)
            max_daily_loss_pct = portfolio_cfg.get('max_daily_loss_pct', 2.0)
            max_daily_loss_dollars = account_size * (max_daily_loss_pct / 100)
            portfolio_status = {
                'active_positions': len(port_data.get('active_positions', {})),
                'max_total_positions': portfolio_cfg.get('max_total_positions', 2),
                '0dte_positions': sum(1 for p in port_data.get('active_positions', {}).values() if p.get('is_0dte', True)),
                'max_0dte_positions': portfolio_cfg.get('max_0dte_positions', 1),
                'daily_risk_used': port_data.get('daily_risk_used', 0),
                'max_daily_risk': max_daily_risk,
                'daily_realized_pnl': port_data.get('daily_realized_pnl', port_data.get('daily_pnl', 0)),
                'max_daily_loss_pct': max_daily_loss_pct,
                'max_daily_loss_dollars': max_daily_loss_dollars,
                'circuit_breaker': port_data.get('circuit_breaker_triggered', False),
                'dte0_trades_today': port_data.get('dte0_trades_today', 0),
                'tnt_trades_today': port_data.get('tnt_trades_today', 0),
            }
    except Exception:
        pass

    # E*TRADE token status + auto-renewal
    etrade_token = _try_renew_etrade_token()

    return jsonify({
        'version': APP_VERSION,
        'bot': bot,
        'mode': mode,
        'market': market,
        'spx': {
            'price': spx_price,
            'change': spx.get('change'),
            'change_pct': spx.get('change_pct'),
        },
        'heartbeat': log_data['last_heartbeat'],
        'strategy': params_summary,
        'account': account,
        'today_summary': today_summary,
        'tag_n_turn': tag_n_turn_status,
        'bnb': bnb_status,
        'orb': orb_status,
        'portfolio': portfolio_status,
        'etrade_token': etrade_token,
    })


@app.route('/api/today')
def api_today():
    """Today's activity: bars, pulses, signals, trades, intraday chart data."""
    log_data = parse_todays_log()
    today_str = datetime.now(ET).date().strftime('%Y-%m-%d')

    # Yahoo intraday bars for candlestick chart
    intraday = yahoo.get_intraday_bars('30m', '1d') or []
    chart_bars = []
    for b in intraday:
        ts = b['timestamp']
        if hasattr(ts, 'isoformat'):
            ts = ts.isoformat()
        chart_bars.append({
            'timestamp': str(ts),
            'open': b['open'],
            'high': b['high'],
            'low': b['low'],
            'close': b['close'],
        })

    # Today's signals (only those during market hours)
    all_signals = load_signals()
    today_signals = [
        s for s in all_signals
        if s.get('timestamp', '').startswith(today_str)
        and _is_during_market_hours(s.get('timestamp', ''))
    ]

    # Get trade classification and SPX data
    spx = yahoo.get_spx_quote() or {}
    spx_price = spx.get('price')
    prev_close = spx.get('previous_close')
    today_trades = []
    today_pnl = 0.0
    open_positions = []
    try:
        conn = get_db_connection()
        open_pos, closed = classify_trades(conn, spx_price)

        # Today's trades = all trades entered today
        for t in closed:
            if (t.get('entry_time', '') or '').startswith(today_str):
                today_trades.append(t)
                today_pnl += t.get('pnl') or 0
        for t in open_pos:
            if (t.get('entry_time', '') or '').startswith(today_str):
                today_trades.append(t)

        # Only truly open positions for strike lines
        # Add current SPX price and time remaining for frontend
        now = datetime.now(ET)
        for p in open_pos:
            p['current_price'] = spx_price
            exp_dt = _parse_expiration(p.get('expiration'))
            if exp_dt:
                remaining = exp_dt - now
                p['time_remaining_secs'] = max(0, int(remaining.total_seconds()))
            else:
                p['time_remaining_secs'] = 0
        open_positions = open_pos
        conn.close()
    except Exception:
        pass

    # Pulse bar times for highlighting on chart
    pulse_times = set()
    for p in log_data['pulses']:
        pt = p.get('time', '')[:5]
        pulse_times.add(pt)

    return jsonify({
        'log_bars': log_data['bars'],
        'chart_bars': chart_bars,
        'pulses': log_data['pulses'],
        'pulse_times': list(pulse_times),
        'signals': today_signals,
        'trades': today_trades,
        'today_pnl': today_pnl,
        'building_bar': log_data['building_bar'],
        'open_positions': open_positions,
        'prev_close': prev_close,
    })


@app.route('/api/signals')
def api_signals():
    """Signal log with optional days filter. Excludes signals outside market hours."""
    days = request.args.get('days', 30, type=int)
    cutoff = (datetime.now(ET).date() - timedelta(days=days)).isoformat()

    all_signals = load_signals()
    filtered = [
        s for s in all_signals
        if s.get('timestamp', '') >= cutoff
        and _is_during_market_hours(s.get('timestamp', ''))
    ]
    filtered.sort(key=lambda s: s.get('timestamp', ''), reverse=True)

    return jsonify({'signals': filtered, 'days': days})


@app.route('/api/history')
def api_history():
    """Historical: daily_stats, aggregate metrics, trade history with running total."""
    days = request.args.get('days', 30, type=int)
    cutoff = (datetime.now(ET).date() - timedelta(days=days)).isoformat()

    daily_stats = []
    trades = []
    aggregate = {
        'total_trades': 0, 'wins': 0, 'losses': 0,
        'total_pnl': 0.0, 'win_rate': 0.0,
        'avg_win': 0.0, 'avg_loss': 0.0,
        'max_win': 0.0, 'max_loss': 0.0,
    }

    try:
        conn = get_db_connection()

        # Daily stats from table
        rows = conn.execute(
            "SELECT * FROM daily_stats WHERE date >= ? ORDER BY date DESC",
            (cutoff,)
        ).fetchall()
        for r in rows:
            daily_stats.append(dict(r))

        # Get all closed trades (including dashboard-resolved expired ones)
        spx = yahoo.get_spx_quote() or {}
        spx_price = spx.get('price')
        _, all_closed = classify_trades(conn, spx_price)

        # Filter by cutoff
        closed = [
            t for t in all_closed
            if (t.get('entry_time', '') or '') >= cutoff
        ]

        # Compute aggregate from trade-level data (more accurate than daily_stats)
        if closed:
            valid = [t for t in closed if t.get('flag') != 'LOW_CREDIT']
            aggregate['total_trades'] = len(valid)
            wins_list = [t['pnl'] for t in valid if (t.get('pnl') or 0) > 0]
            losses_list = [t['pnl'] for t in valid if (t.get('pnl') or 0) < 0]
            aggregate['wins'] = len(wins_list)
            aggregate['losses'] = len(losses_list)
            aggregate['total_pnl'] = round(sum(t.get('pnl') or 0 for t in valid), 2)
            if aggregate['total_trades'] > 0:
                aggregate['win_rate'] = round(
                    aggregate['wins'] / aggregate['total_trades'] * 100, 1
                )
            if wins_list:
                aggregate['max_win'] = round(max(wins_list), 2)
                aggregate['avg_win'] = round(sum(wins_list) / len(wins_list), 2)
            if losses_list:
                aggregate['max_loss'] = round(min(losses_list), 2)
                aggregate['avg_loss'] = round(sum(losses_list) / len(losses_list), 2)

        # Compute running total (oldest first, then reverse for display)
        closed_chrono = sorted(closed, key=lambda t: t.get('entry_time', ''))
        running = 0.0
        for t in closed_chrono:
            pnl = t.get('pnl') or 0
            running += pnl
            t['running_total'] = round(running, 2)
        trades = list(reversed(closed_chrono))

        conn.close()
    except Exception:
        pass

    return jsonify({
        'daily_stats': daily_stats,
        'aggregate': aggregate,
        'trades': trades,
        'days': days,
    })


@app.route('/api/journal')
def api_journal():
    """Trade journal with entry reasons, exit analysis, and market context."""
    days = request.args.get('days', 90, type=int)
    direction_filter = request.args.get('direction', '')
    outcome_filter = request.args.get('outcome', '')
    flagged_only = request.args.get('flagged', '') == 'true'
    strategy_filter = request.args.get('strategy', '')
    day_type_filter = request.args.get('day_type', '')
    exit_reason_filter = request.args.get('exit_reason', '')

    cutoff = (datetime.now(ET).date() - timedelta(days=days)).isoformat()

    # Get SPX price and all trades
    spx = yahoo.get_spx_quote() or {}
    spx_price = spx.get('price')

    try:
        conn = get_db_connection()
        _ensure_journal_notes_table(conn)
        _, all_closed = classify_trades(conn, spx_price)
        open_pos, _ = classify_trades(conn, spx_price)
    except Exception:
        conn = None
        all_closed = []
        open_pos = []

    # Include open positions as well
    all_trades = all_closed + open_pos

    # Filter by cutoff date
    all_trades = [
        t for t in all_trades
        if (t.get('entry_time', '') or '') >= cutoff
    ]

    # Load signals for correlation
    all_signals = load_signals()

    # Build log cache: parse log bars for each unique trade date
    log_cache = {}
    for trade in all_trades:
        entry_time = trade.get('entry_time', '')
        trade_date = entry_time[:10] if entry_time else None
        if trade_date and trade_date not in log_cache:
            log_cache[trade_date] = _parse_log_bars_for_date(trade_date)

    # Strategy params
    strat = STRATEGY_PARAMS.get('strategy', {})
    timing = STRATEGY_PARAMS.get('timing', {})
    risk = STRATEGY_PARAMS.get('risk', {})
    portfolio = STRATEGY_PARAMS.get('portfolio', {})

    journal_entries = []
    total_duration_hours = 0
    total_pct_max = 0
    duration_count = 0
    pct_max_count = 0
    exit_reasons = {}

    for trade in all_trades:
        # Correlate with signal
        signal = _correlate_trade_with_signal(trade, all_signals)

        # Build entry reasons
        entry_reasons = _reconstruct_entry_reasons(trade, signal, strat, timing, risk, portfolio)

        # Exit analysis
        exit_analysis = _compute_exit_analysis(trade)

        # Market context (expanded)
        market_context = _get_market_context(trade, signal=signal, log_cache=log_cache)

        # Pulse bar details
        pulse_bar = _parse_pulse_bar_from_log(trade, log_cache, signal=signal)

        # Strike analysis
        strike_analysis = _build_strike_analysis(trade, signal)

        # Journal notes
        journal_notes = _get_journal_notes(conn, trade.get('id')) if conn else {
            'rating': None, 'what_differently': None,
            'review_notes': None, 'news_catalyst': None,
        }

        # Compute totals for aggregation
        spread_width = trade.get('spread_width') or abs(
            (trade.get('long_strike') or 0) - (trade.get('short_strike') or 0)
        )
        credit = trade.get('credit_received') or 0
        qty = trade.get('quantity') or 1
        max_profit_total = credit * 100 * qty
        max_risk_total = (spread_width - credit) * 100 * qty if spread_width > credit else 0
        risk_reward = round(max_profit_total / max_risk_total, 2) if max_risk_total > 0 else 0

        # Direction display
        dir_raw = (trade.get('direction') or '').lower()
        dir_display = 'CALL CREDIT' if dir_raw == 'bearish' else 'PUT CREDIT' if dir_raw == 'bullish' else dir_raw.upper()

        # Get strategy type (default to daily_income for legacy trades)
        strategy_type = trade.get('strategy_type') or 'daily_income'

        entry = {
            'id': trade.get('id'),
            'entry_time': trade.get('entry_time'),
            'exit_time': trade.get('exit_time'),
            'direction': dir_display,
            'direction_raw': dir_raw,
            'status': trade.get('status'),
            'strategy_type': strategy_type,
            'short_strike': trade.get('short_strike'),
            'long_strike': trade.get('long_strike'),
            'spread_width': spread_width,
            'credit_received': credit,
            'total_credit': round(max_profit_total, 2),
            'quantity': qty,
            'max_profit_total': round(max_profit_total, 2),
            'max_risk_total': round(max_risk_total, 2),
            'risk_reward_ratio': risk_reward,
            'expiration': trade.get('expiration'),
            'entry_reasons': entry_reasons,
            'exit_analysis': exit_analysis,
            'market_context': market_context,
            'pulse_bar': pulse_bar,
            'strike_analysis': strike_analysis,
            'journal_notes': journal_notes,
            'signal': signal,
            'flag': trade.get('flag'),
            'flag_note': trade.get('flag_note'),
            'notes': trade.get('notes'),
            # Enhanced context fields
            'spx_at_entry': trade.get('spx_at_entry') or trade.get('underlying_price_at_entry'),
            'spx_at_exit': trade.get('spx_at_exit'),
            'vix_at_entry': trade.get('vix_at_entry'),
            'day_open': trade.get('day_open'),
            'gap_pct': trade.get('gap_pct'),
            'intraday_move_at_entry': trade.get('intraday_move_at_entry'),
            'profit_captured_pct': trade.get('profit_captured_pct'),
            'time_in_trade_minutes': trade.get('time_in_trade_minutes'),
            'day_type': trade.get('day_type'),
            'daily_move_pct': trade.get('daily_move_pct'),
        }

        # Apply filters
        if direction_filter and direction_filter.lower() != dir_raw:
            continue
        if outcome_filter:
            if outcome_filter == 'win' and exit_analysis['outcome'] != 'win':
                continue
            if outcome_filter == 'loss' and exit_analysis['outcome'] != 'loss':
                continue
            if outcome_filter == 'open' and exit_analysis['outcome'] != 'open':
                continue
        if flagged_only and not trade.get('flag'):
            continue
        if strategy_filter and strategy_type != strategy_filter:
            continue
        if day_type_filter and trade.get('day_type') != day_type_filter:
            continue
        if exit_reason_filter and exit_analysis.get('exit_reason') != exit_reason_filter:
            continue

        journal_entries.append(entry)

        # Aggregate stats (exclude flagged)
        if not trade.get('flag'):
            if exit_analysis['duration_hours'] is not None:
                total_duration_hours += exit_analysis['duration_hours']
                duration_count += 1
            if exit_analysis['pct_of_max_profit'] is not None:
                total_pct_max += exit_analysis['pct_of_max_profit']
                pct_max_count += 1
            reason = exit_analysis.get('exit_reason') or 'unknown'
            exit_reasons[reason] = exit_reasons.get(reason, 0) + 1

    # Close DB connection
    if conn:
        conn.close()

    # Sort newest first
    journal_entries.sort(key=lambda e: e.get('entry_time') or '', reverse=True)

    # Compute stats (valid = non-flagged)
    valid = [e for e in journal_entries if not e.get('flag')]
    wins = [e for e in valid if e['exit_analysis']['outcome'] == 'win']
    losses = [e for e in valid if e['exit_analysis']['outcome'] == 'loss']
    total_pnl = sum((e['exit_analysis']['pnl'] or 0) for e in valid)
    most_common_exit = max(exit_reasons, key=exit_reasons.get) if exit_reasons else None

    stats = {
        'total_entries': len(journal_entries),
        'valid_entries': len(valid),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': round(len(wins) / len(valid) * 100, 1) if valid else 0,
        'avg_duration_hours': round(total_duration_hours / duration_count, 1) if duration_count else None,
        'avg_pct_max_profit': round(total_pct_max / pct_max_count, 1) if pct_max_count else None,
        'total_pnl': round(total_pnl, 2),
        'most_common_exit_reason': most_common_exit,
    }

    # Strategy breakdown stats
    strategy_breakdown = {}
    for strat_type in ['daily_income', 'tag_n_turn', 'bnb', 'orb']:
        strat_trades = [e for e in valid if e.get('strategy_type') == strat_type]
        strat_wins = [e for e in strat_trades if e['exit_analysis']['outcome'] == 'win']
        strat_pnl = sum((e['exit_analysis']['pnl'] or 0) for e in strat_trades)
        strategy_breakdown[strat_type] = {
            'count': len(strat_trades),
            'wins': len(strat_wins),
            'win_rate': round(len(strat_wins) / len(strat_trades) * 100, 1) if strat_trades else 0,
            'total_pnl': round(strat_pnl, 2),
        }

    # Calculate portfolio loss limit from percentage
    account_size = portfolio.get('account_size', 50000)
    max_daily_loss_pct = portfolio.get('max_daily_loss_pct', 2.0)
    max_daily_loss_dollars = account_size * (max_daily_loss_pct / 100)

    strategy_params = {
        'pulse_threshold': strat.get('pulse_threshold', 10),
        'spread_width': strat.get('spread_width', 5),
        'profit_target_pct': strat.get('profit_target_pct', 80),
        'contracts': strat.get('contracts_per_trade', 5),
        'min_credit': MIN_CREDIT_THRESHOLD,
        'morning_start': timing.get('morning_start', '09:30'),
        'morning_end': timing.get('morning_end', '11:30'),
        'max_daily_loss_pct': max_daily_loss_pct,
        'max_daily_loss_dollars': max_daily_loss_dollars,
    }

    return jsonify({
        'journal': journal_entries,
        'stats': stats,
        'strategy_params': strategy_params,
        'strategy_breakdown': strategy_breakdown,
    })


@app.route('/api/journal/notes', methods=['POST'])
def api_journal_save_notes():
    """Save or update journal notes for a trade."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'JSON body required'}), 400

    trade_id = data.get('trade_id')
    if not trade_id:
        return jsonify({'error': 'trade_id is required'}), 400

    rating = data.get('rating')
    if rating is not None:
        try:
            rating = int(rating)
            if rating < 1 or rating > 5:
                return jsonify({'error': 'rating must be 1-5'}), 400
        except (ValueError, TypeError):
            return jsonify({'error': 'rating must be an integer 1-5'}), 400

    what_differently = data.get('what_differently')
    review_notes = data.get('review_notes')
    news_catalyst = data.get('news_catalyst')

    try:
        conn = get_db_connection()
        _ensure_journal_notes_table(conn)
        conn.execute(
            """INSERT OR REPLACE INTO journal_notes
               (trade_id, rating, what_differently, review_notes, news_catalyst, updated_at)
               VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (trade_id, rating, what_differently, review_notes, news_catalyst)
        )
        conn.commit()
        conn.close()
        return jsonify({'status': 'ok', 'trade_id': trade_id})
    except Exception as e:
        logger.error(f"Failed to save journal notes: {e}")
        return jsonify({'error': 'Failed to save journal notes'}), 500


def _correlate_trade_with_signal(trade, signals):
    """Match a trade to its originating signal via entry_order_id or timestamps."""
    entry_order_id = trade.get('entry_order_id')

    # Try matching by order ID first
    if entry_order_id:
        for s in signals:
            if s.get('order_id') == entry_order_id:
                return s

    # Fallback: match by timestamp proximity (within 60 seconds)
    entry_time = trade.get('entry_time', '')
    if entry_time:
        try:
            trade_dt = datetime.fromisoformat(entry_time)
            for s in signals:
                sig_ts = s.get('timestamp', '')
                if sig_ts:
                    sig_dt = datetime.fromisoformat(sig_ts)
                    if abs((trade_dt - sig_dt).total_seconds()) < 60:
                        return s
        except (ValueError, TypeError):
            pass

    return None


def _reconstruct_entry_reasons(trade, signal, strat, timing, risk, portfolio):
    """Build a list of entry reason checkboxes for a trade."""
    reasons = []

    # 1. Pulse bar detected
    setup_bar = trade.get('setup_bar_time', '')
    direction = (trade.get('direction') or '').upper()
    pulse_detail = f"{direction} pulse"
    if setup_bar:
        pulse_detail += f" at {setup_bar}"
    reasons.append({
        'id': 'pulse_detected',
        'label': 'Pulse bar detected',
        'met': True,  # Trade exists, so pulse was detected
        'detail': pulse_detail,
    })

    # 2. Close % met threshold
    threshold = strat.get('pulse_threshold', 10)
    reasons.append({
        'id': 'close_pct_threshold',
        'label': f'Close % met {threshold}% threshold',
        'met': True,  # Enforced by PulseBarDetector
        'detail': f'Threshold: {threshold}%',
    })

    # 3. Within setup window
    morning_start = timing.get('morning_start', '09:30')
    morning_end = timing.get('morning_end', '11:30')
    entry_time = trade.get('entry_time', '')
    in_window = True
    entry_hhmm = ''
    if entry_time:
        try:
            entry_dt = datetime.fromisoformat(entry_time)
            if entry_dt.tzinfo:
                entry_dt = entry_dt.astimezone(ET)
            entry_hhmm = entry_dt.strftime('%H:%M')
            in_window = morning_start <= entry_hhmm <= morning_end
        except (ValueError, TypeError):
            pass
    reasons.append({
        'id': 'setup_window',
        'label': f'Within {morning_start}-{morning_end} ET window',
        'met': in_window,
        'detail': f'Entry at {entry_hhmm} ET' if entry_hhmm else 'Entry time unknown',
    })

    # 4. Credit >= minimum
    credit = trade.get('credit_received', 0)
    min_credit = MIN_CREDIT_THRESHOLD
    credit_met = credit >= min_credit
    reasons.append({
        'id': 'min_credit',
        'label': f'Credit >= ${min_credit:.2f} minimum',
        'met': credit_met,
        'detail': f'Credit: ${credit:.2f}',
    })

    # 5. Daily trade limit not reached
    reasons.append({
        'id': 'daily_trade_limit',
        'label': 'Daily trade limit not reached',
        'met': True,  # Trade exists, so limit wasn't reached
        'detail': '0DTE limit: 1 per day (shared DI/ORB/B&B)',
    })

    # 6. Daily loss limit (portfolio-level, percentage-based)
    account_size = portfolio.get('account_size', 50000)
    loss_pct = portfolio.get('max_daily_loss_pct', 2.0)
    loss_dollars = account_size * (loss_pct / 100)
    reasons.append({
        'id': 'daily_loss_limit',
        'label': f'Daily loss < {loss_pct}% (${loss_dollars:,.0f}) limit',
        'met': True,  # Trade exists, so loss limit wasn't breached
        'detail': f'Circuit breaker: {loss_pct}% of ${account_size:,.0f} = ${loss_dollars:,.0f}',
    })

    # 7. No existing open position
    reasons.append({
        'id': 'no_open_position',
        'label': 'No existing open position',
        'met': True,  # Trade exists, so no conflict
        'detail': 'Position was clear at entry',
    })

    # 8. SPX price context
    spx_at_entry = trade.get('underlying_price_at_entry')
    short_strike = trade.get('short_strike')
    if spx_at_entry and short_strike:
        distance = round(spx_at_entry - short_strike, 1)
        above_below = 'above' if distance > 0 else 'below'
        reasons.append({
            'id': 'spx_context',
            'label': 'SPX price context at entry',
            'met': True,
            'detail': f'SPX at ${spx_at_entry:,.2f}, short {short_strike} ({abs(distance)}pts {above_below})',
        })
    else:
        reasons.append({
            'id': 'spx_context',
            'label': 'SPX price context at entry',
            'met': True,
            'detail': 'Price data unavailable',
        })

    return reasons


def _compute_exit_analysis(trade):
    """Compute exit analysis fields for a trade."""
    pnl = trade.get('pnl')
    exit_reason = trade.get('exit_reason')
    entry_time = trade.get('entry_time', '')
    exit_time = trade.get('exit_time', '')
    status = trade.get('status', '')

    # Duration
    duration_display = None
    duration_hours = None
    if entry_time and exit_time:
        try:
            entry_dt = datetime.fromisoformat(entry_time)
            exit_dt = datetime.fromisoformat(exit_time)
            delta = exit_dt - entry_dt
            total_secs = int(delta.total_seconds())
            if total_secs >= 0:
                hours = total_secs // 3600
                minutes = (total_secs % 3600) // 60
                duration_display = f'{hours}h {minutes}m'
                duration_hours = round(total_secs / 3600, 1)
        except (ValueError, TypeError):
            pass

    # % of max profit captured
    pct_of_max_profit = None
    credit = trade.get('credit_received', 0)
    qty = trade.get('quantity', 1)
    max_profit = credit * 100 * qty
    if pnl is not None and max_profit > 0:
        pct_of_max_profit = round(pnl / max_profit * 100, 1)

    # Outcome
    if status == 'active':
        outcome = 'open'
    elif pnl is not None:
        if pnl > 0:
            outcome = 'win'
        elif pnl < 0:
            outcome = 'loss'
        else:
            outcome = 'breakeven'
    else:
        outcome = 'open'

    return {
        'exit_reason': exit_reason,
        'duration_display': duration_display,
        'duration_hours': duration_hours,
        'pnl': pnl,
        'pct_of_max_profit': pct_of_max_profit,
        'outcome': outcome,
    }


def _parse_log_bars_for_date(date_str):
    """Parse log file for completed bars and pulse detections on a given date.

    Args:
        date_str: 'YYYY-MM-DD' date string

    Returns:
        list of dicts with bar OHLC data and pulse info for that date.
    """
    bars = []
    bar_re = re.compile(
        r'Bar\(time=(\d{2}:\d{2}), '
        r'O=([\d.]+), H=([\d.]+), L=([\d.]+), C=([\d.]+)\)'
    )
    pulse_re = re.compile(r'(BULLISH|BEARISH) PULSE detected at')

    try:
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                if date_str not in line:
                    continue

                m = bar_re.search(line)
                if m:
                    bars.append({
                        'time': m.group(1),
                        'open': float(m.group(2)),
                        'high': float(m.group(3)),
                        'low': float(m.group(4)),
                        'close': float(m.group(5)),
                        'is_pulse': False,
                    })

                m = pulse_re.search(line)
                if m and bars:
                    # Mark the most recent bar as a pulse bar
                    bars[-1]['is_pulse'] = True
                    bars[-1]['pulse_direction'] = m.group(1)
    except FileNotFoundError:
        pass

    return bars


def _parse_pulse_bar_from_log(trade, log_cache, signal=None):
    """Find the pulse bar that triggered this trade.

    Primary source: pulse_bar dict embedded in the signal JSON (captured at trade time).
    Fallback: match setup_bar_time against parsed log bars.

    Returns dict with bar details or None.
    """
    # Primary: use signal-embedded pulse bar data (exact OHLC captured at signal time)
    if signal and signal.get('pulse_bar'):
        pb = signal['pulse_bar']
        return {
            'time': pb.get('time'),
            'open': pb.get('open'),
            'high': pb.get('high'),
            'low': pb.get('low'),
            'close': pb.get('close'),
            'close_position_pct': pb.get('close_position_pct'),
            'bar_range': pb.get('bar_range'),
            'volume': None,
        }

    # Fallback: match by setup_bar_time in log cache
    setup_bar_time = trade.get('setup_bar_time', '')
    if not setup_bar_time:
        return None

    # Extract HH:MM from setup_bar_time
    if 'T' in setup_bar_time:
        hhmm = setup_bar_time.split('T')[1][:5]
    elif len(setup_bar_time) >= 5:
        # Could be "HH:MM" directly or a full timestamp
        m = re.search(r'(\d{2}:\d{2})', setup_bar_time)
        hhmm = m.group(1) if m else None
    else:
        hhmm = None

    if not hhmm:
        return None

    # Determine the date of this trade for log_cache lookup
    entry_time = trade.get('entry_time', '')
    trade_date = entry_time[:10] if entry_time else None
    if not trade_date or trade_date not in log_cache:
        return None

    bars = log_cache[trade_date]
    for bar in bars:
        if bar['time'] == hhmm:
            bar_range = bar['high'] - bar['low']
            if bar_range > 0:
                close_pos_pct = round(((bar['close'] - bar['low']) / bar_range) * 100, 1)
            else:
                close_pos_pct = 50.0

            return {
                'time': bar['time'],
                'open': bar['open'],
                'high': bar['high'],
                'low': bar['low'],
                'close': bar['close'],
                'close_position_pct': close_pos_pct,
                'bar_range': round(bar_range, 2),
                'volume': None,
            }

    return None


def _build_strike_analysis(trade, signal):
    """Build strike selection analysis for a trade.

    Returns dict with strike_rationale, delta_note, credit_vs_min, distance_to_short.
    """
    short_strike = trade.get('short_strike')
    spx_at_entry = trade.get('underlying_price_at_entry')
    credit = trade.get('credit_received', 0)

    # Strike rationale
    if short_strike and spx_at_entry:
        dist = round(abs(short_strike - spx_at_entry), 1)
        direction_word = 'above' if short_strike > spx_at_entry else 'below'
        strike_rationale = (
            f"Short strike {short_strike} selected {dist}pts "
            f"{direction_word} SPX at ${spx_at_entry:,.2f}"
        )
    else:
        strike_rationale = "Strike data unavailable"

    # Delta note (not tracked in current system)
    delta_note = "N/A (not tracked)"

    # Credit vs minimum
    min_credit = MIN_CREDIT_THRESHOLD
    if credit > 0 and min_credit > 0:
        pct_above = round((credit - min_credit) / min_credit * 100, 0)
        credit_vs_min = {
            'received': credit,
            'minimum': min_credit,
            'pct_above_floor': pct_above,
            'display': f"${credit:.2f} vs ${min_credit:.2f} min ({pct_above:.0f}% above floor)",
        }
    else:
        credit_vs_min = {
            'received': credit,
            'minimum': min_credit,
            'pct_above_floor': 0,
            'display': f"${credit:.2f} vs ${min_credit:.2f} min",
        }

    # Distance to short strike
    if short_strike and spx_at_entry:
        pts = round(abs(short_strike - spx_at_entry), 1)
        direction_label = 'above' if short_strike > spx_at_entry else 'below'
        distance_to_short = {
            'points': pts,
            'direction_label': direction_label,
        }
    else:
        distance_to_short = {
            'points': None,
            'direction_label': None,
        }

    return {
        'strike_rationale': strike_rationale,
        'delta_note': delta_note,
        'credit_vs_min': credit_vs_min,
        'distance_to_short': distance_to_short,
    }


def _get_market_context(trade, signal=None, log_cache=None):
    """Get market context at the time of trade entry.

    Expanded to include SPX open, SPX at signal, and VIX level.
    """
    spx_price = trade.get('underlying_price_at_entry')

    # SPX open: for today's trades use live quote, for historical use first log bar
    spx_open = None
    entry_time = trade.get('entry_time', '')
    trade_date = entry_time[:10] if entry_time else None
    today_str = datetime.now(ET).date().isoformat()

    if trade_date == today_str:
        try:
            spx_quote = yahoo.get_spx_quote()
            if spx_quote:
                spx_open = spx_quote.get('open')
        except Exception:
            pass
    elif trade_date and log_cache and trade_date in log_cache:
        bars = log_cache[trade_date]
        if bars:
            spx_open = bars[0]['open']

    # SPX at signal time
    spx_at_signal = None
    if signal:
        spx_at_signal = signal.get('underlying_price')

    # VIX level: prefer signal-captured value, fall back to live quote for today
    vix_level = None
    if signal:
        vix_level = signal.get('vix_at_signal')
    if vix_level is None:
        try:
            vix_quote = yahoo.get_vix_quote()
            if vix_quote:
                vix_level = vix_quote.get('price')
        except Exception:
            pass

    return {
        'spx_price': spx_price,
        'spx_open': spx_open,
        'spx_at_signal': spx_at_signal,
        'vix_level': vix_level,
    }


def _get_journal_notes(conn, trade_id):
    """Fetch journal notes for a trade from the journal_notes table."""
    try:
        row = conn.execute(
            "SELECT rating, what_differently, review_notes, news_catalyst "
            "FROM journal_notes WHERE trade_id = ?",
            (trade_id,)
        ).fetchone()
        if row:
            return dict(row)
    except Exception:
        pass
    return {
        'rating': None,
        'what_differently': None,
        'review_notes': None,
        'news_catalyst': None,
    }


@app.route('/api/logs/recent')
def api_logs_recent():
    """Return the last N log lines for the dashboard Logs tab."""
    limit = request.args.get('limit', 50, type=int)
    limit = min(limit, 200)  # Cap at 200
    lines = []
    try:
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            # Read all lines and take the last N
            all_lines = f.readlines()
            tail = all_lines[-limit:]
        for raw in tail:
            raw = raw.rstrip('\n\r')
            if not raw:
                continue
            # Parse level from log format: "YYYY-MM-DD HH:MM:SS - name - LEVEL - msg"
            level = 'INFO'
            for lv in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'):
                if f' - {lv} - ' in raw:
                    level = lv
                    break
            lines.append({'line': raw, 'level': level})
    except FileNotFoundError:
        pass
    return jsonify({'lines': lines})


@app.route('/api/stale-positions')
def api_stale_positions():
    """Check for trades still marked 'active' from a PREVIOUS day (not today).

    Only flags genuinely orphaned positions, not the current session's trades.
    """
    today_str = datetime.now(ET).date().strftime('%Y-%m-%d')
    try:
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT id, entry_time, direction, short_strike, long_strike, "
            "credit_received, quantity, strategy_type "
            "FROM trades WHERE status = 'active' "
            "AND DATE(entry_time) < ? ORDER BY entry_time",
            (today_str,)
        ).fetchall()
        conn.close()
        positions = [dict(r) for r in rows]
        return jsonify({
            'count': len(positions),
            'positions': positions,
        })
    except Exception as e:
        logger.error(f"Stale positions check failed: {e}")
        return jsonify({'count': 0, 'positions': [], 'error': 'Failed to check positions'})


@app.route('/api/events')
def api_events():
    """Recent system events."""
    limit = request.args.get('limit', 20, type=int)
    events = []
    try:
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT * FROM system_events ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        ).fetchall()
        for r in rows:
            events.append(dict(r))
        conn.close()
    except Exception:
        pass
    return jsonify({'events': events})


# ---------------------------------------------------------------------------
# Trading Mode API
# ---------------------------------------------------------------------------

@app.route('/api/settings/trading-mode', methods=['GET'])
def api_get_trading_mode():
    """Get the current trading mode (dry-run or live)."""
    return jsonify({'mode': get_trading_mode()})


@app.route('/api/settings/trading-mode', methods=['PUT'])
def api_set_trading_mode():
    """Set the trading mode (dry-run or live)."""
    data = request.get_json()
    if not data or 'mode' not in data:
        return jsonify({'error': 'mode is required'}), 400

    mode = data['mode']
    if mode not in ('dry-run', 'live'):
        return jsonify({'error': 'mode must be "dry-run" or "live"'}), 400

    if mode == 'live' and not is_etrade_configured():
        return jsonify({
            'error': 'Cannot enable live trading without E*TRADE credentials configured.'
        }), 400

    if save_trading_mode(mode):
        return jsonify({
            'success': True,
            'mode': mode,
            'message': f'Trading mode set to {mode}. Restart the bot for changes to take effect.',
        })
    else:
        return jsonify({'error': 'Failed to save trading mode'}), 500


# ---------------------------------------------------------------------------
# Settings API
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, overlay: dict) -> dict:
    """Deep merge overlay into base dict."""
    result = base.copy()
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_settings() -> dict:
    """Load settings from YAML, overlaid with runtime overrides."""
    # Load base settings from YAML
    settings = {}
    if SETTINGS_FILE.exists():
        with open(SETTINGS_FILE, 'r') as f:
            settings = yaml.safe_load(f) or {}

    # Apply runtime overrides if they exist
    if RUNTIME_SETTINGS_FILE.exists():
        try:
            overrides = json.loads(RUNTIME_SETTINGS_FILE.read_text())
            settings = _deep_merge(settings, overrides)
        except (json.JSONDecodeError, OSError):
            pass

    return settings


def _save_runtime_settings(changes: dict) -> bool:
    """Save runtime setting changes (doesn't modify YAML)."""
    # Load existing overrides
    overrides = {}
    if RUNTIME_SETTINGS_FILE.exists():
        try:
            overrides = json.loads(RUNTIME_SETTINGS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    # Apply new changes using dot notation paths
    for path, value in changes.items():
        keys = path.split('.')
        current = overrides
        for key in keys[:-1]:
            current = current.setdefault(key, {})
        current[keys[-1]] = value

    # Ensure directory exists
    RUNTIME_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Save
    RUNTIME_SETTINGS_FILE.write_text(json.dumps(overrides, indent=2))

    # Notify bot to reload settings
    _notify_bot_settings_changed()

    return True


def _notify_bot_settings_changed():
    """Touch a file that the bot watches for settings reload."""
    try:
        SETTINGS_CHANGED_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_CHANGED_FILE.touch()
    except OSError:
        pass


@app.route('/api/settings', methods=['GET'])
def api_get_settings():
    """Get current settings (YAML + runtime overrides)."""
    settings = _load_settings()
    return jsonify(settings)


@app.route('/api/settings', methods=['POST'])
def api_update_settings():
    """Update settings at runtime."""
    changes = request.json
    if not changes:
        return jsonify({'success': False, 'error': 'No changes provided'})

    try:
        _save_runtime_settings(changes)
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Settings update failed: {e}")
        return jsonify({'success': False, 'error': 'Failed to update settings'})


@app.route('/api/settings/reset', methods=['POST'])
def api_reset_settings():
    """Reset to YAML defaults by clearing runtime overrides."""
    try:
        if RUNTIME_SETTINGS_FILE.exists():
            RUNTIME_SETTINGS_FILE.unlink()
        _notify_bot_settings_changed()
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Settings reset failed: {e}")
        return jsonify({'success': False, 'error': 'Failed to reset settings'})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(host=None, port=None):
    """Run the dashboard server."""
    h = host or DASHBOARD_HOST
    p = port or DASHBOARD_PORT
    app.run(host=h, port=p, debug=False, threaded=True)


if __name__ == '__main__':
    run()
