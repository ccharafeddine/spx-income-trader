"""
Replay engine for demo mode.

Loads a JSONL recording, plays events at configurable speed,
and maintains in-memory state dicts that mirror the dashboard API
response shapes. API routes check the engine's state instead of
querying the real DB/broker/Yahoo.
"""

import json
import time
import threading
import logging
from pathlib import Path
from datetime import datetime, timezone

import pytz

logger = logging.getLogger(__name__)
ET = pytz.timezone('America/New_York')


class ReplayEngine:
    """State machine with threaded playback for recorded trading sessions."""

    def __init__(self, recording_path: Path):
        self.recording_path = Path(recording_path)
        self.events = []
        self.cursor = 0
        self.speed = 1.0
        self.playing = False
        self.replay_time = 0.0
        self._lock = threading.Lock()
        self._thread = None
        self._finished = False

        # State dicts matching API response shapes
        self._status = {}
        self._today = {}
        self._signals = {}
        self._history = {}
        self._risk = {}

    def load(self):
        """Load events from the JSONL recording file."""
        self.events = []
        with open(self.recording_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        if not self.events:
            raise ValueError(f"No events found in {self.recording_path}")

        # Sort by timestamp
        self.events.sort(key=lambda e: e.get('t', 0))
        self.replay_time = self.events[0]['t']
        self.cursor = 0
        self._finished = False

        # Initialize empty state
        self._init_state()

        logger.info(
            f"Loaded {len(self.events)} events from {self.recording_path.name} "
            f"(span: {self._format_duration()})"
        )

    def _format_duration(self) -> str:
        if len(self.events) < 2:
            return "0s"
        span = self.events[-1]['t'] - self.events[0]['t']
        hours = int(span // 3600)
        minutes = int((span % 3600) // 60)
        return f"{hours}h {minutes}m"

    def _init_state(self):
        """Build empty state dicts matching API response shapes."""
        self._status = {
            'version': 'demo',
            'bot': {
                'running': True,
                'stopping': False,
                'crashed': False,
                'mode': 'dry-run',
                'uptime_seconds': 0,
                'trades_today': 0,
                'daily_pnl': 0.0,
                'status': 'running',
            },
            'mode': 'DEMO',
            'active_broker': 'demo',
            'market': {'is_open': False, 'status': 'closed'},
            'spx': {'price': None, 'change': None, 'change_pct': None},
            'vix': None,
            'heartbeat': None,
            'strategy': {
                'pulse_threshold': 10,
                'spread_width': 5,
                'profit_target_pct': 80,
                'contracts': 5,
            },
            'account': {
                'starting_capital': 50000,
                'realized_pnl': 0,
                'unrealized_pnl': 0,
                'current_balance': 50000,
                'total_equity': 50000,
                'total_return_pct': 0,
                'daily_pnl': 0,
                'positions': [],
                'closed_trades': [],
            },
            'today_summary': None,
            'tag_n_turn': {'enabled': False},
            'bnb': {'enabled': False},
            'orb': {'enabled': False},
            'portfolio': {
                'active_positions': 0,
                'max_total_positions': 2,
                '0dte_positions': 0,
                'max_0dte_positions': 1,
                'daily_risk_used': 0,
                'max_daily_risk': 2500,
                'daily_realized_pnl': 0,
                'max_daily_loss_pct': 2.0,
                'max_daily_loss_dollars': 1000,
                'circuit_breaker': False,
                'dte0_trades_today': 0,
                'tnt_trades_today': 0,
            },
            'etrade_token': None,
        }

        self._today = {
            'bot_bars': [],
            'log_bars': [],
            'chart_bars': [],
            'pulses': [],
            'pulse_times': [],
            'signals': [],
            'trades': [],
            'today_pnl': 0.0,
            'building_bar': None,
            'open_positions': [],
            'prev_close': None,
        }

        self._signals = {'signals': [], 'days': 30}

        self._history = {
            'daily_stats': [],
            'aggregate': {
                'total_trades': 0, 'wins': 0, 'losses': 0,
                'total_pnl': 0.0, 'win_rate': 0.0,
                'avg_win': 0.0, 'avg_loss': 0.0,
                'max_win': 0.0, 'max_loss': 0.0,
            },
            'trades': [],
            'days': 30,
        }

        self._risk = {
            'daily': {
                'realized_pnl': 0.0,
                'max_loss_pct': 2.0,
                'max_loss_dollars': 1000.0,
                'breaker_triggered': False,
            },
            'weekly': {
                'enabled': False,
                'realized_pnl': 0.0,
                'max_loss_pct': 4.0,
                'max_loss_dollars': 2000.0,
                'breaker_triggered': False,
                'period_label': '',
            },
            'monthly': {
                'enabled': False,
                'realized_pnl': 0.0,
                'max_loss_pct': 8.0,
                'max_loss_dollars': 4000.0,
                'breaker_triggered': False,
                'period_label': '',
            },
            'streaks': {'win_streak': 0, 'loss_streak': 0, 'active': 'none'},
        }

    # ------------------------------------------------------------------
    # Public state getters (called by Flask routes)
    # ------------------------------------------------------------------

    def get_status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def get_today(self) -> dict:
        with self._lock:
            return dict(self._today)

    def get_signals(self) -> dict:
        with self._lock:
            return dict(self._signals)

    def get_history(self) -> dict:
        with self._lock:
            return dict(self._history)

    def get_risk_status(self) -> dict:
        with self._lock:
            return dict(self._risk)

    def get_info(self) -> dict:
        """Replay metadata for the demo controls UI."""
        with self._lock:
            progress = 0.0
            if self.events:
                progress = self.cursor / len(self.events)
            replay_dt = datetime.fromtimestamp(self.replay_time, tz=ET)
            return {
                'playing': self.playing,
                'speed': self.speed,
                'cursor': self.cursor,
                'total_events': len(self.events),
                'progress': round(progress, 4),
                'replay_time': replay_dt.strftime('%H:%M:%S ET'),
                'replay_timestamp': self.replay_time,
                'finished': self._finished,
            }

    # ------------------------------------------------------------------
    # Playback controls
    # ------------------------------------------------------------------

    def play(self):
        """Start or resume playback."""
        if self.playing:
            return
        if self._finished:
            return
        self.playing = True
        self._thread = threading.Thread(
            target=self._replay_loop, daemon=True, name='demo-replay'
        )
        self._thread.start()
        logger.info(f"Demo playback started at {self.speed}x")

    def pause(self):
        """Pause playback."""
        self.playing = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        logger.info("Demo playback paused")

    def set_speed(self, speed: float):
        """Set playback speed multiplier."""
        self.speed = max(0.1, min(speed, 120.0))
        logger.info(f"Demo speed set to {self.speed}x")

    def jump_to_next_event(self):
        """Advance by one event (step mode)."""
        with self._lock:
            if self.cursor < len(self.events):
                event = self.events[self.cursor]
                self._process_event(event)
                self.replay_time = event['t']
                self.cursor += 1
                if self.cursor >= len(self.events):
                    self._finished = True

    def seek(self, pct: float):
        """Seek to a position (0.0 to 1.0). Replays all events up to that point."""
        pct = max(0.0, min(1.0, pct))
        target_idx = int(pct * len(self.events))

        was_playing = self.playing
        if was_playing:
            self.pause()

        with self._lock:
            # Reset state and replay from the beginning
            self._init_state()
            self.cursor = 0
            for i in range(target_idx):
                self._process_event(self.events[i])
                self.cursor = i + 1
            if target_idx > 0:
                self.replay_time = self.events[target_idx - 1]['t']
            else:
                self.replay_time = self.events[0]['t'] if self.events else 0
            self._finished = self.cursor >= len(self.events)

        if was_playing and not self._finished:
            self.play()

    # ------------------------------------------------------------------
    # Playback thread
    # ------------------------------------------------------------------

    def _replay_loop(self):
        last_wall = time.monotonic()
        while self.playing and self.cursor < len(self.events):
            now_wall = time.monotonic()
            elapsed_sim = (now_wall - last_wall) * self.speed
            target_time = self.replay_time + elapsed_sim

            with self._lock:
                while (self.cursor < len(self.events)
                       and self.events[self.cursor]['t'] <= target_time):
                    self._process_event(self.events[self.cursor])
                    self.cursor += 1
                self.replay_time = target_time

            last_wall = now_wall

            if self.cursor >= len(self.events):
                self._finished = True
                self.playing = False
                logger.info("Demo playback finished")
                break

            time.sleep(0.1)

    # ------------------------------------------------------------------
    # Event-to-state mapping
    # ------------------------------------------------------------------

    def _process_event(self, event: dict):
        """Update state dicts based on event type."""
        etype = event.get('type', '')
        handler = self._handlers.get(etype)
        if handler:
            handler(self, event)

    def _on_price_tick(self, event):
        price = event.get('spx_price')
        if price:
            prev = self._status['spx'].get('price')
            self._status['spx']['price'] = price
            if prev:
                self._status['spx']['change'] = round(price - prev, 2)
                self._status['spx']['change_pct'] = round(
                    (price - prev) / prev * 100, 2
                ) if prev else 0

        vix = event.get('vix_level')
        if vix:
            from src.data.vix_provider import classify_vix
            self._status['vix'] = {
                'level': round(vix, 2),
                'regime': classify_vix(vix),
            }

    def _on_bar_complete(self, event):
        bar_time = event.get('time', '')
        bar_data = {
            'timestamp': bar_time,
            'open': event.get('open', 0),
            'high': event.get('high', 0),
            'low': event.get('low', 0),
            'close': event.get('close', 0),
        }
        self._today['chart_bars'].append(bar_data)

        # Also add to bot_bars format (time as HH:MM)
        time_str = bar_time
        if 'T' in time_str:
            time_str = time_str.split('T')[1][:5]
        elif len(time_str) > 5:
            time_str = time_str[:5]
        self._today['bot_bars'].append({
            'time': time_str,
            'open': event.get('open', 0),
            'high': event.get('high', 0),
            'low': event.get('low', 0),
            'close': event.get('close', 0),
        })

        # Clear building bar
        self._today['building_bar'] = None

    def _on_pulse_detected(self, event):
        direction = event.get('direction', '')
        bar_data = event.get('bar', {})
        self._today['pulses'].append({
            'direction': direction,
            'time': bar_data.get('time', event.get('time', '')),
            'trigger_price': event.get('trigger_price'),
        })
        # Add pulse time for chart highlighting
        pt = event.get('time', '')[:5]
        if pt and pt not in self._today['pulse_times']:
            self._today['pulse_times'].append(pt)

    def _on_setup_created(self, event):
        direction = event.get('direction', '')
        trigger = event.get('trigger_price', 0)
        self._status['_pending_setup'] = {
            'direction': direction,
            'trigger_price': trigger,
            'window': event.get('window', 'morning'),
        }

    def _on_setup_expired(self, event):
        self._status.pop('_pending_setup', None)
        # Log as a signal rejection
        ts = datetime.fromtimestamp(event['t'], tz=ET).isoformat()
        self._signals['signals'].insert(0, {
            'timestamp': ts,
            'type': 'SETUP_EXPIRED',
            'direction': event.get('direction', ''),
            'trigger_price': event.get('trigger_price'),
            'reason': event.get('reason', 'Window closed'),
        })

    def _on_breakout_trigger(self, event):
        self._status.pop('_pending_setup', None)
        ts = datetime.fromtimestamp(event['t'], tz=ET).isoformat()
        self._today['signals'].append({
            'timestamp': ts,
            'type': 'BREAKOUT',
            'direction': event.get('direction', ''),
            'current_price': event.get('current_price'),
            'trigger_price': event.get('trigger_price'),
        })
        self._signals['signals'].insert(0, {
            'timestamp': ts,
            'type': 'BREAKOUT',
            'direction': event.get('direction', ''),
            'current_price': event.get('current_price'),
            'trigger_price': event.get('trigger_price'),
        })

    def _on_trade_entered(self, event):
        ts = datetime.fromtimestamp(event['t'], tz=ET).isoformat()
        trade = {
            'id': event.get('trade_id', ''),
            'trade_id': event.get('trade_id', ''),
            'strategy': event.get('strategy', 'daily_income'),
            'direction': event.get('direction', ''),
            'short_strike': event.get('strikes', {}).get('short'),
            'long_strike': event.get('strikes', {}).get('long'),
            'credit': event.get('credit', 0),
            'quantity': event.get('quantity', 1),
            'entry_time': ts,
            'status': 'open',
            'pnl': 0,
            'unrealized_pnl': 0,
        }
        self._today['trades'].append(trade)
        self._today['open_positions'].append(trade)

        # Update counters
        self._status['bot']['trades_today'] = len(self._today['trades'])
        portfolio = self._status.get('portfolio') or {}
        portfolio['active_positions'] = len(self._today['open_positions'])
        portfolio['0dte_positions'] = len(self._today['open_positions'])

        # Log signal
        self._signals['signals'].insert(0, {
            'timestamp': ts,
            'type': 'TRADE_ENTRY',
            'trade_id': trade['trade_id'],
            'direction': trade['direction'],
            'credit': trade['credit'],
        })

    def _on_trade_exited(self, event):
        trade_id = event.get('trade_id', '')
        pnl = event.get('pnl', 0)
        ts = datetime.fromtimestamp(event['t'], tz=ET).isoformat()

        # Move from open to closed
        self._today['open_positions'] = [
            p for p in self._today['open_positions']
            if p.get('trade_id') != trade_id
        ]

        # Update the trade in today's trades
        for t in self._today['trades']:
            if t.get('trade_id') == trade_id:
                t['status'] = 'closed'
                t['pnl'] = pnl
                t['pnl_pct'] = event.get('pnl_pct', 0)
                t['exit_reason'] = event.get('exit_reason', '')
                t['exit_time'] = ts
                t['duration_minutes'] = event.get('duration_minutes', 0)
                break

        # Update P&L
        self._today['today_pnl'] += pnl
        self._status['bot']['daily_pnl'] = round(self._today['today_pnl'], 2)
        self._status['account']['daily_pnl'] = round(self._today['today_pnl'], 2)
        self._status['account']['realized_pnl'] += pnl
        self._status['account']['current_balance'] = (
            self._status['account']['starting_capital']
            + self._status['account']['realized_pnl']
        )

        # Risk tracking
        self._risk['daily']['realized_pnl'] = round(self._today['today_pnl'], 2)

        # Portfolio status
        portfolio = self._status.get('portfolio') or {}
        portfolio['active_positions'] = len(self._today['open_positions'])
        portfolio['0dte_positions'] = len(self._today['open_positions'])
        portfolio['daily_realized_pnl'] = round(self._today['today_pnl'], 2)

        # History
        closed_trade = {
            'trade_id': trade_id,
            'pnl': pnl,
            'pnl_pct': event.get('pnl_pct', 0),
            'exit_reason': event.get('exit_reason', ''),
            'entry_time': '',
            'exit_time': ts,
            'direction': event.get('direction', ''),
            'running_total': round(self._today['today_pnl'], 2),
        }
        # Find entry time from today's trades
        for t in self._today['trades']:
            if t.get('trade_id') == trade_id:
                closed_trade['entry_time'] = t.get('entry_time', '')
                closed_trade['direction'] = t.get('direction', '')
                break
        self._history['trades'].insert(0, closed_trade)

        # Update history aggregate
        agg = self._history['aggregate']
        agg['total_trades'] += 1
        agg['total_pnl'] = round(agg['total_pnl'] + pnl, 2)
        if pnl > 0:
            agg['wins'] += 1
            agg['max_win'] = max(agg['max_win'], pnl)
        elif pnl < 0:
            agg['losses'] += 1
            agg['max_loss'] = min(agg['max_loss'], pnl)
        if agg['total_trades'] > 0:
            agg['win_rate'] = round(agg['wins'] / agg['total_trades'] * 100, 1)

        # Update streaks
        streaks = self._risk['streaks']
        if pnl > 0:
            if streaks['active'] == 'win':
                streaks['win_streak'] += 1
            else:
                streaks['active'] = 'win'
                streaks['win_streak'] = 1
                streaks['loss_streak'] = 0
        elif pnl < 0:
            if streaks['active'] == 'loss':
                streaks['loss_streak'] += 1
            else:
                streaks['active'] = 'loss'
                streaks['loss_streak'] = 1
                streaks['win_streak'] = 0

        # Log signal
        self._signals['signals'].insert(0, {
            'timestamp': ts,
            'type': 'TRADE_EXIT',
            'trade_id': trade_id,
            'pnl': pnl,
            'exit_reason': event.get('exit_reason', ''),
        })

    def _on_position_update(self, event):
        positions = event.get('positions', [])
        for pos_update in positions:
            tid = pos_update.get('trade_id', '')
            for p in self._today['open_positions']:
                if p.get('trade_id') == tid:
                    p['unrealized_pnl'] = pos_update.get('unrealized_pnl', 0)
                    p['current_price'] = pos_update.get('spx_price', 0)

    def _on_circuit_breaker(self, event):
        triggered = event.get('triggered', False)
        self._risk['daily']['breaker_triggered'] = triggered
        if self._status.get('portfolio'):
            self._status['portfolio']['circuit_breaker'] = triggered

    def _on_market_open(self, event):
        self._status['market']['is_open'] = True
        self._status['market']['status'] = 'open'
        price = event.get('spx_price')
        if price:
            self._status['spx']['price'] = price
            self._today['prev_close'] = price

    def _on_market_close(self, event):
        self._status['market']['is_open'] = False
        self._status['market']['status'] = 'closed'
        price = event.get('spx_price')
        if price:
            self._status['spx']['price'] = price

    def _on_status_update(self, event):
        ts = datetime.fromtimestamp(event['t'], tz=ET).strftime('%Y-%m-%d %H:%M:%S')
        self._status['heartbeat'] = ts
        if event.get('spx_price'):
            self._status['spx']['price'] = event['spx_price']
        if event.get('daily_pnl') is not None:
            self._status['bot']['daily_pnl'] = event['daily_pnl']

    def _on_signal_evaluated(self, event):
        # Lightweight - just counts, no state change needed
        pass

    # Handler dispatch table
    _handlers = {
        'price_tick': _on_price_tick,
        'bar_complete': _on_bar_complete,
        'pulse_detected': _on_pulse_detected,
        'setup_created': _on_setup_created,
        'setup_expired': _on_setup_expired,
        'breakout_trigger': _on_breakout_trigger,
        'trade_entered': _on_trade_entered,
        'trade_exited': _on_trade_exited,
        'position_update': _on_position_update,
        'circuit_breaker': _on_circuit_breaker,
        'market_open': _on_market_open,
        'market_close': _on_market_close,
        'status_update': _on_status_update,
        'signal_evaluated': _on_signal_evaluated,
    }
