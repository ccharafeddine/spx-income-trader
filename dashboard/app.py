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
import math
import sqlite3
import ctypes
import time
import threading
import yaml
from collections import defaultdict
from pathlib import Path
from datetime import datetime, date, timedelta
from flask import Flask, render_template, jsonify, request, redirect, url_for

# Project root setup
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.utils.app_paths import CONFIG_DIR, DATA_DIR, DB_PATH

# Settings files - use writable paths that work in both dev and packaged mode
SETTINGS_FILE = CONFIG_DIR / 'strategy_params.yaml'
RUNTIME_SETTINGS_FILE = DATA_DIR / 'database' / 'runtime_settings.json'
SETTINGS_CHANGED_FILE = DATA_DIR / 'database' / '.settings_changed'

from config.settings import (
    BASE_DIR, DATABASE_PATH, LOG_FILE,
    DASHBOARD_PORT, DASHBOARD_HOST, STRATEGY_PARAMS,
    ETRADE_CONFIG, is_etrade_configured, save_etrade_credentials, clear_etrade_credentials,
    get_trading_mode, save_trading_mode, get_etrade_credentials,
    is_schwab_configured, is_any_broker_configured, load_strategy_params,
    save_schwab_credentials, clear_schwab_credentials, get_schwab_credentials,
)
from src.data.yahoo_finance import YahooFinanceProvider
from src.utils.version import APP_VERSION

import pytz

logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(32)
ET = pytz.timezone('US/Eastern')

# Per-session API token for CSRF protection.
# Embedded in every served page; validated on all state-changing requests.
# Since the dashboard binds to 127.0.0.1, this token prevents cross-origin
# requests from malicious websites from controlling the trading bot.
import secrets
API_TOKEN = secrets.token_hex(32)


# ---------------------------------------------------------------------------
# Middleware: CSRF token validation + setup redirect
# ---------------------------------------------------------------------------

@app.before_request
def validate_api_token():
    """Validate API token on all state-changing requests (CSRF protection)."""
    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None

    # Allow setup form POST (no token embedded yet), static, and OAuth callbacks
    csrf_exempt = ['/setup', '/auth/etrade/', '/auth/schwab/']
    if any(request.path.startswith(p) for p in csrf_exempt):
        return None

    # Check token in header or form data
    token = request.headers.get('X-API-Token') or request.form.get('_api_token')
    if not token or token != API_TOKEN:
        logger.warning(f"CSRF token validation failed for {request.method} {request.path}")
        return jsonify({'error': 'Invalid or missing API token'}), 403


@app.before_request
def check_setup_required():
    """Redirect to setup if no broker is configured."""
    # Allow setup, static, auth, and broker API routes without credentials
    allowed_paths = ['/setup', '/static', '/api/setup/status', '/auth/etrade/',
                     '/auth/schwab/', '/api/schwab-auth-status', '/api/broker/']
    if any(request.path.startswith(p) for p in allowed_paths):
        return None

    # Skip broker check in demo mode
    if getattr(app, '_demo_mode', False):
        return None

    # Check if any broker is configured
    if not is_any_broker_configured():
        return redirect(url_for('setup'))

@app.context_processor
def inject_api_token():
    """Make API token available in all templates for CSRF protection."""
    return {'api_token': API_TOKEN}


# Demo mode defaults (set by app_desktop.py --demo)
app._demo_mode = False
app._replay_engine = None


@app.context_processor
def inject_demo_mode():
    """Make demo_mode flag available in all templates."""
    return {'demo_mode': getattr(app, '_demo_mode', False)}


# Shared Yahoo Finance provider (has its own 60s cache)
yahoo = YahooFinanceProvider()

LOCKFILE = BASE_DIR / 'bot.lock'
SIGNAL_LOG = BASE_DIR / 'logs' / 'signals.json'
# Legacy path for backward compatibility with older dry-run logs
_LEGACY_SIGNAL_LOG = BASE_DIR / 'logs' / 'dry_run_signals.json'
MIN_CREDIT_THRESHOLD = 1.00  # Signals below this were before safeguards


def _get_starting_capital():
    """Read account_size from YAML (not cached -- always fresh)."""
    try:
        if SETTINGS_FILE.exists():
            with open(SETTINGS_FILE, 'r') as f:
                settings = yaml.safe_load(f) or {}
            return float(settings.get('portfolio', {}).get('account_size', 50000.0))
    except Exception:
        pass
    return 50000.0

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
    """Parse today's entries from the trading log file.

    Also checks the most recent rotated log (.1) in case the log
    rotated mid-day and early bars are in the previous file.
    """
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

    # Read both current and most recent rotated log (in case of mid-day rotation)
    log_files = [LOG_FILE + '.1', LOG_FILE]  # .1 first so current file wins for dedup
    for log_path in log_files:
        try:
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
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

    # Deduplicate bars by time (keep last occurrence from multiple bot runs)
    seen = {}
    for bar in bars:
        seen[bar['time']] = bar
    deduped_bars = sorted(seen.values(), key=lambda b: b['time'])

    return {
        'bars': deduped_bars,
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


def _ensure_trades_db():
    """Ensure the mode-specific trades DB has the correct schema."""
    try:
        from database.db_manager import DatabaseManager
        DatabaseManager.ensure_schema(DATABASE_PATH)
    except Exception as e:
        logger.warning(f"Could not initialize trades DB schema: {e}")


_ensure_trades_db()


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


_daily_journal_table_checked = False

def _ensure_daily_journal_table(conn):
    """Create daily_journal table if it doesn't exist (cached after first check)."""
    global _daily_journal_table_checked
    if _daily_journal_table_checked:
        return
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_journal (
            date DATE PRIMARY KEY,
            bars_built INTEGER DEFAULT 0,
            pulse_bars_found INTEGER DEFAULT 0,
            signals_evaluated INTEGER DEFAULT 0,
            trades_entered INTEGER DEFAULT 0,
            spx_open REAL,
            spx_close REAL,
            spx_change_pct REAL,
            vix_level REAL,
            vix_regime TEXT,
            rejection_reasons TEXT,
            market_context TEXT,
            no_trade_summary TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    _daily_journal_table_checked = True


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


def compute_account(conn, spx_price, starting_capital=None):
    """Compute account balance, realized/unrealized P&L."""
    if starting_capital is None:
        starting_capital = _get_starting_capital()
    open_positions, closed_trades = classify_trades(conn, spx_price)
    now = datetime.now(ET)

    # Realized P&L from valid closed/expired trades (exclude flagged)
    valid_closed = [t for t in closed_trades if not t.get('flag')]
    realized_pnl = sum(t.get('pnl') or 0 for t in valid_closed)

    # Today's realized P&L (also exclude flagged)
    today_str = datetime.now(ET).date().isoformat()
    daily_realized = 0.0
    for t in valid_closed:
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

    current_balance = starting_capital + realized_pnl
    total_return_pct = ((current_balance + unrealized_pnl - starting_capital)
                        / starting_capital * 100) if starting_capital else 0

    return {
        'starting_capital': starting_capital,
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
    """Setup/onboarding page for broker configuration."""
    error = None
    success = None

    if request.method == 'POST':
        broker_type = request.form.get('broker_type', '')

        if broker_type == 'dry_run':
            # Set active broker to dry_run in YAML
            _set_active_broker('dry_run')
            return redirect(url_for('index'))

        elif broker_type == 'schwab':
            app_key = request.form.get('schwab_app_key', '').strip()
            app_secret = request.form.get('schwab_app_secret', '').strip()
            account_number = request.form.get('schwab_account_number', '').strip()
            callback_url = request.form.get('schwab_callback_url', 'https://127.0.0.1').strip()

            if not app_key or not app_secret:
                error = 'App Key and App Secret are required.'
            else:
                if _save_schwab_credentials(app_key, app_secret, account_number, callback_url):
                    _set_active_broker('schwab')
                    return redirect(url_for('index'))
                else:
                    error = 'Failed to save Schwab credentials.'

        elif broker_type == 'etrade':
            consumer_key = request.form.get('consumer_key', '').strip()
            consumer_secret = request.form.get('consumer_secret', '').strip()
            account_id = request.form.get('account_id', '').strip()
            environment = request.form.get('environment', 'sandbox')

            if not consumer_key or not consumer_secret or not account_id:
                error = 'All fields are required.'
            else:
                sandbox = environment != 'production'
                if save_etrade_credentials(consumer_key, consumer_secret, account_id, sandbox):
                    _set_active_broker('etrade')
                    return redirect(url_for('index'))
                else:
                    error = 'Failed to save credentials. Make sure keyring is installed.'
        else:
            error = 'Please select a broker.'

        return render_template('setup.html',
            error=error,
            is_configured=is_any_broker_configured(),
            active_broker=STRATEGY_PARAMS.get('broker', {}).get('active', 'dry_run'),
        )

    # GET request
    return render_template('setup.html',
        is_configured=is_any_broker_configured(),
        active_broker=STRATEGY_PARAMS.get('broker', {}).get('active', 'dry_run'),
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

    # Active broker info
    active_broker = STRATEGY_PARAMS.get('broker', {}).get('active', 'dry_run')
    schwab_cfg = STRATEGY_PARAMS.get('broker', {}).get('schwab', {})
    schwab_key = schwab_cfg.get('app_key', '')
    schwab_account = schwab_cfg.get('account_number', '')
    masked_schwab_key = '****' + schwab_key[-4:] if len(schwab_key) > 4 else schwab_key
    masked_schwab_account = '****' + schwab_account[-4:] if len(schwab_account) > 4 else schwab_account

    # E*TRADE masked values
    key = ETRADE_CONFIG.get('consumer_key') or ''
    account = ETRADE_CONFIG.get('account_id') or ''
    masked_key = '****' + key[-4:] if len(key) > 4 else key
    masked_account = '****' + account[-4:] if len(account) > 4 else account

    return render_template('settings.html',
        message=message,
        message_type=message_type,
        active_broker=active_broker,
        masked_key=masked_key,
        masked_account=masked_account,
        masked_schwab_key=masked_schwab_key,
        masked_schwab_account=masked_schwab_account,
        schwab_callback_url=schwab_cfg.get('callback_url', 'https://127.0.0.1'),
        is_production=not ETRADE_CONFIG.get('sandbox', True),
        credential_source=ETRADE_CONFIG.get('credential_source', 'none').title()
    )


@app.route('/api/setup/status')
def api_setup_status():
    """Check if any broker is configured."""
    return jsonify({
        'configured': is_any_broker_configured(),
        'active_broker': STRATEGY_PARAMS.get('broker', {}).get('active', 'dry_run'),
    })


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
    """Clear stored credentials from keychain (both E*TRADE and Schwab)."""
    try:
        etrade_ok = clear_etrade_credentials()
        schwab_ok = clear_schwab_credentials()
        if etrade_ok or schwab_ok:
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Failed to clear credentials'})
    except Exception as e:
        logger.error(f"Failed to clear credentials: {e}")
        return jsonify({'success': False, 'error': 'Failed to clear credentials'})


# ---------------------------------------------------------------------------
# Broker configuration helpers
# ---------------------------------------------------------------------------

def _save_schwab_credentials(app_key: str, app_secret: str, account_number: str,
                              callback_url: str = 'https://127.0.0.1') -> bool:
    """Save Schwab credentials to OS keyring (secure storage)."""
    try:
        if not save_schwab_credentials(app_key, app_secret, account_number, callback_url):
            return False

        # Also update non-secret fields in YAML for reference (account_number, callback_url)
        with open(SETTINGS_FILE, 'r') as f:
            params = yaml.safe_load(f) or {}

        if 'broker' not in params:
            params['broker'] = {}
        if 'schwab' not in params['broker']:
            params['broker']['schwab'] = {}

        params['broker']['schwab']['account_number'] = account_number
        params['broker']['schwab']['callback_url'] = callback_url
        # Remove secrets from YAML if they were there from a previous version
        params['broker']['schwab'].pop('app_key', None)
        params['broker']['schwab'].pop('app_secret', None)

        with open(SETTINGS_FILE, 'w') as f:
            yaml.dump(params, f, default_flow_style=False, sort_keys=False)

        # Update in-memory config
        STRATEGY_PARAMS.setdefault('broker', {}).setdefault('schwab', {})
        STRATEGY_PARAMS['broker']['schwab']['account_number'] = account_number
        STRATEGY_PARAMS['broker']['schwab']['callback_url'] = callback_url
        return True
    except Exception as e:
        logger.error(f"Failed to save Schwab credentials: {e}")
        return False


def _set_active_broker(broker_name: str) -> bool:
    """Set the active broker in strategy_params.yaml."""
    if broker_name not in ('dry_run', 'etrade', 'schwab'):
        return False
    try:
        with open(SETTINGS_FILE, 'r') as f:
            params = yaml.safe_load(f) or {}

        if 'broker' not in params:
            params['broker'] = {}
        params['broker']['active'] = broker_name

        with open(SETTINGS_FILE, 'w') as f:
            yaml.dump(params, f, default_flow_style=False, sort_keys=False)

        # Update in-memory config
        STRATEGY_PARAMS.setdefault('broker', {})['active'] = broker_name
        return True
    except Exception as e:
        logger.error(f"Failed to set active broker: {e}")
        return False


@app.route('/api/broker/active', methods=['GET'])
def api_broker_active_get():
    """Get the currently active broker."""
    return jsonify({
        'active': STRATEGY_PARAMS.get('broker', {}).get('active', 'dry_run'),
        'schwab_configured': is_schwab_configured(),
        'etrade_configured': is_etrade_configured(),
    })


@app.route('/api/broker/active', methods=['POST'])
def api_broker_active_set():
    """Set the active broker."""
    data = request.get_json()
    broker = data.get('broker', '')
    if _set_active_broker(broker):
        return jsonify({'success': True, 'active': broker})
    return jsonify({'success': False, 'error': f'Invalid broker: {broker}'}), 400


@app.route('/api/schwab/credentials', methods=['POST'])
def api_schwab_credentials():
    """Save Schwab credentials to strategy_params.yaml."""
    data = request.get_json()
    app_key = (data.get('app_key') or '').strip()
    app_secret = (data.get('app_secret') or '').strip()
    account_number = (data.get('account_number') or '').strip()
    callback_url = (data.get('callback_url') or 'https://127.0.0.1').strip()

    if not app_key or not app_secret:
        return jsonify({'success': False, 'error': 'App Key and App Secret are required.'}), 400

    if _save_schwab_credentials(app_key, app_secret, account_number, callback_url):
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Failed to save credentials.'}), 500


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


# ---------------------------------------------------------------------------
# Schwab OAuth2 flow
# After authorizing on Schwab's site, the browser redirects to the callback
# URL (https://127.0.0.1) with the auth code. That page won't load (no
# local server), but the URL with the code is in the address bar. The user
# copies that URL and pastes it here. Auto-submit fires immediately on paste
# to beat the ~30-second auth code expiry.
# ---------------------------------------------------------------------------

_pending_schwab_oauth = {}


def _resolve_schwab_token_path():
    """Resolve the Schwab token file path (absolute)."""
    schwab_cfg = STRATEGY_PARAMS.get('broker', {}).get('schwab', {})
    token_path = schwab_cfg.get('token_path')
    if token_path:
        from pathlib import Path as _Path
        p = _Path(token_path)
        if not p.is_absolute():
            from config.settings import BASE_DIR
            p = BASE_DIR / token_path
        return str(p)
    from config.settings import BASE_DIR
    return str(BASE_DIR / 'database' / 'schwab_token.json')


@app.route('/auth/schwab/start')
def auth_schwab_start():
    """Initiate Schwab OAuth2 flow. Returns auth URL for the browser."""
    try:
        import schwab.auth as schwab_auth
        schwab_creds = get_schwab_credentials()
        app_key = schwab_creds.get('app_key', '')
        callback_url = schwab_creds.get('callback_url', 'https://127.0.0.1')

        if not app_key:
            return jsonify({'error': 'Schwab app_key not configured. Save credentials first.'}), 400

        auth_context = schwab_auth.get_auth_context(app_key, callback_url)
        _pending_schwab_oauth['auth_context'] = {
            'callback_url': auth_context.callback_url,
            'authorization_url': auth_context.authorization_url,
            'state': auth_context.state,
        }

        logger.info("Schwab OAuth2 flow initiated")
        return jsonify({'auth_url': auth_context.authorization_url})

    except Exception as e:
        logger.error(f"Failed to start Schwab OAuth flow: {e}")
        return jsonify({'error': f'Failed to start authentication: {e}'}), 500


@app.route('/auth/schwab/complete', methods=['POST'])
def auth_schwab_complete():
    """Exchange the redirect URL (pasted by user) for tokens."""
    ctx_data = _pending_schwab_oauth.get('auth_context')
    if not ctx_data:
        return jsonify({'success': False, 'message': 'No OAuth flow in progress. Click Connect first.'}), 400

    redirect_url = request.json.get('redirect_url', '').strip()
    if not redirect_url or 'code=' not in redirect_url:
        return jsonify({'success': False, 'message': 'Invalid URL. Paste the full URL from your browser address bar.'}), 400

    try:
        import schwab.auth as schwab_auth
        from src.brokers.schwab_auth import SchwabAuth

        schwab_creds = get_schwab_credentials()

        auth_context = schwab_auth.AuthContext(
            callback_url=ctx_data['callback_url'],
            authorization_url=ctx_data['authorization_url'],
            state=ctx_data['state'],
        )

        token_path = _resolve_schwab_token_path()
        from pathlib import Path as _Path
        _Path(token_path).parent.mkdir(parents=True, exist_ok=True)

        def token_write_func(token_data, *args, **kwargs):
            with open(token_path, 'w') as f:
                json.dump(token_data, f, indent=2)
            try:
                os.chmod(token_path, 0o600)
            except OSError:
                pass

        schwab_auth.client_from_received_url(
            api_key=schwab_creds.get('app_key', ''),
            app_secret=schwab_creds.get('app_secret', ''),
            auth_context=auth_context,
            received_url=redirect_url,
            token_write_func=token_write_func,
        )

        # Write metadata
        auth_obj = SchwabAuth(token_path=token_path)
        auth_obj._write_metadata()

        # Clear pending state
        _pending_schwab_oauth.pop('auth_context', None)

        logger.info("Schwab OAuth2 authentication completed successfully")
        return jsonify({'success': True, 'message': 'Schwab connected successfully! Token saved.'})

    except Exception as e:
        logger.error(f"Schwab OAuth callback failed: {e}")
        return jsonify({'success': False, 'message': f'Token exchange failed: {e}'}), 400


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
    if getattr(app, '_demo_mode', False) and app._replay_engine:
        return jsonify(app._replay_engine.get_status())
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
        _sc = _get_starting_capital()
        account = {
            'starting_capital': _sc,
            'realized_pnl': 0, 'unrealized_pnl': 0,
            'current_balance': _sc, 'total_equity': _sc,
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
        'contracts': strat.get('max_contracts_override', 5),
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

    # VIX
    vix_data = None
    try:
        vix_quote = yahoo.get_vix_quote()
        if vix_quote and vix_quote.get('price'):
            from src.data.vix_provider import classify_vix
            vix_level = vix_quote['price']
            vix_data = {
                'level': round(vix_level, 2),
                'regime': classify_vix(vix_level),
            }
            from src.utils.metrics import vix_level as vix_metric
            vix_metric.set(vix_level)
    except Exception:
        pass

    # Active broker name for header badge
    active_broker = STRATEGY_PARAMS.get('broker', {}).get('active', 'dry_run')

    return jsonify({
        'version': APP_VERSION,
        'bot': bot,
        'mode': mode,
        'active_broker': active_broker,
        'market': market,
        'spx': {
            'price': spx_price,
            'change': spx.get('change'),
            'change_pct': spx.get('change_pct'),
        },
        'vix': vix_data,
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


@app.route('/api/schwab-auth-status')
def api_schwab_auth_status():
    """Schwab token/auth status for dashboard display."""
    active = STRATEGY_PARAMS.get('broker', {}).get('active', 'dry_run')
    if active != 'schwab':
        return jsonify({'configured': False})

    try:
        from src.brokers.schwab_auth import SchwabAuth
        # Use get_schwab_credentials() which checks keyring first, then YAML
        schwab_creds = get_schwab_credentials()
        schwab_cfg = STRATEGY_PARAMS.get('broker', {}).get('schwab', {})
        auth = SchwabAuth(
            app_key=schwab_creds.get('app_key', ''),
            app_secret=schwab_creds.get('app_secret', ''),
            callback_url=schwab_creds.get('callback_url', 'https://127.0.0.1'),
            token_path=schwab_cfg.get('token_path'),
        )
        authenticated = auth.is_authenticated()
        token_status = auth.get_token_status()
        return jsonify({
            'configured': True,
            'authenticated': authenticated,
            'token_status': token_status,
        })
    except Exception as e:
        logger.error(f"Schwab auth status check failed: {e}")
        return jsonify({
            'configured': True,
            'authenticated': False,
            'token_status': {'valid': False, 'hours_remaining': 0, 'expiring_soon': True},
        })


@app.route('/api/today')
def api_today():
    """Today's activity: bars, pulses, signals, trades, intraday chart data."""
    if getattr(app, '_demo_mode', False) and app._replay_engine:
        return jsonify(app._replay_engine.get_today())
    log_data = parse_todays_log()
    today_str = datetime.now(ET).date().strftime('%Y-%m-%d')

    # Bar data priority: bot memory > log parsing > Yahoo Finance
    # 1) Bot in-memory bars (desktop mode only, most reliable)
    bot_bars = None
    get_bot_bars = getattr(app, '_desktop_get_bot_bars', None)
    if get_bot_bars:
        bot_bars = get_bot_bars()

    # 2) Yahoo intraday bars for candlestick chart (cached, fast after first call)
    chart_bars = []
    try:
        intraday = yahoo.get_intraday_bars('30m', '1d') or []
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
    except Exception as e:
        logger.debug(f"Yahoo intraday bars unavailable: {e}")

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
        'bot_bars': bot_bars,
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
    if getattr(app, '_demo_mode', False) and app._replay_engine:
        return jsonify(app._replay_engine.get_signals())
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
    if getattr(app, '_demo_mode', False) and app._replay_engine:
        return jsonify(app._replay_engine.get_history())
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

    # days=0 means all time (no cutoff)
    cutoff = None if days == 0 else (datetime.now(ET).date() - timedelta(days=days)).isoformat()

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

    # Filter by cutoff date (skip if all-time)
    if cutoff:
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
            'vix_regime': trade.get('vix_regime'),
            'day_open': trade.get('day_open'),
            'gap_pct': trade.get('gap_pct'),
            'intraday_move_at_entry': trade.get('intraday_move_at_entry'),
            'profit_captured_pct': trade.get('profit_captured_pct'),
            'time_in_trade_minutes': trade.get('time_in_trade_minutes'),
            'day_type': trade.get('day_type'),
            'daily_move_pct': trade.get('daily_move_pct'),
            'theoretical_credit': trade.get('theoretical_credit'),
            'actual_credit': trade.get('actual_credit'),
            'slippage': trade.get('slippage'),
            'slippage_pct': trade.get('slippage_pct'),
            'economic_events': trade.get('economic_events'),
            # Market metadata
            'day_of_week': trade.get('day_of_week'),
            'day_of_week_name': trade.get('day_of_week_name'),
            'entry_time_bucket': trade.get('entry_time_bucket'),
            'sma50': trade.get('sma50'),
            'sma200': trade.get('sma200'),
            'spx_vs_sma50': trade.get('spx_vs_sma50'),
            'spx_vs_sma50_pct': trade.get('spx_vs_sma50_pct'),
            'spx_vs_sma200': trade.get('spx_vs_sma200'),
            'spx_vs_sma200_pct': trade.get('spx_vs_sma200_pct'),
            'prior_day_high': trade.get('prior_day_high'),
            'prior_day_low': trade.get('prior_day_low'),
            'spx_vs_prior_range': trade.get('spx_vs_prior_range'),
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
        'contracts': strat.get('max_contracts_override', 5),
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


@app.route('/api/journal/totals')
def api_journal_totals():
    """All-time stats using the same classify_trades + exit analysis as the journal."""
    try:
        conn = get_db_connection()
        spx = yahoo.get_spx_quote() or {}
        spx_price = spx.get('price')
        _, closed = classify_trades(conn, spx_price)
        open_pos, _ = classify_trades(conn, spx_price)
        conn.close()
    except Exception as e:
        logger.error(f"Journal totals error: {e}")
        return jsonify({'error': 'Database error'}), 500

    all_trades = closed + open_pos

    # Compute exit analysis for each trade (same as journal endpoint)
    valid = []
    dur_hours = []
    cap_vals = []
    for t in all_trades:
        ea = _compute_exit_analysis(t)
        if t.get('flag'):
            continue  # exclude flagged trades from stats
        valid.append(ea)
        if ea['duration_hours'] is not None:
            dur_hours.append(ea['duration_hours'])
        if ea['pct_of_max_profit'] is not None:
            cap_vals.append(ea['pct_of_max_profit'])

    wins = [e for e in valid if e['outcome'] == 'win']
    losses = [e for e in valid if e['outcome'] == 'loss']
    total_pnl = sum((e['pnl'] or 0) for e in valid)

    return jsonify({
        'trades': len(valid),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': round(len(wins) / len(valid) * 100, 1) if valid else 0,
        'total_pnl': round(total_pnl, 2),
        'avg_duration_hours': round(sum(dur_hours) / len(dur_hours), 1) if dur_hours else None,
        'avg_capture': round(sum(cap_vals) / len(cap_vals), 1) if cap_vals else None,
    })


@app.route('/api/journal/calendar')
def api_journal_calendar():
    """Monthly calendar view data for the trade journal."""
    now = datetime.now(ET)
    year = request.args.get('year', now.year, type=int)
    month = request.args.get('month', now.month, type=int)

    # Clamp to valid range
    if month < 1 or month > 12 or year < 2020 or year > 2099:
        return jsonify({'error': 'Invalid month/year'}), 400

    # Compute first/last day of month
    first_day = f"{year:04d}-{month:02d}-01"
    if month == 12:
        last_day = f"{year + 1:04d}-01-01"
    else:
        last_day = f"{year:04d}-{month + 1:02d}-01"

    try:
        conn = get_db_connection()

        # Get trades for the month
        rows = conn.execute(
            """SELECT entry_time, exit_time, pnl, strategy_type, status,
                      direction, credit_received, quantity
               FROM trades
               WHERE DATE(entry_time) >= ? AND DATE(entry_time) < ?""",
            (first_day, last_day)
        ).fetchall()

        # Group by date (exclude flagged trades from stats)
        days_data = {}
        month_dur_hours = []
        month_cap_vals = []
        for row in rows:
            entry_time = row['entry_time'] or ''
            date_str = entry_time[:10] if entry_time else None
            if not date_str:
                continue

            # Skip flagged trades (same logic as _annotate_trade)
            credit = row['credit_received'] or 0
            is_flagged = credit < MIN_CREDIT_THRESHOLD

            if date_str not in days_data:
                days_data[date_str] = {
                    'trades': 0, 'pnl': 0.0,
                    'wins': 0, 'losses': 0,
                    'strategies': set()
                }
            d = days_data[date_str]
            if not is_flagged:
                d['trades'] += 1
                pnl = row['pnl'] or 0
                d['pnl'] += pnl
                if pnl > 0:
                    d['wins'] += 1
                elif pnl < 0:
                    d['losses'] += 1
                # Duration
                if row['entry_time'] and row['exit_time']:
                    try:
                        delta = datetime.fromisoformat(row['exit_time']) - datetime.fromisoformat(row['entry_time'])
                        secs = delta.total_seconds()
                        if secs >= 0:
                            month_dur_hours.append(secs / 3600)
                    except (ValueError, TypeError):
                        pass
                # Capture
                qty = row['quantity'] or 1
                max_profit = credit * 100 * qty
                if pnl and max_profit > 0:
                    month_cap_vals.append(pnl / max_profit * 100)
            strat = row['strategy_type'] or 'daily_income'
            d['strategies'].add(strat)

        # Get system events for no-trade reasons
        events = conn.execute(
            """SELECT DATE(timestamp) as event_date, event_type, message
               FROM system_events
               WHERE DATE(timestamp) >= ? AND DATE(timestamp) < ?""",
            (first_day, last_day)
        ).fetchall()

        # Build no-trade reasons from events
        event_reasons = {}
        for ev in events:
            event_date = ev['event_date']
            if event_date and event_date not in days_data:
                etype = (ev['event_type'] or '').lower()
                msg = (ev['message'] or '').lower()
                reason = None
                if 'circuit_breaker' in etype or 'circuit_breaker' in msg:
                    reason = 'circuit_breaker'
                elif 'pdt' in etype or 'pdt' in msg:
                    reason = 'pdt_restricted'
                elif 'bot_started' in etype or 'bot_stopped' in etype:
                    reason = 'no_setups'
                if reason and event_date not in event_reasons:
                    event_reasons[event_date] = reason

        # Check daily_stats for days bot ran but had no trades
        daily_rows = conn.execute(
            """SELECT date FROM daily_stats
               WHERE date >= ? AND date < ? AND trades_count = 0""",
            (first_day, last_day)
        ).fetchall()
        for dr in daily_rows:
            ds = dr['date']
            if ds and ds not in days_data and ds not in event_reasons:
                event_reasons[ds] = 'no_setups'

        # Get daily_journal entries for the month
        _ensure_daily_journal_table(conn)
        journal_rows = conn.execute(
            """SELECT date, bars_built, pulse_bars_found, signals_evaluated,
                      trades_entered, spx_open, spx_close, spx_change_pct,
                      vix_level, vix_regime, no_trade_summary
               FROM daily_journal
               WHERE date >= ? AND date < ?""",
            (first_day, last_day)
        ).fetchall()
        journal_data = {}
        for jr in journal_rows:
            journal_data[jr['date']] = dict(jr)

        conn.close()
    except Exception as e:
        logger.error(f"Calendar API error: {e}")
        return jsonify({'error': 'Database error'}), 500

    # Build response - convert sets to lists, round pnl
    result_days = {}
    total_pnl = 0.0
    total_trades = 0
    total_wins = 0
    total_losses = 0
    trading_days = 0

    for date_str, d in days_data.items():
        d['strategies'] = sorted(d['strategies'])
        d['pnl'] = round(d['pnl'], 2)
        # Merge daily_journal data if available
        if date_str in journal_data:
            jd = journal_data[date_str]
            d['journal'] = {
                'bars_built': jd.get('bars_built', 0),
                'pulse_bars_found': jd.get('pulse_bars_found', 0),
                'signals_evaluated': jd.get('signals_evaluated', 0),
                'spx_change_pct': jd.get('spx_change_pct'),
                'vix_level': jd.get('vix_level'),
                'vix_regime': jd.get('vix_regime'),
            }
        result_days[date_str] = d
        total_pnl += d['pnl']
        total_trades += d['trades']
        total_wins += d['wins']
        total_losses += d['losses']
        if d['trades'] > 0:
            trading_days += 1

    # Add no-trade days with reasons (prefer daily_journal data over system events)
    for date_str, jd in journal_data.items():
        if date_str not in result_days:
            summary_text = jd.get('no_trade_summary', '')
            # Determine reason from journal data
            reason = 'no_setups'
            if date_str in event_reasons:
                reason = event_reasons[date_str]
            result_days[date_str] = {
                'trades': 0, 'pnl': 0, 'wins': 0, 'losses': 0,
                'strategies': [], 'no_trade_reason': reason,
                'no_trade_summary': summary_text,
                'journal': {
                    'bars_built': jd.get('bars_built', 0),
                    'pulse_bars_found': jd.get('pulse_bars_found', 0),
                    'signals_evaluated': jd.get('signals_evaluated', 0),
                    'spx_change_pct': jd.get('spx_change_pct'),
                    'vix_level': jd.get('vix_level'),
                    'vix_regime': jd.get('vix_regime'),
                },
            }

    # Add remaining event-only days
    for date_str, reason in event_reasons.items():
        if date_str not in result_days:
            result_days[date_str] = {
                'trades': 0, 'pnl': 0, 'wins': 0, 'losses': 0,
                'strategies': [], 'no_trade_reason': reason
            }

    summary = {
        'total_pnl': round(total_pnl, 2),
        'trades': total_trades,
        'wins': total_wins,
        'losses': total_losses,
        'win_rate': round(total_wins / (total_wins + total_losses) * 100, 1) if (total_wins + total_losses) > 0 else 0,
        'trading_days': trading_days,
        'avg_duration_hours': round(sum(month_dur_hours) / len(month_dur_hours), 1) if month_dur_hours else None,
        'avg_capture': round(sum(month_cap_vals) / len(month_cap_vals), 1) if month_cap_vals else None,
    }

    return jsonify({
        'year': year,
        'month': month,
        'days': result_days,
        'summary': summary,
    })


@app.route('/api/journal/daily/<date_str>')
def api_journal_daily(date_str):
    """Get full daily journal entry for a specific date, including rejection timeline."""
    # Validate date format
    try:
        from datetime import date as date_type
        parts = date_str.split('-')
        date_type(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400

    conn = None
    try:
        conn = get_db_connection()
        _ensure_daily_journal_table(conn)

        row = conn.execute(
            "SELECT * FROM daily_journal WHERE date = ?",
            (date_str,)
        ).fetchone()

        if not row:
            conn.close()
            return jsonify({'error': 'No journal entry for this date', 'date': date_str}), 404

        data = dict(row)

        # Parse JSON fields
        try:
            data['rejection_reasons'] = json.loads(data.get('rejection_reasons') or '[]')
        except (json.JSONDecodeError, TypeError):
            data['rejection_reasons'] = []
        try:
            data['market_context'] = json.loads(data.get('market_context') or '{}')
        except (json.JSONDecodeError, TypeError):
            data['market_context'] = {}

        conn.close()
        return jsonify(data)
    except Exception as e:
        if conn:
            conn.close()
        logger.error(f"Daily journal API error: {e}")
        return jsonify({'error': 'Database error'}), 500


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

    # Classify VIX regime
    vix_regime = None
    if vix_level is not None:
        try:
            from src.data.vix_provider import classify_vix
            vix_regime = classify_vix(vix_level)
        except Exception:
            pass

    return {
        'spx_price': spx_price,
        'spx_open': spx_open,
        'spx_at_signal': spx_at_signal,
        'vix_level': vix_level,
        'vix_regime': vix_regime,
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


@app.route('/api/risk-status')
def api_risk_status():
    """Risk management status: daily circuit breaker + layered drawdown."""
    if getattr(app, '_demo_mode', False) and app._replay_engine:
        return jsonify(app._replay_engine.get_risk_status())
    portfolio_cfg = _load_settings().get('portfolio', {})
    account_size = portfolio_cfg.get('account_size', 50000)
    max_daily_loss_pct = portfolio_cfg.get('max_daily_loss_pct', 2.0)
    max_daily_loss_dollars = account_size * (max_daily_loss_pct / 100)

    # Daily circuit breaker from portfolio_state.json
    daily = {
        'realized_pnl': 0.0,
        'max_loss_pct': max_daily_loss_pct,
        'max_loss_dollars': round(max_daily_loss_dollars, 2),
        'breaker_triggered': False,
    }
    try:
        port_path = BASE_DIR / 'database' / 'portfolio_state.json'
        if port_path.exists():
            import json
            with open(port_path, 'r') as f:
                port_data = json.load(f)
            daily['realized_pnl'] = round(
                port_data.get('daily_realized_pnl', port_data.get('daily_pnl', 0)), 2
            )
            daily['breaker_triggered'] = port_data.get('circuit_breaker_triggered', False)
    except Exception:
        pass

    # Drawdown (weekly/monthly/consecutive) from drawdown_state.json
    dd_cfg = portfolio_cfg.get('drawdown_limits', {})
    weekly_cfg = dd_cfg.get('weekly', {})
    monthly_cfg = dd_cfg.get('monthly', {})
    consec_cfg = dd_cfg.get('consecutive_losses', {})

    weekly = {
        'enabled': weekly_cfg.get('enabled', False),
        'realized_pnl': 0.0,
        'max_loss_pct': weekly_cfg.get('max_loss_pct', 4.0),
        'max_loss_dollars': round(account_size * (weekly_cfg.get('max_loss_pct', 4.0) / 100), 2),
        'breaker_triggered': False,
        'period_label': '',
    }
    monthly = {
        'enabled': monthly_cfg.get('enabled', False),
        'realized_pnl': 0.0,
        'max_loss_pct': monthly_cfg.get('max_loss_pct', 8.0),
        'max_loss_dollars': round(account_size * (monthly_cfg.get('max_loss_pct', 8.0) / 100), 2),
        'breaker_triggered': False,
        'period_label': '',
    }
    try:
        dd_path = BASE_DIR / 'database' / 'drawdown_state.json'
        if dd_path.exists():
            import json
            from datetime import date as _date, datetime as _dt
            with open(dd_path, 'r') as f:
                dd = json.load(f)

            today = _date.today()
            iso_year, iso_week, _ = today.isocalendar()

            # Weekly (only if period matches)
            if dd.get('current_iso_year') == iso_year and dd.get('current_iso_week') == iso_week:
                weekly['realized_pnl'] = round(dd.get('weekly_realized_pnl', 0.0), 2)
                weekly['breaker_triggered'] = dd.get('weekly_breaker_triggered', False)
            weekly['period_label'] = f'W{iso_week}'

            # Monthly (only if period matches)
            if dd.get('current_month_year') == today.year and dd.get('current_month') == today.month:
                monthly['realized_pnl'] = round(dd.get('monthly_realized_pnl', 0.0), 2)
                monthly['breaker_triggered'] = dd.get('monthly_breaker_triggered', False)
            monthly['period_label'] = today.strftime('%b %Y')
    except Exception:
        pass

    # Compute win/loss streaks from trade history
    streaks = {'win_streak': 0, 'loss_streak': 0, 'active': 'none'}
    try:
        db_path = Path(DATABASE_PATH)
        if db_path.exists():
            import sqlite3
            conn = sqlite3.connect(str(db_path), timeout=10)
            rows = conn.execute(
                "SELECT pnl FROM trades WHERE LOWER(status) = 'closed' AND pnl IS NOT NULL "
                "ORDER BY exit_time DESC"
            ).fetchall()
            conn.close()

            if rows:
                # Determine active streak from most recent trade
                first_pnl = rows[0][0]
                if first_pnl > 0:
                    streaks['active'] = 'win'
                elif first_pnl < 0:
                    streaks['active'] = 'loss'

                # Count active streak length
                active_count = 0
                for r in rows:
                    pnl = r[0]
                    if streaks['active'] == 'win' and pnl > 0:
                        active_count += 1
                    elif streaks['active'] == 'loss' and pnl < 0:
                        active_count += 1
                    else:
                        break

                if streaks['active'] == 'win':
                    streaks['win_streak'] = active_count
                elif streaks['active'] == 'loss':
                    streaks['loss_streak'] = active_count

                # Count previous (frozen) streak from remaining trades.
                # Store in separate keys so they don't overwrite the active streak.
                remaining = rows[active_count:]
                if remaining:
                    prev_count = 0
                    prev_pnl = remaining[0][0]
                    if prev_pnl > 0:
                        for r in remaining:
                            if r[0] > 0:
                                prev_count += 1
                            else:
                                break
                        streaks['prev_win_streak'] = prev_count
                    elif prev_pnl < 0:
                        for r in remaining:
                            if r[0] < 0:
                                prev_count += 1
                            else:
                                break
                        streaks['prev_loss_streak'] = prev_count
    except Exception:
        pass

    return jsonify({
        'daily': daily,
        'weekly': weekly,
        'monthly': monthly,
        'streaks': streaks,
    })


# ---------------------------------------------------------------------------
# Performance Analytics API
# ---------------------------------------------------------------------------

def _analytics_downsample(data, max_points):
    """Evenly downsample a list, keeping first and last elements."""
    if len(data) <= max_points:
        return data
    step = (len(data) - 1) / (max_points - 1)
    indices = [round(i * step) for i in range(max_points)]
    return [data[i] for i in indices]


def _analytics_empty():
    """Return zero-filled analytics response for no-trade case."""
    return {
        'trade_count': 0,
        'date_range': {'start': None, 'end': None},
        'core': {
            'total_return_dollar': 0, 'total_return_pct': 0,
            'annualized_return': 0, 'sharpe_ratio': 0,
            'sortino_ratio': 0, 'calmar_ratio': 0,
            'max_drawdown_dollar': 0, 'max_drawdown_pct': 0,
            'max_drawdown_duration_days': 0,
        },
        'trade_metrics': {
            'total_trades': 0, 'win_rate': 0, 'profit_factor': 0,
            'expectancy': 0, 'avg_win': 0, 'avg_loss': 0,
            'win_loss_ratio': 0, 'max_consecutive_wins': 0,
            'max_consecutive_losses': 0, 'avg_duration_minutes': 0,
            'total_pnl': 0, 'gross_profit': 0, 'gross_loss': 0,
        },
        'equity_curve': [],
        'drawdown_series': [],
        'monthly_returns': [],
        'by_day_of_week': [],
        'by_vix_regime': [],
        'by_strategy': [],
        'by_time_bucket': [],
        'by_direction': [],
        'pnl_distribution': [],
        'rolling': {'dates': [], 'win_rate_7d': [], 'win_rate_30d': [], 'win_rate_90d': []},
    }


def _analytics_sharpe(daily_returns, risk_free_rate=0.045, ann_factor=252):
    """Annualized Sharpe ratio."""
    if len(daily_returns) < 2:
        return 0
    daily_rf = risk_free_rate / ann_factor
    excess = [r - daily_rf for r in daily_returns]
    mean_excess = sum(excess) / len(excess)
    variance = sum((r - mean_excess) ** 2 for r in excess) / (len(excess) - 1)
    std_dev = math.sqrt(variance) if variance > 0 else 0
    if std_dev == 0:
        return 0
    return (mean_excess / std_dev) * math.sqrt(ann_factor)


def _analytics_sortino(daily_returns, risk_free_rate=0.045, ann_factor=252):
    """Annualized Sortino ratio (downside deviation only)."""
    if len(daily_returns) < 2:
        return 0
    daily_rf = risk_free_rate / ann_factor
    excess = [r - daily_rf for r in daily_returns]
    mean_excess = sum(excess) / len(excess)
    downside = [min(r, 0) ** 2 for r in excess]
    downside_var = sum(downside) / (len(downside) - 1) if len(downside) > 1 else 0
    downside_dev = math.sqrt(downside_var)
    if downside_dev == 0:
        return 0
    return (mean_excess / downside_dev) * math.sqrt(ann_factor)


def _analytics_max_drawdown(equity_curve, initial_capital):
    """Compute max drawdown in dollars, percent, and duration."""
    if not equity_curve:
        return 0, 0, 0
    peak = initial_capital
    max_dd_dollar = 0
    max_dd_pct = 0
    peak_idx = 0
    max_dd_duration = 0
    for i, ec in enumerate(equity_curve):
        equity = ec.get('equity', initial_capital)
        if equity >= peak:
            peak = equity
            peak_idx = i
        dd_dollar = peak - equity
        dd_pct = (dd_dollar / peak * 100) if peak > 0 else 0
        if dd_dollar > max_dd_dollar:
            max_dd_dollar = dd_dollar
            max_dd_pct = dd_pct
            max_dd_duration = i - peak_idx
    return max_dd_dollar, max_dd_pct, max_dd_duration


def _analytics_drawdown_series(equity_curve, initial_capital):
    """Compute drawdown percentage series for charting."""
    if not equity_curve:
        return []
    peak = initial_capital
    series = []
    for ec in equity_curve:
        equity = ec.get('equity', initial_capital)
        if equity > peak:
            peak = equity
        dd_pct = ((peak - equity) / peak * 100) if peak > 0 else 0
        series.append({'date': ec['date'], 'drawdown_pct': round(dd_pct, 2)})
    return series


def _analytics_core_metrics(pnls, daily_returns, equity_curve, starting_capital, final_equity):
    """Core performance metrics: returns, Sharpe, Sortino, Calmar, max DD."""
    total_return_dollar = final_equity - starting_capital
    total_return_pct = (total_return_dollar / starting_capital) * 100 if starting_capital > 0 else 0

    total_days = len(equity_curve)
    years = total_days / 252 if total_days > 0 else 1
    annualized_return = ((final_equity / starting_capital) ** (1 / years) - 1) * 100 if years > 0 and starting_capital > 0 else 0

    sharpe = _analytics_sharpe(daily_returns)
    sortino = _analytics_sortino(daily_returns)
    dd_dollar, dd_pct, dd_duration = _analytics_max_drawdown(equity_curve, starting_capital)
    calmar = (annualized_return / abs(dd_pct)) if dd_pct != 0 else 0

    return {
        'total_return_dollar': round(total_return_dollar, 2),
        'total_return_pct': round(total_return_pct, 2),
        'annualized_return': round(annualized_return, 2),
        'sharpe_ratio': round(sharpe, 3),
        'sortino_ratio': round(sortino, 3),
        'calmar_ratio': round(calmar, 3),
        'max_drawdown_dollar': round(dd_dollar, 2),
        'max_drawdown_pct': round(dd_pct, 2),
        'max_drawdown_duration_days': dd_duration,
    }


def _analytics_trade_metrics(trades):
    """Trade-level statistics: win rate, profit factor, expectancy, streaks, duration."""
    if not trades:
        return _analytics_empty()['trade_metrics']

    total = len(trades)
    pnls = [t.get('pnl', 0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    win_rate = (len(wins) / total * 100) if total > 0 else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    win_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 999.99

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 999.99

    expectancy = sum(pnls) / total if total > 0 else 0

    # Streaks
    max_wins = max_losses = current_wins = current_losses = 0
    for pnl in pnls:
        if pnl > 0:
            current_wins += 1
            current_losses = 0
            max_wins = max(max_wins, current_wins)
        else:
            current_losses += 1
            current_wins = 0
            max_losses = max(max_losses, current_losses)

    # Average duration (live trades use time_in_trade_minutes)
    durations = []
    for t in trades:
        d = t.get('time_in_trade_minutes') or t.get('duration_minutes') or 0
        if d > 0:
            durations.append(d)
    avg_duration = sum(durations) / len(durations) if durations else 0

    return {
        'total_trades': total,
        'win_rate': round(win_rate, 1),
        'profit_factor': round(min(profit_factor, 999.99), 2),
        'expectancy': round(expectancy, 2),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'win_loss_ratio': round(min(win_loss_ratio, 999.99), 2),
        'max_consecutive_wins': max_wins,
        'max_consecutive_losses': max_losses,
        'avg_duration_minutes': round(avg_duration, 0),
        'total_pnl': round(sum(pnls), 2),
        'gross_profit': round(gross_profit, 2),
        'gross_loss': round(gross_loss, 2),
    }


def _analytics_by_dow(trades):
    """Win rate + P&L by day of week."""
    day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
    day_stats = {i: {'wins': 0, 'total': 0, 'pnl': 0} for i in range(5)}

    for t in trades:
        # Prefer stored day_of_week column, fall back to parsing entry_time
        dow = t.get('day_of_week')
        if dow is None:
            entry_time = t.get('entry_time', '')
            if not entry_time:
                continue
            try:
                dow = datetime.fromisoformat(entry_time).weekday()
            except (ValueError, TypeError):
                continue
        if dow is not None and 0 <= dow < 5:
            day_stats[dow]['total'] += 1
            day_stats[dow]['pnl'] += t.get('pnl', 0)
            if t.get('pnl', 0) > 0:
                day_stats[dow]['wins'] += 1

    return [
        {
            'day': day_names[i],
            'total': day_stats[i]['total'],
            'wins': day_stats[i]['wins'],
            'win_rate': round((day_stats[i]['wins'] / day_stats[i]['total'] * 100), 1) if day_stats[i]['total'] > 0 else 0,
            'total_pnl': round(day_stats[i]['pnl'], 2),
        }
        for i in range(5)
    ]


def _analytics_by_vix(trades):
    """Win rate + P&L by VIX regime."""
    regimes = {
        'low': {'label': 'Low (<15)', 'wins': 0, 'total': 0, 'pnl': 0},
        'normal': {'label': 'Normal (15-20)', 'wins': 0, 'total': 0, 'pnl': 0},
        'elevated': {'label': 'Elevated (20-30)', 'wins': 0, 'total': 0, 'pnl': 0},
        'high': {'label': 'High (>30)', 'wins': 0, 'total': 0, 'pnl': 0},
    }
    for t in trades:
        vix = t.get('vix_at_entry') or 0
        if vix <= 0:
            continue
        if vix < 15:
            regime = 'low'
        elif vix < 20:
            regime = 'normal'
        elif vix < 30:
            regime = 'elevated'
        else:
            regime = 'high'
        regimes[regime]['total'] += 1
        regimes[regime]['pnl'] += t.get('pnl', 0)
        if t.get('pnl', 0) > 0:
            regimes[regime]['wins'] += 1

    return [
        {
            'regime': v['label'],
            'total': v['total'],
            'wins': v['wins'],
            'win_rate': round((v['wins'] / v['total'] * 100), 1) if v['total'] > 0 else 0,
            'total_pnl': round(v['pnl'], 2),
        }
        for v in regimes.values()
    ]


def _analytics_by_strategy(trades):
    """Win rate + P&L by strategy type."""
    strategies = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    for t in trades:
        st = t.get('strategy_type') or 'daily_income'
        strategies[st]['total'] += 1
        strategies[st]['pnl'] += t.get('pnl', 0)
        if t.get('pnl', 0) > 0:
            strategies[st]['wins'] += 1

    return [
        {
            'strategy': k,
            'total': v['total'],
            'wins': v['wins'],
            'win_rate': round((v['wins'] / v['total'] * 100), 1) if v['total'] > 0 else 0,
            'total_pnl': round(v['pnl'], 2),
        }
        for k, v in strategies.items()
    ]


def _analytics_by_time_bucket(trades):
    """Win rate + P&L by entry time bucket."""
    buckets = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    for t in trades:
        bucket = t.get('entry_time_bucket')
        if not bucket:
            entry = t.get('entry_time', '')
            if entry and len(entry) >= 16:
                try:
                    h = int(entry[11:13])
                    if h < 10:
                        bucket = 'Open (9:30-10)'
                    elif h < 11:
                        bucket = 'Mid-Morning (10-11)'
                    elif h < 13:
                        bucket = 'Midday (11-1)'
                    elif h < 15:
                        bucket = 'Afternoon (1-3)'
                    else:
                        bucket = 'Close (3-4)'
                except (ValueError, IndexError):
                    continue
            else:
                continue
        buckets[bucket]['total'] += 1
        buckets[bucket]['pnl'] += t.get('pnl', 0)
        if t.get('pnl', 0) > 0:
            buckets[bucket]['wins'] += 1

    # Sort by time order
    order = ['Open (9:30-10)', 'Mid-Morning (10-11)', 'Midday (11-1)', 'Afternoon (1-3)', 'Close (3-4)']
    result = []
    seen = set()
    for b in order:
        if b in buckets:
            v = buckets[b]
            result.append({
                'bucket': b,
                'total': v['total'],
                'wins': v['wins'],
                'win_rate': round((v['wins'] / v['total'] * 100), 1) if v['total'] > 0 else 0,
                'total_pnl': round(v['pnl'], 2),
            })
            seen.add(b)
    for b, v in buckets.items():
        if b not in seen:
            result.append({
                'bucket': b,
                'total': v['total'],
                'wins': v['wins'],
                'win_rate': round((v['wins'] / v['total'] * 100), 1) if v['total'] > 0 else 0,
                'total_pnl': round(v['pnl'], 2),
            })
    return result


def _analytics_by_direction(trades):
    """Win rate + P&L by direction (bullish/bearish)."""
    dirs = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})
    for t in trades:
        d = t.get('direction') or 'unknown'
        dirs[d]['total'] += 1
        dirs[d]['pnl'] += t.get('pnl', 0)
        if t.get('pnl', 0) > 0:
            dirs[d]['wins'] += 1

    return [
        {
            'direction': k.title(),
            'total': v['total'],
            'wins': v['wins'],
            'win_rate': round((v['wins'] / v['total'] * 100), 1) if v['total'] > 0 else 0,
            'total_pnl': round(v['pnl'], 2),
        }
        for k, v in dirs.items()
    ]


def _analytics_monthly(daily_pnl, starting_capital):
    """Monthly returns grid from daily P&L map.

    return_pct is relative to start-of-month equity, not initial capital.
    """
    monthly = defaultdict(float)
    for day_str, pnl in daily_pnl.items():
        try:
            d = date.fromisoformat(day_str)
            monthly[(d.year, d.month)] += pnl
        except (ValueError, TypeError):
            continue

    rows = []
    running_equity = starting_capital
    for (year, month), pnl in sorted(monthly.items()):
        start_equity = running_equity
        pct = round((pnl / start_equity) * 100, 2) if start_equity > 0 else 0
        rows.append({
            'year': year,
            'month': month,
            'pnl': round(pnl, 2),
            'return_pct': pct,
        })
        running_equity += pnl

    return rows


def _analytics_rolling(equity_curve):
    """Rolling 7d/30d/90d win rate from equity curve."""
    if len(equity_curve) < 2:
        return {'dates': [], 'win_rate_7d': [], 'win_rate_30d': [], 'win_rate_90d': []}

    dates = []
    wr7 = []
    wr30 = []
    wr90 = []

    for i in range(len(equity_curve)):
        dates.append(equity_curve[i]['date'])
        for window, out in [(7, wr7), (30, wr30), (90, wr90)]:
            start = max(0, i - window + 1)
            window_data = equity_curve[start:i + 1]
            wins = sum(1 for d in window_data if d.get('daily_pnl', 0) > 0)
            total = len(window_data)
            out.append(round((wins / total * 100), 1) if total > 0 else 0)

    # Downsample if too many points
    if len(dates) > 300:
        indices = [round(i * (len(dates) - 1) / 299) for i in range(300)]
        dates = [dates[i] for i in indices]
        wr7 = [wr7[i] for i in indices]
        wr30 = [wr30[i] for i in indices]
        wr90 = [wr90[i] for i in indices]

    return {'dates': dates, 'win_rate_7d': wr7, 'win_rate_30d': wr30, 'win_rate_90d': wr90}


def _analytics_compute(closed_trades, starting_capital):
    """Compute full analytics payload from a list of closed trade dicts."""
    # Filter to trades with valid P&L
    valid = [t for t in closed_trades if t.get('pnl') is not None]
    if not valid:
        return _analytics_empty()

    trades = sorted(valid, key=lambda t: t.get('exit_time') or t.get('entry_time') or '')
    pnls = [t.get('pnl', 0) for t in trades]

    # Build daily P&L
    daily_pnl = defaultdict(float)
    for t in trades:
        exit_t = t.get('exit_time') or t.get('entry_time') or ''
        if exit_t:
            day = exit_t[:10]
            daily_pnl[day] += t.get('pnl', 0)

    # Build equity curve
    equity = starting_capital
    equity_curve = []
    for day in sorted(daily_pnl.keys()):
        dpnl = daily_pnl[day]
        equity += dpnl
        equity_curve.append({'date': day, 'equity': round(equity, 2), 'daily_pnl': round(dpnl, 2)})

    # Daily returns
    daily_returns = []
    for ec in equity_curve:
        prev = ec['equity'] - ec['daily_pnl']
        daily_returns.append(ec['daily_pnl'] / prev if prev > 0 else 0)

    core = _analytics_core_metrics(pnls, daily_returns, equity_curve, starting_capital, equity)
    trade_metrics = _analytics_trade_metrics(trades)

    return {
        'trade_count': len(trades),
        'date_range': {
            'start': equity_curve[0]['date'] if equity_curve else None,
            'end': equity_curve[-1]['date'] if equity_curve else None,
        },
        'core': core,
        'trade_metrics': trade_metrics,
        'equity_curve': _analytics_downsample(equity_curve, 300),
        'drawdown_series': _analytics_downsample(_analytics_drawdown_series(equity_curve, starting_capital), 300),
        'monthly_returns': _analytics_monthly(daily_pnl, starting_capital),
        'by_day_of_week': _analytics_by_dow(trades),
        'by_vix_regime': _analytics_by_vix(trades),
        'by_strategy': _analytics_by_strategy(trades),
        'by_time_bucket': _analytics_by_time_bucket(trades),
        'by_direction': _analytics_by_direction(trades),
        'pnl_distribution': [round(p, 2) for p in pnls],
        'rolling': _analytics_rolling(equity_curve),
    }


@app.route('/api/analytics')
def api_analytics():
    """Performance analytics from live trades or backtest results."""
    source = request.args.get('source', 'live')

    if source == 'backtest':
        run_id = request.args.get('run_id')
        if not run_id:
            return jsonify({'success': False, 'error': 'run_id required'}), 400
        try:
            db_path = str(DB_PATH)
            conn = sqlite3.connect(db_path, timeout=10)
            row = conn.execute("SELECT full_results FROM backtest_runs WHERE id = ?", (run_id,)).fetchone()
            conn.close()
            if not row:
                return jsonify({'success': False, 'error': 'Run not found'}), 404
            report = json.loads(row[0])
            report['source'] = 'backtest'
            # Add fields the frontend expects that older report.py runs don't have
            tm = report.get('trade_metrics', {})
            report.setdefault('trade_count', tm.get('total_trades', 0))
            report.setdefault('date_range', {
                'start': report['equity_curve'][0]['date'] if report.get('equity_curve') else None,
                'end': report['equity_curve'][-1]['date'] if report.get('equity_curve') else None,
            })
            # Compute missing breakdowns from stored trades if available
            trades = report.pop('_trades', None)
            if trades:
                if 'pnl_distribution' not in report:
                    report['pnl_distribution'] = [round(t.get('pnl', 0), 2) for t in trades]
                if 'by_time_bucket' not in report:
                    report['by_time_bucket'] = _analytics_by_time_bucket(trades)
                if 'by_direction' not in report:
                    report['by_direction'] = _analytics_by_direction(trades)
            report.setdefault('by_time_bucket', [])
            report.setdefault('by_direction', [])
            report.setdefault('pnl_distribution', [])
            # Compute rolling win rate from equity_curve if not stored
            if 'rolling' not in report and report.get('equity_curve'):
                report['rolling'] = _analytics_rolling(report['equity_curve'])
            report.setdefault('rolling', {'dates': [], 'win_rate_7d': [], 'win_rate_30d': [], 'win_rate_90d': []})
            return jsonify({'success': True, **report})
        except Exception as e:
            logger.error(f"Analytics backtest error: {e}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 500

    # Live trades
    try:
        db_path = DATABASE_PATH
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        spx_quote = yahoo.get_spx_quote() or {}
        spx_price = spx_quote.get('price')
        _, closed_trades = classify_trades(conn, spx_price)
        conn.close()

        starting_capital = _get_starting_capital()
        result = _analytics_compute(closed_trades, starting_capital)
        result['source'] = 'live'
        result['starting_capital'] = starting_capital
        return jsonify({'success': True, **result})
    except Exception as e:
        logger.error(f"Analytics error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/economic-events')
def api_economic_events():
    """Today's and upcoming economic calendar events."""
    try:
        from src.data.economic_calendar import get_today_events, get_upcoming_events, is_high_impact_day
        today_events = get_today_events()
        upcoming = get_upcoming_events(days=7)
        return jsonify({
            'today': today_events,
            'is_high_impact': is_high_impact_day(),
            'upcoming': upcoming,
        })
    except Exception as e:
        logger.warning(f"Economic calendar API error: {e}")
        return jsonify({'today': [], 'is_high_impact': False, 'upcoming': []})


# ---------------------------------------------------------------------------
# Trading Mode API
# ---------------------------------------------------------------------------

def _update_portfolio_yaml(updates: dict):
    """Update specific portfolio fields in strategy_params.yaml."""
    settings = {}
    if SETTINGS_FILE.exists():
        with open(SETTINGS_FILE, 'r') as f:
            settings = yaml.safe_load(f) or {}
    portfolio = settings.setdefault('portfolio', {})
    portfolio.update(updates)
    with open(SETTINGS_FILE, 'w') as f:
        yaml.dump(settings, f, default_flow_style=False, sort_keys=False)
    _notify_bot_settings_changed()


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

    if mode == 'live':
        active = STRATEGY_PARAMS.get('broker', {}).get('active', 'dry_run')
        if active == 'dry_run':
            return jsonify({
                'error': 'Cannot enable live trading with dry_run broker. Select Schwab or E*TRADE first.'
            }), 400
        if active == 'schwab' and not is_schwab_configured():
            return jsonify({
                'error': 'Cannot enable live trading without Schwab credentials configured.'
            }), 400
        if active == 'etrade' and not is_etrade_configured():
            return jsonify({
                'error': 'Cannot enable live trading without E*TRADE credentials configured.'
            }), 400

    # Auto-switch account size based on mode
    settings = _load_settings()
    portfolio = settings.get('portfolio', {})
    current_size = float(portfolio.get('account_size', 50000.0))

    if mode == 'live':
        # Save current balance as dry-run balance, fetch live balance from broker
        try:
            from src.brokers.broker_factory import get_broker
            broker = get_broker(settings)
            broker.connect()
            balance_data = broker.get_account_balance()
            live_balance = float(balance_data.get('net_account_value', 0))
            if live_balance <= 0:
                return jsonify({
                    'error': 'Could not fetch account balance from broker. Cannot switch to live.'
                }), 400
            _update_portfolio_yaml({
                'dry_run_account_size': current_size,
                'account_size': live_balance,
                'live_account_size': live_balance,
            })
        except Exception as e:
            logger.warning(f"Failed to fetch live account balance: {e}")
            return jsonify({
                'error': f'Failed to fetch account balance from broker: {e}'
            }), 400
    else:
        # Switching to dry-run: save current as live, restore dry-run balance
        dry_run_size = float(portfolio.get('dry_run_account_size', current_size))
        _update_portfolio_yaml({
            'live_account_size': current_size,
            'account_size': dry_run_size,
        })

    if save_trading_mode(mode):
        return jsonify({
            'success': True,
            'mode': mode,
            'message': f'Trading mode set to {mode}. Restart the bot for changes to take effect.',
        })
    else:
        return jsonify({'error': 'Failed to save trading mode'}), 500


@app.route('/api/settings/validate-live', methods=['POST'])
def api_validate_live():
    """Run pre-live validation checks before switching to live trading."""
    checks = []
    settings = _load_settings()
    portfolio_cfg = settings.get('portfolio', {})
    sizing_cfg = portfolio_cfg.get('position_sizing', {})
    active_broker = settings.get('broker', {}).get('active', 'dry_run')

    # --- Check 1: Credentials (broker-aware) ---
    if active_broker == 'schwab':
        creds_ok = is_schwab_configured()
        creds_detail = ('Schwab credentials configured' if creds_ok
                        else 'Missing Schwab app_key or app_secret')
    elif active_broker == 'etrade':
        creds = get_etrade_credentials()
        creds_ok = bool(
            creds.get('consumer_key')
            and creds.get('consumer_secret')
            and creds.get('account_id')
        )
        creds_detail = ('All E*TRADE credentials configured' if creds_ok
                        else 'Missing consumer_key, consumer_secret, or account_id')
    else:
        creds_ok = False
        creds_detail = f'Active broker is "{active_broker}" - select Schwab or E*TRADE for live trading'

    checks.append({
        'id': 'credentials',
        'label': 'API Credentials Set',
        'severity': 'critical',
        'passed': creds_ok,
        'detail': creds_detail,
    })

    # --- Check 2: Environment (broker-aware) ---
    if active_broker == 'schwab':
        # Schwab has no sandbox concept in the same way; always production API
        env_ok = True
        env_detail = 'Schwab API (production)'
    elif active_broker == 'etrade':
        creds = get_etrade_credentials()
        sandbox = creds.get('sandbox', True)
        env_ok = not sandbox
        env_detail = ('Production environment' if env_ok
                      else 'Sandbox mode is enabled - live orders will hit the sandbox API')
    else:
        env_ok = False
        env_detail = 'No live broker selected'

    checks.append({
        'id': 'environment',
        'label': 'Environment is Production',
        'severity': 'warning',
        'passed': env_ok,
        'detail': env_detail,
    })

    # --- Checks 3-4 require credentials ---
    broker_label = 'Schwab' if active_broker == 'schwab' else 'E*TRADE'
    broker = None
    balance_data = None

    if creds_ok:
        try:
            from src.brokers.broker_factory import get_broker
            broker = get_broker(settings)
            if active_broker == 'schwab':
                connected = broker.auth.is_authenticated()
            elif active_broker == 'etrade':
                connected = broker.connect() if hasattr(broker, 'connect') else broker.auth.is_authenticated()
            else:
                connected = False
        except Exception as e:
            connected = False
            logger.error(f"Validate-live broker connect failed: {e}")

        # Check 3: Connection
        if connected:
            try:
                balance_data = broker.get_account_balance()
                conn_ok = True
                conn_detail = f"Connected - account value ${balance_data.get('net_account_value', 0):,.2f}"
            except Exception as e:
                conn_ok = False
                conn_detail = f'Balance fetch failed: {e}'
        else:
            conn_ok = False
            conn_detail = f'Could not connect to {broker_label} API - check tokens/credentials'

        checks.append({
            'id': 'connection',
            'label': f'{broker_label} API Connection',
            'severity': 'critical',
            'passed': conn_ok,
            'detail': conn_detail,
        })

        # Check 4: SPX Options Permissions
        if conn_ok:
            try:
                from datetime import date as _date, timedelta as _td
                # Find next trading day (skip weekends) for options chain check
                check_date = _date.today()
                if check_date.weekday() >= 5:  # Saturday=5, Sunday=6
                    check_date += _td(days=(7 - check_date.weekday()))  # Next Monday
                chain = broker.get_options_chain('SPX', check_date.strftime('%Y-%m-%d'))
                opts_ok = len(chain) > 0
                opts_detail = (f'{len(chain)} strikes available ({check_date})'
                               if opts_ok
                               else 'No SPX option data returned - options may not be enabled')
            except Exception as e:
                opts_ok = False
                opts_detail = f'Options query failed: {e}'
        else:
            opts_ok = False
            opts_detail = 'Skipped - API connection failed'

        checks.append({
            'id': 'options',
            'label': 'SPX Options Permissions',
            'severity': 'critical',
            'passed': opts_ok,
            'detail': opts_detail,
        })
    else:
        # Credentials missing - skip connection checks
        checks.append({
            'id': 'connection',
            'label': f'{broker_label} API Connection',
            'severity': 'critical',
            'passed': False,
            'detail': 'Skipped - credentials not configured',
        })
        checks.append({
            'id': 'options',
            'label': 'SPX Options Permissions',
            'severity': 'critical',
            'passed': False,
            'detail': 'Skipped - credentials not configured',
        })

    # --- Check 5: Max Contracts ---
    max_contracts = sizing_cfg.get('max_contracts', 20)
    contracts_ok = max_contracts <= 2
    checks.append({
        'id': 'contracts',
        'label': 'Max Contracts Setting',
        'severity': 'warning',
        'passed': contracts_ok,
        'detail': f'max_contracts = {max_contracts}'
                  + ('' if contracts_ok else ' (consider lowering for initial live trading)'),
    })

    # --- Check 6: Account Size Auto-Sync ---
    checks.append({
        'id': 'account_size',
        'label': 'Account Size Auto-Sync',
        'severity': 'warning',
        'passed': True,
        'detail': 'Account size will be set from broker balance on switch to live',
    })

    can_proceed = all(c['passed'] for c in checks if c['severity'] == 'critical')
    all_passed = all(c['passed'] for c in checks)

    return jsonify({
        'checks': checks,
        'can_proceed': can_proceed,
        'all_passed': all_passed,
    })


@app.route('/api/account-size/save', methods=['POST'])
def api_save_account_size():
    """Update the paper account balance (dry-run mode only)."""
    data = request.get_json()
    try:
        new_size = float(data.get('account_size', 0))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Invalid amount'}), 400
    if new_size <= 0:
        return jsonify({'success': False, 'error': 'Amount must be positive'}), 400
    mode = get_trading_mode()
    if mode == 'live':
        return jsonify({'success': False, 'error': 'Cannot edit account size in live mode'}), 400
    _update_portfolio_yaml({'account_size': new_size, 'dry_run_account_size': new_size})
    return jsonify({'success': True})


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


# Allowlist of settings paths that can be modified via the API.
# Prevents arbitrary config injection (e.g., broker credentials, risk overrides).
ALLOWED_SETTINGS_PATHS = {
    'strategy.pulse_threshold',
    'strategy.spread_width',
    'strategy.profit_target_pct',
    'strategy.max_contracts_override',
    'timing.morning_start',
    'timing.morning_end',
    'timing.afternoon_start',
    'timing.afternoon_end',
    'timing.afternoon_enabled',
    'portfolio.risk_per_trade_pct',
    'portfolio.min_contracts',
    'portfolio.max_contracts',
    'portfolio.max_daily_loss_pct',
    'portfolio.max_daily_risk_pct',
    'portfolio.max_total_positions',
    'portfolio.position_sizing.max_contracts',
    'portfolio.drawdown_limits.weekly.max_loss_pct',
    'portfolio.drawdown_limits.monthly.max_loss_pct',
    'monitoring.enable_1pm_check',
    'monitoring.auto_1pm_close',
    'monitoring.trending_threshold',
    'tag_n_turn.enabled',
    'tag_n_turn.bb_period',
    'tag_n_turn.bb_std',
    'tag_n_turn.min_dte',
    'tag_n_turn.max_dte',
    'tag_n_turn.max_contracts_override',
    'orb.enabled',
    'orb.range_minutes',
    'bnb.enabled',
    'bnb.aggressive_roll',
    'risk.max_daily_loss',
    'risk.max_account_risk_pct',
    'notifications.slack.enabled',
    'notifications.slack.webhook_url',
    'notifications.slack.min_level',
    'notifications.discord.enabled',
    'notifications.discord.webhook_url',
    'notifications.discord.min_level',
    'notifications.webhook.enabled',
    'notifications.webhook.url',
    'notifications.webhook.min_level',
}


def _save_runtime_settings(changes: dict) -> bool:
    """Save runtime setting changes (doesn't modify YAML).

    Only allows paths in ALLOWED_SETTINGS_PATHS to prevent arbitrary injection.
    """
    # Validate all paths against allowlist
    rejected = [p for p in changes if p not in ALLOWED_SETTINGS_PATHS]
    if rejected:
        raise ValueError(f"Disallowed settings paths: {', '.join(rejected)}")

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


def _redact_secrets(d: dict, parent_key: str = '') -> dict:
    """Deep-copy a dict, replacing sensitive values with '****'."""
    secret_keys = {'app_key', 'app_secret', 'consumer_key', 'consumer_secret',
                   'password', 'secret', 'token', 'twilio_token', 'twilio_sid',
                   'webhook_url'}
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = _redact_secrets(v, k)
        elif k in secret_keys and isinstance(v, str) and v:
            out[k] = '****' + v[-4:] if len(v) > 4 else '****'
        else:
            out[k] = v
    return out


@app.route('/api/settings', methods=['GET'])
def api_get_settings():
    """Get current settings (YAML + runtime overrides), with secrets redacted."""
    settings = _load_settings()
    return jsonify(_redact_secrets(settings))


@app.route('/api/settings', methods=['POST'])
def api_update_settings():
    """Update settings at runtime."""
    changes = request.json
    if not changes:
        return jsonify({'success': False, 'error': 'No changes provided'})

    try:
        _save_runtime_settings(changes)
        return jsonify({'success': True})
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
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
# Notifications API
# ---------------------------------------------------------------------------

@app.route('/api/notifications/test', methods=['POST'])
def api_notifications_test():
    """Send a test notification to a single channel."""
    data = request.json or {}
    channel = data.get('channel')
    if channel not in ('slack', 'discord', 'webhook'):
        return jsonify({'success': False, 'message': 'Invalid channel'}), 400

    try:
        from src.utils.notifications import NotificationManager
        nm = NotificationManager()

        # Override config from request data so test uses the URL being configured
        # (not yet saved to YAML)
        if channel == 'slack' and data.get('webhook_url'):
            nm.slack_config = {'enabled': True, 'webhook_url': data['webhook_url'], 'min_level': 'info'}
        elif channel == 'discord' and data.get('webhook_url'):
            nm.discord_config = {'enabled': True, 'webhook_url': data['webhook_url'], 'min_level': 'info'}
        elif channel == 'webhook' and data.get('url'):
            nm.webhook_config = {'enabled': True, 'url': data['url'], 'min_level': 'info'}

        success, error = nm.send_test(channel)
        if success:
            return jsonify({'success': True, 'message': 'Test notification sent!'})
        else:
            return jsonify({'success': False, 'message': error or 'Failed to send'})
    except Exception as e:
        logger.error(f"Notification test failed: {e}")
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/notifications/save', methods=['POST'])
def api_notifications_save():
    """Save notification settings directly to strategy_params.yaml."""
    data = request.json
    if not data:
        return jsonify({'success': False, 'error': 'No data provided'}), 400

    try:
        # Load existing YAML
        settings = {}
        if SETTINGS_FILE.exists():
            with open(SETTINGS_FILE, 'r') as f:
                settings = yaml.safe_load(f) or {}

        # Build notifications section from request
        notifications = {
            'slack': {
                'enabled': bool(data.get('slack_enabled', False)),
                'webhook_url': data.get('slack_webhook_url', ''),
                'min_level': data.get('slack_min_level', 'info'),
            },
            'discord': {
                'enabled': bool(data.get('discord_enabled', False)),
                'webhook_url': data.get('discord_webhook_url', ''),
                'min_level': data.get('discord_min_level', 'info'),
            },
            'webhook': {
                'enabled': bool(data.get('webhook_enabled', False)),
                'url': data.get('webhook_url', ''),
                'min_level': data.get('webhook_min_level', 'warning'),
            },
        }

        settings['notifications'] = notifications

        # Write back to YAML
        with open(SETTINGS_FILE, 'w') as f:
            yaml.dump(settings, f, default_flow_style=False, sort_keys=False)

        # Notify bot to reload
        _notify_bot_settings_changed()

        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Notification settings save failed: {e}")
        return jsonify({'success': False, 'error': 'Failed to save notification settings'})


# ---------------------------------------------------------------------------
# Backtest API
# ---------------------------------------------------------------------------

# In-memory backtest job state (only one backtest at a time)
_backtest_job = {
    'running': False,
    'progress': 0,
    'total_days': 0,
    'current_date': None,
    'started_at': None,
    'results': None,
    'error': None,
    'job_id': None,
}
_backtest_lock = threading.Lock()


def _init_backtest_table():
    """Create backtest_runs table if it doesn't exist."""
    db_path = str(DB_PATH)
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backtest_runs (
                id TEXT PRIMARY KEY,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                params TEXT,
                results_summary TEXT,
                total_trades INTEGER DEFAULT 0,
                total_return_pct REAL DEFAULT 0,
                sharpe_ratio REAL DEFAULT 0,
                max_drawdown_pct REAL DEFAULT 0,
                win_rate REAL DEFAULT 0,
                full_results TEXT
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Failed to init backtest table: {e}")


# Initialize on module load
_init_backtest_table()


@app.route('/api/backtest/run', methods=['POST'])
def api_backtest_run():
    """Kick off a backtest in a background thread."""
    with _backtest_lock:
        if _backtest_job['running']:
            return jsonify({
                'success': False,
                'error': 'A backtest is already running',
                'job_id': _backtest_job['job_id'],
            }), 409

    data = request.json or {}
    start_date_str = data.get('start_date', '2024-01-01')
    end_date_str = data.get('end_date', '2025-12-31')
    capital = float(data.get('capital', 50000))
    pulse_threshold = float(data.get('pulse_threshold', 10.0))
    spread_width = float(data.get('spread_width', 5.0))
    profit_target = float(data.get('profit_target', 80.0))
    min_credit = float(data.get('min_credit', 1.00))
    max_contracts = int(data.get('max_contracts', 5))
    slippage_raw = data.get('slippage', None)
    slippage = float(slippage_raw) if slippage_raw else None
    max_daily_loss = float(data.get('max_daily_loss', 2.0))
    strategies = data.get('strategies', None)

    import uuid as _uuid
    job_id = str(_uuid.uuid4())[:8]

    with _backtest_lock:
        _backtest_job.update({
            'running': True,
            'progress': 0,
            'total_days': 0,
            'current_date': None,
            'started_at': datetime.now(ET).isoformat(),
            'results': None,
            'error': None,
            'job_id': job_id,
        })

    params = {
        'start_date': start_date_str,
        'end_date': end_date_str,
        'capital': capital,
        'pulse_threshold': pulse_threshold,
        'spread_width': spread_width,
        'profit_target': profit_target,
        'min_credit': min_credit,
        'max_contracts': max_contracts,
        'slippage': slippage,
        'max_daily_loss': max_daily_loss,
        'strategies': strategies,
    }

    def _run_backtest():
        try:
            from datetime import date as _date
            from src.backtest.data_loader import (
                download_spx_bars, download_vix_daily,
                load_bars_from_csv, load_vix_daily,
            )
            from src.backtest.engine import BacktestEngine
            from src.backtest.report import generate_report

            sd = _date.fromisoformat(start_date_str)
            ed = _date.fromisoformat(end_date_str)

            # Download data
            spx_csv = str(download_spx_bars(sd, ed))
            vix_daily = {}
            try:
                vix_csv = str(download_vix_daily(sd, ed))
                vix_daily = load_vix_daily(vix_csv, sd, ed)
            except Exception:
                pass

            bars_df = load_bars_from_csv(spx_csv, sd, ed)

            def progress_cb(current, total, trading_day):
                with _backtest_lock:
                    _backtest_job['progress'] = current
                    _backtest_job['total_days'] = total
                    _backtest_job['current_date'] = str(trading_day)

            engine = BacktestEngine(
                bars_df=bars_df,
                vix_daily=vix_daily,
                initial_capital=capital,
                pulse_threshold=pulse_threshold,
                spread_width=spread_width,
                profit_target_pct=profit_target,
                min_credit=min_credit,
                max_contracts=max_contracts,
                slippage=slippage,
                max_daily_loss_pct=max_daily_loss,
                progress_callback=progress_cb,
                strategies=strategies,
            )

            results = engine.run()
            report = generate_report(results)

            # Save to DB
            try:
                db_path = str(DB_PATH)
                conn = sqlite3.connect(db_path, timeout=10)
                conn.execute("PRAGMA journal_mode=WAL")
                core = report.get('core', {})
                tm = report.get('trade_metrics', {})
                conn.execute("""
                    INSERT INTO backtest_runs
                    (id, started_at, completed_at, params, results_summary,
                     total_trades, total_return_pct, sharpe_ratio,
                     max_drawdown_pct, win_rate, full_results)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    job_id,
                    _backtest_job['started_at'],
                    datetime.now(ET).isoformat(),
                    json.dumps(params),
                    json.dumps({k: v for k, v in report.items()
                                if k not in ('equity_curve', 'drawdown_series', 'daily_pnl_series', '_trades')}),
                    tm.get('total_trades', 0),
                    core.get('total_return_pct', 0),
                    core.get('sharpe_ratio', 0),
                    core.get('max_drawdown_pct', 0),
                    tm.get('win_rate', 0),
                    json.dumps(report),
                ))
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"Failed to save backtest results: {e}")

            with _backtest_lock:
                _backtest_job['results'] = report
                _backtest_job['running'] = False

        except Exception as e:
            logger.error(f"Backtest failed: {e}", exc_info=True)
            with _backtest_lock:
                _backtest_job['error'] = str(e)
                _backtest_job['running'] = False

    thread = threading.Thread(target=_run_backtest, daemon=True, name='backtest')
    thread.start()

    return jsonify({'success': True, 'job_id': job_id})


@app.route('/api/backtest/status')
def api_backtest_status():
    """Get backtest job progress."""
    with _backtest_lock:
        total = _backtest_job['total_days']
        progress = _backtest_job['progress']
        pct = (progress / total * 100) if total > 0 else 0

        return jsonify({
            'running': _backtest_job['running'],
            'progress': progress,
            'total_days': total,
            'progress_pct': round(pct, 1),
            'current_date': _backtest_job['current_date'],
            'started_at': _backtest_job['started_at'],
            'error': _backtest_job['error'],
            'job_id': _backtest_job['job_id'],
            'has_results': _backtest_job['results'] is not None,
        })


@app.route('/api/backtest/results')
def api_backtest_results():
    """Get backtest results (from current run or historical)."""
    # Check for specific run ID
    run_id = request.args.get('run_id')
    if run_id:
        try:
            db_path = str(DB_PATH)
            conn = sqlite3.connect(db_path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            row = conn.execute(
                "SELECT full_results FROM backtest_runs WHERE id = ?",
                (run_id,)
            ).fetchone()
            conn.close()
            if row:
                return jsonify({'success': True, 'report': json.loads(row[0])})
            return jsonify({'success': False, 'error': 'Run not found'}), 404
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    # Return current job results
    with _backtest_lock:
        if _backtest_job['results']:
            return jsonify({'success': True, 'report': _backtest_job['results']})
        if _backtest_job['error']:
            return jsonify({'success': False, 'error': _backtest_job['error']})
        if _backtest_job['running']:
            return jsonify({'success': False, 'error': 'Backtest still running'})
        return jsonify({'success': False, 'error': 'No results available'})


@app.route('/api/backtest/history')
def api_backtest_history():
    """List previous backtest runs."""
    try:
        db_path = str(DB_PATH)
        conn = sqlite3.connect(db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute("""
            SELECT id, started_at, completed_at, params,
                   total_trades, total_return_pct, sharpe_ratio,
                   max_drawdown_pct, win_rate
            FROM backtest_runs
            ORDER BY started_at DESC
            LIMIT 20
        """).fetchall()
        conn.close()

        runs = []
        for row in rows:
            runs.append({
                'id': row[0],
                'started_at': row[1],
                'completed_at': row[2],
                'params': json.loads(row[3]) if row[3] else {},
                'total_trades': row[4],
                'total_return_pct': row[5],
                'sharpe_ratio': row[6],
                'max_drawdown_pct': row[7],
                'win_rate': row[8],
            })

        return jsonify({'success': True, 'runs': runs})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Demo Mode API
# ---------------------------------------------------------------------------

@app.route('/api/demo/info')
def api_demo_info():
    """Replay metadata: playing, speed, progress, time."""
    engine = getattr(app, '_replay_engine', None)
    if not engine:
        return jsonify({'error': 'Demo mode not active'}), 404
    return jsonify(engine.get_info())


@app.route('/api/demo/control', methods=['POST'])
def api_demo_control():
    """Control replay: play, pause, speed, seek, jump."""
    engine = getattr(app, '_replay_engine', None)
    if not engine:
        return jsonify({'error': 'Demo mode not active'}), 404

    data = request.get_json() or {}
    action = data.get('action', '')

    if action == 'play':
        engine.play()
    elif action == 'pause':
        engine.pause()
    elif action == 'speed':
        engine.set_speed(float(data.get('value', 1)))
    elif action == 'seek':
        engine.seek(float(data.get('value', 0)))
    elif action == 'jump':
        engine.jump_to_next_event()
    else:
        return jsonify({'error': f'Unknown action: {action}'}), 400

    return jsonify({'ok': True, **engine.get_info()})


# ---------------------------------------------------------------------------
# Prometheus metrics & health
# ---------------------------------------------------------------------------

@app.route('/metrics')
def prometheus_metrics():
    """Expose Prometheus metrics. Returns 404 with install hint if prometheus_client not installed."""
    from src.utils.metrics import PROMETHEUS_AVAILABLE, generate_latest, CONTENT_TYPE_LATEST
    if not PROMETHEUS_AVAILABLE:
        return jsonify({
            'error': 'prometheus_client not installed',
            'hint': 'pip install prometheus_client  (or: pip install -r requirements-monitoring.txt)',
        }), 404
    return app.response_class(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.route('/api/health')
def api_health():
    """Enhanced health check with bot and system status."""
    bot = check_bot_status()
    bot_running = bot.get('running', False)

    # Parse heartbeat from today's log
    last_heartbeat = None
    try:
        log_data = parse_todays_log()
        if log_data.get('last_heartbeat'):
            last_heartbeat = log_data['last_heartbeat'].get('time')
    except Exception:
        pass

    # Count errors in last hour from error.log
    errors_last_hour = 0
    try:
        from src.utils.app_paths import LOG_DIR
        error_log = LOG_DIR / 'error.log'
        if error_log.exists():
            one_hour_ago = datetime.now() - timedelta(hours=1)
            with open(error_log, 'r') as f:
                for line in f:
                    try:
                        ts_str = line[:19]
                        ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
                        if ts >= one_hour_ago:
                            errors_last_hour += 1
                    except (ValueError, IndexError):
                        pass
    except Exception:
        pass

    # Open positions and daily P&L from DB
    open_positions = 0
    daily_pnl = 0.0
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM trades WHERE status = 'open'")
        open_positions = cursor.fetchone()[0]
        today_str = datetime.now(ET).date().isoformat()
        cursor.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades "
            "WHERE status = 'closed' AND date(entry_time) = ?",
            (today_str,)
        )
        daily_pnl = round(cursor.fetchone()[0], 2)
        conn.close()
    except Exception:
        pass

    # Broker connection
    broker_connected = False
    active_broker = None
    try:
        if is_etrade_configured():
            broker_connected = True
            active_broker = 'etrade'
        elif is_schwab_configured():
            broker_connected = True
            active_broker = 'schwab'
    except Exception:
        pass

    return jsonify({
        'status': 'healthy' if bot_running else 'degraded',
        'bot_running': bot_running,
        'uptime_seconds': bot.get('uptime_seconds'),
        'last_heartbeat': last_heartbeat,
        'broker_connected': broker_connected,
        'active_broker': active_broker,
        'open_positions': open_positions,
        'daily_pnl': daily_pnl,
        'errors_last_hour': errors_last_hour,
        'version': APP_VERSION,
    })


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
