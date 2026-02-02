"""
SPX Income Trading System - Main Application

This is the main entry point for the trading bot.
"""

import sys
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
        self.market_data = MarketDataFeed(broker)
        
        # State
        self.running = False
        self.tz = pytz.timezone("America/New_York")
        self.trades_today = 0
        self.daily_pnl = 0.0
        
        # Parameters
        self.max_daily_trades = STRATEGY_PARAMS['strategy']['max_daily_trades']
        self.max_daily_loss = STRATEGY_PARAMS['risk']['max_daily_loss']
        
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
    
        while self.running:
            try:
                current_time = datetime.now(self.tz)
            
                # Check if market is open
                if not self._is_market_open(current_time):
                    logger.info(f"Market closed ({current_time.strftime('%a %H:%M')} ET). Next check in 60s...")
                    time_module.sleep(60)
                    consecutive_errors = 0  # Reset error counter
                    continue
            
                # Check daily limits
                if not self._check_daily_limits():
                    logger.info("Daily limits reached, monitoring only")
                    time_module.sleep(300)
                    consecutive_errors = 0
                    continue
            
                # Monitor existing positions
                self.position_manager.monitor_positions()
            
                # Look for new setups
                if self._is_setup_window(current_time):
                    self._check_for_setups()
            
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
        """Check if we're in the morning setup window"""
        morning_start = time(9, 30)
        morning_end = time(11, 30)
        
        current_time = dt.time()
        return morning_start <= current_time <= morning_end
    
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
        """Check for new trading setups"""
        try:
            # Get current SPX price
            current_price = self.broker.get_current_price("SPX")
            
            # Update bar builder
            current_time = datetime.now(self.tz)
            bar = self.bar_builder.add_price(current_time, current_price)
            
            # If bar just completed, check for setup
            if bar:
                logger.info(f"New 30-min bar completed: {bar}")
                
                # Check if we already have an open position
                if self.position_manager.has_open_position():
                    logger.debug("Already have open position, skipping setup check")
                    return
                
                # Evaluate for pulse bar setup
                direction = self.strategy.evaluate_setup(bar, current_price)
                
                if direction:
                    logger.info(f"Setup detected: {direction.value.upper()}")
                    self._execute_setup(bar, current_price, direction)
                else:
                    logger.debug("No setup detected")
        
        except Exception as e:
            logger.error(f"Error checking for setups: {e}", exc_info=True)
    
    def _execute_setup(self, setup_bar, current_price, direction):
        """Execute a trading setup"""
        try:
            logger.info("=" * 60)
            logger.info(f"EXECUTING {direction.value.upper()} SETUP")
            logger.info("=" * 60)
            
            # Get options chain
            expiration = datetime.now(self.tz).strftime("%Y%m%d")
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
            
            # Place order
            quantity = STRATEGY_PARAMS['strategy']['contracts_per_trade']
            trade = self.position_manager.enter_trade(
                spread,
                setup_bar,
                quantity
            )
            
            if trade:
                logger.info(f"✓ Trade executed: {trade.id}")
                
                self.trades_today += 1
                
                # Send notification
                if self.notifier:
                    self.notifier.send(
                        f"🎯 Trade Entered: {spread.direction.value.upper()}",
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
            sys.exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # Start bot
        bot.start()
    
    except Exception as e:
        logger.error(f"Failed to start bot: {e}", exc_info=True)
        return 1
    
    return 0


if __name__ == "__main__":
    sys.exit(main())