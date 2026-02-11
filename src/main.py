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
from src.core.strategy import SPXIncomeStrategy
from src.core.position_manager import PositionManager
from src.core.bar_builder import BarBuilder
from src.core.bollinger_filter import BollingerFilter
from src.core.tag_n_turn import TagNTurnStrategy
from src.core.bnb_strategy import BnBStrategy
from src.core.orb_strategy import ORBStrategy
from src.core.portfolio_manager import PortfolioManager, StrategyType
from src.core.pdt_tracker import PDTTracker
from src.data.market_data import MarketDataFeed
from database.db_manager import DatabaseManager
from src.utils.notifications import NotificationManager
from src.utils.logging import setup_logging


# Configure logging
setup_logging(LOG_FILE, LOG_LEVEL)
logger = logging.getLogger(__name__)


class BotAlreadyRunningError(Exception):
    """Raised when another bot instance is already running."""
    pass


class TradingBot:
    """Main trading bot orchestrator"""
    
    def __init__(
        self,
        broker,
        strategy: SPXIncomeStrategy,
        db_manager: DatabaseManager,
        notification_manager: Optional[NotificationManager] = None,
        dry_run: bool = False,
        skip_confirm: bool = False
    ):
        self.broker = broker
        self.strategy = strategy
        self.db = db_manager
        self.notifier = notification_manager
        self.dry_run = dry_run
        self.skip_confirm = skip_confirm

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

        # Initialize components
        self.position_manager = PositionManager(
            broker, strategy, db_manager,
            pdt_tracker=self.pdt_tracker
        )
        self.bar_builder = BarBuilder(interval_minutes=30)
        filters_cfg = STRATEGY_PARAMS.get('filters', {})
        self.bollinger_enabled = filters_cfg.get('bollinger_enabled', True)
        self.extreme_move_override_pct = filters_cfg.get('extreme_move_override_pct', 1.5)
        self.bollinger = BollingerFilter(
            period=filters_cfg.get('bollinger_period', 50),
            num_std=filters_cfg.get('bollinger_std', 2.0),
            extreme_move_pct=self.extreme_move_override_pct,
        )
        self.market_data = MarketDataFeed(broker)
        
        # State
        self.running = False
        self._shutdown_called = False
        self.tz = pytz.timezone("America/New_York")
        self.trades_today = 0
        self.daily_pnl = 0.0
        self.current_trading_date = None

        # Token auto-renewal for live broker (E*TRADE tokens expire after 2 hours)
        self._token_renewal_thread = None
        self._token_renewal_stop = threading.Event()

        # Instance lock (file-based, prevents multiple bot instances)
        self._lock_fd = None
        self._lockfile_path = BASE_DIR / 'bot.lock'

        # Signal log (shared by both dry-run and live modes for dashboard)
        self._signal_log_path = BASE_DIR / 'logs' / 'signals.json'
        self._init_signal_log()

        # Pending setup state (pulse bar detected, waiting for breakout)
        self.pending_setup = None  # {direction, bar, trigger_price, timestamp}

        # Market state (updated every cycle by _update_market_state)
        self._current_spx_price = 0.0
        self._last_completed_bar = None

        # Parameters
        self.max_daily_trades = STRATEGY_PARAMS['strategy']['max_daily_trades']
        self.max_daily_loss = STRATEGY_PARAMS['risk']['max_daily_loss']

        # Afternoon window (Bed & Breakfast system, p.23-26)
        timing_cfg = STRATEGY_PARAMS.get('timing', {})
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
        sizing_cfg = portfolio_cfg.get('position_sizing', {})
        self.portfolio = PortfolioManager(
            account_size=portfolio_cfg.get('account_size', 50000.0),
            max_total_positions=portfolio_cfg.get('max_total_positions', 3),
            max_0dte_positions=portfolio_cfg.get('max_0dte_positions', 2),
            max_daily_risk_pct=portfolio_cfg.get('max_daily_risk_pct', 5.0),
            max_daily_loss_pct=portfolio_cfg.get('max_daily_loss_pct', 2.0),
            strategy_priority=portfolio_cfg.get('priority'),
            # Position sizing parameters
            sizing_method=sizing_cfg.get('method', 'percent_risk'),
            risk_per_trade_pct=sizing_cfg.get('risk_per_trade_pct', 2.0),
            min_contracts=sizing_cfg.get('min_contracts', 1),
            max_contracts=sizing_cfg.get('max_contracts', 20),
        )

        logger.info("TradingBot initialized")
    
    def _start_token_renewal(self):
        """Start background token renewal if broker has an auth object.

        E*TRADE access tokens expire after 2 hours of inactivity.
        This thread renews them every 90 minutes to keep the session alive
        throughout the 6.5-hour trading day.
        """
        auth = getattr(self.broker, 'auth', None)
        if auth is None or not hasattr(auth, '_renew_token'):
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
        
        logger.info("=" * 60)
        logger.info("SPX Income Trading Bot Starting")
        logger.info("=" * 60)
        if self.dry_run:
            logger.info("*** DRY RUN MODE - NO ORDERS WILL BE PLACED ***")
            logger.info("Using real market data from Yahoo Finance")
        logger.info(f"Mode: {TRADING_MODE.upper()}")
        logger.info(f"Max daily trades: {self.max_daily_trades}")
        logger.info(f"Max daily loss: ${self.max_daily_loss}")
        logger.info("=" * 60)

        # Start token auto-renewal for live broker
        self._start_token_renewal()

        # Send startup notification
        if self.notifier:
            self.notifier.send(
                "Trading Bot Started",
                f"Mode: {TRADING_MODE}\nTime: {datetime.now(self.tz).strftime('%H:%M:%S EST')}"
            )
        
        # Log system event
        self.db.log_event("bot_started", "Trading bot started", {
            "mode": TRADING_MODE,
            "max_daily_trades": self.max_daily_trades
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
                        logger.info(
                            f"New trading day: {today}. "
                            f"Resetting counters (prev: {self.trades_today} trades, "
                            f"${self.daily_pnl:.2f} P&L)"
                        )
                    self.current_trading_date = today
                    self._restore_daily_counters(today)
                    self.pending_setup = None
                    self.bar_builder.reset()
                    self._last_completed_bar = None
                    self.bollinger.day_open = None  # Reset for new day, will set at market open
                    self.position_manager._day_open = None
                    self.position_manager._prev_close = None

                    # Reset parallel strategies for new day
                    self.portfolio.reset_daily()
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
                    logger.info(f"[Heartbeat] Loop #{loop_count} at {current_time.strftime('%H:%M:%S')} ET{setup_str}{bb_str}")
                    last_heartbeat = current_time

                # Check if market is open
                if not self._is_market_open(current_time):
                    logger.info(f"Market closed ({current_time.strftime('%a %H:%M')} ET). Next check in 60s...")
                    self._interruptible_sleep(60)
                    consecutive_errors = 0  # Reset error counter
                    continue

                # Monitor existing positions ALWAYS (even when daily limits reached)
                closed_pnl = self.position_manager.monitor_positions()
                if closed_pnl != 0:
                    self.daily_pnl += closed_pnl
                    logger.info(f"Daily P&L updated: ${self.daily_pnl:.2f} (trade closed: ${closed_pnl:+.2f})")

                # Update portfolio risk tracking for closed trades
                for closed in self.position_manager.recently_closed:
                    self.portfolio.close_position(closed['id'], closed['pnl'])
                self.position_manager.recently_closed.clear()

                # Update market state: build bars, feed parallel strategies
                # (runs every cycle during market hours, NOT gated by setup window)
                self._update_market_state(current_time)

                # Check daily limits (for new entries only - monitoring always runs above)
                if not self._check_daily_limits():
                    if self.pending_setup:
                        logger.info("Daily limits reached, clearing pending setup")
                        self.pending_setup = None
                    else:
                        logger.info("Daily limits reached, monitoring only")
                    self._interruptible_sleep(300)
                    consecutive_errors = 0
                    continue

                # Check pending breakout triggers (after daily limits gate)
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
                        window_end = time(11, 30)
                        window_label = "9:30-11:30"
                    if current_time.time() > window_end:
                        logger.info(
                            f"Pending {ps['direction'].value.upper()} setup expired "
                            f"({ps_window} window closed at {window_end.strftime('%H:%M')} ET without breakout). "
                            f"Trigger was {'above' if ps['direction'].value == 'bullish' else 'below'} "
                            f"${ps['trigger_price']:,.2f}"
                        )
                        self.pending_setup = None

                # Log outside-window status periodically
                if not self._is_setup_window(current_time):
                    if loop_count == 1 or loop_count % 20 == 0:
                        windows_str = "9:30-11:30"
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
        """Restore trades_today and daily_pnl from DB (survives mid-day restarts)."""
        try:
            summary = self.db.get_daily_summary(trade_date)
            self.trades_today = summary['trades_count']
            self.daily_pnl = summary['realized_pnl']
            logger.info(
                f"Restored daily counters from DB: "
                f"{self.trades_today} trades, ${self.daily_pnl:.2f} P&L"
            )
        except Exception as e:
            logger.warning(f"Could not restore daily counters: {e}")
            self.trades_today = 0
            self.daily_pnl = 0.0

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
        """Check if market is currently open"""
        market_open = time(9, 30)
        market_close = time(16, 0)
        
        current_time = dt.time()
        is_weekday = dt.weekday() < 5
        
        return is_weekday and market_open <= current_time < market_close
    
    def _is_setup_window(self, dt: datetime) -> bool:
        """Check if we're in any active setup window (morning or afternoon)."""
        current_time = dt.time()

        # Morning window
        morning_start = time(9, 30)
        morning_end = time(11, 30)
        if morning_start <= current_time <= morning_end:
            return True

        # Afternoon window (Bed & Breakfast)
        if self.afternoon_enabled:
            if self.afternoon_start <= current_time <= self.afternoon_end:
                return True

        return False

    def _get_active_window(self, dt: datetime) -> str:
        """Return which setup window is currently active: 'morning', 'afternoon', or 'none'."""
        current_time = dt.time()

        morning_start = time(9, 30)
        morning_end = time(11, 30)
        if morning_start <= current_time <= morning_end:
            return 'morning'

        if self.afternoon_enabled:
            if self.afternoon_start <= current_time <= self.afternoon_end:
                return 'afternoon'

        return 'none'
    
    def _check_daily_limits(self) -> bool:
        """Check if daily trading limits have been reached"""
        if self.trades_today >= self.max_daily_trades:
            logger.warning(f"Max daily trades reached: {self.trades_today}/{self.max_daily_trades}")
            return False
        
        if self.daily_pnl <= -self.max_daily_loss:
            logger.warning(f"Max daily loss reached: ${self.daily_pnl:.2f}")
            if self.notifier:
                self.notifier.send(
                    "⚠️ Daily Loss Limit Reached",
                    f"Daily P&L: ${self.daily_pnl:.2f}\nStopping new trades."
                )
            return False
        
        return True
    
    def _update_market_state(self, current_time):
        """Update market data, build bars, and run parallel strategy checks.

        Called every cycle during market hours, regardless of setup window.
        This ensures bars build continuously and parallel strategies (Tag 'n Turn,
        ORB, B&B) receive data even outside the daily income setup window.
        """
        try:
            # Get current SPX price
            self._current_spx_price = self.broker.get_current_price("SPX")
            if self._current_spx_price == 0:
                logger.warning("Failed to get SPX price")
                return

            current_price = self._current_spx_price
            logger.debug(f"SPX price: ${current_price:,.2f}")

            # Set day open price for extreme move override (first price of the day)
            if self.bollinger.day_open is None and current_price > 0:
                self.bollinger.set_day_open(current_price)

                # Cache day_open and prev_close for trade context
                try:
                    md = getattr(self.broker, 'market_data', None)
                    if md is None:
                        from src.data.yahoo_finance import YahooFinanceProvider
                        md = YahooFinanceProvider()
                    spx_quote = md.get_spx_quote()
                    if spx_quote:
                        day_open = spx_quote.get('open', current_price)
                        prev_close = spx_quote.get('previous_close', 0)
                        if day_open and day_open > 0:
                            self.position_manager.set_daily_cache(day_open, prev_close)
                except Exception as e:
                    logger.warning(f"Failed to cache daily open/prev_close: {e}")

            # Update bar builder (runs every cycle so bars build continuously)
            self._last_completed_bar = self.bar_builder.add_price(current_time, current_price)

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
                logger.info(f"New 30-min bar completed: {bar}")

                # Feed bar to Bollinger filter
                self.bollinger.add_bar(bar)

                # Feed bar to Tag 'n Turn strategy (if enabled)
                if self.tag_n_turn_enabled and self.tag_n_turn:
                    self.tag_n_turn.on_bar_complete(bar)

                # ORB: Set opening range on first bar (10:00 completion)
                if self.orb_enabled and self.orb_strategy:
                    if bar.timestamp.time() == time(10, 0):
                        self.orb_strategy.set_opening_range(bar)

                # B&B: Process bar for end-of-day signals (15:00-16:00)
                if self.bnb_enabled and self.bnb_strategy:
                    bnb_action = self.bnb_strategy.on_bar_complete(bar, current_price)
                    if bnb_action:
                        logger.info(f"B&B ACTION: {bnb_action}")
                        self.db.log_event("bnb_action", "B&B strategy action", bnb_action)

            # Check parallel strategy tick-level signals (run every cycle, own timing)
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

                tnt_exit = self.tag_n_turn.check_exit_conditions(current_price)
                if tnt_exit:
                    logger.info(
                        f"TAG 'N TURN EXIT: {tnt_exit['reason']} @ ${current_price:,.2f}"
                    )
                    self.db.log_event("tag_n_turn_exit", "Tag 'n Turn exit signal", {
                        "reason": tnt_exit['reason'],
                        "exit_price": current_price,
                    })

            if self.orb_enabled and self.orb_strategy:
                orb_signal = self.orb_strategy.check_breakout(current_price)
                if orb_signal:
                    logger.info(
                        f"ORB SIGNAL: {orb_signal['direction'].upper()} "
                        f"entry @ ${current_price:,.2f} ({orb_signal['trigger']})"
                    )
                    self.db.log_event("orb_signal", "ORB breakout signal", orb_signal)

            if self.bnb_enabled and self.bnb_strategy:
                bnb_signal = self.bnb_strategy.check_entry_signal(current_price, current_time)
                if bnb_signal:
                    logger.info(
                        f"B&B ENTRY SIGNAL: {bnb_signal['direction'].upper()} "
                        f"@ ${current_price:,.2f} (from {bnb_signal['signal_date']})"
                    )
                    self.db.log_event("bnb_signal", "B&B entry signal", bnb_signal)

        except Exception as e:
            logger.error(f"Error updating market state: {e}", exc_info=True)

    def _check_breakout_trigger(self, current_time):
        """Check if a pending setup's breakout level has been hit.

        Called every cycle after daily limits check. Separated from
        _update_market_state so breakout entries respect daily limits.
        """
        if not self.pending_setup or self.position_manager.has_open_position():
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

        morning_start = time(9, 30)
        morning_end = time(11, 30)
        if morning_start <= ct < morning_end:
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
            # Check if we already have an open position
            if self.position_manager.has_open_position():
                logger.debug("Already have open position, skipping setup check")
                return

            current_time = datetime.now(self.tz)

            # Evaluate for pulse bar setup
            direction = self.strategy.evaluate_setup(bar, current_price)

            if direction:
                # Pulse bar detected -- store as pending setup, DON'T enter yet
                trigger_price = bar.high if direction.value == 'bullish' else bar.low
                above_below = 'above' if direction.value == 'bullish' else 'below'

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

                window_label = f" [{active_window} window]" if active_window != 'morning' else ""
                logger.info(
                    f"PENDING SETUP: {direction.value.upper()} pulse bar at "
                    f"{bar.timestamp.strftime('%H:%M')} "
                    f"(H=${bar.high:.2f} L=${bar.low:.2f} C=${bar.close:.2f}). "
                    f"Entry trigger: price {above_below} ${trigger_price:,.2f}{window_label}"
                )
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

            # Get options chain (format: YYYY-MM-DD for dry_run_broker compatibility)
            expiration = datetime.now(self.tz).strftime("%Y-%m-%d")
            options_chain = self.broker.get_options_chain("SPX", expiration)

            # Construct spread
            spread = self.strategy.construct_spread(
                current_price,
                direction,
                options_chain
            )

            if not spread:
                logger.warning("Failed to construct spread")
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
            fixed_fallback = STRATEGY_PARAMS['strategy'].get('contracts_per_trade', 5)
            quantity = self.portfolio.calculate_position_size(
                strategy=StrategyType.DAILY_INCOME,
                max_risk_per_contract=max_risk_per_contract,
                fixed_contracts=fixed_fallback,
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
                return

            sizing_method = self.portfolio.sizing_method.value
            logger.info(
                f"Position sizing: {sizing_method} -> {quantity} contracts "
                f"(risk ${max_risk_per_contract:.2f} per contract, "
                f"max risk ${quantity * max_risk_per_contract:.2f}, "
                f"account ${self.portfolio.account_size:,.0f})"
            )
            trade = self.position_manager.enter_trade(
                spread,
                setup_bar,
                quantity,
                breakout_time=breakout_time
            )

            if trade:
                logger.info(f"Trade executed: {trade.id}")

                self.trades_today += 1

                # Register with portfolio manager for risk tracking
                self.portfolio.register_position(
                    position_id=trade.id,
                    strategy=StrategyType.DAILY_INCOME,
                    direction=spread.direction.value,
                    contracts=quantity,
                    max_risk=max_risk_per_contract * quantity,
                    is_0dte=True,
                )

                # Log signal for dashboard (works in both dry-run and live)
                self._log_signal("TRADE_ENTRY", {
                    "trade_id": trade.id,
                    "direction": spread.direction.value,
                    "short_strike": spread.short_leg.strike,
                    "long_strike": spread.long_leg.strike,
                    "quantity": quantity,
                    "credit_received": spread.credit_received,
                    "max_risk": spread.max_risk * quantity,
                    "underlying_price": current_price,
                    "expiration": spread.expiration.isoformat() if spread.expiration else None,
                    "sizing_method": sizing_method,
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
                        f"Contracts: {quantity}"
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

            with open(self._signal_log_path, 'w') as f:
                json.dump(log_data, f, indent=2, default=str)

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
                self.notifier.send(
                    "Trading Bot Stopped",
                    f"Trades today: {self.trades_today}\n"
                    f"Daily P&L: ${self.daily_pnl:.2f}"
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
            broker = DryRunBroker(initial_balance=50000.0)
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

            # Authenticate with E*TRADE
            etrade_auth = ETradeAuth()
            logger.info("Authenticating with E*TRADE...")
            if not etrade_auth.authenticate():
                logger.error("E*TRADE authentication failed. Cannot start live trading.")
                print("ERROR: E*TRADE authentication failed. Check your credentials and try again.")
                return 1
            logger.info("E*TRADE authentication successful")

            # Create live broker
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

        # Create bot
        bot = TradingBot(
            broker, strategy, db_manager, notifier,
            dry_run=(args.dry_run or args.mode == 'dry-run'),
            skip_confirm=skip_confirm
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