"""
Tests for demo/replay mode: recorder, replay engine, generator, and Flask integration.
"""

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# EventRecorder tests
# ---------------------------------------------------------------------------

class TestEventRecorder:
    """Tests for src/demo/recorder.py"""

    def test_creates_output_dir(self, tmp_path):
        from src.demo.recorder import EventRecorder
        out = tmp_path / 'recordings'
        rec = EventRecorder(output_dir=out)
        assert out.exists()
        rec.close()

    def test_writes_valid_jsonl(self, tmp_path):
        from src.demo.recorder import EventRecorder
        rec = EventRecorder(output_dir=tmp_path)
        rec.record('price_tick', spx_price=5890.0, vix_level=16.5)
        rec.close()

        files = list(tmp_path.glob('*.jsonl'))
        assert len(files) == 1

        with open(files[0], 'r') as f:
            lines = f.readlines()
        assert len(lines) == 1

        event = json.loads(lines[0])
        assert event['type'] == 'price_tick'
        assert event['spx_price'] == 5890.0
        assert 't' in event

    def test_multiple_events(self, tmp_path):
        from src.demo.recorder import EventRecorder
        rec = EventRecorder(output_dir=tmp_path)
        rec.record('market_open', spx_price=5890.0)
        rec.record('bar_complete', time='10:00', open=5890, high=5895, low=5885, close=5893)
        rec.record('market_close', spx_price=5895.0)
        rec.close()

        files = list(tmp_path.glob('*.jsonl'))
        with open(files[0], 'r') as f:
            lines = f.readlines()
        assert len(lines) == 3
        assert json.loads(lines[0])['type'] == 'market_open'
        assert json.loads(lines[1])['type'] == 'bar_complete'
        assert json.loads(lines[2])['type'] == 'market_close'

    def test_event_count(self, tmp_path):
        from src.demo.recorder import EventRecorder
        rec = EventRecorder(output_dir=tmp_path)
        assert rec.event_count == 0
        rec.record('market_open', spx_price=5890.0)
        assert rec.event_count == 1
        rec.record('bar_complete', time='10:00', open=1, high=2, low=0, close=1)
        assert rec.event_count == 2
        rec.close()

    def test_privacy_scrub(self, tmp_path):
        from src.demo.recorder import EventRecorder
        rec = EventRecorder(output_dir=tmp_path)
        rec.record('status_update', account_id='12345', api_key='secret', spx_price=5890)
        rec.close()

        files = list(tmp_path.glob('*.jsonl'))
        with open(files[0], 'r') as f:
            event = json.loads(f.readline())
        assert 'account_id' not in event
        assert 'api_key' not in event
        assert event['spx_price'] == 5890

    def test_price_tick_throttle(self, tmp_path):
        from src.demo.recorder import EventRecorder
        rec = EventRecorder(output_dir=tmp_path)
        # First price tick should be recorded
        rec.record('price_tick', spx_price=5890.0)
        # Immediate second should be throttled
        rec.record('price_tick', spx_price=5891.0)
        rec.record('price_tick', spx_price=5892.0)
        rec.close()

        files = list(tmp_path.glob('*.jsonl'))
        with open(files[0], 'r') as f:
            lines = f.readlines()
        # Only 1 recorded (rest throttled within 30s)
        assert len(lines) == 1

    def test_non_price_events_not_throttled(self, tmp_path):
        from src.demo.recorder import EventRecorder
        rec = EventRecorder(output_dir=tmp_path)
        rec.record('bar_complete', time='10:00', open=1, high=2, low=0, close=1)
        rec.record('bar_complete', time='10:30', open=1, high=2, low=0, close=1)
        rec.record('bar_complete', time='11:00', open=1, high=2, low=0, close=1)
        rec.close()

        files = list(tmp_path.glob('*.jsonl'))
        with open(files[0], 'r') as f:
            lines = f.readlines()
        assert len(lines) == 3

    def test_thread_safety(self, tmp_path):
        from src.demo.recorder import EventRecorder
        rec = EventRecorder(output_dir=tmp_path)
        errors = []

        def writer(n):
            try:
                for i in range(20):
                    rec.record('bar_complete', time=f'{n}:{i:02d}',
                               open=1, high=2, low=0, close=1)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rec.close()
        assert len(errors) == 0
        assert rec.event_count == 100  # 5 threads * 20 events

    def test_file_path_property(self, tmp_path):
        from src.demo.recorder import EventRecorder
        rec = EventRecorder(output_dir=tmp_path)
        assert rec.file_path is None  # No writes yet
        rec.record('market_open', spx_price=5890.0)
        assert rec.file_path is not None
        assert rec.file_path.suffix == '.jsonl'
        rec.close()

    def test_nested_dict_scrub(self, tmp_path):
        from src.demo.recorder import EventRecorder
        rec = EventRecorder(output_dir=tmp_path)
        rec.record('trade_entered', nested={'account_number': 'XXXX', 'strike': 5880})
        rec.close()

        files = list(tmp_path.glob('*.jsonl'))
        with open(files[0], 'r') as f:
            event = json.loads(f.readline())
        assert 'account_number' not in event.get('nested', {})
        assert event['nested']['strike'] == 5880


# ---------------------------------------------------------------------------
# ReplayEngine tests
# ---------------------------------------------------------------------------

class TestReplayEngine:
    """Tests for src/demo/replay.py"""

    @pytest.fixture
    def recording_file(self, tmp_path):
        """Create a minimal recording file for testing."""
        path = tmp_path / 'test.jsonl'
        base_t = 1739800000.0  # Arbitrary base timestamp
        events = [
            {'t': base_t, 'type': 'market_open', 'spx_price': 5890.0},
            {'t': base_t + 60, 'type': 'price_tick', 'spx_price': 5891.0, 'vix_level': 16.5},
            {'t': base_t + 1800, 'type': 'bar_complete',
             'time': '2026-02-17T10:00:00', 'open': 5890, 'high': 5895,
             'low': 5885, 'close': 5893, 'tick_count': 30},
            {'t': base_t + 1801, 'type': 'pulse_detected',
             'direction': 'bullish', 'bar': {'time': '10:00'}, 'trigger_price': 5895},
            {'t': base_t + 1802, 'type': 'setup_created',
             'direction': 'bullish', 'trigger_price': 5895, 'window': 'morning'},
            {'t': base_t + 2280, 'type': 'breakout_trigger',
             'direction': 'bullish', 'current_price': 5896.5, 'trigger_price': 5895},
            {'t': base_t + 2285, 'type': 'trade_entered',
             'trade_id': 'test_001', 'strategy': 'daily_income',
             'direction': 'bullish',
             'strikes': {'short': 5870, 'long': 5865},
             'credit': 0.85, 'quantity': 5},
            {'t': base_t + 5000, 'type': 'position_update',
             'positions': [{'trade_id': 'test_001', 'unrealized_pnl': 200.0, 'spx_price': 5900}]},
            {'t': base_t + 16000, 'type': 'trade_exited',
             'trade_id': 'test_001', 'pnl': 340.0, 'pnl_pct': 80.0,
             'exit_reason': 'profit_target', 'duration_minutes': 228},
            {'t': base_t + 23400, 'type': 'market_close',
             'spx_price': 5905.0, 'daily_pnl': 340.0},
        ]
        with open(path, 'w') as f:
            for e in events:
                f.write(json.dumps(e) + '\n')
        return path

    def test_load(self, recording_file):
        from src.demo.replay import ReplayEngine
        engine = ReplayEngine(recording_file)
        engine.load()
        assert len(engine.events) == 10
        assert engine.cursor == 0

    def test_load_empty_file_raises(self, tmp_path):
        from src.demo.replay import ReplayEngine
        empty = tmp_path / 'empty.jsonl'
        empty.write_text('')
        engine = ReplayEngine(empty)
        with pytest.raises(ValueError, match="No events"):
            engine.load()

    def test_initial_state_shapes(self, recording_file):
        from src.demo.replay import ReplayEngine
        engine = ReplayEngine(recording_file)
        engine.load()

        status = engine.get_status()
        assert 'bot' in status
        assert 'market' in status
        assert 'spx' in status
        assert 'account' in status
        assert status['mode'] == 'DEMO'
        assert status['active_broker'] == 'demo'

        today = engine.get_today()
        assert 'chart_bars' in today
        assert 'bot_bars' in today
        assert 'trades' in today
        assert 'open_positions' in today
        assert isinstance(today['today_pnl'], (int, float))

        signals = engine.get_signals()
        assert 'signals' in signals

        history = engine.get_history()
        assert 'aggregate' in history
        assert 'trades' in history

        risk = engine.get_risk_status()
        assert 'daily' in risk
        assert 'streaks' in risk

    def test_price_tick_updates_status(self, recording_file):
        from src.demo.replay import ReplayEngine
        engine = ReplayEngine(recording_file)
        engine.load()

        # Process market_open + price_tick
        engine.jump_to_next_event()  # market_open
        engine.jump_to_next_event()  # price_tick

        status = engine.get_status()
        assert status['spx']['price'] == 5891.0

    def test_bar_complete_updates_today(self, recording_file):
        from src.demo.replay import ReplayEngine
        engine = ReplayEngine(recording_file)
        engine.load()

        # Process up to bar_complete (3 events)
        for _ in range(3):
            engine.jump_to_next_event()

        today = engine.get_today()
        assert len(today['chart_bars']) == 1
        assert today['chart_bars'][0]['high'] == 5895

    def test_trade_lifecycle(self, recording_file):
        from src.demo.replay import ReplayEngine
        engine = ReplayEngine(recording_file)
        engine.load()

        # Process all events
        for _ in range(len(engine.events)):
            engine.jump_to_next_event()

        today = engine.get_today()
        # Trade should have been entered and exited
        assert len(today['trades']) == 1
        assert today['trades'][0]['status'] == 'closed'
        assert today['trades'][0]['pnl'] == 340.0
        assert today['today_pnl'] == 340.0

        # No open positions
        assert len(today['open_positions']) == 0

        # History should have 1 trade
        history = engine.get_history()
        assert len(history['trades']) == 1
        assert history['aggregate']['total_trades'] == 1
        assert history['aggregate']['wins'] == 1
        assert history['aggregate']['total_pnl'] == 340.0

    def test_circuit_breaker(self, tmp_path):
        from src.demo.replay import ReplayEngine
        path = tmp_path / 'cb.jsonl'
        events = [
            {'t': 1000, 'type': 'market_open', 'spx_price': 5890},
            {'t': 2000, 'type': 'circuit_breaker', 'daily_pnl': -500, 'limit': 1000, 'triggered': True},
        ]
        with open(path, 'w') as f:
            for e in events:
                f.write(json.dumps(e) + '\n')

        engine = ReplayEngine(path)
        engine.load()
        engine.jump_to_next_event()
        engine.jump_to_next_event()

        risk = engine.get_risk_status()
        assert risk['daily']['breaker_triggered'] is True

    def test_speed_control(self, recording_file):
        from src.demo.replay import ReplayEngine
        engine = ReplayEngine(recording_file)
        engine.load()

        engine.set_speed(10.0)
        assert engine.speed == 10.0

        engine.set_speed(0.01)
        assert engine.speed == 0.1  # Clamped to min

        engine.set_speed(200)
        assert engine.speed == 120.0  # Clamped to max

    def test_seek(self, recording_file):
        from src.demo.replay import ReplayEngine
        engine = ReplayEngine(recording_file)
        engine.load()

        # Seek to 50%
        engine.seek(0.5)
        info = engine.get_info()
        assert info['cursor'] == 5  # ~50% of 10 events

        # Seek to 100%
        engine.seek(1.0)
        info = engine.get_info()
        assert info['cursor'] == 10
        assert info['finished'] is True

    def test_seek_to_zero(self, recording_file):
        from src.demo.replay import ReplayEngine
        engine = ReplayEngine(recording_file)
        engine.load()

        # Process some events first
        for _ in range(5):
            engine.jump_to_next_event()

        # Seek back to start
        engine.seek(0.0)
        info = engine.get_info()
        assert info['cursor'] == 0

    def test_jump_to_next_event(self, recording_file):
        from src.demo.replay import ReplayEngine
        engine = ReplayEngine(recording_file)
        engine.load()

        engine.jump_to_next_event()
        assert engine.cursor == 1

        engine.jump_to_next_event()
        assert engine.cursor == 2

    def test_get_info(self, recording_file):
        from src.demo.replay import ReplayEngine
        engine = ReplayEngine(recording_file)
        engine.load()

        info = engine.get_info()
        assert info['playing'] is False
        assert info['speed'] == 1.0
        assert info['cursor'] == 0
        assert info['total_events'] == 10
        assert info['progress'] == 0.0
        assert info['finished'] is False

    def test_play_pause(self, recording_file):
        from src.demo.replay import ReplayEngine
        engine = ReplayEngine(recording_file)
        engine.load()
        engine.set_speed(60.0)  # Fast replay

        engine.play()
        assert engine.playing is True
        time.sleep(0.5)  # Let it process some events
        engine.pause()
        assert engine.playing is False

        # Should have advanced past cursor=0
        assert engine.cursor > 0

    def test_market_open_close(self, recording_file):
        from src.demo.replay import ReplayEngine
        engine = ReplayEngine(recording_file)
        engine.load()

        # market_open
        engine.jump_to_next_event()
        status = engine.get_status()
        assert status['market']['is_open'] is True

        # Seek to end (includes market_close)
        engine.seek(1.0)
        status = engine.get_status()
        assert status['market']['is_open'] is False

    def test_position_update(self, recording_file):
        from src.demo.replay import ReplayEngine
        engine = ReplayEngine(recording_file)
        engine.load()

        # Process up through position_update (8 events)
        for _ in range(8):
            engine.jump_to_next_event()

        today = engine.get_today()
        assert len(today['open_positions']) == 1
        assert today['open_positions'][0]['unrealized_pnl'] == 200.0

    def test_streak_tracking(self, tmp_path):
        from src.demo.replay import ReplayEngine
        path = tmp_path / 'streaks.jsonl'
        events = [
            {'t': 1000, 'type': 'market_open', 'spx_price': 5890},
            {'t': 2000, 'type': 'trade_entered', 'trade_id': 't1',
             'strategy': 'daily_income', 'direction': 'bullish',
             'strikes': {'short': 5870, 'long': 5865}, 'credit': 0.85, 'quantity': 1},
            {'t': 3000, 'type': 'trade_exited', 'trade_id': 't1',
             'pnl': 50, 'pnl_pct': 80, 'exit_reason': 'profit_target', 'duration_minutes': 17},
            {'t': 4000, 'type': 'trade_entered', 'trade_id': 't2',
             'strategy': 'daily_income', 'direction': 'bearish',
             'strikes': {'short': 5910, 'long': 5915}, 'credit': 0.80, 'quantity': 1},
            {'t': 5000, 'type': 'trade_exited', 'trade_id': 't2',
             'pnl': 40, 'pnl_pct': 75, 'exit_reason': 'profit_target', 'duration_minutes': 17},
        ]
        with open(path, 'w') as f:
            for e in events:
                f.write(json.dumps(e) + '\n')

        engine = ReplayEngine(path)
        engine.load()
        for _ in range(len(engine.events)):
            engine.jump_to_next_event()

        risk = engine.get_risk_status()
        assert risk['streaks']['win_streak'] == 2
        assert risk['streaks']['active'] == 'win'


# ---------------------------------------------------------------------------
# Generator tests
# ---------------------------------------------------------------------------

class TestDemoGenerator:
    """Tests for scripts/generate_demo_recording.py"""

    def test_winning_day_produces_events(self):
        from scripts.generate_demo_recording import generate_winning_day
        events = generate_winning_day()
        assert len(events) > 50
        types = {e['type'] for e in events}
        assert 'market_open' in types
        assert 'market_close' in types
        assert 'trade_entered' in types
        assert 'trade_exited' in types
        assert 'pulse_detected' in types

    def test_losing_day_produces_events(self):
        from scripts.generate_demo_recording import generate_losing_day
        events = generate_losing_day()
        assert len(events) > 50
        types = {e['type'] for e in events}
        assert 'market_open' in types
        assert 'trade_exited' in types
        # The losing scenario should have a negative P&L exit
        exit_events = [e for e in events if e['type'] == 'trade_exited']
        assert any(e['pnl'] < 0 for e in exit_events)

    def test_no_setup_day_no_trades(self):
        from scripts.generate_demo_recording import generate_no_setup_day
        events = generate_no_setup_day()
        assert len(events) > 50
        types = {e['type'] for e in events}
        assert 'market_open' in types
        assert 'market_close' in types
        assert 'trade_entered' not in types
        assert 'trade_exited' not in types

    def test_valid_jsonl_format(self):
        from scripts.generate_demo_recording import generate_winning_day
        events = generate_winning_day()
        for event in events:
            # Should be JSON-serializable
            line = json.dumps(event, default=str)
            parsed = json.loads(line)
            assert 't' in parsed
            assert 'type' in parsed

    def test_chronological_order(self):
        from scripts.generate_demo_recording import generate_winning_day
        events = generate_winning_day()
        timestamps = [e['t'] for e in events]
        assert timestamps == sorted(timestamps)

    def test_all_scenarios_load_in_replay(self, tmp_path):
        """Each generated scenario should load successfully in ReplayEngine."""
        from scripts.generate_demo_recording import (
            generate_winning_day, generate_losing_day, generate_no_setup_day
        )
        from src.demo.replay import ReplayEngine

        for gen_func, name in [
            (generate_winning_day, 'winning'),
            (generate_losing_day, 'losing'),
            (generate_no_setup_day, 'no_setup'),
        ]:
            events = gen_func()
            path = tmp_path / f'{name}.jsonl'
            with open(path, 'w') as f:
                for e in events:
                    f.write(json.dumps(e, default=str) + '\n')

            engine = ReplayEngine(path)
            engine.load()
            assert len(engine.events) > 0

            # Process all events without error
            for _ in range(len(engine.events)):
                engine.jump_to_next_event()


# ---------------------------------------------------------------------------
# Flask integration tests
# ---------------------------------------------------------------------------

class TestFlaskIntegration:
    """Test demo mode routes via Flask test client."""

    @pytest.fixture
    def demo_app(self, tmp_path):
        """Create a Flask test client with demo mode enabled."""
        from scripts.generate_demo_recording import generate_winning_day
        from src.demo.replay import ReplayEngine

        # Write a recording
        path = tmp_path / 'test.jsonl'
        events = generate_winning_day()
        with open(path, 'w') as f:
            for e in events:
                f.write(json.dumps(e, default=str) + '\n')

        # Create replay engine
        engine = ReplayEngine(path)
        engine.load()

        # Configure Flask app
        from dashboard.app import app, API_TOKEN
        app._demo_mode = True
        app._replay_engine = engine
        app.config['TESTING'] = True

        client = app.test_client()
        yield client, engine, API_TOKEN

        # Cleanup
        app._demo_mode = False
        app._replay_engine = None

    def test_status_returns_demo_data(self, demo_app):
        client, engine, token = demo_app
        resp = client.get('/api/status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['mode'] == 'DEMO'
        assert data['active_broker'] == 'demo'

    def test_today_returns_demo_data(self, demo_app):
        client, engine, token = demo_app
        resp = client.get('/api/today')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'chart_bars' in data
        assert 'trades' in data

    def test_signals_returns_demo_data(self, demo_app):
        client, engine, token = demo_app
        resp = client.get('/api/signals')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'signals' in data

    def test_history_returns_demo_data(self, demo_app):
        client, engine, token = demo_app
        resp = client.get('/api/history')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'aggregate' in data
        assert 'trades' in data

    def test_risk_status_returns_demo_data(self, demo_app):
        client, engine, token = demo_app
        resp = client.get('/api/risk-status')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'daily' in data
        assert 'streaks' in data

    def test_demo_info_route(self, demo_app):
        client, engine, token = demo_app
        resp = client.get('/api/demo/info')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'playing' in data
        assert 'speed' in data
        assert 'progress' in data

    def test_demo_control_play(self, demo_app):
        client, engine, token = demo_app
        resp = client.post('/api/demo/control',
                           json={'action': 'play'},
                           headers={'X-API-Token': token})
        assert resp.status_code == 200
        time.sleep(0.3)
        assert engine.playing is True
        engine.pause()

    def test_demo_control_pause(self, demo_app):
        client, engine, token = demo_app
        engine.play()
        time.sleep(0.2)
        resp = client.post('/api/demo/control',
                           json={'action': 'pause'},
                           headers={'X-API-Token': token})
        assert resp.status_code == 200
        assert engine.playing is False

    def test_demo_control_speed(self, demo_app):
        client, engine, token = demo_app
        resp = client.post('/api/demo/control',
                           json={'action': 'speed', 'value': 30},
                           headers={'X-API-Token': token})
        assert resp.status_code == 200
        assert engine.speed == 30.0

    def test_demo_control_seek(self, demo_app):
        client, engine, token = demo_app
        resp = client.post('/api/demo/control',
                           json={'action': 'seek', 'value': 0.5},
                           headers={'X-API-Token': token})
        assert resp.status_code == 200
        assert engine.cursor > 0

    def test_demo_control_jump(self, demo_app):
        client, engine, token = demo_app
        resp = client.post('/api/demo/control',
                           json={'action': 'jump'},
                           headers={'X-API-Token': token})
        assert resp.status_code == 200
        assert engine.cursor == 1

    def test_demo_control_requires_token(self, demo_app):
        client, engine, token = demo_app
        resp = client.post('/api/demo/control', json={'action': 'play'})
        assert resp.status_code == 403

    def test_demo_mode_skips_broker_check(self, demo_app):
        """In demo mode, setup redirect should be skipped."""
        client, engine, token = demo_app
        resp = client.get('/')
        # Should not redirect to /setup
        assert resp.status_code == 200
