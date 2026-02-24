"""
SPX Income Trading System - Main Application

This is the main entry point for the trading bot.
"""

import sys
import os
import argparse
import logging
from pathlib import Path
from datetime import datetime, time
import time as time_module
import signal
import json
import threading
from typing import Optional
import pytz

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import (
    BASE_DIR,
    ETRADE_CONFIG,
    TRADING_MODE,
    STRATEGY_PARAMS,
    DATABASE_PATH,
    LOG_FILE,
    LOG_LEVEL
)
from src.brokers.dry_run_broker import DryRunBroker
from src.brokers.etrade_broker import ETradeBroker
from src.brokers.etrade_auth import ETradeAuth
from src.brokers.broker_factory import get_broker
from src.core.strategy import SPXIncomeStrategy
from src.core.position_manager import PositionManager
from src.core.bar_builder import BarBuilder
from src.core.bar_aggregator_5min import BarAggregator5Min
from src.core.bollinger_filter import BollingerFilter
from src.core.tag_n_turn import TagNTurnStrategy
from src.core.bnb_strategy import BnBStrategy
from src.core.orb_strategy import ORBStrategy
from src.core.portfolio_manager import PortfolioManager, StrategyType
from src.core.pdt_tracker import PDTTracker
from src.core.reconciler import TradeReconciler
from src.data.market_data import MarketDataFeed
from src.data.price_feed import create_price_feed
from database.db_manager import DatabaseManager
from src.utils.notifications import NotificationManager
from src.utils.logging import setup_logging, log_trade_event
from src.utils import metrics


# Configure logging
setup_logging(LOG_FILE, LOG_LEVEL)
logger = logging.getLogger(__name__)


class BotAlreadyRunningError(Exception):
    """Raised when another bot instance is already running."""
    pass


from src.utils.market_calendar import get_market_close_time


class TradingBot:
    """Main trading bot orchestrator"""
    
    def __init__(
        self,
        broker,
        strategy: SPXIncomeStrategy,
        db_manager: DatabaseManager,
        notification_manager: Optional[NotificationManager] = None,
        dry_run: bool = False,
        skip_confirm: bool = False,
        recorder=None,
    ):
        self.broker = broker
        self.strategy = strategy
        self.db = db_manager
        self.notifier = notification_manager
        self.dry_run = dry_run
        self.skip_confirm = skip_confirm
        self.recorder = recorder

        # Initialize PDT Tracker
        pdt_cfg = STRATEGY_PARAMS.get('pdt', {})
        pdt_enabled = pdt_cfg.get('pdt_protection', True)

        # Account equity callback for PDT threshold checking
        def get_account_equity():
            try:
                balance = broker.get_account_balance()
                return balance.get('net_account_value', 0)
            except Exception:
                return 0

        self.pdt_tracker = PDTTracker(
            db_path=DATABASE_PATH,
            enabled=pdt_enabled,
            threshold=pdt_cfg.get('pdt_threshold', 25000),
            max_day_trades=pdt_cfg.get('pdt_max_day_trades', 3),
            window_days=pdt_cfg.get('pdt_window_days', 5),
            get_account_equity=get_account_equity,
        )

        # PDT mode detection for 1pm management
        pdt_threshold = pdt_cfg.get('pdt_threshold', 25000)
        force_pdt = pdt_cfg.get('force_pdt_mode', None)

        if force_pdt is not None:
            strategy.pdt_mode_active = bool(force_pdt)
        elif dry_run:
            starting_capital = STRATEGY_PARAMS.get('portfolio', {}).get('starting_capital',
                               STRATEGY_PARAMS.get('portfolio', {}).get('account_size', 50000))
            strategy.pdt_mode_active = starting_capital < pdt_threshold
        else:
            equity = get_account_equity()
            strategy.pdt_mode_active = (equity or 0) < pdt_threshold

        logger.info(f"PDT mode for 1pm management: {'ACTIVE' if strategy.pdt_mode_active else 'INACTIVE'} "
                    f"(threshold=${pdt_threshold:,})")

        # Initialize components
        self.position_manager = PositionManager(
            broker, strategy, db_manager,
            pdt_tracker=self.pdt_tracker
        )
        self.bar_builder = BarBuilder(interval_minutes=30)
        self.bar_aggregator_5min = BarAggregator5Min()
        filters_cfg = STRATEGY_PARAMS.get('filters', {})
        self.bollinger_enabled = filters_cfg.get('bollinger_enabled', True)
        self.extreme_move_override_pct = filters_cfg.get('extreme_move_override_pct', 1.5)
        self.bollinger = BollingerFilter(
            period=filters_cfg.get('bollinger_period', 50),
            num_std=filters_cfg.get('bollinger_std', 2.0),
            extreme_move_pct=self.extreme_move_override_pct,
        )
        self.market_data = MarketDataFeed(broker)
        self.price_feed = create_price_feed(
            trading_mode='dry-run' if dry_run else 'live',
            broker=broker,
        )

        # State
        self.running = False
        self._shutdown_called = False
        self.tz = pytz.timezone("America/New_York")
        self.dte0_trades_today = 0   # Shared 0DTE slot (DI/ORB/B&B)
        self.tnt_trades_today = 0    # TNT swing slot
        self.current_trading_date = None
        self._market_was_open = False  # Edge detection for recorder

        # Token auto-renewal for live broker (E*TRADE tokens expire after 2 hours)
        self._token_renewal_thread = None
        self._token_renewal_stop = threading.Event()

        # Instance lock (file-based, prevents multiple bot instances)
        self._lock_fd = None
        self._lockfile_path = BASE_DIR / 'bot.lock'

        # Signal log (shared by both dry-run and live modes for dashboard)
        self._signal_log_path = BASE_DIR / 'logs' / 'signals.json'
        self._init_signal_log()

        # B&B on_day_end lifecycle (called once per day at market close)
        self._bnb_day_end_called = False

        # Pending setup state (pulse bar detected, waiting for breakout)
        self.pending_setup = None  # {direction, bar, trigger_price, timestamp}

        # Market state (updated every cycle by _update_market_state)
        self._current_spx_price = 0.0
        self._last_completed_bar = None
        self._stale_price_count = 0  # consecutive cycles with identical price
        self._last_spx_price = 0.0   # previous cycle's price for staleness check

        # Daily journal tracking (lightweight accumulator, flushed at EOD)
        self._journal_rejections = []   # List of {timestamp, strategy, reason, detail}
        self._journal_bars_built = 0
        self._journal_pulse_bars = 0
        self._journal_signals_evaluated = 0
        self._journal_trades_entered = 0
        self._journal_finalized = False
        self._reconciliation_done_today = False

        # Daily loss limit is enforced solely by PortfolioManager.daily_realized_pnl
        # to prevent dual-tracker drift. No separate TradingBot.daily_pnl.

        # Morning setup window (configurable via timing settings)
        timing_cfg = STRATEGY_PARAMS.get('timing', {})
        morning_start_str = timing_cfg.get('morning_start', '09:30')
        morning_end_str = timing_cfg.get('morning_end', '11:30')
        self.morning_start = time(int(morning_start_str.split(':')[0]), int(morning_start_str.split(':')[1]))
        self.morning_end = time(int(morning_end_str.split(':')[0]), int(morning_end_str.split(':')[1]))

        # Afternoon window (Bed & Breakfast system, p.23-26)
        self.afternoon_enabled = timing_cfg.get('afternoon_enabled', False)
        afternoon_start_str = timing_cfg.get('afternoon_start', '15:00')
        afternoon_end_str = timing_cfg.get('afternoon_end', '15:30')
        self.afternoon_start = time(int(afternoon_start_str.split(':')[0]), int(afternoon_start_str.split(':')[1]))
        self.afternoon_end = time(int(afternoon_end_str.split(':')[0]), int(afternoon_end_str.split(':')[1]))

        if self.afternoon_enabled:
            logger.info(f"Afternoon window (Bed & Breakfast): {afternoon_start_str}-{afternoon_end_str} ET")
        else:
            logger.info("Afternoon window (Bed & Breakfast): disabled")

        # Tag 'n Turn swing strategy (separate parallel strategy)
        tnt_cfg = STRATEGY_PARAMS.get('tag_n_turn', {})
        self.tag_n_turn_enabled = tnt_cfg.get('enabled', False)
        if self.tag_n_turn_enabled:
            self.tag_n_turn = TagNTurnStrategy(tnt_cfg)
            logger.info("Tag 'n Turn swing strategy: ENABLED")
        else:
            self.tag_n_turn = None
            logger.info("Tag 'n Turn swing strategy: disabled")

        # B&B (Bed & Breakfast) strategy - overnight signals
        bnb_cfg = STRATEGY_PARAMS.get('bnb', {})
        self.bnb_enabled = bnb_cfg.get('enabled', False)
        if self.bnb_enabled:
            self.bnb_strategy = BnBStrategy(bnb_cfg)
            logger.info("B&B strategy: ENABLED")
        else:
            self.bnb_strategy = None
            logger.info("B&B strategy: disabled")

        # ORB (Opening Range Breakout) strategy
        orb_cfg = STRATEGY_PARAMS.get('orb', {})
        self.orb_enabled = orb_cfg.get('enabled', False)
        if self.orb_enabled:
            self.orb_strategy = ORBStrategy(orb_cfg)
            logger.info("ORB strategy: ENABLED")
        else:
            self.orb_strategy = None
            logger.info("ORB strategy: disabled")

        # Portfolio Manager (coordinates all strategies)
        portfolio_cfg = STRATEGY_PARAMS.get('portfolio', {})
        strat_cfg = STRATEGY_PARAMS.get('strategy', {})
        self.portfolio = PortfolioManager(
            account_size=portfolio_cfg.get('account_size', 50000.0),
            max_total_positions=portfolio_cfg.get('max_total_positions', 2),
            max_0dte_positions=portfolio_cfg.get('max_0dte_positions', 1),
            max_daily_risk_pct=portfolio_cfg.get('max_daily_risk_pct', 5.0),
            max_daily_loss_pct=portfolio_cfg.get('max_daily_loss_pct', 2.0),
            strategy_priority=portfolio_cfg.get('priority'),
            # Position sizing parameters
            daily_contracts=portfolio_cfg.get('daily_contracts', 7),
            swing_contracts=portfolio_cfg.get('swing_contracts', 2),
            spread_width=strat_cfg.get('spread_width', 5.0),
            min_contracts=portfolio_cfg.get('min_contracts', 1),
            max_contracts=portfolio_cfg.get('max_contracts', 10),
            # Layered drawdown limits (weekly/monthly)
            drawdown_limits=portfolio_cfg.get('drawdown_limits'),
        )

        logger.info("TradingBot initialized")
    
    def _start_token_renewal(self):
        """Start background token renewal/health check for the active broker.

        E*TRADE: Renews access tokens every 90 minutes (2-hour expiry).
        Schwab: Checks refresh token health every 60 minutes (7-day expiry).
        """
        auth = getattr(self.broker, 'auth', None)
        if auth is None:
            return

        # Schwab: periodic health check (access token refresh is automatic via schwab-py)
        if hasattr(auth, 'check_token_health'):
            def _schwab_health_loop():
                interval = 60 * 60  # Check every hour
                while not self._token_renewal_stop.wait(interval):
                    try:
                        health = auth.check_token_health()
                        if health['action'] == 'expired':
                            logger.error(
                                "SCHWAB REFRESH TOKEN EXPIRED. "
                                "Trading will fail until re-authenticated. "
                                "Run: python -m src.brokers.schwab_auth"
                            )
                            self.db.log_event("schwab_token_expired",
                                              "Schwab refresh token expired - re-auth required")
                            if self.notifier:
                                self.notifier.send(
                                    "Schwab Token Expired",
                                    "Refresh token expired. Re-authenticate immediately.",
                                    level='critical'
                                )
                        elif health['action'] == 'critical':
                            logger.error(
                                f"Schwab token expires in {health['hours_remaining']:.1f} hours! "
                                "Re-authenticate immediately."
                            )
                            self.db.log_event("schwab_token_critical",
                                              f"Schwab token expires in {health['hours_remaining']:.1f}h")
                            if self.notifier:
                                self.notifier.send(
                                    "Schwab Token Critical",
                                    f"Token expires in {health['hours_remaining']:.1f} hours. Re-authenticate now.",
                                    level='critical'
                                )
                        elif health['action'] == 'warn':
                            logger.warning(
                                f"Schwab token expires in {health['hours_remaining']:.1f} hours. "
                                "Plan to re-authenticate soon."
                            )
                            if self.notifier:
                                self.notifier.send(
                                    "Schwab Token Warning",
                                    f"Token expires in {health['hours_remaining']:.1f} hours. Plan to re-authenticate.",
                                    level='warning'
                                )
                    except Exception as e:
                        logger.error(f"Schwab token health check error: {e}")

            self._token_renewal_stop.clear()
            self._token_renewal_thread = threading.Thread(
                target=_schwab_health_loop, name="schwab-token-health", daemon=True
            )
            self._token_renewal_thread.start()
            logger.info("Schwab token health monitor started (hourly checks)")
            return

        # E*TRADE: active token renewal
        if not hasattr(auth, '_renew_token'):
            return

        def _renewal_loop():
            interval = 90 * 60  # 90 minutes
            while not self._token_renewal_stop.wait(interval):
                try:
                    if auth._renew_token():
                        logger.info("OAuth token renewed successfully")
                    else:
                        logger.warning(
                            "OAuth token renewal failed - session may expire. "
                            "Re-authentication may be needed."
                        )
                except Exception as e:
                    logger.error(f"OAuth token renewal error: {e}")

        self._token_renewal_stop.clear()
        self._token_renewal_thread = threading.Thread(
            target=_renewal_loop, name="token-renewal", daemon=True
        )
        self._token_renewal_thread.start()
        logger.info("Token auto-renewal started (every 90 minutes)")

    def _stop_token_renewal(self):
        """Stop the background token renewal thread."""
        if self._token_renewal_thread and self._token_renewal_thread.is_alive():
            self._token_renewal_stop.set()
            self._token_renewal_thread.join(timeout=5)
            logger.info("Token auto-renewal stopped")

    def _acquire_instance_lock(self):
        """Acquire an OS-level exclusive file lock to prevent multiple bot instances.

        Uses msvcrt.locking on Windows, fcntl.flock on Unix.
        The OS automatically releases the lock when the process exits (even on crash).
        Raises BotAlreadyRunningError if another instance holds the lock.
        """
        self._lock_fd = open(self._lockfile_path, 'a+')
        try:
            self._lock_fd.seek(0)
            if sys.platform == 'win32':
                import msvcrt
                # Lock the first byte (non-blocking)
                try:
                    msvcrt.locking(self._lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
                except (OSError, IOError):
                    # Lock failed -- check if the existing PID is alive
                    self._lock_fd.seek(0)
                    existing = self._lock_fd.read().strip()
                    self._lock_fd.close()
                    self._lock_fd = None
                    existing_pid = int(existing) if existing.isdigit() else None
                    if existing_pid and self._is_pid_alive(existing_pid):
                        raise BotAlreadyRunningError(
                            f"Another bot instance is running (PID {existing_pid})"
                        )
                    # Stale lock -- delete and retry once
                    try:
                        self._lockfile_path.unlink()
                    except OSError:
                        pass
                    self._lock_fd = open(self._lockfile_path, 'a+')
                    self._lock_fd.seek(0)
                    try:
                        msvcrt.locking(self._lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
                    except (OSError, IOError):
                        self._lock_fd.close()
                        self._lock_fd = None
                        raise BotAlreadyRunningError(
                            "Cannot acquire bot lock (retry failed)"
                        )
            else:
                import fcntl
                try:
                    fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (OSError, IOError):
                    # Lock failed -- check if the existing PID is alive
                    self._lock_fd.seek(0)
                    existing = self._lock_fd.read().strip()
                    self._lock_fd.close()
                    self._lock_fd = None
                    existing_pid = int(existing) if existing.isdigit() else None
                    if existing_pid and self._is_pid_alive(existing_pid):
                        raise BotAlreadyRunningError(
                            f"Another bot instance is running (PID {existing_pid})"
                        )
                    # Stale lock -- delete and retry once
                    try:
                        self._lockfile_path.unlink()
                    except OSError:
                        pass
                    self._lock_fd = open(self._lockfile_path, 'a+')
                    try:
                        fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except (OSError, IOError):
                        self._lock_fd.close()
                        self._lock_fd = None
                        raise BotAlreadyRunningError(
                            "Cannot acquire bot lock (retry failed)"
                        )

            # Lock acquired -- write our PID
            self._lock_fd.seek(0)
            self._lock_fd.truncate()
            self._lock_fd.write(str(os.getpid()))
            self._lock_fd.flush()
            logger.info(f"Instance lock acquired: {self._lockfile_path} (PID {os.getpid()})")

        except BotAlreadyRunningError:
            raise
        except Exception as e:
            if self._lock_fd:
                self._lock_fd.close()
                self._lock_fd = None
            raise BotAlreadyRunningError(f"Failed to acquire instance lock: {e}")

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        """Check if a process with the given PID is still running."""
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _release_instance_lock(self):
        """Release the OS-level file lock and delete the lockfile."""
        if self._lock_fd is not None:
            try:
                self._lock_fd.close()  # OS releases the lock automatically
            except OSError:
                pass
            self._lock_fd = None
        # Best-effort removal of the lockfile
        try:
            self._lockfile_path.unlink(missing_ok=True)
        except OSError:
            pass
        logger.info("Instance lock released")

    def start(self):
        """Start the trading bot"""
        self._acquire_instance_lock()
        self.running = True
        self._start_time = time_module.monotonic()
        
        logger.info("=" * 60)
        logger.info("SPX Income Trading Bot Starting")
        logger.info("=" * 60)
        if self.dry_run:
            logger.info("*** DRY RUN MODE - NO ORDERS WILL BE PLACED ***")
            logger.info("Using real market data from Yahoo Finance")
        logger.info(f"Mode: {TRADING_MODE.upper()}")
        logger.info(f"Position limits: 0DTE={self.dte0_trades_today}/1 (DI/ORB/B&B), Swing={self.tnt_trades_today}/1 (TNT)")
        logger.info(f"Max daily loss: ${self.portfolio.max_daily_loss:.0f} ({self.portfolio.max_daily_loss_pct}%)")
        logger.info("=" * 60)

        # Start token auto-renewal for live broker
        self._start_token_renewal()

        # Send startup notification
        if self.notifier:
            self.notifier.send(
                "Trading Bot Started",
                f"Mode: {TRADING_MODE}\nTime: {datetime.now(self.tz).strftime('%H:%M:%S EST')}",
                level='info'
            )
        
        # Log system event
        self.db.log_event("bot_started", "Trading bot started", {
            "mode": TRADING_MODE,
            "dte0_trades": self.dte0_trades_today,
            "tnt_trades": self.tnt_trades_today
        })
        
        # Resolve any trades that expired while bot was offline
        try:
            self.position_manager.resolve_expired_trades()
        except Exception as e:
            logger.warning(f"Failed to resolve expired trades: {e}")

        # Restore daily counters from DB (survives mid-day restarts)
        today = datetime.now(self.tz).date()
        self.current_trading_date = today
        self._restore_daily_counters(today)

        # Reconcile broker positions against DB (live mode only)
        self._reconcile_positions()

        # Seed Bollinger filter with historical data
        if self.bollinger_enabled:
            try:
                bars_loaded = self.bollinger.seed_historical()
                bb_status = self.bollinger.get_status()
                logger.info(
                    f"Bollinger filter: {bars_loaded} bars loaded, "
                    f"bias={bb_status['current_bias'] or 'none'}, "
                    f"bands={'ready' if bb_status['has_data'] else 'insufficient data'}"
                )
            except Exception as e:
                logger.warning(f"Bollinger filter seed failed (will build from live data): {e}")
        else:
            logger.info("Bollinger filter disabled by config")

        # Seed Tag 'n Turn BB filter
        if self.tag_n_turn_enabled and self.tag_n_turn:
            try:
                tnt_bars = self.tag_n_turn.seed_historical()
                tnt_status = self.tag_n_turn.get_status()
                logger.info(
                    f"Tag 'n Turn: {tnt_bars} bars loaded, "
                    f"state={tnt_status['state']}"
                )
            except Exception as e:
                logger.warning(f"Tag 'n Turn seed failed: {e}")

        try:
            self._run_main_loop()
        except KeyboardInterrupt:
            logger.info("Shutdown signal received")
            self.shutdown()
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
            self.shutdown(error=True)
    
    def _interruptible_sleep(self, seconds):
        """Sleep in small increments, exiting early if self.running becomes False."""
        end = time_module.time() + seconds
        while self.running and time_module.time() < end:
            time_module.sleep(min(0.5, end - time_module.time()))

    def _run_main_loop(self):
        """Main trading loop"""
        consecutive_errors = 0
        max_consecutive_errors = 5
        loop_count = 0
        last_heartbeat = datetime.now(self.tz)

        while self.running:
            try:
                current_time = datetime.now(self.tz)
                loop_count += 1

                # Daily reset check - reset counters when date changes
                today = current_time.date()
                if today != self.current_trading_date:
                    if self.current_trading_date is not None:
                        total_prev = self.dte0_trades_today + self.tnt_trades_today
                        logger.info(
                            f"New trading day: {today}. "
                            f"Resetting counters (prev: {total_prev} trades, "
                            f"${self.portfolio.daily_realized_pnl:.2f} P&L)"
                        )
                    self.current_trading_date = today
                    self._restore_daily_counters(today)
                    self.pending_setup = None
                    self.bar_builder.reset()
                    self.bar_aggregator_5min.reset()
                    self._last_completed_bar = None
                    self.bollinger.day_open = None  # Reset for new day, will set at market open
                    self.position_manager._day_open = None
                    self.position_manager._prev_close = None

                    # Reset daily journal counters for new day
                    self._journal_rejections = []
                    self._journal_bars_built = 0
                    self._journal_pulse_bars = 0
                    self._journal_signals_evaluated = 0
                    self._journal_trades_entered = 0
                    self._journal_finalized = False
                    self._reconciliation_done_today = False

                    # Reset parallel strategies for new day
                    self._bnb_day_end_called = False
                    self.portfolio.reset_daily()
                    metrics.circuit_breaker_active.set(0)
                    metrics.daily_pnl_dollars.set(0)
                    if self.orb_enabled and self.orb_strategy:
                        self.orb_strategy.reset_daily()
                    if self.bnb_enabled and self.bnb_strategy:
                        self.bnb_strategy.on_day_start()  # Activate overnight signal

                    # Refresh PDT tracker account equity for new day
                    if self.pdt_tracker:
                        self.pdt_tracker.refresh_account_equity()
                        pdt_status = self.pdt_tracker.get_pdt_status()
                        logger.info(
                            f"PDT status: {pdt_status['day_trades_used']}/{pdt_status['max_day_trades']} "
                            f"day trades used, restricted={pdt_status['is_restricted']}"
                        )

                # Heartbeat logging every 5 minutes
                if (current_time - last_heartbeat).total_seconds() >= 300:
                    setup_str = ""
                    if self.pending_setup:
                        ps = self.pending_setup
                        setup_str = (
                            f" | Pending {ps['direction'].value.upper()} setup "
                            f"- trigger {'above' if ps['direction'].value == 'bullish' else 'below'} "
                            f"${ps['trigger_price']:,.2f}"
                        )
                    bb_str = ""
                    if self.bollinger.has_sufficient_data:
                        bias = self.bollinger.current_bias or 'none'
                        bb_str = f" | BB bias={bias.upper()}"
                    pf_str = ""
                    if not self.price_feed.is_healthy():
                        pf_health = self.price_feed.get_health_status()
                        pf_str = (
                            f" | PRICE FEED UNHEALTHY "
                            f"({pf_health['source']}, "
                            f"{pf_health['consecutive_failures']} failures)"
                        )
                    logger.info(f"[Heartbeat] Loop #{loop_count} at {current_time.strftime('%H:%M:%S')} ET{setup_str}{bb_str}{pf_str}")
                    last_heartbeat = current_time
                    metrics.bot_uptime_seconds.set(
                        time_module.monotonic() - self._start_time)

                    # Persist price feed health for dashboard
                    try:
                        pf_state_path = Path(BASE_DIR) / 'database' / 'price_feed_state.json'
                        pf_state_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(pf_state_path, 'w') as _pf:
                            json.dump(self.price_feed.get_health_status(), _pf)
                    except Exception:
                        pass
                    if self.recorder:
                        self.recorder.record('status_update',
                            loop_count=loop_count, spx_price=self._current_spx_price,
                            daily_pnl=self.portfolio.daily_realized_pnl,
                            bars_built=self._journal_bars_built,
                            pulse_bars=self._journal_pulse_bars,
                            positions_count=len(self.position_manager.get_open_trades()))

                # Check for dashboard settings changes
                settings_changed_file = Path(BASE_DIR) / 'database' / '.settings_changed'
                if settings_changed_file.exists():
                    try:
                        settings_changed_file.unlink()
                        if self.notifier:
                            self.notifier.reload_config()
                            logger.info("Settings changed: notification config reloaded")
                    except OSError:
                        pass

                # Check for on-demand reconciliation request
                recon_trigger = Path(DATABASE_PATH).parent / '.reconcile_requested'
                if recon_trigger.exists() and not self.dry_run:
                    try:
                        recon_trigger.unlink()
                    except OSError:
                        pass
                    self._run_pnl_reconciliation()

                # Check if market is open -- record market_open/market_close transitions
                market_open_now = self._is_market_open(current_time)
                if self.recorder:
                    if market_open_now and not self._market_was_open:
                        self.recorder.record('market_open', spx_price=self._current_spx_price)
                    elif not market_open_now and self._market_was_open:
                        self.recorder.record('market_close',
                            spx_price=self._current_spx_price,
                            daily_pnl=self.portfolio.daily_realized_pnl)
                self._market_was_open = market_open_now

                if not market_open_now:
                    # B&B: Finalize overnight signal once at market close
                    if (self.bnb_enabled and self.bnb_strategy
                            and not self._bnb_day_end_called
                            and self.current_trading_date == current_time.date()
                            and current_time.time() >= time(16, 0)):
                        try:
                            spx_close = self.price_feed.get_latest_price()
                            if spx_close and spx_close > 0:
                                self.bnb_strategy.on_day_end(spx_close)
                                self._bnb_day_end_called = True
                                logger.info(f"B&B on_day_end: SPX close ${spx_close:,.2f}")
                        except Exception as e:
                            logger.warning(f"B&B on_day_end failed: {e}")

                    # Finalize daily journal once after market close
                    if (not self._journal_finalized
                            and self.current_trading_date == current_time.date()
                            and current_time.time() >= time(16, 0)):
                        self._finalize_daily_journal()

                    # Run P&L reconciliation once after market close (live mode only)
                    if not self.dry_run and not self._reconciliation_done_today:
                        self._run_pnl_reconciliation()

                    logger.info(f"Market closed ({current_time.strftime('%a %H:%M')} ET). Next check in 60s...")
                    self._interruptible_sleep(60)
                    consecutive_errors = 0  # Reset error counter
                    continue

                # Monitor existing positions ALWAYS (even when daily limits reached)
                closed_pnl = self.position_manager.monitor_positions()
                if closed_pnl != 0:
                    logger.info(f"Trade closed: ${closed_pnl:+.2f}")

                # Record position updates for demo replay
                if self.recorder:
                    open_positions = self.position_manager.get_open_trades()
                    if open_positions:
                        pos_data = []
                        for p in open_positions:
                            pos_data.append({
                                'trade_id': getattr(p, 'id', str(p)),
                                'unrealized_pnl': getattr(p, 'unrealized_pnl', 0),
                                'spx_price': self._current_spx_price,
                            })
                        self.recorder.record('position_update', positions=pos_data)

                # Update portfolio risk tracking for closed trades (single P&L source of truth)
                self._drain_recently_closed()
                if closed_pnl != 0:
                    logger.info(f"Daily realized P&L: ${self.portfolio.daily_realized_pnl:.2f}")

                # Notify on closed trades
                if self.notifier:
                    for closed in self.position_manager.recently_closed_trades:
                        self.notifier.send(
                            f"Trade Closed: {closed['direction'].upper()}",
                            f"P&L: ${closed['pnl']:+.2f} ({closed['pnl_pct']:+.1f}%)\n"
                            f"Strikes: {closed['strikes']}\n"
                            f"Reason: {closed['reason']}\n"
                            f"Duration: {closed['duration']}\n"
                            f"Daily Total: ${self.portfolio.daily_realized_pnl:+.2f}",
                            level='info'
                        )
                self.position_manager.recently_closed_trades.clear()

                # Update market state: build bars, feed parallel strategies
                # (runs every cycle during market hours, NOT gated by setup window)
                self._update_market_state(current_time)

                # Global circuit breaker - blocks ALL strategies
                if not self._check_daily_loss_circuit_breaker():
                    if not any(r['reason'] == 'circuit_breaker' and r['strategy'] == 'all'
                               for r in self._journal_rejections):
                        self._record_rejection('all', 'circuit_breaker',
                            f"Daily P&L ${self.portfolio.daily_realized_pnl:.2f} hit limit")
                    if self.pending_setup:
                        logger.info("Daily loss circuit breaker active, clearing pending setup")
                        self.pending_setup = None
                    else:
                        logger.info("Daily loss circuit breaker active, monitoring only")
                    self._interruptible_sleep(300)
                    consecutive_errors = 0
                    continue

                # Daily Income 0DTE limit check (shared with ORB/B&B)
                if not self._check_0dte_limit():
                    if self.pending_setup:
                        logger.info("Daily Income limit reached, clearing pending setup")
                        self._record_rejection('daily_income', '0dte_limit_reached',
                            f"Already traded {self.dte0_trades_today} 0DTE today")
                        self.pending_setup = None
                else:
                    # Check pending breakout triggers (only when DI can still trade)
                    self._check_breakout_trigger(current_time)

                # Evaluate completed bars for new pulse setups (bar must have started in a window)
                if self._last_completed_bar and self._bar_in_setup_window(self._last_completed_bar.timestamp):
                    self._check_for_setups()

                # Expire pending setups past their window
                if self.pending_setup and not self._is_setup_window(current_time):
                    ps = self.pending_setup
                    ps_window = ps.get('window', 'morning')
                    if ps_window == 'afternoon':
                        window_end = self.afternoon_end
                        window_label = f"{self.afternoon_start.strftime('%H:%M')}-{self.afternoon_end.strftime('%H:%M')}"
                    else:
                        window_end = self.morning_end
                        window_label = f"{self.morning_start.strftime('%H:%M')}-{self.morning_end.strftime('%H:%M')}"
                    if current_time.time() > window_end:
                        above_below = 'above' if ps['direction'].value == 'bullish' else 'below'
                        logger.info(
                            f"Pending {ps['direction'].value.upper()} setup expired "
                            f"({ps_window} window closed at {window_end.strftime('%H:%M')} ET without breakout). "
                            f"Trigger was {above_below} "
                            f"${ps['trigger_price']:,.2f}"
                        )
                        self._record_rejection('daily_income', 'setup_expired',
                            f"{ps['direction'].value} setup from {ps['bar'].timestamp.strftime('%H:%M')} bar, "
                            f"trigger {above_below} ${ps['trigger_price']:,.2f} never hit before {window_end.strftime('%H:%M')}")
                        if self.recorder:
                            self.recorder.record('setup_expired',
                                direction=ps['direction'].value,
                                trigger_price=ps['trigger_price'],
                                reason=f'{ps_window} window closed')
                        self.pending_setup = None

                # Log outside-window status periodically
                if not self._is_setup_window(current_time):
                    if loop_count == 1 or loop_count % 20 == 0:
                        windows_str = f"{self.morning_start.strftime('%H:%M')}-{self.morning_end.strftime('%H:%M')}"
                        if self.afternoon_enabled:
                            windows_str += f", {self.afternoon_start.strftime('%H:%M')}-{self.afternoon_end.strftime('%H:%M')}"
                        logger.info(f"Outside setup windows ({windows_str} ET). Current: {current_time.strftime('%H:%M')} ET. Monitoring only.")

                # Reset error counter on success
                consecutive_errors = 0

                # Sleep before next iteration
                self._interruptible_sleep(30)
            
            except KeyboardInterrupt:
                raise  # Let this propagate
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Error in main loop ({consecutive_errors}/{max_consecutive_errors}): {e}", 
                            exc_info=True)
            
                # Degrade to monitoring-only if too many consecutive errors
                if consecutive_errors >= max_consecutive_errors:
                    if self.position_manager.open_trades:
                        logger.critical(
                            f"Too many consecutive errors ({consecutive_errors}), but "
                            f"{len(self.position_manager.open_trades)} open trades remain. "
                            f"Continuing in MONITORING-ONLY mode (no new entries)."
                        )
                        # Don't break - keep monitoring positions
                        # The daily limits gate will prevent new entries anyway
                        # since trades_today will be >= max_daily_trades or errors block setup
                    else:
                        logger.critical(f"Too many consecutive errors ({consecutive_errors}), no open trades, shutting down")
                        self.shutdown(error=True)
                        break
            
                # Exponential backoff
                sleep_time = min(60 * (2 ** consecutive_errors), 300)  # Max 5 minutes
                logger.info(f"Sleeping {sleep_time}s before retry...")
                self._interruptible_sleep(sleep_time)
    
    def _restore_daily_counters(self, trade_date):
        """Restore trade counts and daily_pnl from DB (survives mid-day restarts)."""
        try:
            counts = self.db.get_daily_counts_by_strategy(trade_date)
            # DI + ORB share the 0DTE slot (B&B is informational-only)
            self.dte0_trades_today = (
                counts.get('daily_income', 0)
                + counts.get('orb', 0)
            )
            self.tnt_trades_today = counts.get('tag_n_turn', 0)

            # Restore daily P&L into portfolio (single source of truth)
            summary = self.db.get_daily_summary(trade_date)
            self.portfolio.daily_realized_pnl = summary['realized_pnl']

            total = self.dte0_trades_today + self.tnt_trades_today
            logger.info(
                f"Restored daily counters from DB: "
                f"{total} trades (0DTE={self.dte0_trades_today}/1, TNT={self.tnt_trades_today}/1), "
                f"${self.portfolio.daily_realized_pnl:.2f} P&L"
            )
        except Exception as e:
            logger.warning(f"Could not restore daily counters: {e}")
            self.dte0_trades_today = 0
            self.tnt_trades_today = 0
            self.portfolio.daily_realized_pnl = 0.0

    def _reconcile_positions(self):
        """Compare broker positions against DB active trades.

        Logs warnings for any discrepancies. Does not auto-fix to avoid
        accidental interference with real positions.
        """
        if self.dry_run or not hasattr(self.broker, 'get_open_positions'):
            return

        try:
            broker_positions = self.broker.get_open_positions()
            # Filter to SPX option positions only
            spx_positions = [p for p in broker_positions if 'SPX' in p.get('symbol', '')]

            db_active = self.db.get_active_trades_raw()
            db_count = len(db_active)
            broker_count = len(spx_positions)

            if db_count == 0 and broker_count == 0:
                logger.info("Position reconciliation: no open positions (broker and DB agree)")
                return

            if db_count != broker_count:
                logger.warning(
                    f"POSITION MISMATCH: DB has {db_count} active trades, "
                    f"broker has {broker_count} SPX positions. "
                    f"Manual review required!"
                )
                self.db.log_event("reconciliation_mismatch", "Position count mismatch", {
                    'db_active': db_count,
                    'broker_positions': broker_count,
                })
            else:
                logger.info(f"Position reconciliation: {db_count} active trades match broker")

        except Exception as e:
            logger.warning(f"Position reconciliation failed (non-fatal): {e}")

    def _is_market_open(self, dt: datetime) -> bool:
        """Check if market is currently open (respects early close days)."""
        market_open = time(9, 30)
        market_close = get_market_close_time(dt)

        current_time = dt.time()
        is_weekday = dt.weekday() < 5

        return is_weekday and market_open <= current_time < market_close
    
    def _is_setup_window(self, dt: datetime) -> bool:
        """Check if we're in any active setup window (morning or afternoon)."""
        current_time = dt.time()

        # Morning window (from timing config)
        if self.morning_start <= current_time <= self.morning_end:
            return True

        # Afternoon window (Bed & Breakfast)
        if self.afternoon_enabled:
            if self.afternoon_start <= current_time <= self.afternoon_end:
                return True

        return False

    def _get_active_window(self, dt: datetime) -> str:
        """Return which setup window is currently active: 'morning', 'afternoon', or 'none'."""
        current_time = dt.time()

        if self.morning_start <= current_time <= self.morning_end:
            return 'morning'

        if self.afternoon_enabled:
            if self.afternoon_start <= current_time <= self.afternoon_end:
                return 'afternoon'

        return 'none'
    
    def _check_daily_loss_circuit_breaker(self) -> bool:
        """Check global daily loss circuit breaker. Blocks ALL strategies.

        Delegates to PortfolioManager.daily_realized_pnl as the single source
        of truth for daily P&L tracking. This prevents drift between two
        independent trackers after mid-day restarts.
        """
        if self.portfolio.circuit_breaker_triggered:
            return False
        daily_pnl = self.portfolio.daily_realized_pnl
        max_loss = self.portfolio.max_daily_loss
        # Warn at 80% of limit
        if (daily_pnl <= -max_loss * 0.8
                and not getattr(self, '_cb_warning_sent', False)
                and self.notifier):
            self._cb_warning_sent = True
            self.notifier.send(
                "Circuit Breaker Warning",
                f"Daily P&L ${daily_pnl:.2f} approaching limit -${max_loss:.0f}",
                level='warning'
            )
        if daily_pnl <= -max_loss:
            self.portfolio.circuit_breaker_triggered = True
            metrics.circuit_breaker_active.set(1)
            logger.warning(f"Max daily loss reached: ${daily_pnl:.2f} (limit: -${max_loss:.0f})")
            if self.recorder:
                self.recorder.record('circuit_breaker',
                    daily_pnl=daily_pnl, limit=max_loss, triggered=True)
            if self.notifier:
                self.notifier.send(
                    "Daily Loss Limit Reached",
                    f"Daily P&L: ${daily_pnl:.2f}\nStopping new trades.",
                    level='critical'
                )
            return False
        return True

    def _check_0dte_limit(self) -> bool:
        """Shared 0DTE slot: DI/ORB/B&B share 1 trade per day."""
        if self.dte0_trades_today >= 1:
            logger.debug(f"0DTE daily limit reached: {self.dte0_trades_today}/1")
            return False
        return True

    def _check_tnt_limit(self) -> bool:
        """TNT has its own slot: max 1 per day."""
        if self.tnt_trades_today >= 1:
            logger.debug(f"TNT daily limit reached: {self.tnt_trades_today}/1")
            return False
        return True

    def _record_rejection(self, strategy: str, reason: str, detail: str = ''):
        """Record a trade rejection event for the daily journal."""
        self._journal_rejections.append({
            'timestamp': datetime.now(self.tz).strftime('%H:%M:%S'),
            'strategy': strategy,
            'reason': reason,
            'detail': detail,
        })
        metrics.rejections_total.labels(reason=reason).inc()
        log_trade_event('trade_rejected', strategy=strategy, reason=reason, detail=detail)

    def _finalize_daily_journal(self):
        """Write the daily journal entry to DB at end of day."""
        if self._journal_finalized:
            return
        self._journal_finalized = True

        try:
            trade_date = self.current_trading_date
            if not trade_date:
                return

            # Get market data for the day
            spx_open = None
            spx_close = None
            spx_change_pct = None
            vix_level = None
            vix_regime = None

            try:
                from src.data.yahoo_finance import YahooFinanceProvider
                yf_provider = YahooFinanceProvider()
                spx_quote = yf_provider.get_spx_quote()
                if spx_quote:
                    spx_open = spx_quote.get('open')
                    spx_close = spx_quote.get('price')
                    spx_change_pct = spx_quote.get('change_pct')
                vix_quote = yf_provider.get_vix_quote()
                if vix_quote:
                    vix_level = vix_quote.get('price')
            except Exception:
                pass

            # Get VIX regime from drawdown manager or strategy params
            try:
                from src.core.drawdown_manager import DrawdownManager
                dm = DrawdownManager(STRATEGY_PARAMS.get('portfolio', {}))
                if vix_level:
                    vix_regime = dm.get_vix_regime(vix_level)
            except Exception:
                if vix_level:
                    if vix_level < 15:
                        vix_regime = 'low'
                    elif vix_level < 20:
                        vix_regime = 'normal'
                    elif vix_level < 30:
                        vix_regime = 'elevated'
                    else:
                        vix_regime = 'high'

            # Build human-readable summary
            no_trade_summary = self._build_no_trade_summary(
                spx_open, spx_close, spx_change_pct, vix_level, vix_regime
            )

            # Market context
            market_context = {}
            if spx_open and spx_close:
                day_range = abs(spx_close - spx_open)
                if spx_change_pct is not None:
                    if abs(spx_change_pct) < 0.3:
                        market_context['day_type'] = 'flat'
                    elif spx_change_pct > 0.8:
                        market_context['day_type'] = 'trending_up'
                    elif spx_change_pct < -0.8:
                        market_context['day_type'] = 'trending_down'
                    else:
                        market_context['day_type'] = 'choppy'
                market_context['range'] = round(day_range, 2)

            journal_data = {
                'bars_built': self._journal_bars_built,
                'pulse_bars_found': self._journal_pulse_bars,
                'signals_evaluated': self._journal_signals_evaluated,
                'trades_entered': self._journal_trades_entered,
                'spx_open': spx_open,
                'spx_close': spx_close,
                'spx_change_pct': round(spx_change_pct, 2) if spx_change_pct else None,
                'vix_level': round(vix_level, 2) if vix_level else None,
                'vix_regime': vix_regime,
                'rejection_reasons': self._journal_rejections,
                'market_context': market_context,
                'no_trade_summary': no_trade_summary,
            }

            self.db.save_daily_journal(trade_date, journal_data)
            logger.info(
                f"Daily journal saved: {self._journal_bars_built} bars, "
                f"{self._journal_pulse_bars} pulse bars, "
                f"{self._journal_trades_entered} trades, "
                f"{len(self._journal_rejections)} rejections"
            )

            # Send EOD summary notification
            if self.notifier:
                try:
                    dm = self.portfolio.drawdown_manager
                    self.notifier.send_eod_summary({
                        'trades_count': self._journal_trades_entered,
                        'daily_pnl': self.portfolio.daily_realized_pnl,
                        'weekly_pnl': dm.weekly_realized_pnl if dm else 0.0,
                        'monthly_pnl': dm.monthly_realized_pnl if dm else 0.0,
                        'equity': self.portfolio.account_size,
                        'no_trade_reason': no_trade_summary if self._journal_trades_entered == 0 else None,
                    })
                except Exception as eod_err:
                    logger.warning(f"Failed to send EOD summary: {eod_err}")

        except Exception as e:
            logger.warning(f"Failed to save daily journal: {e}")

    def _run_pnl_reconciliation(self):
        """Run P&L reconciliation against broker and write result to state file."""
        try:
            reconciler = TradeReconciler(self.broker, self.db)
            result = reconciler.reconcile(self.current_trading_date)

            # Write state file for dashboard
            recon_path = Path(DATABASE_PATH).parent / 'reconciliation_state.json'
            recon_path.parent.mkdir(parents=True, exist_ok=True)
            with open(recon_path, 'w') as f:
                json.dump(result, f, indent=2)

            n_disc = len(result['discrepancies'])
            logger.info(
                f"Reconciliation complete: {result['matched']} matched, "
                f"{n_disc} discrepancies, DB P&L ${result['total_db_pnl']:+.2f}"
            )

            if n_disc > 0 and self.notifier:
                summary_lines = [f"{d['field']}: {d['status']}" for d in result['discrepancies'][:5]]
                self.notifier.send_alert(
                    f"P&L Reconciliation: {n_disc} discrepancy(ies) found",
                    '\n'.join(summary_lines),
                )

            self._reconciliation_done_today = True

        except Exception as e:
            logger.warning(f"Reconciliation failed: {e}")

    def _build_no_trade_summary(self, spx_open, spx_close, spx_change_pct, vix_level, vix_regime):
        """Build a human-readable summary of why no trades were taken."""
        parts = []

        if self._journal_pulse_bars > 0:
            parts.append(f"{self._journal_pulse_bars} pulse bar{'s' if self._journal_pulse_bars > 1 else ''} detected")
            # Check if any setup formed but didn't trigger
            setup_reasons = [r for r in self._journal_rejections if r['reason'] == 'setup_expired']
            if setup_reasons:
                for sr in setup_reasons:
                    parts.append(sr['detail'])
            elif self._journal_trades_entered == 0:
                # Pulse bar but no trade - check specific rejections
                credit_reasons = [r for r in self._journal_rejections if r['reason'] == 'credit_below_minimum']
                if credit_reasons:
                    parts.append(f"credit too low ({credit_reasons[0]['detail']})")
                else:
                    breaker_reasons = [r for r in self._journal_rejections if r['reason'] == 'circuit_breaker']
                    if breaker_reasons:
                        parts.append("circuit breaker blocked entry")
                    else:
                        parts.append("no breakout confirmation")
        else:
            parts.append("No pulse bars formed")

        # Add market context
        market_bits = []
        if spx_change_pct is not None:
            direction = 'up' if spx_change_pct > 0 else 'down'
            market_bits.append(f"SPX {direction} {abs(spx_change_pct):.2f}%")
        if vix_level and vix_regime:
            market_bits.append(f"VIX {vix_level:.1f} ({vix_regime})")
        if market_bits:
            parts.append('. '.join(market_bits))

        return '. '.join(parts) + '.' if parts else 'No data available.'

    def _drain_recently_closed(self):
        """Process recently_closed trades into portfolio manager."""
        for closed in self.position_manager.recently_closed:
            self.portfolio.close_position(closed['id'], closed['pnl'])
            pnl = closed['pnl']
            result = 'win' if pnl >= 0 else 'loss'
            metrics.trades_total.labels(
                strategy=closed.get('strategy', 'unknown'),
                direction=closed.get('direction', 'unknown'),
                result=result,
            ).inc()
            metrics.trade_pnl_dollars.observe(pnl)
            metrics.daily_pnl_dollars.set(self.portfolio.daily_realized_pnl)
            metrics.open_positions_count.set(
                len(self.position_manager.get_open_trades()))
            log_trade_event('trade_exited',
                trade_id=closed['id'], pnl=pnl,
                pnl_pct=closed.get('pnl_pct', 0),
                exit_reason=closed.get('exit_reason', ''),
                strategy=closed.get('strategy', 'unknown'),
                direction=closed.get('direction', 'unknown'))

            if self.recorder:
                self.recorder.record('trade_exited',
                    trade_id=closed['id'],
                    pnl=closed['pnl'],
                    pnl_pct=closed.get('pnl_pct', 0),
                    exit_reason=closed.get('exit_reason', ''),
                    duration_minutes=closed.get('duration_minutes', 0))

            # Check consecutive loss streak for notification
            if closed['pnl'] < 0 and self.notifier:
                dm = self.portfolio.drawdown_manager
                if dm and dm.consec_pause_until is not None:
                    self.notifier.send(
                        "Trading Paused: Consecutive Losses",
                        f"{dm.consecutive_losses} consecutive losses. "
                        f"Trading paused until {dm.consec_pause_until.strftime('%Y-%m-%d %H:%M')}.",
                        level='critical'
                    )
                elif dm and dm.consecutive_losses >= 3:
                    self.notifier.send(
                        "Loss Streak Warning",
                        f"{dm.consecutive_losses} consecutive losses.",
                        level='warning'
                    )
        self.position_manager.recently_closed.clear()

    def _update_market_state(self, current_time):
        """Update market data, build bars, and run parallel strategy checks.

        Called every cycle during market hours, regardless of setup window.
        This ensures bars build continuously and parallel strategies (Tag 'n Turn,
        ORB, B&B) receive data even outside the daily income setup window.
        """
        try:
            # Get current SPX price via price feed abstraction
            price = self.price_feed.get_latest_price()
            if price is None or price == 0:
                logger.warning("Failed to get SPX price")
                return
            self._current_spx_price = price

            current_price = self._current_spx_price

            # Stale price detection: warn if price unchanged for 3+ cycles
            if current_price == self._last_spx_price and current_price > 0:
                self._stale_price_count += 1
                if self._stale_price_count >= 3:
                    logger.warning(
                        f"STALE PRICE: SPX ${current_price:,.2f} unchanged for "
                        f"{self._stale_price_count} consecutive cycles - "
                        f"possible data feed issue or trading halt"
                    )
            else:
                self._stale_price_count = 0
            self._last_spx_price = current_price

            logger.debug(f"SPX price: ${current_price:,.2f}")
            metrics.spx_price.set(current_price)

            # Record price tick (throttled inside recorder)
            if self.recorder:
                vix = getattr(self, '_last_vix_level', None)
                self.recorder.record('price_tick', spx_price=current_price, vix_level=vix)

            # Set day open price for extreme move override (first price of the day)
            if self.bollinger.day_open is None and current_price > 0:
                self.bollinger.set_day_open(current_price)

                # Cache day_open and prev_close for trade context
                try:
                    bar_data = self.price_feed.get_latest_bar_data()
                    if bar_data:
                        day_open = bar_data.get('open', current_price)
                        prev_close = bar_data.get('previous_close', 0)
                    else:
                        # Broker feeds don't return OHLC; fall back to Yahoo
                        from src.data.yahoo_finance import YahooFinanceProvider
                        spx_quote = YahooFinanceProvider().get_spx_quote()
                        day_open = spx_quote.get('open', current_price) if spx_quote else current_price
                        prev_close = spx_quote.get('previous_close', 0) if spx_quote else 0
                    if day_open and day_open > 0:
                        self.position_manager.set_daily_cache(day_open, prev_close)
                except Exception as e:
                    logger.warning(f"Failed to cache daily open/prev_close: {e}")

            # Update bar builder (runs every cycle so bars build continuously)
            self._last_completed_bar = self.bar_builder.add_price(current_time, current_price)
            self.bar_aggregator_5min.add_price(current_time, current_price)

            # Log current bar building status periodically (with pending setup info)
            if self.bar_builder.current_bar_start and self.bar_builder.tick_count % 5 == 0:
                setup_str = ""
                if self.pending_setup:
                    ps = self.pending_setup
                    above_below = 'above' if ps['direction'].value == 'bullish' else 'below'
                    dist = abs(current_price - ps['trigger_price'])
                    setup_str = (
                        f" | Pending {ps['direction'].value.upper()} "
                        f"trigger {above_below} ${ps['trigger_price']:,.2f} "
                        f"({dist:.1f}pts away)"
                    )
                logger.info(f"Building bar {self.bar_builder.current_bar_start.strftime('%H:%M')}: "
                           f"O=${self.bar_builder.open_price:.2f} H=${self.bar_builder.high_price:.2f} "
                           f"L=${self.bar_builder.low_price:.2f} C=${current_price:.2f} "
                           f"({self.bar_builder.tick_count} ticks)"
                           f"{setup_str}")

            # Process completed bar through all filters and parallel strategies
            bar = self._last_completed_bar
            if bar:
                self._journal_bars_built += 1
                logger.info(f"New 30-min bar completed: {bar}")
                if self.recorder:
                    self.recorder.record('bar_complete',
                        time=bar.timestamp.isoformat(),
                        open=bar.open, high=bar.high,
                        low=bar.low, close=bar.close,
                        tick_count=getattr(bar, 'tick_count', 0))

                # Feed bar to Bollinger filter
                self.bollinger.add_bar(bar)

                # Feed bar to Tag 'n Turn strategy (if enabled)
                if self.tag_n_turn_enabled and self.tag_n_turn:
                    self.tag_n_turn.on_bar_complete(bar)

                # ORB: Set opening range on first bar (starts 9:30, completes at 10:00)
                if self.orb_enabled and self.orb_strategy:
                    if bar.timestamp.time() == time(9, 30):
                        self.orb_strategy.set_opening_range(bar)

                # B&B: Process bar for end-of-day signals (15:00-16:00)
                if self.bnb_enabled and self.bnb_strategy:
                    self.bnb_strategy.on_bar_complete(bar, current_price)

            # Check parallel strategy tick-level signals (run every cycle, own timing)
            # These execute BEFORE the Daily Income limit gate so they're never
            # blocked by DI's max_daily_trades=1.

            if self.tag_n_turn_enabled and self.tag_n_turn:
                tnt_signal = self.tag_n_turn.check_entry_signal(current_price)
                if tnt_signal:
                    logger.info(
                        f"TAG 'N TURN SIGNAL: {tnt_signal['direction'].value.upper()} "
                        f"entry @ ${current_price:,.2f}, "
                        f"target=${tnt_signal['target_price']:,.2f}"
                    )
                    self.db.log_event("tag_n_turn_signal", "Tag 'n Turn entry signal", {
                        "direction": tnt_signal['direction'].value,
                        "entry_price": current_price,
                        "target_price": tnt_signal['target_price'],
                        "stop_price": tnt_signal['stop_price'],
                    })
                    # Execute if TNT limit allows and circuit breaker not tripped
                    if not self._check_daily_loss_circuit_breaker():
                        self._record_rejection('tag_n_turn', 'circuit_breaker',
                            f"Daily P&L ${self.portfolio.daily_realized_pnl:.2f} hit limit")
                    elif not self._check_tnt_limit():
                        self._record_rejection('tag_n_turn', 'tnt_limit_reached',
                            f"Already traded {self.tnt_trades_today} TNT today")
                    if (self._check_daily_loss_circuit_breaker()
                            and self._check_tnt_limit()):
                        tnt_cfg = STRATEGY_PARAMS.get('tag_n_turn', {})
                        exp_str = self._find_dte_expiration(
                            tnt_cfg.get('min_dte', 3), tnt_cfg.get('max_dte', 7)
                        )
                        success = self._execute_strategy_trade(
                            strategy_type=StrategyType.TAG_N_TURN,
                            direction=tnt_signal['direction'],
                            current_price=current_price,
                            is_0dte=False,
                            spread_width=tnt_cfg.get('spread_width', 10.0),
                            min_credit=tnt_cfg.get('min_credit', 2.00),
                            expiration_str=exp_str,
                        )
                        if success:
                            self.tag_n_turn.on_position_opened({
                                'direction': tnt_signal['direction'].value,
                                'entry_price': current_price,
                                'target_price': tnt_signal['target_price'],
                                'stop_price': tnt_signal['stop_price'],
                                'entry_time': datetime.now(self.tz).isoformat(),
                            })
                        else:
                            # Reset state machine so it can re-signal on next tick
                            self.tag_n_turn._reset_to_idle("Trade execution failed")

                tnt_exit = self.tag_n_turn.check_exit_conditions(current_price)
                if tnt_exit:
                    logger.info(
                        f"TAG 'N TURN EXIT SIGNAL: {tnt_exit['reason']} @ ${current_price:,.2f}"
                    )
                    self.db.log_event("tag_n_turn_exit", "Tag 'n Turn exit signal", {
                        "reason": tnt_exit['reason'],
                        "exit_price": current_price,
                    })

                    # Execute the close
                    tnt_positions = self.portfolio.get_positions_by_strategy(StrategyType.TAG_N_TURN)
                    if tnt_positions:
                        trade_id = tnt_positions[0].position_id
                        realized = self.position_manager.close_trade_by_id(
                            trade_id, f"TNT: {tnt_exit['reason']}"
                        )
                        if realized is not None:
                            self._drain_recently_closed()
                            logger.info(
                                f"TNT trade closed, P&L: ${realized:+.2f}, "
                                f"daily: ${self.portfolio.daily_realized_pnl:.2f}"
                            )
                            self.tag_n_turn.on_position_closed({
                                'reason': tnt_exit['reason'],
                                'exit_price': current_price,
                                'exit_time': datetime.now(self.tz).isoformat(),
                                'pnl': realized,
                            })
                            self._log_signal("TNT_EXIT", {
                                "trade_id": trade_id,
                                "strategy": "tag_n_turn",
                                "reason": tnt_exit['reason'],
                                "exit_price": current_price,
                                "pnl": realized,
                            })
                        else:
                            logger.warning(
                                "TNT exit: close order not filled, will retry next cycle"
                            )
                    else:
                        logger.warning(
                            "TNT exit signal but no TNT position found in portfolio"
                        )

            if self.orb_enabled and self.orb_strategy:
                orb_signal = self.orb_strategy.check_breakout(current_price, current_time)
                if orb_signal:
                    logger.info(
                        f"ORB SIGNAL: {orb_signal['direction'].upper()} "
                        f"entry @ ${current_price:,.2f} ({orb_signal['trigger']})"
                    )
                    self.db.log_event("orb_signal", "ORB breakout signal", orb_signal)
                    # Execute if 0DTE limit allows and circuit breaker not tripped
                    if (self._check_daily_loss_circuit_breaker()
                            and self._check_0dte_limit()):
                        from src.models.spread import TradeDirection as TD
                        orb_dir = TD.BULLISH if orb_signal['direction'] == 'bullish' else TD.BEARISH
                        orb_ok = self._execute_strategy_trade(
                            strategy_type=StrategyType.ORB,
                            direction=orb_dir,
                            current_price=current_price,
                            is_0dte=True,
                            spread_width=5.0,
                        )
                        if not orb_ok:
                            # Rollback premature state set by check_breakout()
                            self.orb_strategy.rollback_entry()
                    else:
                        # Limit or circuit breaker blocked execution - rollback strategy state
                        if not self._check_daily_loss_circuit_breaker():
                            self._record_rejection('orb', 'circuit_breaker',
                                f"Daily P&L ${self.portfolio.daily_realized_pnl:.2f} hit limit")
                        else:
                            self._record_rejection('orb', '0dte_limit_reached',
                                f"Already traded {self.dte0_trades_today} 0DTE today")
                        self.orb_strategy.rollback_entry()

            # B&B is informational-only (no entry) - bias checked in _check_for_setups()

        except Exception as e:
            logger.error(f"Error updating market state: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Parallel strategy execution helpers
    # ------------------------------------------------------------------

    def _find_dte_expiration(self, min_dte: int, max_dte: int) -> str:
        """Find an expiration date in the given DTE range, skipping weekends.

        Returns YYYY-MM-DD string for the midpoint DTE.
        """
        from datetime import timedelta
        now = datetime.now(self.tz)
        target_dte = (min_dte + max_dte) // 2
        exp_date = now.date() + timedelta(days=target_dte)
        # Skip weekends
        while exp_date.weekday() >= 5:  # 5=Sat, 6=Sun
            exp_date += timedelta(days=1)
        return exp_date.strftime("%Y-%m-%d")

    def _construct_strategy_spread(
        self,
        current_price: float,
        direction,
        options_chain,
        spread_width: float = 5.0,
        min_credit: float = 1.00,
        expiration_str: str = None,
    ):
        """Construct a credit spread with configurable parameters.

        Like SPXIncomeStrategy.construct_spread() but accepts variable
        spread_width, min_credit, and expiration. Reuses ATM strike logic.

        Returns:
            CreditSpread or None
        """
        from src.models.spread import CreditSpread, OptionLeg, TradeDirection

        try:
            # Round to nearest $5 strike (same as strategy._select_strikes)
            if current_price < 1000 or current_price > 20000:
                logger.error(f"SPX price ${current_price:,.2f} outside range")
                return None

            atm_strike = round(current_price / 5) * 5

            if direction == TradeDirection.BULLISH:
                short_strike = atm_strike
                long_strike = short_strike - spread_width
                option_type = 'put'
            else:
                short_strike = atm_strike
                long_strike = short_strike + spread_width
                option_type = 'call'

            if short_strike not in options_chain or long_strike not in options_chain:
                logger.warning(f"Strikes ${short_strike}/${long_strike} not in chain")
                return None

            if direction == TradeDirection.BULLISH:
                short_bid = options_chain[short_strike]['put_bid']
                short_ask = options_chain[short_strike]['put_ask']
                long_bid = options_chain[long_strike]['put_bid']
                long_ask = options_chain[long_strike]['put_ask']
            else:
                short_bid = options_chain[short_strike]['call_bid']
                short_ask = options_chain[short_strike]['call_ask']
                long_bid = options_chain[long_strike]['call_bid']
                long_ask = options_chain[long_strike]['call_ask']

            short_price = short_bid
            long_price = long_ask

            # Bid-ask sanity: reject stale/zero chains or excessively wide spreads
            if short_bid <= 0 or long_ask <= 0:
                logger.warning(
                    f"Zero/negative option price: short_bid=${short_bid}, "
                    f"long_ask=${long_ask} - chain may be stale"
                )
                self._record_rejection('spread', 'stale_chain',
                    f"Zero option prices (short_bid={short_bid}, long_ask={long_ask})")
                return None
            for label, bid, ask in [('short', short_bid, short_ask), ('long', long_bid, long_ask)]:
                if ask > 0 and bid > 0:
                    spread_pct = (ask - bid) / ((ask + bid) / 2)
                    if spread_pct > 0.50:
                        logger.warning(
                            f"Wide bid-ask on {label} leg: bid=${bid:.2f} ask=${ask:.2f} "
                            f"({spread_pct:.0%}) - chain may be stale"
                        )

            credit = short_price - long_price

            # Mid-price for slippage tracking
            short_mid = (short_bid + short_ask) / 2
            long_mid = (long_bid + long_ask) / 2
            theoretical_mid_credit = round(short_mid - long_mid, 4)

            if credit <= 0:
                logger.warning(f"Non-positive credit ${credit:.2f}, rejecting")
                self._record_rejection('spread', 'credit_below_minimum',
                    f"Credit ${credit:.2f} (non-positive)")
                return None
            if credit < min_credit:
                logger.warning(f"Credit ${credit:.2f} below min ${min_credit:.2f}")
                self._record_rejection('spread', 'credit_below_minimum',
                    f"Credit ${credit:.2f} < min ${min_credit:.2f}")
                return None

            short_leg = OptionLeg(
                strike=short_strike, option_type=option_type,
                action='sell', price=short_price,
            )
            long_leg = OptionLeg(
                strike=long_strike, option_type=option_type,
                action='buy', price=long_price,
            )

            now = datetime.now(self.tz)
            if expiration_str:
                exp_date = datetime.strptime(expiration_str, "%Y-%m-%d")
                close_t = get_market_close_time(exp_date)
                expiration = self.tz.localize(
                    exp_date.replace(hour=close_t.hour, minute=close_t.minute,
                                     second=0, microsecond=0)
                )
            else:
                close_t = get_market_close_time(now)
                expiration = now.replace(hour=close_t.hour, minute=close_t.minute,
                                         second=0, microsecond=0)

            spread = CreditSpread(
                direction=direction,
                short_leg=short_leg,
                long_leg=long_leg,
                credit_received=credit,
                entry_time=now,
                expiration=expiration,
                underlying_price_at_entry=current_price,
                theoretical_mid_credit=theoretical_mid_credit,
            )

            logger.info(
                f"Spread constructed: {spread.direction.value.upper()} "
                f"${short_strike}/{long_strike}, credit=${credit:.2f}, "
                f"width=${spread_width}"
            )
            return spread

        except Exception as e:
            logger.error(f"Failed to construct strategy spread: {e}", exc_info=True)
            return None

    def _execute_strategy_trade(
        self,
        strategy_type: StrategyType,
        direction,
        current_price: float,
        is_0dte: bool = True,
        spread_width: float = 5.0,
        min_credit: float = 1.00,
        expiration_str: str = None,
    ) -> bool:
        """Generic execution for any parallel strategy trade.

        Steps: get chain -> construct spread -> size position -> risk gate ->
        enter trade -> register -> log.

        Returns True if trade executed successfully.
        """
        from src.models.spread import TradeDirection

        strategy_name = strategy_type.value
        logger.info(f"Executing {strategy_name.upper()} trade: {direction.value.upper()} @ ${current_price:,.2f}")

        # PDT entry gate - block if no day trade slots available
        if not self.pdt_tracker.can_open_trade():
            logger.warning(f"{strategy_name}: Trade BLOCKED by PDT entry gate - no day trade slots")
            self._record_rejection(strategy_name, 'pdt_restricted', 'No day trade slots available')
            self.db.log_event("pdt_entry_blocked", f"PDT blocked {strategy_name} entry", {
                "strategy": strategy_name,
                "direction": direction.value,
                "price": current_price,
            })
            return False

        try:
            # 1. Get options chain
            if expiration_str:
                chain_exp = expiration_str
            else:
                chain_exp = datetime.now(self.tz).strftime("%Y-%m-%d")
            options_chain = self.broker.get_options_chain("SPX", chain_exp)
            if not options_chain:
                logger.warning(f"{strategy_name}: No options chain for {chain_exp}")
                return False

            # 2. Construct spread
            spread = self._construct_strategy_spread(
                current_price, direction, options_chain,
                spread_width=spread_width,
                min_credit=min_credit,
                expiration_str=expiration_str,
            )

            # Retry with synthetic chain in dry-run mode if real chain failed
            if not spread and hasattr(self.broker, '_build_synthetic_chain'):
                logger.warning(f"{strategy_name}: Retrying with synthetic pricing")
                synthetic_chain = self.broker._build_synthetic_chain("SPX", chain_exp)
                if synthetic_chain:
                    spread = self._construct_strategy_spread(
                        current_price, direction, synthetic_chain,
                        spread_width=spread_width,
                        min_credit=min_credit,
                        expiration_str=expiration_str,
                    )

            if not spread:
                logger.warning(f"{strategy_name}: Failed to construct spread")
                return False

            # 3. Position sizing
            max_risk_per_contract = spread.max_risk
            quantity = self.portfolio.calculate_position_size(
                strategy=strategy_type,
                max_risk_per_contract=max_risk_per_contract,
            )

            # 4. Portfolio risk gate
            allowed, deny_reason = self.portfolio.can_enter_position(
                strategy=strategy_type,
                contracts=quantity,
                max_risk_per_contract=max_risk_per_contract,
                is_0dte=is_0dte,
            )
            if not allowed:
                logger.warning(f"{strategy_name}: Portfolio risk blocked: {deny_reason}")
                self._record_rejection(strategy_name, 'portfolio_risk_blocked', deny_reason)
                return False

            # 5. Enter trade (auto-confirm for parallel strategies)
            # Use a dummy bar since parallel strategies don't use pulse bar setup
            from src.models.bar import Bar
            dummy_bar = Bar(
                timestamp=datetime.now(self.tz),
                open=current_price, high=current_price,
                low=current_price, close=current_price,
            )

            trade = self.position_manager.enter_trade(
                spread, dummy_bar, quantity,
                strategy_type=strategy_name,
            )
            if not trade:
                logger.error(f"{strategy_name}: Trade execution failed")
                return False

            actual_qty = trade.quantity

            # 6. Update counters
            self._journal_trades_entered += 1
            if is_0dte:
                self.dte0_trades_today += 1
            else:
                self.tnt_trades_today += 1

            metrics.open_positions_count.set(
                len(self.position_manager.get_open_trades()))
            log_trade_event('trade_entered',
                trade_id=trade.id, strategy=strategy_name,
                direction=spread.direction.value,
                short_strike=spread.short_leg.strike,
                long_strike=spread.long_leg.strike,
                credit=spread.credit_received, quantity=actual_qty,
                underlying_price=current_price)

            if self.recorder:
                self.recorder.record('trade_entered',
                    trade_id=trade.id, strategy=strategy_name,
                    direction=spread.direction.value,
                    strikes={'short': spread.short_leg.strike,
                             'long': spread.long_leg.strike},
                    credit=spread.credit_received, quantity=actual_qty)

            # 7. Register with portfolio
            self.portfolio.register_position(
                position_id=trade.id,
                strategy=strategy_type,
                direction=spread.direction.value,
                contracts=actual_qty,
                max_risk=max_risk_per_contract * actual_qty,
                is_0dte=is_0dte,
            )

            # 8. Log signal
            self._log_signal(f"{strategy_name.upper()}_ENTRY", {
                "trade_id": trade.id,
                "strategy": strategy_name,
                "direction": spread.direction.value,
                "short_strike": spread.short_leg.strike,
                "long_strike": spread.long_leg.strike,
                "quantity": actual_qty,
                "credit_received": spread.credit_received,
                "theoretical_mid_credit": spread.theoretical_mid_credit,
                "max_risk": spread.max_risk * actual_qty,
                "underlying_price": current_price,
                "expiration": spread.expiration.isoformat() if spread.expiration else None,
                "is_0dte": is_0dte,
            })

            # 9. Notification
            if self.notifier:
                self.notifier.send(
                    f"{strategy_name.upper()} Trade: {spread.direction.value.upper()}",
                    f"Strikes: ${spread.short_leg.strike}/${spread.long_leg.strike}\n"
                    f"Credit: ${spread.credit_received:.2f}\n"
                    f"Contracts: {actual_qty}",
                    level='info'
                )

            # Partial fill notification
            if actual_qty != quantity and self.notifier:
                self.notifier.send(
                    "Partial Fill Warning",
                    f"Requested {quantity} contracts, filled {actual_qty}.\n"
                    f"Strategy: {strategy_name}\n"
                    f"Strikes: ${spread.short_leg.strike}/${spread.long_leg.strike}\n"
                    f"Trade {trade.id[:8]} proceeding with {actual_qty} contracts.",
                    level='warning'
                )

            logger.info(f"{strategy_name}: Trade entered {trade.id}")
            return True

        except Exception as e:
            logger.error(f"{strategy_name}: Execution error: {e}", exc_info=True)
            return False

    def _check_breakout_trigger(self, current_time):
        """Check if a pending setup's breakout level has been hit.

        Called every cycle after daily limits check. Separated from
        _update_market_state so breakout entries respect daily limits.

        Re-checks the circuit breaker here because _update_market_state()
        may have closed positions (B&B/TNT exit) that pushed daily P&L
        past the limit since the main loop's circuit breaker check.
        """
        if not self.pending_setup:
            return
        # Re-check circuit breaker (may have been tripped by strategy closes)
        if not self._check_daily_loss_circuit_breaker():
            logger.info("Circuit breaker tripped during breakout check, clearing pending setup")
            self.pending_setup = None
            return
        # Only block on Daily Income's own positions (not ORB/TNT/B&B)
        if self.portfolio.has_position_for_strategy(StrategyType.DAILY_INCOME):
            return

        current_price = self._current_spx_price
        if current_price <= 0:
            return

        ps = self.pending_setup
        triggered = False

        if ps['direction'].value == 'bullish' and current_price > ps['trigger_price']:
            logger.info(
                f"BREAKOUT CONFIRMED: SPX ${current_price:,.2f} > "
                f"setup bar high ${ps['trigger_price']:,.2f} "
                f"(BULLISH trigger from {ps['bar'].timestamp.strftime('%H:%M')} bar)"
            )
            triggered = True
        elif ps['direction'].value == 'bearish' and current_price < ps['trigger_price']:
            logger.info(
                f"BREAKOUT CONFIRMED: SPX ${current_price:,.2f} < "
                f"setup bar low ${ps['trigger_price']:,.2f} "
                f"(BEARISH trigger from {ps['bar'].timestamp.strftime('%H:%M')} bar)"
            )
            triggered = True

        if triggered:
            if self.recorder:
                self.recorder.record('breakout_trigger',
                    direction=ps['direction'].value,
                    current_price=current_price,
                    trigger_price=ps['trigger_price'])
            setup_bar = ps['bar']
            direction = ps['direction']
            self.pending_setup = None
            self._execute_setup(setup_bar, current_price, direction, breakout_time=current_time)

    def _bar_in_setup_window(self, bar_time) -> bool:
        """Check if a bar's start time falls within any setup window.

        Uses strict less-than for the end boundary so a bar starting at exactly
        the window end (e.g. 11:30) is excluded -- it would complete at 12:00,
        outside the intended window.
        """
        if bar_time.tzinfo is None:
            ct = bar_time.time()
        else:
            ct = bar_time.astimezone(self.tz).time()

        if self.morning_start <= ct < self.morning_end:
            return True

        if self.afternoon_enabled:
            if self.afternoon_start <= ct < self.afternoon_end:
                return True

        return False

    def _check_for_setups(self):
        """Evaluate the most recently completed bar for a pulse bar setup.

        Only called when a bar completed whose start time falls within a setup
        window.  Market state updates (bar building, parallel strategies) happen
        in _update_market_state() which runs every market-hours cycle.
        """
        bar = self._last_completed_bar
        if not bar:
            return

        current_price = self._current_spx_price
        if current_price <= 0:
            return

        try:
            # Only block on Daily Income's own positions
            if self.portfolio.has_position_for_strategy(StrategyType.DAILY_INCOME):
                logger.debug("Daily Income already has open position, skipping setup check")
                self._record_rejection('daily_income', 'position_limit',
                    'Daily Income position already open')
                return

            current_time = datetime.now(self.tz)

            # Evaluate for pulse bar setup
            self._journal_signals_evaluated += 1
            direction = self.strategy.evaluate_setup(bar, current_price)

            if self.recorder:
                self.recorder.record('signal_evaluated',
                    bar_time=bar.timestamp.isoformat(),
                    result=direction.value if direction else None)

            if direction:
                # Morning bias filter: block bearish DI on Up/Strong Up days
                if self.strategy.di_morning_bias_filter and direction.value == 'bearish':
                    day_open = getattr(self.position_manager, '_day_open', None)
                    allowed, regime, move_pct = SPXIncomeStrategy.check_morning_bias_filter(
                        direction, day_open, current_price
                    )
                    if not allowed:
                        logger.info(
                            f"DI bearish setup skipped — morning bias is {regime} "
                            f"(SPX {'+' if move_pct >= 0 else ''}{move_pct:.2f}%)"
                        )
                        self._record_rejection('daily_income', 'morning_bias_filter',
                            f"Bearish pulse at {bar.timestamp.strftime('%H:%M')}, "
                            f"bias {regime} (SPX {'+' if move_pct >= 0 else ''}{move_pct:.2f}%)")
                        return

                self._journal_pulse_bars += 1
                metrics.signals_total.labels(strategy='daily_income', direction=direction.value).inc()
                log_trade_event('signal_detected', strategy='daily_income',
                    direction=direction.value, bar_time=bar.timestamp.isoformat(),
                    trigger_price=bar.high if direction.value == 'bullish' else bar.low)
                # Pulse bar detected -- store as pending setup, DON'T enter yet
                trigger_price = bar.high if direction.value == 'bullish' else bar.low
                above_below = 'above' if direction.value == 'bullish' else 'below'

                if self.recorder:
                    self.recorder.record('pulse_detected',
                        direction=direction.value,
                        bar={'time': bar.timestamp.strftime('%H:%M'),
                             'open': bar.open, 'high': bar.high,
                             'low': bar.low, 'close': bar.close},
                        trigger_price=trigger_price)

                if self.pending_setup:
                    logger.info(
                        f"Replacing pending {self.pending_setup['direction'].value.upper()} setup "
                        f"with new {direction.value.upper()} pulse bar"
                    )

                active_window = self._get_active_window(current_time)
                self.pending_setup = {
                    'direction': direction,
                    'bar': bar,
                    'trigger_price': trigger_price,
                    'timestamp': current_time,
                    'window': active_window,
                }

                if self.recorder:
                    self.recorder.record('setup_created',
                        direction=direction.value,
                        trigger_price=trigger_price,
                        window=active_window)

                window_label = f" [{active_window} window]" if active_window != 'morning' else ""
                logger.info(
                    f"PENDING SETUP: {direction.value.upper()} pulse bar at "
                    f"{bar.timestamp.strftime('%H:%M')} "
                    f"(H=${bar.high:.2f} L=${bar.low:.2f} C=${bar.close:.2f}). "
                    f"Entry trigger: price {above_below} ${trigger_price:,.2f}{window_label}"
                )

                # B&B confluence check (informational only)
                if self.bnb_enabled and self.bnb_strategy:
                    bnb_bias = self.bnb_strategy.get_bias()
                    if bnb_bias:
                        if self.bnb_strategy.validate_signal(current_price):
                            di_dir = direction.value  # 'bullish' or 'bearish'
                            if bnb_bias == di_dir:
                                logger.info(f"B&B confluence confirmed: {bnb_bias.upper()} bias matches DI")
                            else:
                                logger.info(f"B&B divergence warning: {bnb_bias.upper()} bias vs DI {di_dir.upper()}")
                        else:
                            logger.info("B&B signal invalidated by morning gap")
            else:
                logger.debug("No setup detected")

        except Exception as e:
            logger.error(f"Error checking for setups: {e}", exc_info=True)
    
    def _execute_setup(self, setup_bar, current_price, direction, breakout_time=None):
        """Execute a trading setup after breakout confirmation.

        Args:
            setup_bar: The pulse bar that formed the setup.
            current_price: SPX price at breakout confirmation.
            direction: TradeDirection (BULLISH/BEARISH).
            breakout_time: Datetime when breakout was confirmed (None for legacy calls).
        """
        try:
            logger.info("=" * 60)
            logger.info(f"EXECUTING {direction.value.upper()} SETUP")
            if breakout_time:
                logger.info(
                    f"Setup bar: {setup_bar.timestamp.strftime('%H:%M')} | "
                    f"Breakout confirmed: {breakout_time.strftime('%H:%M:%S')} ET"
                )
            logger.info("=" * 60)

            # PDT entry gate - block if no day trade slots available
            if not self.pdt_tracker.can_open_trade():
                logger.warning("DI setup BLOCKED by PDT entry gate - no day trade slots")
                self._record_rejection('daily_income', 'pdt_restricted', 'No day trade slots available')
                self.db.log_event("pdt_entry_blocked", "PDT blocked DI entry", {
                    "strategy": "daily_income",
                    "direction": direction.value,
                    "price": current_price,
                })
                return

            # Get options chain (format: YYYY-MM-DD for dry_run_broker compatibility)
            expiration = datetime.now(self.tz).strftime("%Y-%m-%d")
            options_chain = self.broker.get_options_chain("SPX", expiration)

            # Construct spread
            spread = self.strategy.construct_spread(
                current_price,
                direction,
                options_chain
            )

            # If spread construction failed (e.g. stale/zero prices from Yahoo),
            # retry with synthetic chain in dry-run mode
            if not spread and hasattr(self.broker, '_build_synthetic_chain'):
                logger.warning("Failed to construct spread with real chain, retrying with synthetic pricing")
                synthetic_chain = self.broker._build_synthetic_chain("SPX", expiration)
                if synthetic_chain:
                    spread = self.strategy.construct_spread(
                        current_price,
                        direction,
                        synthetic_chain
                    )

            if not spread:
                logger.warning("Failed to construct spread")
                self._record_rejection('daily_income', 'stale_chain',
                    f"Could not construct {direction.value} spread at SPX {current_price:.0f}")
                return

            # Display trade details
            logger.info(f"Direction: {spread.direction.value.upper()}")
            logger.info(f"Short Strike: ${spread.short_leg.strike}")
            logger.info(f"Long Strike: ${spread.long_leg.strike}")
            logger.info(f"Credit: ${spread.credit_received:.2f}")
            logger.info(f"Max Profit: ${spread.max_profit:.2f}")
            logger.info(f"Max Risk: ${spread.max_risk:.2f}")
            logger.info(f"Breakeven: ${spread.breakeven:.2f}")

            # Confirm trade
            if not self._confirm_trade(spread):
                logger.info("Trade cancelled by user")
                return

            # Calculate position size based on configured method
            # max_risk per contract = (spread_width - credit) * 100
            max_risk_per_contract = spread.max_risk
            quantity = self.portfolio.calculate_position_size(
                strategy=StrategyType.DAILY_INCOME,
                max_risk_per_contract=max_risk_per_contract,
            )

            # Check portfolio risk limits before entry
            allowed, deny_reason = self.portfolio.can_enter_position(
                strategy=StrategyType.DAILY_INCOME,
                contracts=quantity,
                max_risk_per_contract=max_risk_per_contract,
                is_0dte=True,
            )
            if not allowed:
                logger.warning(f"Portfolio risk check blocked entry: {deny_reason}")
                self._record_rejection('daily_income', 'portfolio_risk_blocked', deny_reason)
                return

            logger.info(
                f"Position sizing: {quantity} contracts "
                f"(risk ${max_risk_per_contract:.2f} per contract, "
                f"max risk ${quantity * max_risk_per_contract:.2f}, "
                f"account ${self.portfolio.account_size:,.0f})"
            )
            trade = self.position_manager.enter_trade(
                spread,
                setup_bar,
                quantity,
                breakout_time=breakout_time,
                strategy_type='daily_income'
            )

            if trade:
                # BB agreement analytics (never gates entry)
                trade.bb_agreement = SPXIncomeStrategy.compute_bb_agreement(
                    self.bollinger, spread.direction
                )
                actual_qty = trade.quantity
                logger.info(f"Trade executed: {trade.id}")
                self._journal_trades_entered += 1
                metrics.open_positions_count.set(
                    len(self.position_manager.get_open_trades()))
                log_trade_event('trade_entered',
                    trade_id=trade.id, strategy='daily_income',
                    direction=spread.direction.value,
                    short_strike=spread.short_leg.strike,
                    long_strike=spread.long_leg.strike,
                    credit=spread.credit_received, quantity=actual_qty,
                    underlying_price=current_price)

                self.dte0_trades_today += 1

                if self.recorder:
                    self.recorder.record('trade_entered',
                        trade_id=trade.id, strategy='daily_income',
                        direction=spread.direction.value,
                        strikes={'short': spread.short_leg.strike,
                                 'long': spread.long_leg.strike},
                        credit=spread.credit_received, quantity=actual_qty)

                # Register with portfolio manager for risk tracking
                self.portfolio.register_position(
                    position_id=trade.id,
                    strategy=StrategyType.DAILY_INCOME,
                    direction=spread.direction.value,
                    contracts=actual_qty,
                    max_risk=max_risk_per_contract * actual_qty,
                    is_0dte=True,
                )

                # Log signal for dashboard (works in both dry-run and live)
                self._log_signal("TRADE_ENTRY", {
                    "trade_id": trade.id,
                    "direction": spread.direction.value,
                    "short_strike": spread.short_leg.strike,
                    "long_strike": spread.long_leg.strike,
                    "quantity": actual_qty,
                    "credit_received": spread.credit_received,
                    "theoretical_mid_credit": spread.theoretical_mid_credit,
                    "max_risk": spread.max_risk * actual_qty,
                    "underlying_price": current_price,
                    "expiration": spread.expiration.isoformat() if spread.expiration else None,
                    "sizing_method": "budget",
                })

                # Clear pending setup now that we've entered
                self.pending_setup = None

                # Send notification
                if self.notifier:
                    self.notifier.send(
                        f"Trade Entered: {spread.direction.value.upper()}",
                        f"Strikes: ${spread.short_leg.strike}/${spread.long_leg.strike}\n"
                        f"Credit: ${spread.credit_received:.2f}\n"
                        f"Max Profit: ${spread.max_profit:.2f}\n"
                        f"Contracts: {actual_qty}",
                        level='info'
                    )

                # Partial fill notification
                if actual_qty != quantity and self.notifier:
                    self.notifier.send(
                        "Partial Fill Warning",
                        f"Requested {quantity} contracts, filled {actual_qty}.\n"
                        f"Strikes: ${spread.short_leg.strike}/${spread.long_leg.strike}\n"
                        f"Trade {trade.id[:8]} proceeding with {actual_qty} contracts.",
                        level='warning'
                    )
            else:
                logger.error("Trade execution failed")

        except Exception as e:
            logger.error(f"Error executing setup: {e}", exc_info=True)
    
    def _confirm_trade(self, spread) -> bool:
        """Prompt user to confirm trade (in interactive mode)"""
        # Always show trade details
        print("\n" + "=" * 60)
        if self.dry_run:
            print("DRY RUN - TRADE SIGNAL DETECTED")
        else:
            print("TRADE CONFIRMATION REQUIRED")
        print("=" * 60)
        print(f"Direction: {spread.direction.value.upper()}")
        print(f"Short Strike: ${spread.short_leg.strike}")
        print(f"Long Strike: ${spread.long_leg.strike}")
        print(f"Credit: ${spread.credit_received:.2f}")
        print(f"Max Profit: ${spread.max_profit:.2f}")
        print(f"Max Risk: ${spread.max_risk:.2f}")
        print("=" * 60)

        # In dry-run mode with skip_confirm, auto-accept
        if self.dry_run and self.skip_confirm:
            print("[DRY RUN] Auto-accepting signal (--no-confirm)")
            return True

        # Skip confirmation in non-interactive mode or if flag set
        if self.skip_confirm or not sys.stdin.isatty():
            return True

        if self.dry_run:
            response = input("Log this signal? (yes/no): ").strip().lower()
        else:
            response = input("Execute this trade? (yes/no): ").strip().lower()
        return response in ['yes', 'y']
    
    # ------------------------------------------------------------------
    # Signal logging (unified for dry-run and live modes)
    # ------------------------------------------------------------------

    MAX_SIGNALS = 5000

    def _init_signal_log(self):
        """Ensure the signal log file exists."""
        self._signal_log_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._signal_log_path.exists():
            with open(self._signal_log_path, 'w') as f:
                json.dump({"signals": [], "created": datetime.now(self.tz).isoformat()}, f)

    def _log_signal(self, signal_type: str, data: dict):
        """Log a trading signal to the unified signals.json file.

        Called from _execute_setup() on both entry and exit so the
        dashboard /api/signals endpoint works in all modes.
        Uses atomic write (temp file + rename) to prevent corruption.
        """
        try:
            with open(self._signal_log_path, 'r') as f:
                log_data = json.load(f)

            signal = {
                "timestamp": datetime.now(self.tz).isoformat(),
                "type": signal_type,
                "mode": "dry-run" if self.dry_run else "live",
                **data,
            }
            log_data["signals"].append(signal)

            # Rotate when log gets too large
            if len(log_data["signals"]) > self.MAX_SIGNALS:
                archive = self._signal_log_path.with_suffix('.old.json')
                with open(archive, 'w') as f:
                    json.dump(log_data, f, indent=2, default=str)
                log_data["signals"] = log_data["signals"][-1000:]
                logger.info(f"Signal log rotated, archived to {archive}")

            # Atomic write: write to temp file then rename
            import tempfile
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=self._signal_log_path.parent,
                suffix='.tmp',
            )
            try:
                with os.fdopen(tmp_fd, 'w') as f:
                    json.dump(log_data, f, indent=2, default=str)
                os.replace(tmp_path, self._signal_log_path)
            except Exception:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

        except Exception as e:
            logger.error(f"Failed to log signal: {e}")

    def shutdown(self, error: bool = False):
        """Shutdown the trading bot. Safe to call multiple times."""
        if self._shutdown_called:
            logger.debug("shutdown() already called, skipping")
            return
        self._shutdown_called = True

        logger.info("Shutting down trading bot...")
        self.running = False

        # Close event recorder if active
        if self.recorder:
            self.recorder.close()

        # Release the instance lock
        self._release_instance_lock()

        # Stop token renewal thread
        self._stop_token_renewal()

        # Log shutdown event
        try:
            self.db.log_event(
                "bot_stopped",
                "Trading bot stopped",
                {"error": error}
            )
        except Exception as e:
            logger.warning(f"Failed to log shutdown event: {e}")

        # Send notification
        if self.notifier:
            try:
                total_trades = self.dte0_trades_today + self.tnt_trades_today
                self.notifier.send(
                    "Trading Bot Stopped",
                    f"Trades today: {total_trades}\n"
                    f"Daily P&L: ${self.portfolio.daily_realized_pnl:.2f}",
                    level='info'
                )
            except Exception as e:
                logger.warning(f"Failed to send shutdown notification: {e}")

        logger.info("Shutdown complete")


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="SPX Income Trading System"
    )
    parser.add_argument(
        '--mode',
        choices=['dry-run', 'live'],
        default=TRADING_MODE,
        help='Trading mode (default: from .env)'
    )
    parser.add_argument(
        '--log-level',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        default=LOG_LEVEL,
        help='Logging level'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Dry run mode: use real market data but do not execute trades'
    )
    parser.add_argument(
        '--no-confirm',
        action='store_true',
        help='Skip trade confirmation prompts (useful for unattended dry runs)'
    )
    parser.add_argument(
        '--confirm-live',
        action='store_true',
        help='Required flag to confirm you want to start LIVE trading with real money'
    )
    parser.add_argument(
        '--auto-trade',
        action='store_true',
        help='Skip per-trade confirmation prompts in live mode (use with caution)'
    )
    parser.add_argument(
        '--record',
        action='store_true',
        help='Record trading session as JSONL for demo replay'
    )

    args = parser.parse_args()

    # Update log level if specified
    if args.log_level != LOG_LEVEL:
        logging.getLogger().setLevel(args.log_level)

    try:
        # Initialize components
        logger.info("Initializing trading system...")

        # Initialize broker based on mode
        if args.dry_run or args.mode == 'dry-run':
            logger.info("*** DRY RUN MODE - Using real market data, no orders will be placed ***")
            broker = DryRunBroker(initial_balance=STRATEGY_PARAMS.get('portfolio', {}).get('account_size', 50000.0))
            skip_confirm = args.no_confirm
        else:
            # --- LIVE TRADING MODE ---
            # Require explicit --confirm-live flag to prevent accidental live starts
            if not args.confirm_live:
                print("\n" + "=" * 60)
                print("WARNING: You are about to start LIVE trading with real money.")
                print("Add --confirm-live to proceed.")
                print("=" * 60 + "\n")
                return 1

            logger.info("*** LIVE TRADING MODE - Real orders will be placed ***")

            # Determine active broker from config (default to schwab)
            active_broker = STRATEGY_PARAMS.get('broker', {}).get('active', 'schwab')

            if active_broker == 'schwab':
                # Schwab broker via factory
                broker = get_broker(STRATEGY_PARAMS)
                # Verify authentication
                from src.brokers.schwab_auth import SchwabAuth
                if not broker.auth.is_authenticated():
                    logger.error("Schwab authentication failed. Run: python -m src.brokers.schwab_auth")
                    print("ERROR: Schwab not authenticated. Run the auth script first.")
                    return 1
                # Check token expiry
                token_status = broker.auth.get_token_status()
                if token_status.get('expiring_soon'):
                    hrs = token_status.get('hours_remaining', 0)
                    logger.warning(f"Schwab token expiring in {hrs:.1f} hours - re-auth soon")
                logger.info("Schwab authentication verified")
            else:
                # E*TRADE broker (original flow)
                etrade_auth = ETradeAuth()
                logger.info("Authenticating with E*TRADE...")
                if not etrade_auth.authenticate():
                    logger.error("E*TRADE authentication failed. Cannot start live trading.")
                    print("ERROR: E*TRADE authentication failed. Check your credentials and try again.")
                    return 1
                logger.info("E*TRADE authentication successful")
                broker = ETradeBroker(auth=etrade_auth)

            # Pre-flight checks: verify broker connectivity before starting the bot
            logger.info("Running pre-flight checks...")
            preflight_passed = True

            # Check 1: Fetch a quote
            try:
                price = broker.get_current_price("SPX")
                if price <= 0:
                    raise ValueError(f"Got invalid SPX price: {price}")
                logger.info(f"  [OK] SPX quote: ${price:,.2f}")
            except Exception as e:
                logger.error(f"  [FAIL] Could not fetch SPX quote: {e}")
                preflight_passed = False

            # Check 2: Fetch options chain
            try:
                today_str = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d")
                chain = broker.get_options_chain("SPX", today_str)
                if not chain:
                    raise ValueError("Options chain returned empty")
                logger.info(f"  [OK] Options chain: {len(chain)} strikes loaded")
            except Exception as e:
                logger.error(f"  [FAIL] Could not fetch options chain: {e}")
                preflight_passed = False

            # Check 3: Read account balance
            try:
                balance = broker.get_account_balance()
                net_value = balance.get('net_account_value', 0)
                if net_value <= 0:
                    raise ValueError(f"Got invalid account value: {net_value}")
                logger.info(f"  [OK] Account balance: ${net_value:,.2f}")
            except Exception as e:
                logger.error(f"  [FAIL] Could not read account balance: {e}")
                preflight_passed = False

            if not preflight_passed:
                logger.error("Pre-flight checks failed. Fix the issues above before starting live trading.")
                return 1

            logger.info("All pre-flight checks passed")

            # In live mode, force manual confirmation of each trade unless --auto-trade is set
            skip_confirm = args.auto_trade
            if not skip_confirm:
                logger.info("Live mode: per-trade confirmation ENABLED (pass --auto-trade to disable)")
            else:
                logger.warning("Live mode: per-trade confirmation DISABLED (--auto-trade active)")

        strategy = SPXIncomeStrategy()
        db_manager = DatabaseManager(DATABASE_PATH)
        notifier = NotificationManager()

        # Create recorder if --record flag is set
        recorder = None
        if args.record:
            from src.demo.recorder import EventRecorder
            recorder = EventRecorder()
            logger.info(f"Demo recording enabled: {recorder._output_dir}")

        # Create bot
        bot = TradingBot(
            broker, strategy, db_manager, notifier,
            dry_run=(args.dry_run or args.mode == 'dry-run'),
            skip_confirm=skip_confirm,
            recorder=recorder,
        )

        # Set up signal handlers
        def signal_handler(sig, frame):
            logger.info("Interrupt signal received")
            bot.shutdown()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # Start bot
        bot.start()

    except BotAlreadyRunningError as e:
        logger.error(str(e))
        print(f"ERROR: {e}")
        return 1

    except Exception as e:
        logger.error(f"Failed to start bot: {e}", exc_info=True)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())