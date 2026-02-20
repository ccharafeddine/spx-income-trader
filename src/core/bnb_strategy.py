"""
B&B (Bed & Breakfast) Strategy - Production Line Trading p.23-25

Concept: Identify setups in the last 60 minutes of trading (15:00-16:00 ET)
to provide directional confluence for DI the next day.

Rules:
- Window: 15:00-16:00 ET (last 2 bars of the day)
- Look for pulse bars in this window
- Only the final bar (15:30) creates a signal; the 15:00 bar can reinforce
- Signal is stored overnight
- Next day: provides directional bias to DI (informational only, V1)
- Gap invalidation: if market gaps against signal, it's invalid
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, time
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

logger = logging.getLogger(__name__)


@dataclass
class BnBSignal:
    """Overnight signal from B&B detection"""
    signal_date: str  # Date the signal was generated (YYYY-MM-DD)
    direction: str  # bullish or bearish
    pulse_bar_time: str  # HH:MM of the pulse bar
    pulse_bar_close: float
    spx_close: float  # EOD close price


class BnBStrategy:
    """
    Bed & Breakfast strategy.

    Scans 15:00-16:00 for pulse bars, generates overnight signals
    for next-day directional confluence with DI.
    """

    BNB_WINDOW_START = time(15, 0)
    BNB_WINDOW_END = time(16, 0)

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
        self.gap_invalidation_pct = config.get('gap_invalidation_pct', 0.3)

        # State
        self.todays_bnb_bars = []
        self._window_pulses: List[Tuple[str, str]] = []  # (bar_time HH:MM, direction)
        self.pending_signal: Optional[BnBSignal] = None  # EOD signal waiting for next day
        self.active_signal: Optional[BnBSignal] = None  # Signal ready for bias today

        # Persistence
        if persistence_path:
            self.persistence_path = persistence_path
        else:
            self.persistence_path = Path(__file__).parent.parent.parent / 'database' / 'bnb_signals.json'

        self._load_signals()

        logger.info(
            f"BnBStrategy initialized: enabled={self.enabled}, "
            f"pulse={self.pulse_threshold}%, window=15:00-16:00 ET, "
            f"gap_invalidation={self.gap_invalidation_pct}%"
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
                # Only load if it's from a previous day (still valid for bias)
                signal_date = sig.get('signal_date')
                today = datetime.now().strftime('%Y-%m-%d')
                if signal_date and signal_date < today:
                    self.active_signal = BnBSignal(**sig)
                    logger.info(
                        f"B&B: Loaded overnight signal from {signal_date}: "
                        f"{self.active_signal.direction.upper()}"
                    )

        except Exception as e:
            logger.error(f"Failed to load B&B signals: {e}")

    def _save_signals(self):
        """Persist signals to disk"""
        try:
            data = {
                'pending_signal': asdict(self.pending_signal) if self.pending_signal else None,
                'active_signal': asdict(self.active_signal) if self.active_signal else None,
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

    def on_bar_complete(self, bar, current_price: float):
        """
        Process a completed bar for B&B signals.

        During 15:00-16:00: Look for pulse bars and collect them.
        Signal resolution happens in on_day_end().
        """
        if not self.enabled:
            return None

        bar_time = bar.timestamp

        # During B&B window, collect and evaluate bars
        if self.is_bnb_window(bar_time):
            self.todays_bnb_bars.append(bar)

            pulse_dir = self._is_pulse_bar(bar)
            if pulse_dir:
                bar_time_str = bar_time.strftime('%H:%M')
                self._window_pulses.append((bar_time_str, pulse_dir))

                logger.info(
                    f"B&B: {pulse_dir.upper()} pulse detected at {bar_time_str}"
                )

        return None

    def on_day_end(self, spx_close: float):
        """Called at market close to resolve window pulses into a signal."""
        # Resolve the window pulses into a signal
        self.pending_signal = None

        if self._window_pulses:
            final_bar_pulse = None
            first_bar_pulse = None

            for bar_time_str, direction in self._window_pulses:
                if bar_time_str >= '15:30':
                    final_bar_pulse = direction
                else:
                    first_bar_pulse = direction

            if final_bar_pulse:
                # Final bar has a pulse - check for conflict
                if first_bar_pulse and first_bar_pulse != final_bar_pulse:
                    # Conflicting directions - no signal
                    logger.info(
                        f"B&B: Conflicting pulses ({first_bar_pulse} vs {final_bar_pulse}), no signal"
                    )
                else:
                    # Final bar pulse is the signal (first bar may reinforce)
                    reinforced = " (reinforced)" if first_bar_pulse == final_bar_pulse else ""
                    # Use the last pulse bar time at 15:30 or later
                    pulse_time = None
                    pulse_close = spx_close
                    for bar_time_str, direction in self._window_pulses:
                        if bar_time_str >= '15:30' and direction == final_bar_pulse:
                            pulse_time = bar_time_str
                            # Try to get close from todays_bnb_bars
                            for b in self.todays_bnb_bars:
                                if b.timestamp.strftime('%H:%M') == bar_time_str:
                                    pulse_close = b.close
                                    break

                    self.pending_signal = BnBSignal(
                        signal_date=datetime.now().strftime('%Y-%m-%d') if not self.todays_bnb_bars
                        else self.todays_bnb_bars[0].timestamp.strftime('%Y-%m-%d'),
                        direction=final_bar_pulse,
                        pulse_bar_time=pulse_time or '15:30',
                        pulse_bar_close=pulse_close,
                        spx_close=spx_close,
                    )

                    logger.info(
                        f"B&B: Signal locked for tomorrow: {final_bar_pulse.upper()}{reinforced} "
                        f"(from {pulse_time or '15:30'})"
                    )
            else:
                # Only first bar (15:00) had pulse, no final bar pulse - no signal
                logger.info("B&B: Only 15:00 bar had pulse, no final bar confirmation - no signal")
        else:
            logger.debug("B&B: No pulses in window, no signal")

        self._save_signals()

        # Reset for next day
        self.todays_bnb_bars = []
        self._window_pulses = []

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

    def get_bias(self) -> Optional[str]:
        """Return 'bullish', 'bearish', or None based on overnight signal."""
        if self.active_signal:
            return self.active_signal.direction
        return None

    def validate_signal(self, current_price: float) -> bool:
        """Check if overnight signal is still valid given morning price.
        If market gapped against the signal, the signal is invalid."""
        if not self.active_signal:
            return False
        gap_pct = self.gap_invalidation_pct
        prev_close = self.active_signal.spx_close
        if prev_close <= 0:
            return True
        move_pct = (current_price - prev_close) / prev_close * 100
        if self.active_signal.direction == 'bullish' and move_pct < -gap_pct:
            return False  # Gapped down against bullish signal
        if self.active_signal.direction == 'bearish' and move_pct > gap_pct:
            return False  # Gapped up against bearish signal
        return True

    def reset_daily(self):
        """Reset daily state (called on new trading day)"""
        self.todays_bnb_bars = []
        self._window_pulses = []
        # Don't reset pending/active signals - they persist overnight

    def get_status(self) -> Dict:
        """Return current B&B status for dashboard"""
        return {
            'enabled': self.enabled,
            'pending_signal': asdict(self.pending_signal) if self.pending_signal else None,
            'active_signal': asdict(self.active_signal) if self.active_signal else None,
            'in_window': False,  # Updated by caller based on current time
        }
