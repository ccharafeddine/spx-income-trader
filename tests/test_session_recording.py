"""
Tests for session recording feature: EventRecorder filename mode,
recording lifecycle, and dashboard list/load routes.
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# EventRecorder: fixed filename mode
# ---------------------------------------------------------------------------

class TestEventRecorderFilename:
    """Tests for the filename parameter added to EventRecorder."""

    def test_fixed_filename_creates_correct_file(self, tmp_path):
        from src.demo.recorder import EventRecorder
        rec = EventRecorder(output_dir=tmp_path, filename='test_recording.jsonl')
        rec.record('price_tick', spx_price=5900.0)
        rec.close()

        expected = tmp_path / 'test_recording.jsonl'
        assert expected.exists()
        with open(expected, 'r') as f:
            event = json.loads(f.readline())
        assert event['type'] == 'price_tick'

    def test_fixed_filename_no_rotation(self, tmp_path):
        """With fixed filename, file should not rotate on date change."""
        from src.demo.recorder import EventRecorder
        rec = EventRecorder(output_dir=tmp_path, filename='session.jsonl')
        rec.record('event_a', data='first')
        rec.record('event_b', data='second')
        rec.close()

        files = list(tmp_path.glob('*.jsonl'))
        assert len(files) == 1
        assert files[0].name == 'session.jsonl'
        with open(files[0], 'r') as f:
            lines = f.readlines()
        assert len(lines) == 2

    def test_file_path_property_with_filename(self, tmp_path):
        from src.demo.recorder import EventRecorder
        rec = EventRecorder(output_dir=tmp_path, filename='my_rec.jsonl')
        assert rec.file_path == tmp_path / 'my_rec.jsonl'
        rec.close()

    def test_file_path_property_without_filename(self, tmp_path):
        from src.demo.recorder import EventRecorder
        rec = EventRecorder(output_dir=tmp_path)
        # Before any recording, file_path should be None (no date set)
        assert rec.file_path is None
        rec.close()

    def test_start_time_tracking(self, tmp_path):
        from src.demo.recorder import EventRecorder
        rec = EventRecorder(output_dir=tmp_path, filename='timed.jsonl')
        assert rec.start_time is None

        before = time.time()
        rec.record('test_event')
        after = time.time()

        assert rec.start_time is not None
        assert before <= rec.start_time <= after
        rec.close()

    def test_default_mode_still_works(self, tmp_path):
        """Default (no filename) mode should still use date-based files."""
        from src.demo.recorder import EventRecorder
        rec = EventRecorder(output_dir=tmp_path)
        rec.record('test_event', value=42)
        rec.close()

        files = list(tmp_path.glob('*.jsonl'))
        assert len(files) == 1
        # Filename should be a date like 2026-02-22.jsonl
        assert files[0].stem.count('-') == 2


# ---------------------------------------------------------------------------
# Recording lifecycle (DesktopApp methods)
# ---------------------------------------------------------------------------

class TestRecordingLifecycle:
    """Tests for DesktopApp.start_recording / stop_recording / get_recording_status."""

    def _make_desktop(self, tmp_path):
        """Create a minimal DesktopApp-like object with recording methods."""
        import threading
        from datetime import datetime

        class FakeBot:
            def __init__(self):
                self.running = True
                self.recorder = None

        class FakeDesktop:
            def __init__(self):
                self._bot = None
                self._bot_lock = threading.Lock()
                self._session_recorder = None
                self._session_recorder_lock = threading.Lock()
                self._tmp_path = tmp_path

            def start_recording(self):
                with self._session_recorder_lock:
                    if self._session_recorder is not None:
                        return False, 'Already recording'
                with self._bot_lock:
                    bot = self._bot
                    if bot is None or not bot.running:
                        return False, 'Bot is not running'
                from src.demo.recorder import EventRecorder
                ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                filename = f'recording_{ts}.jsonl'
                recorder = EventRecorder(output_dir=self._tmp_path, filename=filename)
                with self._session_recorder_lock:
                    self._session_recorder = recorder
                with self._bot_lock:
                    if self._bot is not None:
                        self._bot.recorder = recorder
                return True, filename

            def stop_recording(self):
                with self._session_recorder_lock:
                    recorder = self._session_recorder
                    if recorder is None:
                        return False, 'Not recording'
                    self._session_recorder = None
                with self._bot_lock:
                    if self._bot is not None:
                        self._bot.recorder = None
                recorder.close()
                return True, {
                    'filename': recorder._filename,
                    'event_count': recorder.event_count,
                }

            def get_recording_status(self):
                with self._session_recorder_lock:
                    recorder = self._session_recorder
                if recorder is None:
                    return {'recording': False}
                elapsed = 0
                if recorder.start_time:
                    elapsed = int(time.time() - recorder.start_time)
                return {
                    'recording': True,
                    'filename': recorder._filename,
                    'event_count': recorder.event_count,
                    'elapsed_seconds': elapsed,
                }

        desktop = FakeDesktop()
        bot = FakeBot()
        desktop._bot = bot
        return desktop, bot

    def test_start_stop_recording(self, tmp_path):
        desktop, bot = self._make_desktop(tmp_path)

        success, filename = desktop.start_recording()
        assert success
        assert filename.startswith('recording_')
        assert bot.recorder is not None

        # Record some events
        bot.recorder.record('test_event', data='hello')

        success, info = desktop.stop_recording()
        assert success
        assert info['event_count'] == 1
        assert bot.recorder is None

    def test_double_start_prevention(self, tmp_path):
        desktop, bot = self._make_desktop(tmp_path)

        success1, _ = desktop.start_recording()
        assert success1

        success2, error = desktop.start_recording()
        assert not success2
        assert error == 'Already recording'

        desktop.stop_recording()

    def test_bot_not_running_error(self, tmp_path):
        desktop, bot = self._make_desktop(tmp_path)
        desktop._bot = None

        success, error = desktop.start_recording()
        assert not success
        assert 'not running' in error.lower()

    def test_status_during_recording(self, tmp_path):
        desktop, bot = self._make_desktop(tmp_path)

        status = desktop.get_recording_status()
        assert not status['recording']

        desktop.start_recording()
        bot.recorder.record('test_event')

        status = desktop.get_recording_status()
        assert status['recording']
        assert status['event_count'] == 1

        desktop.stop_recording()

    def test_status_after_stop(self, tmp_path):
        desktop, bot = self._make_desktop(tmp_path)
        desktop.start_recording()
        desktop.stop_recording()

        status = desktop.get_recording_status()
        assert not status['recording']


# ---------------------------------------------------------------------------
# Dashboard routes: list and load-demo
# ---------------------------------------------------------------------------

class TestRecordingListRoute:
    """Tests for GET /api/recording/list."""

    @pytest.fixture
    def client(self, tmp_path):
        """Flask test client with DATA_DIR pointed to tmp_path."""
        with patch('dashboard.app.DATA_DIR', tmp_path):
            from dashboard.app import app
            app.config['TESTING'] = True
            with app.test_client() as c:
                yield c

    def _make_recording(self, tmp_path, filename, events):
        rec_dir = tmp_path / 'database' / 'demo_recordings'
        rec_dir.mkdir(parents=True, exist_ok=True)
        filepath = rec_dir / filename
        with open(filepath, 'w') as f:
            for i, evt in enumerate(events):
                evt.setdefault('t', time.time() + i)
                evt.setdefault('type', 'test')
                f.write(json.dumps(evt) + '\n')
        return filepath

    def test_list_with_files(self, tmp_path, client):
        self._make_recording(tmp_path, 'recording_20260222_100000.jsonl', [
            {'type': 'price_tick', 't': 1000.0},
            {'type': 'trade_opened', 't': 1060.0},
        ])
        resp = client.get('/api/recording/list')
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data['recordings']) == 1
        rec = data['recordings'][0]
        assert rec['filename'] == 'recording_20260222_100000.jsonl'
        assert rec['event_count'] == 2
        assert rec['duration_seconds'] == 60

    def test_list_empty_dir(self, tmp_path, client):
        (tmp_path / 'database' / 'demo_recordings').mkdir(parents=True, exist_ok=True)
        resp = client.get('/api/recording/list')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['recordings'] == []

    def test_list_no_dir(self, tmp_path, client):
        resp = client.get('/api/recording/list')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['recordings'] == []


class TestLoadDemoRoute:
    """Tests for POST /api/recording/load-demo."""

    @pytest.fixture
    def client_with_token(self, tmp_path):
        with patch('dashboard.app.DATA_DIR', tmp_path):
            from dashboard.app import app, API_TOKEN
            app.config['TESTING'] = True
            with app.test_client() as c:
                yield c, API_TOKEN

    def _make_recording(self, tmp_path, filename):
        rec_dir = tmp_path / 'database' / 'demo_recordings'
        rec_dir.mkdir(parents=True, exist_ok=True)
        filepath = rec_dir / filename
        filepath.write_text('{"t":1,"type":"test"}\n')
        return filepath

    def test_copies_file_correctly(self, tmp_path, client_with_token):
        client, token = client_with_token
        self._make_recording(tmp_path, 'recording_20260222.jsonl')

        resp = client.post('/api/recording/load-demo',
            json={'filename': 'recording_20260222.jsonl'},
            headers={'X-API-Token': token})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success']

        dest = tmp_path / 'database' / 'demo_recordings' / 'demo_selected.jsonl'
        assert dest.exists()

    def test_rejects_path_traversal(self, tmp_path, client_with_token):
        client, token = client_with_token

        resp = client.post('/api/recording/load-demo',
            json={'filename': '../../../etc/passwd'},
            headers={'X-API-Token': token})
        assert resp.status_code == 400

        resp = client.post('/api/recording/load-demo',
            json={'filename': 'foo/bar.jsonl'},
            headers={'X-API-Token': token})
        assert resp.status_code == 400

    def test_missing_file_returns_404(self, tmp_path, client_with_token):
        client, token = client_with_token
        (tmp_path / 'database' / 'demo_recordings').mkdir(parents=True, exist_ok=True)

        resp = client.post('/api/recording/load-demo',
            json={'filename': 'nonexistent.jsonl'},
            headers={'X-API-Token': token})
        assert resp.status_code == 404


class TestRecordingStatusRoute:
    """Tests for GET /api/recording/status (only available in desktop mode)."""

    def test_not_desktop_mode_returns_404(self):
        """In standalone mode, the route doesn't exist, returning 404."""
        from dashboard.app import app
        app.config['TESTING'] = True
        with app.test_client() as client:
            resp = client.get('/api/recording/status')
            # Route is only registered by DesktopApp, so 404 in standalone
            assert resp.status_code == 404
