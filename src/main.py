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
from src.brokers.paper_trader import PaperBroker
from src.brokers.dry_run_broker import DryRunBroker
from src.core.strategy import SPXIncomeStrategy
from src.core.position_manager import PositionManager
from src.core.bar_builder import BarBuilder
from src.core.bollinger_filter import BollingerFilter
from src.core.tag_n_turn import TagNTurnStrategy
from src.core.bnb_strategy import BnBStrategy
from src.core.orb_strategy import ORBStrategy
from src.core.portfolio_manager import PortfolioManager, StrategyType
from src.data.market_data import MarketDataFeed
from database.db_manager import DatabaseManager
from src.utils.notifications import NotificationManager
from src.utils.logging import setup_logging


# Configure logging
setup_logging(LOG_FILE, LOG_LEVEL)
logger = logging.getLogger(__name__)


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
        
        # Initialize components
        self.position_manager = PositionManager(broker, strategy, db_manager)
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
        self.tz = pytz.timezone("America/New_York")
        self.trades_today = 0
        self.daily_pnl = 0.0
        self.current_trading_date = None

        # Pending setup state (pulse bar detected, waiting for breakout)
        self.pending_setup = None  # {direction, bar, trigger_price, timestamp}

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
    
    def start(self):
        """Start the trading bot"""
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
                    self.trades_today = 0
                    self.daily_pnl = 0.0
                    self.pending_setup = None
                    self.bollinger.day_open = None  # Reset for new day, will set at market open

                    # Reset parallel strategies for new day
                    self.portfolio.reset_daily()
                    if self.orb_enabled and self.orb_strategy:
                        self.orb_strategy.reset_daily()
                    if self.bnb_enabled and self.bnb_strategy:
                        self.bnb_strategy.on_day_start()  # Activate overnight signal

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
                    time_module.sleep(60)
                    consecutive_errors = 0  # Reset error counter
                    continue

                # Check daily limits
                if not self._check_daily_limits():
                    if self.pending_setup:
                        logger.info("Daily limits reached, clearing pending setup")
                        self.pending_setup = None
                    else:
                        logger.info("Daily limits reached, monitoring only")
                    time_module.sleep(300)
                    consecutive_errors = 0
                    continue

                # Monitor existing positions
                self.position_manager.monitor_positions()

                # Look for new setups
                if self._is_setup_window(current_time):
                    self._check_for_setups()
                else:
                    # Expire pending setup when its specific window closes
                    if self.pending_setup:
                        ps = self.pending_setup
                        ps_window = ps.get('window', 'morning')
                        # Determine the end time for the setup's window
                        if ps_window == 'afternoon':
                            window_end = self.afternoon_end
                            window_label = f"{self.afternoon_start.strftime('%H:%M')}-{self.afternoon_end.strftime('%H:%M')}"
                        else:
                            window_end = time(11, 30)
                            window_label = "9:30-11:30"
                        # Expire if we're past the window end
                        if current_time.time() > window_end:
                            logger.info(
                                f"Pending {ps['direction'].value.upper()} setup expired "
                                f"({ps_window} window closed at {window_end.strftime('%H:%M')} ET without breakout). "
                                f"Trigger was {'above' if ps['direction'].value == 'bullish' else 'below'} "
                                f"${ps['trigger_price']:,.2f}"
                            )
                            self.pending_setup = None
                    # Log once on startup if outside setup window, or every 10 minutes
                    if loop_count == 1 or loop_count % 20 == 0:  # every ~10 min at 30s intervals
                        windows_str = "9:30-11:30"
                        if self.afternoon_enabled:
                            windows_str += f", {self.afternoon_start.strftime('%H:%M')}-{self.afternoon_end.strftime('%H:%M')}"
                        logger.info(f"Outside setup windows ({windows_str} ET). Current: {current_time.strftime('%H:%M')} ET. Monitoring only.")

                # Reset error counter on success
                consecutive_errors = 0

                # Sleep before next iteration
                time_module.sleep(30)
            
            except KeyboardInterrupt:
                raise  # Let this propagate
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Error in main loop ({consecutive_errors}/{max_consecutive_errors}): {e}", 
                            exc_info=True)
            
                # Stop if too many consecutive errors
                if consecutive_errors >= max_consecutive_errors:
                    logger.critical(f"Too many consecutive errors ({consecutive_errors}), shutting down")
                    self.shutdown(error=True)
                    break
            
                # Exponential backoff
                sleep_time = min(60 * (2 ** consecutive_errors), 300)  # Max 5 minutes
                logger.info(f"Sleeping {sleep_time}s before retry...")
                time_module.sleep(sleep_time)
    
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
    
    def _check_for_setups(self):
        """Check for new trading setups and pending breakout triggers.

        Per Production Line Trading strategy:
        1. A pulse bar is a SETUP, not an entry signal.
        2. Entry triggers when the NEXT bar/tick breaks the setup bar's
           high (bullish) or low (bearish).
        """
        try:
            # Redundant market-hours guard - prevents signals if called outside market hours
            current_time = datetime.now(self.tz)
            if not self._is_market_open(current_time):
                logger.warning("_check_for_setups called outside market hours - skipping")
                return

            # Get current SPX price
            current_price = self.broker.get_current_price("SPX")
            if current_price == 0:
                logger.warning("Failed to get SPX price, skipping setup check")
                return

            logger.debug(f"SPX price: ${current_price:,.2f}")

            # Set day open price for extreme move override (first price of the day)
            if self.bollinger.day_open is None and current_price > 0:
                self.bollinger.set_day_open(current_price)

            # Update bar builder
            current_time = datetime.now(self.tz)
            bar = self.bar_builder.add_price(current_time, current_price)

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

            # --- Check pending setup for breakout trigger ---
            if self.pending_setup and not self.position_manager.has_open_position():
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
                    return

            # --- Check Tag 'n Turn entry signals (runs in parallel with daily strategy) ---
            if self.tag_n_turn_enabled and self.tag_n_turn:
                tnt_signal = self.tag_n_turn.check_entry_signal(current_price)
                if tnt_signal:
                    logger.info(
                        f"TAG 'N TURN SIGNAL: {tnt_signal['direction'].value.upper()} "
                        f"entry @ ${current_price:,.2f}, "
                        f"target=${tnt_signal['target_price']:,.2f}"
                    )
                    # TODO: Execute Tag 'n Turn trade when live trading is implemented
                    # For now, just log the signal (multi-day positions need special handling)
                    self.db.log_event("tag_n_turn_signal", "Tag 'n Turn entry signal", {
                        "direction": tnt_signal['direction'].value,
                        "entry_price": current_price,
                        "target_price": tnt_signal['target_price'],
                        "stop_price": tnt_signal['stop_price'],
                    })

                # Check exit conditions for open Tag 'n Turn positions
                tnt_exit = self.tag_n_turn.check_exit_conditions(current_price)
                if tnt_exit:
                    logger.info(
                        f"TAG 'N TURN EXIT: {tnt_exit['reason']} @ ${current_price:,.2f}"
                    )
                    self.db.log_event("tag_n_turn_exit", "Tag 'n Turn exit signal", {
                        "reason": tnt_exit['reason'],
                        "exit_price": current_price,
                    })

            # --- Check ORB breakout signals ---
            if self.orb_enabled and self.orb_strategy:
                orb_signal = self.orb_strategy.check_breakout(current_price)
                if orb_signal:
                    logger.info(
                        f"ORB SIGNAL: {orb_signal['direction'].upper()} "
                        f"entry @ ${current_price:,.2f} ({orb_signal['trigger']})"
                    )
                    self.db.log_event("orb_signal", "ORB breakout signal", orb_signal)

            # --- Check B&B entry signals (morning entry from overnight signal) ---
            if self.bnb_enabled and self.bnb_strategy:
                bnb_signal = self.bnb_strategy.check_entry_signal(current_price, current_time)
                if bnb_signal:
                    logger.info(
                        f"B&B ENTRY SIGNAL: {bnb_signal['direction'].upper()} "
                        f"@ ${current_price:,.2f} (from {bnb_signal['signal_date']})"
                    )
                    self.db.log_event("bnb_signal", "B&B entry signal", bnb_signal)

            # --- If bar just completed, check for new pulse bar setup ---
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

                # Check if we already have an open position
                if self.position_manager.has_open_position():
                    logger.debug("Already have open position, skipping setup check")
                    return

                # Evaluate for pulse bar setup
                direction = self.strategy.evaluate_setup(bar, current_price)

                if direction:
                    # Pulse bar detected — store as pending setup, DON'T enter yet
                    # NOTE: BB filter removed from daily strategy (per plan).
                    # Tag 'n Turn uses BB for its own reversal detection.
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

            # Calculate position size dynamically based on account and risk
            # max_risk per contract = spread width (max loss if spread goes to max loss)
            max_risk_per_contract = spread.max_risk
            fixed_fallback = STRATEGY_PARAMS['strategy'].get('contracts_per_trade', 5)
            quantity = self.portfolio.calculate_position_size(
                strategy=StrategyType.DAILY_INCOME,
                max_risk_per_contract=max_risk_per_contract,
                fixed_contracts=fixed_fallback,
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
    
    def shutdown(self, error: bool = False):
        """Shutdown the trading bot"""
        logger.info("Shutting down trading bot...")
        
        self.running = False
        
        # Log shutdown event
        self.db.log_event(
            "bot_stopped",
            "Trading bot stopped",
            {"error": error}
        )
        
        # Send notification
        if self.notifier:
            self.notifier.send(
                "🛑 Trading Bot Stopped",
                f"Trades today: {self.trades_today}\n"
                f"Daily P&L: ${self.daily_pnl:.2f}"
            )
        
        logger.info("Shutdown complete")


def _check_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="SPX Income Trading System"
    )
    parser.add_argument(
        '--mode',
        choices=['paper', 'live'],
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

    args = parser.parse_args()

    # Update log level if specified
    if args.log_level != LOG_LEVEL:
        logging.getLogger().setLevel(args.log_level)

    # --- PID Lockfile ---
    lockfile = BASE_DIR / 'spx_trader.lock'

    if lockfile.exists():
        try:
            existing_pid = int(lockfile.read_text().strip())
            if _check_pid_alive(existing_pid):
                logger.error(f"Another bot instance is running (PID {existing_pid}). Exiting.")
                print(f"ERROR: Another bot instance is running (PID {existing_pid}). "
                      f"Remove {lockfile} if this is stale.")
                return 1
            else:
                logger.warning(f"Stale lockfile found (PID {existing_pid} not running). Removing.")
                lockfile.unlink()
        except (ValueError, OSError) as e:
            logger.warning(f"Invalid lockfile, removing: {e}")
            lockfile.unlink(missing_ok=True)

    # Write current PID
    try:
        lockfile.write_text(str(os.getpid()))
        logger.info(f"PID lockfile created: {lockfile} (PID {os.getpid()})")
    except OSError as e:
        logger.error(f"Failed to create lockfile: {e}")
        return 1

    try:
        # Initialize components
        logger.info("Initializing trading system...")

        # Initialize broker based on mode
        if args.dry_run:
            logger.info("*** DRY RUN MODE - Using real market data, no orders will be placed ***")
            broker = DryRunBroker(initial_balance=50000.0)
        elif args.mode == 'paper':
            broker = PaperBroker(initial_balance=50000.0)
        else:
            logger.error("Live trading not yet implemented. Use --mode paper or --dry-run")
            return 1

        strategy = SPXIncomeStrategy()
        db_manager = DatabaseManager(DATABASE_PATH)
        notifier = NotificationManager()

        # Create bot
        bot = TradingBot(
            broker, strategy, db_manager, notifier,
            dry_run=args.dry_run,
            skip_confirm=args.no_confirm
        )

        # Set up signal handlers
        def signal_handler(sig, frame):
            logger.info("Interrupt signal received")
            bot.shutdown()
            # Clean up lockfile
            try:
                lockfile.unlink(missing_ok=True)
            except OSError:
                pass
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # Start bot
        bot.start()

    except Exception as e:
        logger.error(f"Failed to start bot: {e}", exc_info=True)
        return 1

    finally:
        # Always clean up lockfile on exit
        try:
            lockfile.unlink(missing_ok=True)
            logger.info("PID lockfile removed")
        except OSError:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())