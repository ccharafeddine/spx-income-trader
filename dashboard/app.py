"""
SPX Income Trader - Web Dashboard

Read-only monitoring dashboard that displays bot status, signals,
trades, and market data. Runs as a separate process from the trading bot.
"""

import sys
import os
import re
import json
import sqlite3
from pathlib import Path
from datetime import datetime, date, timedelta
from flask import Flask, render_template, jsonify, request

# Project root setup
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import (
    BASE_DIR, DATABASE_PATH, LOG_FILE,
    DASHBOARD_PORT, DASHBOARD_HOST, STRATEGY_PARAMS
)
from src.data.yahoo_finance import YahooFinanceProvider

import pytz

app = Flask(__name__)
ET = pytz.timezone('US/Eastern')

# Shared Yahoo Finance provider (has its own 60s cache)
yahoo = YahooFinanceProvider()

LOCKFILE = BASE_DIR / 'spx_trader.lock'
SIGNAL_LOG = BASE_DIR / 'logs' / 'dry_run_signals.json'
STARTING_CAPITAL = 50000.00
MIN_CREDIT_THRESHOLD = 1.00  # Signals below this were before safeguards

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def check_bot_status():
    """Check if the trading bot process is running via its lockfile."""
    if not LOCKFILE.exists():
        return {'running': False, 'pid': None, 'status': 'stopped'}

    try:
        pid = int(LOCKFILE.read_text().strip())
    except (ValueError, OSError):
        return {'running': False, 'pid': None, 'status': 'stopped'}

    try:
        os.kill(pid, 0)
        return {'running': True, 'pid': pid, 'status': 'running'}
    except PermissionError:
        return {'running': True, 'pid': pid, 'status': 'running'}
    except OSError:
        return {'running': False, 'pid': pid, 'status': 'stale'}


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
    """Parse an expiration string into a timezone-aware datetime."""
    if not exp_str:
        return None
    try:
        if 'T' in exp_str:
            dt = datetime.fromisoformat(exp_str)
        else:
            # Try common formats
            for fmt in ('%Y-%m-%d %H:%M:%S%z', '%Y-%m-%d %H:%M:%S'):
                try:
                    dt = datetime.strptime(exp_str, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
        if dt.tzinfo is None:
            dt = ET.localize(dt)
        return dt
    except (ValueError, TypeError):
        return None


def parse_todays_log():
    """Parse today's entries from the trading log file."""
    today_prefix = date.today().strftime('%Y-%m-%d')
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
    """Detect if the bot is running in dry-run mode from recent log lines."""
    try:
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()[-200:]
        found_paper = False
        for line in reversed(lines):
            if 'DRY RUN MODE' in line:
                return 'DRY RUN'
            if 'LIVE MODE' in line:
                return 'LIVE'
            if 'Mode: PAPER' in line:
                found_paper = True
        if found_paper:
            return 'PAPER'
    except FileNotFoundError:
        pass
    return 'UNKNOWN'


def get_db_connection():
    """Get a read-only SQLite connection."""
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def load_signals():
    """Load all signals from the dry-run signals JSON file."""
    try:
        with open(SIGNAL_LOG, 'r') as f:
            data = json.load(f)
        return data.get('signals', [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


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
    today_str = date.today().isoformat()
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
        today_str = date.today().isoformat()
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
    params_summary = {
        'pulse_threshold': strat.get('pulse_threshold', 10),
        'spread_width': strat.get('spread_width', 5),
        'profit_target_pct': strat.get('profit_target_pct', 80),
        'max_daily_trades': strat.get('max_daily_trades', 1),
        'contracts': strat.get('contracts_per_trade', 5),
    }

    return jsonify({
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
    })


@app.route('/api/today')
def api_today():
    """Today's activity: bars, pulses, signals, trades, intraday chart data."""
    log_data = parse_todays_log()
    today_str = date.today().strftime('%Y-%m-%d')

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

    # Get trade classification
    spx = yahoo.get_spx_quote() or {}
    spx_price = spx.get('price')
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
    })


@app.route('/api/signals')
def api_signals():
    """Signal log with optional days filter. Excludes signals outside market hours."""
    days = request.args.get('days', 30, type=int)
    cutoff = (date.today() - timedelta(days=days)).isoformat()

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
    cutoff = (date.today() - timedelta(days=days)).isoformat()

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
# Main
# ---------------------------------------------------------------------------

def run(host=None, port=None):
    """Run the dashboard server."""
    h = host or DASHBOARD_HOST
    p = port or DASHBOARD_PORT
    app.run(host=h, port=p, debug=False, threaded=True)


if __name__ == '__main__':
    run()
