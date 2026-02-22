"""
SPX Income Trader - Desktop Application Entry Point

Unified entry point that manages both the Flask dashboard and the
trading bot in a single process.

- Flask dashboard runs in a background daemon thread on 127.0.0.1
- Trading bot runs in its own daemon thread (started on demand via UI)
- Bot is controllable from the dashboard via API endpoints
- System tray icon with bot status (green=running, red=stopped)
- Minimize-to-tray on window close (configurable via keyring)
- Graceful shutdown on SIGINT/SIGTERM or via tray Quit action

Usage:
    python app_desktop.py                  # Native window (PyWebView)
    python app_desktop.py --dev            # System browser instead of native
    python app_desktop.py --headless       # No UI, just Flask server
    python app_desktop.py --port 8080      # Custom port
    python app_desktop.py --no-tray        # Disable system tray icon
"""

import sys
import os
import traceback

# PyInstaller windowed mode (runw.exe) sets sys.stdout/stderr to None.
# Redirect to devnull before any library import that might write to them.
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

_CRASH_LOG = os.path.join(os.path.expanduser('~'), 'spx_crash.log')

try:
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

    # Default to dry-run mode to avoid E*TRADE credential validation on import
    os.environ.setdefault('TRADING_MODE', 'dry-run')

    from config.settings import (
        BASE_DIR, DATABASE_PATH, LOG_FILE, LOG_LEVEL,
        DASHBOARD_PORT, STRATEGY_PARAMS,
    )
    from src.utils.logging import setup_logging
    from src.utils.version import APP_VERSION, check_for_updates

    # Configure logging
    setup_logging(LOG_FILE, LOG_LEVEL)
    logger = logging.getLogger(__name__)
except Exception:
    with open(_CRASH_LOG, 'w') as f:
        f.write(traceback.format_exc())
    raise


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


def _resolve_demo_recording(arg):
    """Resolve a demo recording path from a CLI argument.

    If arg is '__latest__', scan recordings/ and demo_recordings/ for the
    newest .jsonl file.  Otherwise treat arg as a direct file path.
    """
    from src.utils.app_paths import DATA_DIR

    if arg != '__latest__':
        p = Path(arg)
        if p.exists():
            return p
        # Also check relative to project root
        p = PROJECT_ROOT / arg
        if p.exists():
            return p
        raise FileNotFoundError(f"Recording not found: {arg}")

    # Scan for latest .jsonl in known directories
    candidates = []
    for d in [DATA_DIR / 'database' / 'recordings',
              DATA_DIR / 'database' / 'demo_recordings',
              PROJECT_ROOT / 'database' / 'recordings',
              PROJECT_ROOT / 'database' / 'demo_recordings']:
        if d.is_dir():
            candidates.extend(d.glob('*.jsonl'))

    if not candidates:
        raise FileNotFoundError(
            "No demo recordings found. Run: python scripts/generate_demo_recording.py --all"
        )

    # Return the most recently modified
    return max(candidates, key=lambda p: p.stat().st_mtime)


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

        # Bot startup tracking
        self._bot_starting = False

        # Bot crash tracking
        self._bot_crashed = False
        self._bot_crash_error = None
        self._bot_crash_time = None
        self._watchdog_thread = None

        # System tray state
        self._tray_icon = None           # pystray.Icon instance
        self._tray_available = False     # True only when tray started successfully
        self._force_quit = False         # Bypass minimize-to-tray on close

        # Version update check result (populated by background thread)
        self._update_info = None

        # Session recording state
        self._session_recorder = None
        self._session_recorder_lock = threading.Lock()

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

        # Expose the authoritative bot status function so the dashboard's
        # /api/status can use it instead of the lockfile-based check (which
        # doesn't work in desktop mode since the PID is always this process).
        app._desktop_get_bot_status = desktop.get_bot_status

        # Expose bot's in-memory bars so /api/today can use them directly
        # (more reliable than log file parsing for the current session)
        app._desktop_get_bot_bars = desktop.get_bot_bars
        app._desktop_get_bot_bars_5min = desktop.get_bot_bars_5min

        @app.route('/api/bot/start', methods=['POST'])
        def api_bot_start():
            from flask import request, jsonify
            data = request.get_json() or {}
            mode = data.get('mode', 'dry-run')
            if mode not in ('dry-run', 'live'):
                return jsonify({'success': False, 'error': f'Invalid mode: {mode}'}), 400

            success, error = desktop.start_bot(mode)
            if success:
                return jsonify({'success': True, 'mode': mode})
            return jsonify({'success': False, 'error': error}), 409

        @app.route('/api/bot/stop', methods=['POST'])
        def api_bot_stop():
            from flask import jsonify
            logger.info("API /api/bot/stop called")
            success, error = desktop.stop_bot()
            if success:
                logger.info("API /api/bot/stop returning success (signal sent)")
                return jsonify({'success': True, 'message': 'Shutdown signal sent. Poll /api/bot/status to confirm.'})
            logger.warning(f"API /api/bot/stop returning error: {error}")
            return jsonify({'success': False, 'error': error}), 409

        @app.route('/api/bot/status', methods=['GET'])
        def api_bot_status():
            from flask import jsonify
            return jsonify(desktop.get_bot_status())

        @app.route('/api/version', methods=['GET'])
        def api_version():
            from flask import jsonify
            info = desktop._update_info or {
                'current_version': APP_VERSION,
                'latest_version': None,
                'update_available': False,
                'download_url': None,
                'error': None,
            }
            return jsonify(info)

        @app.route('/api/recording/start', methods=['POST'])
        def api_recording_start():
            from flask import jsonify
            success, result = desktop.start_recording()
            if success:
                return jsonify({'success': True, 'filename': result})
            return jsonify({'success': False, 'error': result}), 409

        @app.route('/api/recording/stop', methods=['POST'])
        def api_recording_stop():
            from flask import jsonify
            success, result = desktop.stop_recording()
            if success:
                return jsonify({'success': True, **result})
            return jsonify({'success': False, 'error': result}), 409

        @app.route('/api/recording/status', methods=['GET'])
        def api_recording_status():
            from flask import jsonify
            return jsonify(desktop.get_recording_status())

        @app.route('/api/settings/minimize-to-tray', methods=['GET', 'PUT'])
        def api_minimize_to_tray():
            from flask import request, jsonify
            if request.method == 'GET':
                return jsonify({'enabled': desktop._get_minimize_to_tray()})
            data = request.get_json() or {}
            enabled = bool(data.get('enabled', True))
            desktop._set_minimize_to_tray(enabled)
            return jsonify({'enabled': enabled})

    # ------------------------------------------------------------------
    # Application lifecycle
    # ------------------------------------------------------------------

    def start(self, headless=False, dev=False, no_tray=False):
        """Start the desktop application (blocks until shutdown)."""
        self._no_tray = no_tray
        logger.info("=" * 60)
        logger.info(f"SPX Income Trader v{APP_VERSION} - Desktop Application")
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

        # Check for updates in background (never blocks startup)
        threading.Thread(
            target=self._run_update_check,
            daemon=True,
            name="update-check",
        ).start()

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
            'The Daily Melt',
            self._url,
            width=1400,
            height=900,
            min_size=(1024, 700),
            resizable=True,
            background_color='#0a0e17',
        )

        # Intercept window close for minimize-to-tray behavior
        self._webview_window.events.closing += self._on_webview_closing

        # Start system tray icon in background thread (unless disabled)
        if not self._no_tray:
            self._start_tray()

        try:
            # webview.start() blocks until the window is destroyed
            webview.start()
        except Exception as e:
            # pythonnet / backend not available (e.g. Python 3.14)
            logger.warning(f"PyWebView backend failed: {e}")
            logger.warning("Falling back to system browser")
            self._webview_window = None
            self._stop_tray()
            self._run_dev()
            return

        # Window was destroyed -- clean up
        self._webview_window = None
        self._stop_tray()
        self.shutdown()

    # ------------------------------------------------------------------
    # Version update check
    # ------------------------------------------------------------------

    def _run_update_check(self):
        """Check for updates in background. Failures are silently ignored."""
        try:
            app_cfg = STRATEGY_PARAMS.get('app', {})
            url = app_cfg.get('update_check_url')
            dl_url = app_cfg.get('download_url')
            self._update_info = check_for_updates(
                update_url=url, download_url=dl_url,
            )
        except Exception as exc:
            logger.debug("Update check failed: %s", exc)
            self._update_info = {
                'current_version': APP_VERSION,
                'latest_version': None,
                'update_available': False,
                'download_url': None,
                'error': str(exc),
            }

    # ------------------------------------------------------------------
    # Bot management
    # ------------------------------------------------------------------

    def start_bot(self, mode='dry-run'):
        """Start the trading bot in a background thread.

        Returns:
            (success: bool, error: str | None)
        """
        with self._bot_lock:
            if self._bot is not None and self._bot.running:
                return False, 'Bot is already running'

            self._bot_mode = mode
            self._bot_started_at = datetime.now()
            self._bot_starting = True
            self._bot_crashed = False
            self._bot_crash_error = None
            self._bot_crash_time = None
            self._bot_thread = threading.Thread(
                target=self._run_bot,
                args=(mode,),
                daemon=True,
                name="trading-bot",
            )
            self._bot_thread.start()
            self._start_watchdog()
            return True, None

    def _run_bot(self, mode):
        """Initialize and run the trading bot (called in daemon thread)."""
        bot = None
        try:
            from src.main import TradingBot, BotAlreadyRunningError
            from src.brokers.dry_run_broker import DryRunBroker
            from src.core.strategy import SPXIncomeStrategy
            from database.db_manager import DatabaseManager
            from src.utils.notifications import NotificationManager

            # Select broker based on mode
            if mode == 'dry-run':
                broker = DryRunBroker(initial_balance=50000.0)
                dry_run = True
            else:
                # Live mode: use broker factory to get the active broker
                from src.brokers.broker_factory import get_broker
                from config.settings import STRATEGY_PARAMS as _sp
                active = _sp.get('broker', {}).get('active', 'schwab')

                if active == 'dry_run':
                    # Config says dry_run but mode is live - fall back to dry run
                    broker = DryRunBroker(initial_balance=50000.0)
                    dry_run = True
                else:
                    broker = get_broker(_sp)
                    # Verify authentication for the active broker
                    if active == 'schwab':
                        if not broker.auth.is_authenticated():
                            logger.error("Schwab authentication failed. Run auth script first.")
                            return
                        logger.info("Schwab authentication verified")
                    elif active == 'etrade':
                        if not broker.auth.authenticate():
                            logger.error("E*TRADE authentication failed")
                            return
                        logger.info("E*TRADE authentication verified")
                    dry_run = False

            strategy = SPXIncomeStrategy()
            db_manager = DatabaseManager(DATABASE_PATH)
            notifier = NotificationManager()

            bot = TradingBot(
                broker, strategy, db_manager, notifier,
                dry_run=dry_run,
                skip_confirm=True,  # No stdin prompts in desktop mode
            )

            with self._bot_lock:
                self._bot = bot
                self._bot_starting = False

            self._update_tray_icon()

            # Blocks until bot.running is set to False
            bot.start()

        except BotAlreadyRunningError as e:
            logger.error(f"Cannot start bot: {e}")
            with self._bot_lock:
                self._bot_crashed = True
                self._bot_crash_error = str(e)
                self._bot_crash_time = datetime.now()
            return
        except Exception as e:
            logger.error(f"Bot thread error: {e}", exc_info=True)
        finally:
            # Close any active session recording before bot cleanup
            self._close_session_recorder()

            # Ensure shutdown cleanup (DB event, notification) always runs.
            # shutdown() is idempotent so this is safe even if it was
            # already called from within start() (e.g. fatal-error path).
            if bot is not None:
                logger.info("Bot thread finishing, running shutdown cleanup")
                try:
                    bot.shutdown()
                except Exception as e:
                    logger.warning(f"Error during bot shutdown cleanup: {e}")

            with self._bot_lock:
                self._bot = None
                self._bot_starting = False
                self._bot_mode = None
                self._bot_started_at = None
            logger.info("Bot thread exited, status reset to stopped")
            self._update_tray_icon()

    def stop_bot(self):
        """Signal the trading bot to stop. Returns immediately without
        waiting for the bot thread to finish -- the dashboard should poll
        /api/bot/status to confirm the bot has fully stopped.

        Returns:
            (success: bool, error: str | None)
        """
        with self._bot_lock:
            if self._bot is None or not self._bot.running:
                logger.debug("stop_bot called but bot is not running")
                return False, 'Bot is not running'
            bot = self._bot

        # Signal stop outside the lock so we never block on I/O while
        # holding _bot_lock.  The bot thread will pick up running=False
        # within ~0.5s (interruptible sleep) and perform its own cleanup.
        logger.info("Stop requested: sending shutdown signal to trading bot")
        bot.running = False
        logger.info("Shutdown signal sent (bot will stop after current iteration)")
        return True, None

    # ------------------------------------------------------------------
    # Bot watchdog (supervisor thread)
    # ------------------------------------------------------------------

    def _start_watchdog(self):
        """Start a watchdog thread to monitor the bot thread."""
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            return
        self._watchdog_thread = threading.Thread(
            target=self._run_watchdog,
            daemon=True,
            name="bot-watchdog",
        )
        self._watchdog_thread.start()

    def _run_watchdog(self):
        """Check every 30 seconds if the bot thread is still alive."""
        while not self._shutting_down:
            time.sleep(30)
            with self._bot_lock:
                bot_thread = self._bot_thread
                bot_instance = self._bot
                was_running = bot_instance is not None

            if bot_thread is None:
                break  # No bot thread to watch

            if not bot_thread.is_alive() and was_running:
                # Bot thread died while it was supposed to be running
                self._bot_crashed = True
                self._bot_crash_time = datetime.now()
                err_msg = "Bot thread died unexpectedly"
                self._bot_crash_error = err_msg
                logger.error(f"WATCHDOG: {err_msg}")

                # Send notification if configured
                try:
                    from src.utils.notifications import NotificationManager
                    notifier = NotificationManager()
                    notifier.send(
                        "Bot Crashed",
                        "The trading bot stopped unexpectedly. Please review and restart.",
                    )
                except Exception as e:
                    logger.warning(f"Watchdog failed to send notification: {e}")

                self._update_tray_icon()
                break

            if not bot_thread.is_alive():
                break  # Bot stopped normally

    def get_bot_bars(self):
        """Return completed bars from the bot's in-memory BarBuilder.

        Returns a list of dicts with time/open/high/low/close, or None
        if the bot is not running.
        """
        with self._bot_lock:
            if self._bot is None:
                return None
            try:
                bb = getattr(self._bot, 'bar_builder', None)
                if bb is None:
                    return None
                bars = bb.get_bars()
                result = []
                for b in bars:
                    result.append({
                        'time': b.timestamp.strftime('%H:%M'),
                        'open': b.open,
                        'high': b.high,
                        'low': b.low,
                        'close': b.close,
                    })
                return result
            except Exception:
                return None

    def get_bot_bars_5min(self):
        """Return completed 5-min bars from the bot's in-memory aggregator.

        Returns a list of dicts with time/open/high/low/close, or None
        if the bot is not running.
        """
        with self._bot_lock:
            if self._bot is None:
                return None
            try:
                agg = getattr(self._bot, 'bar_aggregator_5min', None)
                if agg is None:
                    return None
                bars = agg.get_bars()
                result = []
                for b in bars:
                    result.append({
                        'time': b.timestamp.strftime('%H:%M'),
                        'open': b.open,
                        'high': b.high,
                        'low': b.low,
                        'close': b.close,
                    })
                return result
            except Exception:
                return None

    def get_bot_status(self):
        """Return current bot status as a dict."""
        with self._bot_lock:
            # Bot thread is spawned but still initializing (imports, broker auth)
            if self._bot_starting and self._bot is None:
                uptime = 0
                if self._bot_started_at:
                    uptime = int((datetime.now() - self._bot_started_at).total_seconds())
                return {
                    'running': True,
                    'stopping': False,
                    'crashed': False,
                    'mode': self._bot_mode,
                    'uptime_seconds': uptime,
                    'trades_today': 0,
                    'daily_pnl': 0.0,
                    'status': 'starting',
                }

            if self._bot is not None and self._bot.running:
                uptime = 0
                if self._bot_started_at:
                    uptime = int((datetime.now() - self._bot_started_at).total_seconds())
                return {
                    'running': True,
                    'stopping': False,
                    'crashed': False,
                    'mode': self._bot_mode,
                    'uptime_seconds': uptime,
                    'trades_today': self._bot.dte0_trades_today + self._bot.tnt_trades_today,
                    'daily_pnl': getattr(self._bot.portfolio, 'daily_realized_pnl', 0.0),
                }

            # Bot reference still exists but running=False means we sent
            # the stop signal and the thread is still winding down.
            if self._bot is not None and not self._bot.running:
                return {
                    'running': False,
                    'stopping': True,
                    'crashed': False,
                    'mode': self._bot_mode,
                    'uptime_seconds': 0,
                    'trades_today': 0,
                    'daily_pnl': 0.0,
                }

            status = {
                'running': False,
                'stopping': False,
                'mode': None,
                'uptime_seconds': 0,
                'trades_today': 0,
                'daily_pnl': 0.0,
            }
            if self._bot_crashed:
                status['crashed'] = True
                status['crash_error'] = self._bot_crash_error
                status['crash_time'] = (
                    self._bot_crash_time.isoformat()
                    if self._bot_crash_time else None
                )
            else:
                status['crashed'] = False
            return status

    # ------------------------------------------------------------------
    # System tray
    # ------------------------------------------------------------------

    @staticmethod
    def _get_minimize_to_tray():
        """Read the minimize-to-tray preference from keyring."""
        try:
            import keyring
            val = keyring.get_password('SPXIncomeTrader', 'minimize_to_tray')
            return val != 'false'  # Default True when None or 'true'
        except Exception:
            return True

    @staticmethod
    def _set_minimize_to_tray(enabled):
        """Store the minimize-to-tray preference in keyring."""
        try:
            import keyring
            keyring.set_password(
                'SPXIncomeTrader', 'minimize_to_tray',
                'true' if enabled else 'false',
            )
        except Exception as e:
            logger.warning(f"Failed to save minimize_to_tray setting: {e}")

    @staticmethod
    def _load_branded_icon():
        """Load the branded icon PNG for the system tray.

        Checks packaged (_MEIPASS) path first, then source-tree path.
        Returns a 64x64 RGBA Image or None if the file can't be loaded.
        """
        from PIL import Image
        candidates = []
        # Packaged (PyInstaller): <exe_dir>/_internal/assets/icon.png
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            candidates.append(Path(meipass) / 'assets' / 'icon.png')
        # Source tree: <project_root>/assets/icon.png
        candidates.append(PROJECT_ROOT / 'assets' / 'icon.png')
        for p in candidates:
            if p.exists():
                try:
                    img = Image.open(str(p)).convert('RGBA')
                    return img.resize((64, 64), Image.LANCZOS)
                except Exception:
                    continue
        return None

    @staticmethod
    def _create_fallback_icon(color):
        """Fallback: plain colored circle if branded icon can't be loaded."""
        from PIL import Image, ImageDraw
        size = 64
        img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([4, 4, 60, 60], fill=color)
        return img

    def _get_tray_icon_image(self):
        """Return branded icon with a colored status dot overlay.

        Green dot = bot running, red dot = bot stopped.
        Falls back to a plain colored circle if the PNG is unavailable.
        """
        from PIL import ImageDraw
        color = '#00cc66' if self._is_bot_running() else '#cc3333'
        base = self._load_branded_icon()
        if base is None:
            return self._create_fallback_icon(color)
        img = base.copy()
        draw = ImageDraw.Draw(img)
        # Status dot: bottom-right corner, 16px diameter with 2px dark border
        draw.ellipse([44, 44, 62, 62], fill='#1a1a1a', outline='#1a1a1a', width=2)
        draw.ellipse([46, 46, 60, 60], fill=color)
        return img

    def _update_tray_icon(self):
        """Update the tray icon to reflect current bot status."""
        if self._tray_icon is not None:
            try:
                self._tray_icon.icon = self._get_tray_icon_image()
            except Exception:
                pass

    def _is_bot_running(self):
        """Check if the trading bot is currently running."""
        with self._bot_lock:
            return self._bot is not None and self._bot.running

    def _build_tray_menu(self):
        """Build the right-click context menu for the tray icon."""
        import pystray
        return pystray.Menu(
            pystray.MenuItem(
                'Open Dashboard', self._on_tray_open, default=True,
            ),
            pystray.MenuItem(
                lambda item: (
                    f"Bot: {'Running' if self._is_bot_running() else 'Stopped'}"
                ),
                None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                'Start Bot', self._on_tray_start_bot,
                visible=lambda item: not self._is_bot_running(),
            ),
            pystray.MenuItem(
                'Stop Bot', self._on_tray_stop_bot,
                visible=lambda item: self._is_bot_running(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Quit', self._on_tray_quit),
        )

    def _start_tray(self):
        """Create and start the system tray icon in a daemon thread.

        If pystray is missing or crashes at any point the app continues
        without a tray icon.
        """
        try:
            import pystray  # noqa: F811
        except ImportError:
            logger.warning(
                "System tray not available -- app will close on window "
                "close instead of minimizing."
            )
            return

        try:
            self._tray_icon = pystray.Icon(
                'The Daily Melt',
                icon=self._get_tray_icon_image(),
                title='The Daily Melt',
                menu=self._build_tray_menu(),
            )
        except Exception as e:
            logger.warning(
                f"System tray not available ({e}) -- app will close on "
                "window close instead of minimizing."
            )
            self._tray_icon = None
            return

        def _safe_tray_run():
            try:
                self._tray_icon.run()
            except Exception as e:
                logger.warning(
                    f"System tray crashed ({e}) -- app will close on "
                    "window close instead of minimizing."
                )
                self._tray_icon = None
                self._tray_available = False

        tray_thread = threading.Thread(
            target=_safe_tray_run,
            daemon=True,
            name='system-tray',
        )
        tray_thread.start()
        self._tray_available = True
        logger.info("System tray icon started")

    def _stop_tray(self):
        """Stop the system tray icon."""
        icon = self._tray_icon
        self._tray_icon = None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass

    def _on_tray_open(self, icon, item):
        """Tray action: open or focus the dashboard window."""
        if self._webview_window is not None:
            try:
                self._webview_window.show()
                self._webview_window.restore()
            except Exception as e:
                logger.warning(f"Failed to restore window: {e}")

    def _on_tray_start_bot(self, icon, item):
        """Tray action: start the trading bot in dry-run mode."""
        self.start_bot('dry-run')

    def _on_tray_stop_bot(self, icon, item):
        """Tray action: stop the trading bot."""
        self.stop_bot()

    def _on_tray_quit(self, icon, item):
        """Tray action: gracefully shutdown and exit."""
        logger.info("Quit requested from system tray")
        self._force_quit = True
        self._shutting_down = True
        # Destroy the webview window so webview.start() returns
        if self._webview_window is not None:
            try:
                self._webview_window.destroy()
            except Exception:
                pass
        self._stop_tray()

    def _on_webview_closing(self):
        """Intercept window close -- minimize to tray if enabled."""
        if self._force_quit:
            return True  # Allow close (quit was requested)
        if self._get_minimize_to_tray() and self._tray_available and self._tray_icon is not None:
            try:
                self._webview_window.hide()
            except Exception:
                return True  # Hide failed, allow close
            return False  # Prevent close, window is hidden
        return True  # Setting disabled or no tray, allow close

    # ------------------------------------------------------------------
    # Session recording
    # ------------------------------------------------------------------

    def start_recording(self):
        """Start recording bot events to a JSONL file.

        Returns:
            (success: bool, filename_or_error: str)
        """
        with self._session_recorder_lock:
            if self._session_recorder is not None:
                return False, 'Already recording'

        with self._bot_lock:
            bot = self._bot
            if bot is None or not bot.running:
                return False, 'Bot is not running'

        from src.demo.recorder import EventRecorder
        from src.utils.app_paths import DATA_DIR

        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'recording_{ts}.jsonl'
        output_dir = DATA_DIR / 'database' / 'demo_recordings'

        recorder = EventRecorder(output_dir=output_dir, filename=filename)

        with self._session_recorder_lock:
            self._session_recorder = recorder

        # Attach recorder to the bot
        with self._bot_lock:
            if self._bot is not None:
                self._bot.recorder = recorder

        logger.info(f"Session recording started: {filename}")
        return True, filename

    def stop_recording(self):
        """Stop the active session recording.

        Returns:
            (success: bool, info_or_error: dict | str)
        """
        with self._session_recorder_lock:
            recorder = self._session_recorder
            if recorder is None:
                return False, 'Not recording'
            self._session_recorder = None

        # Detach from bot
        with self._bot_lock:
            if self._bot is not None:
                self._bot.recorder = None

        recorder.close()

        info = {
            'filename': recorder._filename,
            'event_count': recorder.event_count,
            'file_path': str(recorder.file_path) if recorder.file_path else None,
        }
        logger.info(f"Session recording stopped: {info['event_count']} events -> {info['filename']}")
        return True, info

    def get_recording_status(self):
        """Return current recording status."""
        with self._session_recorder_lock:
            recorder = self._session_recorder

        if recorder is None:
            return {'recording': False}

        import time as _time
        elapsed = 0
        if recorder.start_time:
            elapsed = int(_time.time() - recorder.start_time)

        return {
            'recording': True,
            'filename': recorder._filename,
            'event_count': recorder.event_count,
            'elapsed_seconds': elapsed,
            'started_at': recorder.start_time,
        }

    def _close_session_recorder(self):
        """Close active session recorder (called during bot shutdown)."""
        with self._session_recorder_lock:
            recorder = self._session_recorder
            if recorder is None:
                return
            self._session_recorder = None

        recorder.close()
        logger.info(f"Session recording auto-closed on bot stop: {recorder.event_count} events")

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self):
        """Graceful shutdown: stop bot, flush DB."""
        if self._shutdown_complete:
            return
        self._shutdown_complete = True
        self._shutting_down = True  # Ensure any loops also exit
        logger.info("Desktop app shutting down...")

        # Stop the system tray icon
        self._stop_tray()

        # Signal the trading bot to stop (don't hold the lock during I/O)
        with self._bot_lock:
            bot = self._bot
        if bot is not None and bot.running:
            logger.info("Signaling trading bot to stop...")
            bot.running = False

        # Wait for the bot thread to finish (it handles its own cleanup)
        if self._bot_thread and self._bot_thread.is_alive():
            logger.info("Waiting for bot thread to exit...")
            self._bot_thread.join(timeout=10)
            if self._bot_thread.is_alive():
                logger.warning("Bot thread did not exit within 10s timeout")
            else:
                logger.info("Bot thread exited cleanly")

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
    # On macOS packaged builds, default tray off (pystray crashes py2app).
    # Users can force-enable with --tray.
    _macos_packaged = (
        sys.platform == 'darwin'
        and getattr(sys, 'frozen', None) is not None
    )
    tray_group = parser.add_mutually_exclusive_group()
    tray_group.add_argument(
        '--no-tray',
        action='store_true',
        default=_macos_packaged,
        help='Disable the system tray icon (default on macOS packaged builds)',
    )
    tray_group.add_argument(
        '--tray',
        action='store_true',
        help='Force-enable the system tray icon',
    )
    parser.add_argument(
        '--demo',
        nargs='?',
        const='__latest__',
        default=None,
        metavar='RECORDING',
        help='Demo mode: replay a recorded session (optional path to .jsonl)',
    )
    args = parser.parse_args()

    # --tray overrides the macOS default
    if args.tray:
        args.no_tray = False

    # Demo mode: set up replay engine before creating DesktopApp
    if args.demo:
        recording_path = _resolve_demo_recording(args.demo)
        logger.info(f"Demo mode: loading {recording_path}")
        from src.demo.replay import ReplayEngine
        engine = ReplayEngine(recording_path)
        engine.load()
        from dashboard.app import app as flask_app
        flask_app._demo_mode = True
        flask_app._replay_engine = engine

    desktop_app = DesktopApp(port=args.port)

    # Register signal handlers for clean shutdown.
    # In native mode: destroy the webview window (triggers normal shutdown path).
    # In headless/dev mode: set the flag so the blocking loop exits.
    def handle_signal(sig, _frame):
        logger.info(f"Signal {sig} received")
        desktop_app._force_quit = True  # Bypass minimize-to-tray
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

    desktop_app.start(headless=args.headless, dev=args.dev, no_tray=args.no_tray)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        with open(_CRASH_LOG, 'a') as f:
            f.write('\n--- main() crash ---\n')
            f.write(traceback.format_exc())
        raise
