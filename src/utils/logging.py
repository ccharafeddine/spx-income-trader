import json
import logging
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from logging.handlers import RotatingFileHandler


class JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects for machine parsing."""

    def format(self, record):
        entry = {
            'ts': datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'msg': record.getMessage(),
        }
        # Attach structured data if the log record carries it
        data = getattr(record, 'data', None)
        if data:
            entry['data'] = data
        # Attach exception info for errors
        if record.exc_info and record.exc_info[1]:
            tb = traceback.format_exception(*record.exc_info)
            entry['data'] = entry.get('data') or {}
            entry['data']['error_type'] = type(record.exc_info[1]).__name__
            # Truncate traceback to 2000 chars
            tb_str = ''.join(tb)
            entry['data']['traceback'] = tb_str[:2000]
        return json.dumps(entry, default=str)


def setup_logging(log_file: str, log_level: str = "INFO"):
    """
    Setup logging configuration.

    Configures:
    - Console handler (human-readable, stdout)
    - Text file handler (rotating, human-readable)
    - Error-only file handler (WARNING+)
    - JSON structured log handler (bot.jsonl)

    Args:
        log_file: Path to log file
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
    """
    # Create logs directory if it doesn't exist
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Configure root logger
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, log_level))

    # Remove existing handlers
    logger.handlers.clear()

    # Console handler (stdout)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%H:%M:%S'
    )
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    # File handler (rotating, max 10MB, keep 5 backups)
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5
    )
    file_handler.setLevel(getattr(logging, log_level))
    file_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    # Error-only file handler (WARNING+ to separate error.log)
    error_log = log_path.parent / 'error.log'
    error_handler = RotatingFileHandler(
        str(error_log),
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(file_formatter)
    logger.addHandler(error_handler)

    # JSON structured log handler (bot.jsonl)
    json_log = log_path.parent / 'bot.jsonl'
    json_handler = RotatingFileHandler(
        str(json_log),
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
    )
    json_handler.setLevel(getattr(logging, log_level))
    json_handler.setFormatter(JSONFormatter())
    logger.addHandler(json_handler)

    # Suppress werkzeug HTTP request logs (floods log files, ~10 lines/sec from dashboard polling)
    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    logger.info("=" * 60)
    logger.info("Logging initialized")
    logger.info(f"Log file: {log_file}")
    logger.info(f"Log level: {log_level}")
    logger.info("=" * 60)


# ---------------------------------------------------------------------------
# Trade event logger (separate from standard logging)
# ---------------------------------------------------------------------------

_trade_logger = None


def get_trade_logger(log_dir: Path = None) -> logging.Logger:
    """Get or create the trade event logger.

    Writes to logs/trades.jsonl with its own rotation. Each line is a
    JSON object representing a trade lifecycle event (entry, exit, rejection).
    """
    global _trade_logger
    if _trade_logger is not None:
        return _trade_logger

    _trade_logger = logging.getLogger('trade_events')
    _trade_logger.setLevel(logging.INFO)
    _trade_logger.propagate = False  # Don't send to root logger

    if log_dir is None:
        from src.utils.app_paths import DATA_DIR
        log_dir = DATA_DIR / 'logs'
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        str(log_dir / 'trades.jsonl'),
        maxBytes=10*1024*1024,
        backupCount=5,
    )
    handler.setFormatter(JSONFormatter())
    _trade_logger.addHandler(handler)
    return _trade_logger


def log_trade_event(event: str, **data):
    """Log a trade lifecycle event to trades.jsonl.

    Args:
        event: Event type (trade_entered, trade_exited, trade_rejected, etc.)
        **data: Structured data for the event.
    """
    tl = get_trade_logger()
    record = tl.makeRecord(
        'trade_events', logging.INFO, '', 0, event, (), None,
    )
    record.data = {'event': event, **data}
    tl.handle(record)


def reset_trade_logger():
    """Reset the trade logger (for testing)."""
    global _trade_logger
    if _trade_logger:
        for h in _trade_logger.handlers[:]:
            h.close()
            _trade_logger.removeHandler(h)
    _trade_logger = None
