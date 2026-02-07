"""
SPX Income Trader - Desktop Application Entry Point

Unified entry point that manages both the Flask dashboard and the
trading bot in a single process.

- Flask dashboard runs in a background daemon thread on 127.0.0.1
- Trading bot runs in its own daemon thread (started on demand via UI)
- Bot is controllable from the dashboard via API endpoints
- Graceful shutdown on SIGINT/SIGTERM or when the window is closed

Usage:
    python app_desktop.py                  # Native window (PyWebView)
    python app_desktop.py --dev            # System browser instead of native
    python app_desktop.py --headless       # No UI, just Flask server
    python app_desktop.py --port 8080      # Custom port
"""

import sys
import os
import signal
import socket
import threading
import time
import logging
import argparse
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

# Add project root to path
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

# Default to paper mode to avoid E*TRADE credential validation on import
os.environ.setdefault('TRADING_MODE', 'paper')

from config.settings import (
    BASE_DIR, DATABASE_PATH, LOG_FILE, LOG_LEVEL,
    DASHBOARD_PORT, STRATEGY_PARAMS,
)
from src.utils.logging import setup_logging

# Configure logging
setup_logging(LOG_FILE, LOG_LEVEL)
logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _find_available_port(start_port, max_tries=11):
    """Find an available port, trying start_port through start_port + max_tries - 1."""
    for offset in range(max_tries):
        port = start_port + offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('127.0.0.1', port))
                return port
        except OSError:
            if offset == 0:
                logger.warning(f"Port {port} is already in use")
            continue
    return None


def _wait_for_server(url, timeout=10):
    """Poll *url* until it responds or *timeout* seconds elapse."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.15)
    return False


class DesktopApp:
    """Manages the lifecycle of the Flask dashboard and trading bot."""

    def __init__(self, port=None):
        # Resolve an available port
        requested = port or DASHBOARD_PORT
        self._port = _find_available_port(requested)
        if self._port is None:
            raise RuntimeError(
                f"No available port found (tried {requested}-{requested + 10})"
            )
        if self._port != requested:
            logger.warning(f"Port {requested} in use, falling back to {self._port}")

        self._url = f"http://127.0.0.1:{self._port}"

        # Bot state
        self._bot = None
        self._bot_thread = None
        self._bot_mode = None
        self._bot_started_at = None
        self._bot_lock = threading.Lock()

        # App state
        self._flask_thread = None
        self._shutting_down = False      # Signals the main loop to exit
        self._shutdown_complete = False   # Guards against double-shutdown
        self._webview_window = None       # Set only in native mode
        self._lockfile = BASE_DIR / 'spx_trader.lock'

        # Import the Flask app and register bot control routes on it
        from dashboard.app import app as flask_app
        self._flask_app = flask_app
        self._register_bot_api()

    # ------------------------------------------------------------------
    # Bot control API (registered on the Flask app)
    # ------------------------------------------------------------------

    def _register_bot_api(self):
        """Register bot control API endpoints on the Flask app."""
        app = self._flask_app
        desktop = self

        @app.route('/api/bot/start', methods=['POST'])
        def api_bot_start():
            from flask import request, jsonify
            data = request.get_json() or {}
            mode = data.get('mode', 'paper')
            if mode not in ('paper', 'dry-run', 'live'):
                return jsonify({'success': False, 'error': f'Invalid mode: {mode}'}), 400

            success, error = desktop.start_bot(mode)
            if success:
                return jsonify({'success': True, 'mode': mode})
            return jsonify({'success': False, 'error': error}), 409

        @app.route('/api/bot/stop', methods=['POST'])
        def api_bot_stop():
            from flask import jsonify
            success, error = desktop.stop_bot()
            if success:
                return jsonify({'success': True})
            return jsonify({'success': False, 'error': error}), 409

        @app.route('/api/bot/status', methods=['GET'])
        def api_bot_status():
            from flask import jsonify
            return jsonify(desktop.get_bot_status())

    # ------------------------------------------------------------------
    # Application lifecycle
    # ------------------------------------------------------------------

    def start(self, headless=False, dev=False):
        """Start the desktop application (blocks until shutdown)."""
        logger.info("=" * 60)
        logger.info("SPX Income Trader - Desktop Application")
        logger.info(f"Dashboard: {self._url}")
        logger.info("=" * 60)

        # Start Flask in a background daemon thread
        self._flask_thread = threading.Thread(
            target=self._run_flask,
            daemon=True,
            name="flask-dashboard",
        )
        self._flask_thread.start()

        # Wait for Flask to actually be ready before opening any UI
        if not _wait_for_server(self._url):
            logger.error("Flask server failed to start within timeout")
            return

        logger.info(f"Dashboard ready at {self._url}")

        # Launch the appropriate UI mode
        if headless:
            self._run_headless()
        elif dev:
            self._run_dev()
        else:
            self._run_native()

    def _run_flask(self):
        """Run the Flask server (called in daemon thread)."""
        self._flask_app.run(
            host='127.0.0.1',
            port=self._port,
            debug=False,
            threaded=True,
            use_reloader=False,
        )

    def _run_headless(self):
        """No UI -- just block until a shutdown signal arrives."""
        try:
            while not self._shutting_down:
                time.sleep(0.5)
        except (KeyboardInterrupt, SystemExit):
            pass
        self.shutdown()

    def _run_dev(self):
        """Open the dashboard in the system browser, then block."""
        import webbrowser
        webbrowser.open(self._url)
        self._run_headless()

    def _run_native(self):
        """Open a PyWebView native window pointing at the dashboard."""
        try:
            import webview
        except ImportError:
            logger.warning(
                "pywebview is not installed -- falling back to system browser. "
                "Install with: pip install pywebview"
            )
            self._run_dev()
            return

        self._webview_window = webview.create_window(
            'SPX Income Trader',
            self._url,
            width=1400,
            height=900,
            min_size=(1024, 700),
            resizable=True,
            background_color='#0a0e17',
        )

        try:
            # webview.start() blocks until the window is closed
            webview.start()
        except Exception as e:
            # pythonnet / backend not available (e.g. Python 3.14)
            logger.warning(f"PyWebView backend failed: {e}")
            logger.warning("Falling back to system browser")
            self._webview_window = None
            self._run_dev()
            return

        # Window was closed -- clean up
        self._webview_window = None
        self.shutdown()

    # ------------------------------------------------------------------
    # Bot management
    # ------------------------------------------------------------------

    def start_bot(self, mode='paper'):
        """Start the trading bot in a background thread.

        Returns:
            (success: bool, error: str | None)
        """
        with self._bot_lock:
            if self._bot is not None and self._bot.running:
                return False, 'Bot is already running'

            self._bot_mode = mode
            self._bot_started_at = datetime.now()
            self._bot_thread = threading.Thread(
                target=self._run_bot,
                args=(mode,),
                daemon=True,
                name="trading-bot",
            )
            self._bot_thread.start()
            return True, None

    def _run_bot(self, mode):
        """Initialize and run the trading bot (called in daemon thread)."""
        try:
            from src.main import TradingBot
            from src.brokers.paper_trader import PaperBroker
            from src.brokers.dry_run_broker import DryRunBroker
            from src.core.strategy import SPXIncomeStrategy
            from database.db_manager import DatabaseManager
            from src.utils.notifications import NotificationManager

            # Select broker based on mode
            if mode == 'dry-run':
                broker = DryRunBroker(initial_balance=50000.0)
                dry_run = True
            elif mode == 'paper':
                broker = PaperBroker(initial_balance=50000.0)
                dry_run = False
            else:
                # Live mode requires E*TRADE authentication
                from src.brokers.etrade_broker import ETradeBroker
                from src.brokers.etrade_auth import ETradeAuth
                etrade_auth = ETradeAuth()
                if not etrade_auth.authenticate():
                    logger.error("E*TRADE authentication failed")
                    return
                broker = ETradeBroker(auth=etrade_auth)
                dry_run = False

            strategy = SPXIncomeStrategy()
            db_manager = DatabaseManager(DATABASE_PATH)
            notifier = NotificationManager()

            # Write lockfile so the dashboard status indicator works
            try:
                self._lockfile.write_text(str(os.getpid()))
            except OSError:
                pass

            bot = TradingBot(
                broker, strategy, db_manager, notifier,
                dry_run=dry_run,
                skip_confirm=True,  # No stdin prompts in desktop mode
            )

            with self._bot_lock:
                self._bot = bot

            # Blocks until bot.running is set to False
            bot.start()

        except Exception as e:
            logger.error(f"Bot thread error: {e}", exc_info=True)
        finally:
            with self._bot_lock:
                self._bot = None
                self._bot_mode = None
                self._bot_started_at = None
            try:
                self._lockfile.unlink(missing_ok=True)
            except OSError:
                pass

    def stop_bot(self):
        """Gracefully stop the trading bot.

        Returns:
            (success: bool, error: str | None)
        """
        with self._bot_lock:
            if self._bot is None or not self._bot.running:
                return False, 'Bot is not running'
            self._bot.shutdown()
            return True, None

    def get_bot_status(self):
        """Return current bot status as a dict."""
        with self._bot_lock:
            if self._bot is not None and self._bot.running:
                uptime = 0
                if self._bot_started_at:
                    uptime = int((datetime.now() - self._bot_started_at).total_seconds())
                return {
                    'running': True,
                    'mode': self._bot_mode,
                    'uptime_seconds': uptime,
                    'trades_today': self._bot.trades_today,
                    'daily_pnl': self._bot.daily_pnl,
                }
            return {
                'running': False,
                'mode': None,
                'uptime_seconds': 0,
                'trades_today': 0,
                'daily_pnl': 0.0,
            }

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self):
        """Graceful shutdown: stop bot, flush DB, clean lockfile."""
        if self._shutdown_complete:
            return
        self._shutdown_complete = True
        self._shutting_down = True  # Ensure any loops also exit
        logger.info("Desktop app shutting down...")

        # Stop the trading bot if it is running
        with self._bot_lock:
            if self._bot is not None and self._bot.running:
                logger.info("Stopping trading bot...")
                self._bot.shutdown()

        # Wait for the bot thread to finish
        if self._bot_thread and self._bot_thread.is_alive():
            self._bot_thread.join(timeout=10)

        # Clean up lockfile
        try:
            self._lockfile.unlink(missing_ok=True)
        except OSError:
            pass

        logger.info("Desktop app shutdown complete")


# ----------------------------------------------------------------------
# CLI entry point
# ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="SPX Income Trader - Desktop Application",
    )
    parser.add_argument(
        '--headless',
        action='store_true',
        help='Run without any UI (server/background mode)',
    )
    parser.add_argument(
        '--dev',
        action='store_true',
        help='Open in system browser instead of native window (development)',
    )
    parser.add_argument(
        '--port',
        type=int,
        default=None,
        help=f'Dashboard port (default: {DASHBOARD_PORT})',
    )
    args = parser.parse_args()

    desktop_app = DesktopApp(port=args.port)

    # Register signal handlers for clean shutdown.
    # In native mode: destroy the webview window (triggers normal shutdown path).
    # In headless/dev mode: set the flag so the blocking loop exits.
    def handle_signal(sig, _frame):
        logger.info(f"Signal {sig} received")
        if desktop_app._webview_window:
            try:
                desktop_app._webview_window.destroy()
            except Exception:
                desktop_app._shutting_down = True
        else:
            desktop_app._shutting_down = True

    signal.signal(signal.SIGINT, handle_signal)
    try:
        signal.signal(signal.SIGTERM, handle_signal)
    except (OSError, ValueError):
        pass  # SIGTERM registration may fail on some platforms

    desktop_app.start(headless=args.headless, dev=args.dev)


if __name__ == '__main__':
    main()
