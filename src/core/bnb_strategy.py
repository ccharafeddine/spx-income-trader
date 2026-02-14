"""
B&B (Bed & Breakfast) Strategy - Production Line Trading p.23-25
Also includes Just Breakfast logic (p.26)

Concept: Identify setups in the last 60 minutes of trading (15:00-16:00 ET)
for NEXT DAY entry.

Rules:
- Window: 15:00-16:00 ET (last 2 bars of the day)
- Look for pulse bars in this window
- The 15:30 bar is "presumptive" - if it's a pulse, assume continuation into close
- Signal is stored overnight
- Entry: Next day at market open (09:30)
- Exit: After first 30 minutes (10:00) OR roll into main daily setup if first bar confirms

Just Breakfast:
- Uses B&B signal from previous day
- Entry: Opening bell (09:30)
- Exit: After first 30 minutes (10:00)
- AGGRESSIVE: If first bar ALSO qualifies as pulse in same direction, roll to daily
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, time
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


@dataclass
class BnBSignal:
    """Overnight signal from B&B detection"""
    signal_date: str  # Date the signal was generated (YYYY-MM-DD)
    direction: str  # bullish or bearish
    pulse_bar_time: str  # HH:MM of the pulse bar
    pulse_bar_close: float
    is_presumptive: bool  # True if 15:30 bar (presumed continuation)
    spx_close: float  # EOD close price


class BnBStrategy:
    """
    Bed & Breakfast strategy.

    Scans 15:00-16:00 for pulse bars, generates overnight signals
    for next-day morning entry.
    """

    BNB_WINDOW_START = time(15, 0)
    BNB_WINDOW_END = time(16, 0)
    FIRST_BAR_END = time(10, 0)

    def __init__(self, config: Dict[str, Any], persistence_path: Optional[Path] = None):
        """
        Initialize B&B strategy.

        Args:
            config: Configuration dict from strategy_params.yaml bnb section
            persistence_path: Path to JSON file for signal persistence
        """
        self.config = config
        self.enabled = config.get('enabled', False)
        self.pulse_threshold = config.get('pulse_threshold', 10.0)
        self.aggressive_roll = config.get('aggressive_roll', True)
        self.max_contracts_override = config.get('max_contracts_override', 3)

        # State
        self.todays_bnb_bars = []
        self.pending_signal: Optional[BnBSignal] = None  # EOD signal waiting for next day
        self.active_signal: Optional[BnBSignal] = None  # Signal ready for entry today
        self.position_open = False
        self.entry_price: Optional[float] = None

        # Persistence
        if persistence_path:
            self.persistence_path = persistence_path
        else:
            self.persistence_path = Path(__file__).parent.parent.parent / 'database' / 'bnb_signals.json'

        self._load_signals()

        logger.info(
            f"BnBStrategy initialized: enabled={self.enabled}, "
            f"pulse={self.pulse_threshold}%, window=15:00-16:00 ET, "
            f"aggressive_roll={self.aggressive_roll}"
        )

    def _load_signals(self):
        """Load any pending overnight signal"""
        try:
            if not self.persistence_path.exists():
                return

            data = json.loads(self.persistence_path.read_text())

            if data.get('pending_signal'):
                sig = data['pending_signal']
                self.pending_signal = BnBSignal(**sig)
                logger.info(f"B&B: Loaded pending signal from {sig.get('signal_date')}")

            if data.get('active_signal'):
                sig = data['active_signal']
                # Only load if it's from a previous day (still valid for entry)
                signal_date = sig.get('signal_date')
                today = datetime.now().strftime('%Y-%m-%d')
                if signal_date and signal_date < today:
                    self.active_signal = BnBSignal(**sig)
                    logger.info(
                        f"B&B: Loaded overnight signal from {signal_date}: "
                        f"{self.active_signal.direction.upper()}"
                    )

            self.position_open = data.get('position_open', False)
            self.entry_price = data.get('entry_price')

        except Exception as e:
            logger.error(f"Failed to load B&B signals: {e}")

    def _save_signals(self):
        """Persist signals to disk"""
        try:
            data = {
                'pending_signal': asdict(self.pending_signal) if self.pending_signal else None,
                'active_signal': asdict(self.active_signal) if self.active_signal else None,
                'position_open': self.position_open,
                'entry_price': self.entry_price,
            }
            self.persistence_path.parent.mkdir(exist_ok=True)
            self.persistence_path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.error(f"Failed to save B&B signals: {e}")

    def is_bnb_window(self, bar_time: datetime) -> bool:
        """Check if bar falls within B&B window (15:00-16:00)"""
        t = bar_time.time() if hasattr(bar_time, 'time') else bar_time
        return self.BNB_WINDOW_START <= t < self.BNB_WINDOW_END

    def _is_pulse_bar(self, bar) -> Optional[str]:
        """Check if bar is a pulse bar, return direction or None"""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return None

        close_position = (bar.close - bar.low) / bar_range * 100

        if close_position >= (100 - self.pulse_threshold):
            return "bullish"
        if close_position <= self.pulse_threshold:
            return "bearish"
        return None

    def on_bar_complete(self, bar, current_price: float) -> Optional[Dict]:
        """
        Process a completed bar for B&B signals.

        During 15:00-16:00: Look for pulse bars
        Returns action dict if entry/exit signal triggered.
        """
        if not self.enabled:
            return None

        bar_time = bar.timestamp
        action = None

        # During B&B window, collect and evaluate bars
        if self.is_bnb_window(bar_time):
            self.todays_bnb_bars.append(bar)

            pulse_dir = self._is_pulse_bar(bar)
            if pulse_dir:
                is_presumptive = bar_time.time() >= time(15, 30)

                self.pending_signal = BnBSignal(
                    signal_date=bar_time.strftime('%Y-%m-%d'),
                    direction=pulse_dir,
                    pulse_bar_time=bar_time.strftime('%H:%M'),
                    pulse_bar_close=bar.close,
                    is_presumptive=is_presumptive,
                    spx_close=current_price
                )

                logger.info(
                    f"B&B: {pulse_dir.upper()} pulse detected at {bar_time.strftime('%H:%M')} "
                    f"{'(presumptive)' if is_presumptive else ''}"
                )
                self._save_signals()

        # Check for Just Breakfast exit at 10:00
        if self.position_open and bar_time.time() >= self.FIRST_BAR_END:
            # Check if first bar is ALSO a pulse in same direction (roll opportunity)
            first_bar_pulse = self._is_pulse_bar(bar)

            if self.aggressive_roll and first_bar_pulse == self.active_signal.direction:
                action = {
                    "strategy": "bnb",
                    "action": "roll_to_daily",
                    "direction": self.active_signal.direction,
                    "reason": "First bar confirms - rolling to daily setup",
                    "entry_price": self.entry_price,
                }
                logger.info(
                    f"B&B/JB: Rolling to daily setup - first bar confirms "
                    f"{self.active_signal.direction.upper()}"
                )
            else:
                action = {
                    "strategy": "bnb",
                    "action": "exit",
                    "direction": self.active_signal.direction,
                    "reason": "Just Breakfast 30-minute exit",
                    "exit_price": current_price,
                    "entry_price": self.entry_price,
                }
                logger.info(f"B&B/JB: 30-minute exit at ${current_price:,.2f}")

            self.position_open = False
            self.entry_price = None
            self.active_signal = None
            self._save_signals()

        return action

    def check_entry_signal(self, current_price: float, current_time: datetime) -> Optional[Dict]:
        """
        Check if B&B entry conditions are met.

        Called at market open to check for overnight signal entry.
        """
        if not self.enabled or not self.active_signal:
            return None

        if self.position_open:
            return None

        # Entry at market open (09:30-10:00 window)
        t = current_time.time()
        if time(9, 30) <= t < time(10, 0):
            self.position_open = True
            self.entry_price = current_price
            self._save_signals()

            signal = {
                "strategy": "bnb",
                "action": "enter",
                "direction": self.active_signal.direction,
                "entry_price": current_price,
                "signal_date": self.active_signal.signal_date,
                "pulse_bar_time": self.active_signal.pulse_bar_time,
                "is_presumptive": self.active_signal.is_presumptive,
            }

            logger.info(
                f"B&B ENTRY: {self.active_signal.direction.upper()} "
                f"@ ${current_price:,.2f} (signal from {self.active_signal.signal_date})"
            )

            return signal

        return None

    def rollback_entry(self):
        """Rollback premature state changes when trade execution fails.

        check_entry_signal() sets position_open=True BEFORE returning the
        signal dict. If the caller's execution pipeline fails, call this to
        restore the strategy so it can retry on the next tick.
        """
        self.position_open = False
        self.entry_price = None
        self._save_signals()
        logger.info("B&B: Entry rolled back (execution failed), will retry on next tick")

    def on_day_end(self, spx_close: float):
        """Called at market close to finalize B&B signal"""
        if self.pending_signal:
            self.pending_signal.spx_close = spx_close
            self._save_signals()
            logger.info(
                f"B&B: Signal locked for tomorrow: {self.pending_signal.direction.upper()} "
                f"(from {self.pending_signal.pulse_bar_time})"
            )

        # Reset for next day
        self.todays_bnb_bars = []

    def on_day_start(self):
        """Called at market open to activate overnight signal"""
        if self.pending_signal:
            # Move pending to active
            self.active_signal = self.pending_signal
            self.pending_signal = None
            self._save_signals()
            logger.info(
                f"B&B: Activating overnight signal: {self.active_signal.direction.upper()} "
                f"(from {self.active_signal.signal_date} {self.active_signal.pulse_bar_time})"
            )

    def reset_daily(self):
        """Reset daily state (called on new trading day)"""
        self.todays_bnb_bars = []
        # Don't reset pending/active signals - they persist overnight

    def get_status(self) -> Dict:
        """Return current B&B status for dashboard"""
        return {
            'enabled': self.enabled,
            'pending_signal': asdict(self.pending_signal) if self.pending_signal else None,
            'active_signal': asdict(self.active_signal) if self.active_signal else None,
            'position_open': self.position_open,
            'entry_price': self.entry_price,
            'in_window': False,  # Updated by caller based on current time
        }
