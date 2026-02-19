#!/usr/bin/env python3
"""
Generate synthetic demo recordings for replay mode.

Creates three scenarios using simulated price paths:
- Winning Day: Bullish pulse, breakout, put spread, 80% profit target hit.
- Losing Day: Bullish pulse, breakout, reversal, stop loss hit.
- No-Setup Day: Narrow range, no pulse detected, all windows expire.

Usage:
    python scripts/generate_demo_recording.py --all
    python scripts/generate_demo_recording.py --scenario winning
"""
from __future__ import annotations

import sys
import json
import math
import random
import argparse
from pathlib import Path
from datetime import datetime, timedelta

# Project root
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

OUTPUT_DIR = PROJECT_ROOT / 'database' / 'demo_recordings'


def _ts(dt: datetime) -> float:
    """Convert datetime to Unix timestamp."""
    return dt.timestamp()


def _price_path(start: float, minutes: int, volatility: float = 0.0001,
                drift: float = 0.0, seed: int = 42) -> list[float]:
    """Generate a price path using geometric Brownian motion."""
    rng = random.Random(seed)
    prices = [start]
    for _ in range(minutes):
        ret = drift + volatility * rng.gauss(0, 1)
        prices.append(prices[-1] * math.exp(ret))
    return prices


def _make_bars(prices: list[float], start_dt: datetime,
               interval_minutes: int = 30) -> list[dict]:
    """Aggregate minute prices into OHLC bars."""
    bars = []
    for i in range(0, len(prices) - 1, interval_minutes):
        chunk = prices[i:i + interval_minutes]
        if len(chunk) < 2:
            break
        bar_time = start_dt + timedelta(minutes=i)
        bars.append({
            'time': bar_time.strftime('%Y-%m-%dT%H:%M:%S'),
            'open': round(chunk[0], 2),
            'high': round(max(chunk), 2),
            'low': round(min(chunk), 2),
            'close': round(chunk[-1], 2),
            'tick_count': len(chunk),
        })
    return bars


def generate_winning_day(base_date: datetime = None) -> list[dict]:
    """Winning scenario: bullish pulse at 10:00, breakout, put spread, +$340."""
    if base_date is None:
        base_date = datetime(2026, 2, 16, 9, 30)  # Monday

    events = []
    start_price = 5890.0

    # Generate minute-level prices: slight uptrend
    # 9:30 to 16:00 = 390 minutes
    prices = _price_path(start_price, 390, volatility=0.00008, drift=0.000015, seed=100)

    # Market open
    events.append({
        't': _ts(base_date),
        'type': 'market_open',
        'spx_price': round(prices[0], 2),
    })

    # Price ticks every 30 seconds (we'll do every 2 minutes for file size)
    for m in range(0, 390, 2):
        dt = base_date + timedelta(minutes=m)
        events.append({
            't': _ts(dt),
            'type': 'price_tick',
            'spx_price': round(prices[m], 2),
            'vix_level': round(16.5 + random.Random(m).gauss(0, 0.3), 2),
        })

    # Bars every 30 minutes
    bars = _make_bars(prices, base_date, 30)
    for bar in bars:
        bar_dt = datetime.strptime(bar['time'], '%Y-%m-%dT%H:%M:%S')
        bar_dt = bar_dt.replace(tzinfo=base_date.tzinfo)
        events.append({
            't': _ts(bar_dt + timedelta(minutes=30)),
            'type': 'bar_complete',
            **bar,
        })

    # Pulse detected at 10:00 bar (first bar completes at 10:00)
    pulse_time = base_date + timedelta(minutes=30)
    pulse_bar = bars[0] if bars else {'high': 5895, 'low': 5885, 'close': 5893, 'time': '10:00'}
    events.append({
        't': _ts(pulse_time),
        'type': 'pulse_detected',
        'direction': 'bullish',
        'bar': pulse_bar,
        'trigger_price': pulse_bar['high'],
    })

    # Setup created
    events.append({
        't': _ts(pulse_time + timedelta(seconds=1)),
        'type': 'setup_created',
        'direction': 'bullish',
        'trigger_price': pulse_bar['high'],
        'window': 'morning',
    })

    # Signal evaluated
    events.append({
        't': _ts(pulse_time + timedelta(seconds=2)),
        'type': 'signal_evaluated',
        'bar_time': pulse_bar['time'],
        'result': 'bullish',
    })

    # Breakout at 10:08
    breakout_time = base_date + timedelta(minutes=38)
    breakout_price = pulse_bar['high'] + 1.50
    events.append({
        't': _ts(breakout_time),
        'type': 'breakout_trigger',
        'direction': 'bullish',
        'current_price': round(breakout_price, 2),
        'trigger_price': pulse_bar['high'],
    })

    # Trade entered at 10:08
    short_strike = round(breakout_price - 20, -1)  # e.g., 5870
    long_strike = short_strike - 5
    trade_id = f"demo_win_{base_date.strftime('%Y%m%d')}_001"
    events.append({
        't': _ts(breakout_time + timedelta(seconds=5)),
        'type': 'trade_entered',
        'trade_id': trade_id,
        'strategy': 'daily_income',
        'direction': 'bullish',
        'strikes': {'short': short_strike, 'long': long_strike},
        'credit': 0.85,
        'quantity': 5,
    })

    # Position updates every 5 minutes from 10:10 to 14:00
    for m in range(40, 270, 5):
        dt = base_date + timedelta(minutes=m)
        # Simulate P&L improving (put spread profits as price rises)
        progress = (m - 40) / (270 - 40)
        unrealized = round(0.85 * 100 * 5 * progress * 0.8, 2)
        events.append({
            't': _ts(dt),
            'type': 'position_update',
            'positions': [{
                'trade_id': trade_id,
                'unrealized_pnl': unrealized,
                'spx_price': round(prices[m], 2),
            }],
        })

    # Trade exited at 14:00 (profit target)
    exit_time = base_date + timedelta(minutes=270)
    events.append({
        't': _ts(exit_time),
        'type': 'trade_exited',
        'trade_id': trade_id,
        'pnl': 340.0,
        'pnl_pct': 80.0,
        'exit_reason': 'profit_target',
        'duration_minutes': 232,
    })

    # Status updates every 5 minutes
    for m in range(0, 390, 5):
        dt = base_date + timedelta(minutes=m)
        daily_pnl = 340.0 if m >= 270 else 0.0
        events.append({
            't': _ts(dt),
            'type': 'status_update',
            'loop_count': m // 5,
            'spx_price': round(prices[min(m, len(prices) - 1)], 2),
            'daily_pnl': daily_pnl,
            'bars_built': min(m // 30, len(bars)),
            'pulse_bars': 1 if m >= 30 else 0,
            'positions_count': 1 if 38 <= m < 270 else 0,
        })

    # Market close
    close_time = base_date + timedelta(minutes=390)
    events.append({
        't': _ts(close_time),
        'type': 'market_close',
        'spx_price': round(prices[-1], 2),
        'daily_pnl': 340.0,
    })

    # Sort by timestamp
    events.sort(key=lambda e: e['t'])
    return events


def generate_losing_day(base_date: datetime = None) -> list[dict]:
    """Losing scenario: bullish pulse, breakout, reversal, stop loss. -$160."""
    if base_date is None:
        base_date = datetime(2026, 2, 17, 9, 30)  # Tuesday

    events = []
    start_price = 5890.0

    # Price goes up slightly then reverses
    prices_up = _price_path(start_price, 60, volatility=0.00008, drift=0.00003, seed=200)
    # After 60 min, reversal
    prices_down = _price_path(prices_up[-1], 330, volatility=0.00012, drift=-0.00004, seed=201)
    prices = prices_up + prices_down[1:]

    # Market open
    events.append({
        't': _ts(base_date),
        'type': 'market_open',
        'spx_price': round(prices[0], 2),
    })

    # Price ticks every 2 min
    for m in range(0, 390, 2):
        dt = base_date + timedelta(minutes=m)
        events.append({
            't': _ts(dt),
            'type': 'price_tick',
            'spx_price': round(prices[min(m, len(prices) - 1)], 2),
            'vix_level': round(18.0 + random.Random(m + 500).gauss(0, 0.5), 2),
        })

    # Bars
    bars = _make_bars(prices, base_date, 30)
    for bar in bars:
        bar_dt = datetime.strptime(bar['time'], '%Y-%m-%dT%H:%M:%S')
        events.append({
            't': _ts(bar_dt + timedelta(minutes=30)),
            'type': 'bar_complete',
            **bar,
        })

    # Pulse at 10:00
    pulse_time = base_date + timedelta(minutes=30)
    pulse_bar = bars[0] if bars else {'high': 5898, 'low': 5888, 'close': 5896, 'time': '10:00'}
    events.append({
        't': _ts(pulse_time),
        'type': 'pulse_detected',
        'direction': 'bullish',
        'bar': pulse_bar,
        'trigger_price': pulse_bar['high'],
    })
    events.append({
        't': _ts(pulse_time + timedelta(seconds=1)),
        'type': 'setup_created',
        'direction': 'bullish',
        'trigger_price': pulse_bar['high'],
        'window': 'morning',
    })

    # Breakout at 10:12
    breakout_time = base_date + timedelta(minutes=42)
    breakout_price = pulse_bar['high'] + 2.0
    events.append({
        't': _ts(breakout_time),
        'type': 'breakout_trigger',
        'direction': 'bullish',
        'current_price': round(breakout_price, 2),
        'trigger_price': pulse_bar['high'],
    })

    # Trade entered
    short_strike = round(breakout_price - 20, -1)
    long_strike = short_strike - 5
    trade_id = f"demo_loss_{base_date.strftime('%Y%m%d')}_001"
    events.append({
        't': _ts(breakout_time + timedelta(seconds=5)),
        'type': 'trade_entered',
        'trade_id': trade_id,
        'strategy': 'daily_income',
        'direction': 'bullish',
        'strikes': {'short': short_strike, 'long': long_strike},
        'credit': 0.80,
        'quantity': 5,
    })

    # Position updates: P&L worsens as price drops through short strike
    for m in range(45, 240, 5):
        dt = base_date + timedelta(minutes=m)
        progress = (m - 45) / (240 - 45)
        unrealized = round(-0.80 * 100 * 5 * progress * 0.4, 2)  # Loss grows
        events.append({
            't': _ts(dt),
            'type': 'position_update',
            'positions': [{
                'trade_id': trade_id,
                'unrealized_pnl': unrealized,
                'spx_price': round(prices[min(m, len(prices) - 1)], 2),
            }],
        })

    # Circuit breaker warning at 13:00
    events.append({
        't': _ts(base_date + timedelta(minutes=210)),
        'type': 'circuit_breaker',
        'daily_pnl': -160.0,
        'limit': 1000.0,
        'triggered': False,
    })

    # Trade exited at 13:30 (stop loss)
    exit_time = base_date + timedelta(minutes=240)
    events.append({
        't': _ts(exit_time),
        'type': 'trade_exited',
        'trade_id': trade_id,
        'pnl': -160.0,
        'pnl_pct': -40.0,
        'exit_reason': 'stop_loss',
        'duration_minutes': 198,
    })

    # Status updates
    for m in range(0, 390, 5):
        dt = base_date + timedelta(minutes=m)
        daily_pnl = -160.0 if m >= 240 else 0.0
        events.append({
            't': _ts(dt),
            'type': 'status_update',
            'loop_count': m // 5,
            'spx_price': round(prices[min(m, len(prices) - 1)], 2),
            'daily_pnl': daily_pnl,
            'bars_built': min(m // 30, len(bars)),
            'pulse_bars': 1 if m >= 30 else 0,
            'positions_count': 1 if 42 <= m < 240 else 0,
        })

    # Market close
    close_time = base_date + timedelta(minutes=390)
    events.append({
        't': _ts(close_time),
        'type': 'market_close',
        'spx_price': round(prices[-1], 2),
        'daily_pnl': -160.0,
    })

    events.sort(key=lambda e: e['t'])
    return events


def generate_no_setup_day(base_date: datetime = None) -> list[dict]:
    """No-setup scenario: narrow range, no pulse detected, $0 P&L."""
    if base_date is None:
        base_date = datetime(2026, 2, 18, 9, 30)  # Wednesday

    events = []
    start_price = 5890.0

    # Very low volatility, slight mean-reversion
    prices = _price_path(start_price, 390, volatility=0.00003, drift=0.0, seed=300)

    # Market open
    events.append({
        't': _ts(base_date),
        'type': 'market_open',
        'spx_price': round(prices[0], 2),
    })

    # Price ticks every 2 min
    for m in range(0, 390, 2):
        dt = base_date + timedelta(minutes=m)
        events.append({
            't': _ts(dt),
            'type': 'price_tick',
            'spx_price': round(prices[m], 2),
            'vix_level': round(13.5 + random.Random(m + 900).gauss(0, 0.2), 2),
        })

    # Bars
    bars = _make_bars(prices, base_date, 30)
    for bar in bars:
        bar_dt = datetime.strptime(bar['time'], '%Y-%m-%dT%H:%M:%S')
        events.append({
            't': _ts(bar_dt + timedelta(minutes=30)),
            'type': 'bar_complete',
            **bar,
        })

    # Signal evaluated events (no pulse detected for any bar)
    for bar in bars:
        bar_dt = datetime.strptime(bar['time'], '%Y-%m-%dT%H:%M:%S')
        events.append({
            't': _ts(bar_dt + timedelta(minutes=30, seconds=1)),
            'type': 'signal_evaluated',
            'bar_time': bar['time'],
            'result': None,
        })

    # Status updates
    for m in range(0, 390, 5):
        dt = base_date + timedelta(minutes=m)
        events.append({
            't': _ts(dt),
            'type': 'status_update',
            'loop_count': m // 5,
            'spx_price': round(prices[m], 2),
            'daily_pnl': 0.0,
            'bars_built': min(m // 30, len(bars)),
            'pulse_bars': 0,
            'positions_count': 0,
        })

    # Market close
    close_time = base_date + timedelta(minutes=390)
    events.append({
        't': _ts(close_time),
        'type': 'market_close',
        'spx_price': round(prices[-1], 2),
        'daily_pnl': 0.0,
    })

    events.sort(key=lambda e: e['t'])
    return events


def write_recording(events: list[dict], filename: str):
    """Write events to a JSONL file."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / filename
    with open(path, 'w', encoding='utf-8') as f:
        for event in events:
            f.write(json.dumps(event, default=str) + '\n')
    print(f"  Written: {path} ({len(events)} events)")


def main():
    parser = argparse.ArgumentParser(
        description="Generate synthetic demo recordings for replay mode"
    )
    parser.add_argument('--all', action='store_true', help='Generate all scenarios')
    parser.add_argument('--scenario', choices=['winning', 'losing', 'no_setup'],
                        help='Generate a specific scenario')
    args = parser.parse_args()

    if not args.all and not args.scenario:
        args.all = True

    scenarios = {
        'winning': (generate_winning_day, 'demo_winning.jsonl'),
        'losing': (generate_losing_day, 'demo_losing.jsonl'),
        'no_setup': (generate_no_setup_day, 'demo_no_setup.jsonl'),
    }

    targets = scenarios.keys() if args.all else [args.scenario]

    print(f"Generating demo recordings in {OUTPUT_DIR}/")
    for name in targets:
        gen_func, filename = scenarios[name]
        events = gen_func()
        write_recording(events, filename)

    print("Done.")


if __name__ == '__main__':
    main()
