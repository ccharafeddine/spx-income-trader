"""
Event recorder for demo/replay mode.

Captures trading bot events as append-only JSONL for later playback.
Designed for minimal overhead (<1ms per event) in the hot loop.
"""

import json
import time
import threading
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

import pytz

logger = logging.getLogger(__name__)

# Fields stripped from recorded events to protect account privacy
_SENSITIVE_KEYS = frozenset({
    'account_id', 'account_number', 'api_key', 'api_secret',
    'access_token', 'refresh_token', 'consumer_key', 'consumer_secret',
})

# Minimum seconds between consecutive price_tick events
_PRICE_TICK_THROTTLE = 30.0


class EventRecorder:
    """Append-only JSONL recorder for trading bot events.

    Thread-safe. Opens the output file lazily on first write.
    One file per trading day: YYYY-MM-DD.jsonl
    """

    def __init__(self, output_dir: Path = None):
        if output_dir is None:
            from src.utils.app_paths import DATA_DIR
            output_dir = DATA_DIR / 'database' / 'recordings'
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._file = None
        self._file_date = None
        self._last_price_tick_t = 0.0
        self._event_count = 0

    @property
    def event_count(self):
        return self._event_count

    @property
    def file_path(self) -> Optional[Path]:
        if self._file_date is None:
            return None
        return self._output_dir / f'{self._file_date}.jsonl'

    def record(self, event_type: str, **data):
        """Record a single event. Thread-safe, <1ms overhead."""
        now = time.time()

        # Throttle price_tick to at most one per _PRICE_TICK_THROTTLE seconds
        if event_type == 'price_tick':
            if now - self._last_price_tick_t < _PRICE_TICK_THROTTLE:
                return
            self._last_price_tick_t = now

        # Build event dict
        event = {'t': now, 'type': event_type}
        event.update(data)

        # Scrub sensitive fields
        _scrub(event)

        line = json.dumps(event, default=str) + '\n'

        with self._lock:
            self._ensure_file(now)
            self._file.write(line)
            self._file.flush()
            self._event_count += 1

    def close(self):
        """Flush and close the file handle."""
        with self._lock:
            if self._file:
                self._file.flush()
                self._file.close()
                self._file = None
                logger.info(f"Recording closed: {self._event_count} events written")

    def _ensure_file(self, ts: float):
        """Open or rotate the output file if the date changed."""
        et = pytz.timezone('America/New_York')
        today = datetime.fromtimestamp(ts, tz=et).strftime('%Y-%m-%d')
        if self._file_date == today and self._file is not None:
            return
        if self._file is not None:
            self._file.flush()
            self._file.close()
        self._file_date = today
        path = self._output_dir / f'{today}.jsonl'
        self._file = open(path, 'a', encoding='utf-8')
        logger.info(f"Recording to {path}")


def _scrub(event: dict):
    """Remove sensitive keys from an event dict in-place."""
    for key in list(event.keys()):
        if key in _SENSITIVE_KEYS:
            del event[key]
    # Also scrub nested dicts (one level deep)
    for key, val in event.items():
        if isinstance(val, dict):
            for k in list(val.keys()):
                if k in _SENSITIVE_KEYS:
                    del val[k]
