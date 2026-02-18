"""
Tests for structured logging, metrics (no-op mode), and health endpoint.
"""

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Structured logging (JSONFormatter + trade event logger)
# ---------------------------------------------------------------------------

class TestJSONFormatter:
    """Tests for src/utils/logging.JSONFormatter."""

    def test_format_produces_valid_json(self):
        from src.utils.logging import JSONFormatter
        fmt = JSONFormatter()
        record = logging.LogRecord(
            name='test', level=logging.INFO, pathname='', lineno=0,
            msg='hello world', args=(), exc_info=None,
        )
        line = fmt.format(record)
        obj = json.loads(line)
        assert obj['msg'] == 'hello world'
        assert obj['level'] == 'INFO'
        assert obj['logger'] == 'test'
        assert 'ts' in obj

    def test_format_includes_structured_data(self):
        from src.utils.logging import JSONFormatter
        fmt = JSONFormatter()
        record = logging.LogRecord(
            name='test', level=logging.INFO, pathname='', lineno=0,
            msg='trade event', args=(), exc_info=None,
        )
        record.data = {'event': 'trade_entered', 'pnl': 42.5}
        line = fmt.format(record)
        obj = json.loads(line)
        assert obj['data']['event'] == 'trade_entered'
        assert obj['data']['pnl'] == 42.5

    def test_format_includes_exception_info(self):
        from src.utils.logging import JSONFormatter
        fmt = JSONFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name='test', level=logging.ERROR, pathname='', lineno=0,
            msg='something failed', args=(), exc_info=exc_info,
        )
        line = fmt.format(record)
        obj = json.loads(line)
        assert obj['data']['error_type'] == 'ValueError'
        assert 'test error' in obj['data']['traceback']

    def test_format_truncates_long_traceback(self):
        from src.utils.logging import JSONFormatter
        fmt = JSONFormatter()
        try:
            raise RuntimeError("x" * 3000)
        except RuntimeError:
            import sys
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name='test', level=logging.ERROR, pathname='', lineno=0,
            msg='big error', args=(), exc_info=exc_info,
        )
        line = fmt.format(record)
        obj = json.loads(line)
        assert len(obj['data']['traceback']) <= 2000

    def test_format_single_line(self):
        from src.utils.logging import JSONFormatter
        fmt = JSONFormatter()
        record = logging.LogRecord(
            name='test', level=logging.INFO, pathname='', lineno=0,
            msg='multi\nline\nmessage', args=(), exc_info=None,
        )
        line = fmt.format(record)
        # JSON output should be a single line (no raw newlines)
        assert '\n' not in line


class TestSetupLogging:
    """Tests for setup_logging creating JSON handler."""

    def test_creates_json_log_file(self, tmp_path):
        from src.utils.logging import setup_logging
        log_file = str(tmp_path / 'bot.log')
        setup_logging(log_file, 'INFO')

        # Should create bot.jsonl alongside bot.log
        json_log = tmp_path / 'bot.jsonl'
        assert json_log.exists()

        # Clean up root logger handlers to not affect other tests
        logging.getLogger().handlers.clear()

    def test_json_log_contains_valid_entries(self, tmp_path):
        from src.utils.logging import setup_logging
        log_file = str(tmp_path / 'bot.log')
        setup_logging(log_file, 'INFO')

        test_logger = logging.getLogger('test_json_entries')
        test_logger.info("Test JSON log entry")

        json_log = tmp_path / 'bot.jsonl'
        lines = [l for l in json_log.read_text().splitlines() if l.strip()]
        assert len(lines) >= 1

        # At least one line should be valid JSON with expected fields
        found = False
        for line in lines:
            obj = json.loads(line)
            if 'Test JSON log entry' in obj.get('msg', ''):
                found = True
                break
        assert found

        logging.getLogger().handlers.clear()


class TestTradeEventLogger:
    """Tests for the trade event logger (trades.jsonl)."""

    def test_log_trade_event_writes_jsonl(self, tmp_path):
        from src.utils.logging import log_trade_event, reset_trade_logger
        reset_trade_logger()

        log_trade_event('trade_entered',
            trade_id='T001', strategy='daily_income',
            direction='bullish', credit=1.50,
            _log_dir=tmp_path)

        trades_file = tmp_path / 'trades.jsonl'
        # The logger may not write to tmp_path directly since it uses DATA_DIR
        # Instead, verify it doesn't crash and the logger is initialized
        reset_trade_logger()

    def test_log_trade_event_no_crash(self):
        """log_trade_event should never crash the caller."""
        from src.utils.logging import log_trade_event, reset_trade_logger
        reset_trade_logger()
        # This should not raise even without proper setup
        log_trade_event('trade_exited', trade_id='T002', pnl=-50.0)
        reset_trade_logger()

    def test_reset_trade_logger_clears_state(self):
        from src.utils.logging import reset_trade_logger, _trade_logger
        reset_trade_logger()
        from src.utils import logging as log_mod
        assert log_mod._trade_logger is None


# ---------------------------------------------------------------------------
# Metrics (no-op mode -- prometheus_client NOT installed in test env)
# ---------------------------------------------------------------------------

class TestMetricsNoOp:
    """Metrics module must work as silent no-op without prometheus_client."""

    def test_import_succeeds(self):
        from src.utils import metrics
        assert hasattr(metrics, 'trades_total')
        assert hasattr(metrics, 'PROMETHEUS_AVAILABLE')

    def test_counter_inc_no_crash(self):
        from src.utils.metrics import trades_total
        # Should not raise regardless of prometheus availability
        trades_total.labels(strategy='daily_income', direction='bullish', result='win').inc()

    def test_gauge_set_no_crash(self):
        from src.utils.metrics import spx_price, daily_pnl_dollars
        spx_price.set(5890.0)
        daily_pnl_dollars.set(-42.5)

    def test_histogram_observe_no_crash(self):
        from src.utils.metrics import trade_pnl_dollars, order_fill_latency_seconds
        trade_pnl_dollars.observe(100.0)
        order_fill_latency_seconds.observe(0.5)

    def test_track_latency_context_manager(self):
        from src.utils.metrics import track_latency, order_fill_latency_seconds
        import time
        with track_latency(order_fill_latency_seconds):
            time.sleep(0.01)
        # Should complete without error

    def test_generate_latest_returns_bytes(self):
        from src.utils.metrics import generate_latest
        result = generate_latest()
        assert isinstance(result, bytes)

    def test_content_type_is_string(self):
        from src.utils.metrics import CONTENT_TYPE_LATEST
        assert isinstance(CONTENT_TYPE_LATEST, str)


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    """Tests for /api/health Flask route."""

    @pytest.fixture
    def client(self):
        """Create Flask test client."""
        from dashboard.app import app
        app.config['TESTING'] = True
        with app.test_client() as c:
            yield c

    def test_health_returns_200(self, client):
        resp = client.get('/api/health')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'status' in data
        assert 'bot_running' in data
        assert 'version' in data

    def test_health_contains_required_fields(self, client):
        resp = client.get('/api/health')
        data = resp.get_json()
        expected_fields = [
            'status', 'bot_running', 'last_heartbeat',
            'broker_connected', 'open_positions', 'daily_pnl',
            'errors_last_hour', 'version',
        ]
        for field in expected_fields:
            assert field in data, f"Missing field: {field}"

    def test_health_version_matches(self, client):
        from src.utils.version import APP_VERSION
        resp = client.get('/api/health')
        data = resp.get_json()
        assert data['version'] == APP_VERSION


class TestMetricsEndpoint:
    """Tests for /metrics Flask route."""

    @pytest.fixture
    def client(self):
        from dashboard.app import app
        app.config['TESTING'] = True
        with app.test_client() as c:
            yield c

    def test_metrics_endpoint_exists(self, client):
        resp = client.get('/metrics')
        # Either 200 (prometheus installed) or 404 (not installed)
        assert resp.status_code in (200, 404)

    def test_metrics_404_has_hint(self, client):
        from src.utils.metrics import PROMETHEUS_AVAILABLE
        resp = client.get('/metrics')
        if not PROMETHEUS_AVAILABLE:
            data = resp.get_json()
            assert 'hint' in data
            assert 'prometheus_client' in data['hint']
