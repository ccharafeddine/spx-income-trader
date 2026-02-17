"""
Tag 'n Turn Swing Strategy (Production Line Trading p.30-33)

A separate parallel strategy that runs independently from the daily income strategy.
Uses Bollinger Band tags + pulse bar reversals for multi-day swing trades.

State Machine:
  IDLE -> TAG_DETECTED -> PULSE_CONFIRMED -> AWAITING_BREAKOUT -> POSITION_OPEN -> IDLE

Entry Requirements (all must be met):
  1. Price tags Bollinger Band (50-period, 2 std on 30-min bars)
  2. Pulse bar forms in REVERSAL direction (opposite to band tagged)
  3. Next bar breaks pulse bar high/low (confirmation)

Position: Credit spread in reversal direction, 3-7 DTE
Target: Opposite Bollinger Band
Stop: Close beyond entry band
"""

import json
import logging
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any

from .bollinger_filter import BollingerFilter
from .pulse_detector import PulseBarDetector
from ..models.bar import Bar, BarType
from ..models.spread import TradeDirection

logger = logging.getLogger(__name__)


class TNTState(Enum):
    """State machine states for Tag 'n Turn strategy."""
    IDLE = "IDLE"
    TAG_DETECTED = "TAG_DETECTED"
    PULSE_CONFIRMED = "PULSE_CONFIRMED"
    AWAITING_BREAKOUT = "AWAITING_BREAKOUT"
    POSITION_OPEN = "POSITION_OPEN"


class TagNTurnStrategy:
    """
    Tag 'n Turn swing trading strategy.

    Watches for Bollinger Band tags, waits for reversal pulse bars,
    then enters on breakout confirmation for multi-day swing trades.
    """

    def __init__(self, config: Dict[str, Any], persistence_path: Optional[Path] = None):
        """
        Initialize Tag 'n Turn strategy.

        Args:
            config: Configuration dict from strategy_params.yaml tag_n_turn section
            persistence_path: Path to JSON file for state persistence
        """
        self.config = config
        self.enabled = config.get('enabled', False)

        # Bollinger Band filter for tag detection
        self.bb_filter = BollingerFilter(
            period=config.get('bb_period', 50),
            num_std=config.get('bb_std', 2.0),
            extreme_move_pct=0  # No extreme move override for Tag 'n Turn
        )

        # Pulse bar detector for reversal confirmation
        self.pulse_detector = PulseBarDetector(
            threshold_percent=config.get('pulse_threshold', 10.0)
        )

        # Position parameters
        self.max_hold_days = config.get('max_hold_days', 7)
        self.use_credit_spreads = config.get('use_credit_spreads', True)
        self.max_contracts_override = config.get('max_contracts_override', 2)
        self.spread_width = config.get('spread_width', 10.0)
        self.min_credit = config.get('min_credit', 2.00)
        self.min_dte = config.get('min_dte', 3)
        self.max_dte = config.get('max_dte', 7)

        # State machine
        self.state = TNTState.IDLE
        self.tag_info: Optional[Dict] = None  # Band tag details
        self.pulse_info: Optional[Dict] = None  # Confirmed pulse bar
        self.breakout_level: Optional[float] = None  # Price to break for entry
        self.active_position: Optional[Dict] = None  # Open position details
        self.history: list = []  # Completed trades

        # Persistence
        if persistence_path:
            self.persistence_path = persistence_path
        else:
            # Default path relative to project root
            self.persistence_path = Path(__file__).parent.parent.parent / 'database' / 'tag_n_turn_positions.json'

        # Load persisted state if exists
        self._load_state()

        logger.info(
            f"TagNTurnStrategy initialized: enabled={self.enabled}, "
            f"BB({config.get('bb_period', 50)}, {config.get('bb_std', 2.0)}), "
            f"state={self.state.value}"
        )

    def on_bar_complete(self, bar: Bar) -> None:
        """
        Process a completed 30-min bar.

        Updates BB filter and checks for setup conditions based on current state.

        Args:
            bar: Completed Bar object
        """
        if not self.enabled:
            return

        # Always feed bars to BB filter to maintain band calculations
        self.bb_filter.add_bar(bar)

        if not self.bb_filter.has_sufficient_data:
            logger.debug("Tag 'n Turn: Waiting for sufficient BB data")
            return

        # State machine processing
        if self.state == TNTState.IDLE:
            self._check_for_tag(bar)
        elif self.state == TNTState.TAG_DETECTED:
            self._check_for_pulse(bar)
        elif self.state == TNTState.PULSE_CONFIRMED:
            # Pulse just confirmed, set up breakout level
            self._setup_breakout_trigger()
        elif self.state == TNTState.AWAITING_BREAKOUT:
            # Timeout stale breakout setups (6 bars = 3 hours)
            bar_ts = self.pulse_info.get('bar_timestamp') if self.pulse_info else None
            if bar_ts:
                try:
                    pulse_time = datetime.fromisoformat(bar_ts)
                    if bar.timestamp - pulse_time > timedelta(hours=3):
                        self._reset_to_idle(
                            f"Breakout timeout - setup from {pulse_time.strftime('%H:%M')} expired after 3h"
                        )
                except (ValueError, TypeError):
                    pass
        elif self.state == TNTState.POSITION_OPEN:
            # Check exit conditions on each bar
            pass  # Exit checks happen in check_exit_conditions with current price

    def check_entry_signal(self, current_price: float) -> Optional[Dict]:
        """
        Check if entry signal conditions are met.

        Called on each tick/price update to check for breakout confirmation.

        Args:
            current_price: Current SPX price

        Returns:
            Signal dict if entry triggered, None otherwise
        """
        if not self.enabled:
            return None

        if self.state != TNTState.AWAITING_BREAKOUT:
            return None

        if self.pulse_info is None or self.breakout_level is None:
            logger.warning("Tag 'n Turn in AWAITING_BREAKOUT but missing pulse_info/breakout_level")
            self._reset_to_idle("Invalid state - missing data")
            return None

        direction = self.pulse_info['direction']
        triggered = False

        if direction == 'bullish' and current_price > self.breakout_level:
            logger.info(
                f"TAG 'N TURN BREAKOUT: Price ${current_price:,.2f} > "
                f"${self.breakout_level:,.2f} - BULLISH entry triggered"
            )
            triggered = True
        elif direction == 'bearish' and current_price < self.breakout_level:
            logger.info(
                f"TAG 'N TURN BREAKOUT: Price ${current_price:,.2f} < "
                f"${self.breakout_level:,.2f} - BEARISH entry triggered"
            )
            triggered = True

        if triggered:
            # Calculate target (opposite BB) and stop (entry band)
            bands = self.bb_filter.bands
            if direction == 'bullish':
                target_price = bands['upper']
                stop_price = bands['lower'] - 5  # Slightly beyond lower band
            else:
                target_price = bands['lower']
                stop_price = bands['upper'] + 5  # Slightly beyond upper band

            signal = {
                'strategy': 'tag_n_turn',
                'direction': TradeDirection.BULLISH if direction == 'bullish' else TradeDirection.BEARISH,
                'entry_price': current_price,
                'target_price': target_price,
                'stop_price': stop_price,
                'tag_info': self.tag_info,
                'pulse_bar': self.pulse_info,
                'breakout_level': self.breakout_level,
                'timestamp': datetime.now().isoformat(),
            }

            return signal

        return None

    def on_position_opened(self, position: Dict) -> None:
        """
        Called when a Tag 'n Turn position is opened.

        Args:
            position: Position details dict
        """
        self.state = TNTState.POSITION_OPEN
        self.active_position = position
        self._save_state()

        logger.info(
            f"TAG 'N TURN POSITION OPENED: {position.get('direction', 'unknown').upper()} "
            f"@ ${position.get('entry_price', 0):,.2f}, "
            f"target=${position.get('target_price', 0):,.2f}, "
            f"stop=${position.get('stop_price', 0):,.2f}"
        )

    def check_exit_conditions(self, current_price: float) -> Optional[Dict]:
        """
        Check if exit conditions are met for open position.

        Args:
            current_price: Current SPX price

        Returns:
            Exit signal dict if exit triggered, None otherwise
        """
        if not self.enabled or self.state != TNTState.POSITION_OPEN:
            return None

        if self.active_position is None:
            return None

        pos = self.active_position
        direction = pos.get('direction', '').lower()
        target = pos.get('target_price', 0)
        stop = pos.get('stop_price', 0)
        entry_time_str = pos.get('entry_time')

        exit_signal = None
        exit_reason = None

        # Check target hit
        if direction == 'bullish' and current_price >= target:
            exit_reason = 'target_hit'
            logger.info(f"TAG 'N TURN TARGET HIT: ${current_price:,.2f} >= ${target:,.2f}")
        elif direction == 'bearish' and current_price <= target:
            exit_reason = 'target_hit'
            logger.info(f"TAG 'N TURN TARGET HIT: ${current_price:,.2f} <= ${target:,.2f}")

        # Check stop hit
        if direction == 'bullish' and current_price <= stop:
            exit_reason = 'stop_hit'
            logger.info(f"TAG 'N TURN STOP HIT: ${current_price:,.2f} <= ${stop:,.2f}")
        elif direction == 'bearish' and current_price >= stop:
            exit_reason = 'stop_hit'
            logger.info(f"TAG 'N TURN STOP HIT: ${current_price:,.2f} >= ${stop:,.2f}")

        # Check max hold days
        if entry_time_str and not exit_reason:
            try:
                entry_time = datetime.fromisoformat(entry_time_str)
                days_held = (datetime.now() - entry_time).days
                if days_held >= self.max_hold_days:
                    exit_reason = 'max_hold_exceeded'
                    logger.info(f"TAG 'N TURN MAX HOLD: {days_held} days >= {self.max_hold_days}")
            except (ValueError, TypeError):
                pass

        if exit_reason:
            exit_signal = {
                'strategy': 'tag_n_turn',
                'reason': exit_reason,
                'exit_price': current_price,
                'position': pos,
                'timestamp': datetime.now().isoformat(),
            }

        return exit_signal

    def on_position_closed(self, result: Dict) -> None:
        """
        Called when a Tag 'n Turn position is closed.

        Args:
            result: Trade result dict with P&L etc.
        """
        # Archive to history
        if self.active_position:
            trade_record = {
                **self.active_position,
                'exit_time': result.get('exit_time', datetime.now().isoformat()),
                'exit_price': result.get('exit_price'),
                'exit_reason': result.get('reason'),
                'pnl': result.get('pnl', 0),
            }
            self.history.append(trade_record)

        self._reset_to_idle(f"Position closed: {result.get('reason', 'unknown')}")

    def seed_historical(self, symbol: str = '%5EGSPC') -> int:
        """
        Seed BB filter with historical data.

        Args:
            symbol: Yahoo Finance symbol

        Returns:
            Number of bars loaded
        """
        return self.bb_filter.seed_historical(symbol)

    def get_status(self) -> Dict:
        """
        Get current strategy status for dashboard/logging.

        Returns:
            Status dict with state, position info, etc.
        """
        status = {
            'enabled': self.enabled,
            'state': self.state.value,
            'bb_status': self.bb_filter.get_status() if self.enabled else None,
        }

        if self.state == TNTState.TAG_DETECTED and self.tag_info:
            status['tag_info'] = {
                'band': self.tag_info.get('band'),
                'time': self.tag_info.get('time'),
                'reversal_direction': 'BULL' if self.tag_info.get('band') == 'upper' else 'BEAR',
            }

        if self.state == TNTState.PULSE_CONFIRMED and self.pulse_info:
            status['pulse_info'] = {
                'direction': self.pulse_info.get('direction', '').upper(),
                'bar_time': self.pulse_info.get('bar_time'),
            }

        if self.state == TNTState.AWAITING_BREAKOUT:
            status['awaiting_breakout'] = {
                'direction': self.pulse_info.get('direction', '').upper() if self.pulse_info else None,
                'breakout_level': self.breakout_level,
            }

        if self.state == TNTState.POSITION_OPEN and self.active_position:
            pos = self.active_position
            status['active_position'] = {
                'id': pos.get('id'),
                'direction': pos.get('direction', '').upper(),
                'entry_price': pos.get('entry_price'),
                'target_price': pos.get('target_price'),
                'stop_price': pos.get('stop_price'),
                'entry_time': pos.get('entry_time'),
            }

        return status

    def _check_for_tag(self, bar: Bar) -> None:
        """Check if bar tagged a Bollinger Band."""
        bands = self.bb_filter.bands
        if bands is None:
            return

        tagged_lower = bar.low <= bands['lower']
        tagged_upper = bar.high >= bands['upper']

        if tagged_lower or tagged_upper:
            # Determine which band was tagged (closer if both)
            if tagged_lower and tagged_upper:
                dist_lower = abs(bar.close - bands['lower'])
                dist_upper = abs(bar.close - bands['upper'])
                band = 'lower' if dist_lower <= dist_upper else 'upper'
            else:
                band = 'lower' if tagged_lower else 'upper'

            # Reversal direction is opposite of band tagged
            reversal = 'bullish' if band == 'lower' else 'bearish'

            self.tag_info = {
                'band': band,
                'band_value': bands[band],
                'bar_time': bar.timestamp.strftime('%H:%M') if bar.timestamp else None,
                'bar_close': bar.close,
                'reversal_direction': reversal,
            }

            self.state = TNTState.TAG_DETECTED
            self._save_state()

            logger.info(
                f"TAG 'N TURN: {band.upper()} band tagged at {bar.timestamp.strftime('%H:%M') if bar.timestamp else 'unknown'} "
                f"(${bar.close:,.2f}), watching for {reversal.upper()} reversal pulse"
            )

    def _check_for_pulse(self, bar: Bar) -> None:
        """Check if bar is a reversal pulse in expected direction."""
        if self.tag_info is None:
            self._reset_to_idle("Tag info missing")
            return

        expected_direction = self.tag_info['reversal_direction']
        bar_type = self.pulse_detector.analyze_bar(bar)

        # Check if pulse matches expected reversal direction
        if bar_type == BarType.BULLISH_PULSE and expected_direction == 'bullish':
            self._confirm_pulse(bar, 'bullish')
        elif bar_type == BarType.BEARISH_PULSE and expected_direction == 'bearish':
            self._confirm_pulse(bar, 'bearish')
        else:
            # Check if we should reset (opposite band tagged or too many bars)
            bands = self.bb_filter.bands
            if bands:
                opposite_band = 'upper' if self.tag_info['band'] == 'lower' else 'lower'
                if opposite_band == 'upper' and bar.high >= bands['upper']:
                    self._reset_to_idle("Opposite band (upper) tagged before pulse")
                elif opposite_band == 'lower' and bar.low <= bands['lower']:
                    self._reset_to_idle("Opposite band (lower) tagged before pulse")

    def _confirm_pulse(self, bar: Bar, direction: str) -> None:
        """Confirm pulse bar and set up for breakout entry."""
        self.pulse_info = {
            'direction': direction,
            'bar': bar,
            'bar_time': bar.timestamp.strftime('%H:%M') if bar.timestamp else None,
            'bar_timestamp': bar.timestamp.isoformat() if bar.timestamp else None,
            'high': bar.high,
            'low': bar.low,
            'close': bar.close,
        }

        self.state = TNTState.PULSE_CONFIRMED

        logger.info(
            f"TAG 'N TURN PULSE CONFIRMED: {direction.upper()} pulse at "
            f"{bar.timestamp.strftime('%H:%M') if bar.timestamp else 'unknown'} "
            f"(H=${bar.high:,.2f} L=${bar.low:,.2f} C=${bar.close:,.2f})"
        )

        # Immediately set up breakout trigger
        self._setup_breakout_trigger()

    def _setup_breakout_trigger(self) -> None:
        """Set breakout level based on pulse bar."""
        if self.pulse_info is None:
            self._reset_to_idle("Pulse info missing")
            return

        direction = self.pulse_info['direction']

        # Breakout level: pulse bar high for bullish, low for bearish
        if direction == 'bullish':
            self.breakout_level = self.pulse_info['high']
        else:
            self.breakout_level = self.pulse_info['low']

        self.state = TNTState.AWAITING_BREAKOUT
        self._save_state()

        above_below = 'above' if direction == 'bullish' else 'below'
        logger.info(
            f"TAG 'N TURN AWAITING BREAKOUT: Entry triggers {above_below} "
            f"${self.breakout_level:,.2f}"
        )

    def _reset_to_idle(self, reason: str = "") -> None:
        """Reset state machine to IDLE."""
        old_state = self.state.value
        self.state = TNTState.IDLE
        self.tag_info = None
        self.pulse_info = None
        self.breakout_level = None
        self.active_position = None
        self._save_state()

        if reason:
            logger.info(f"TAG 'N TURN RESET: {old_state} -> IDLE ({reason})")

    def _save_state(self) -> None:
        """Persist current state to JSON file."""
        try:
            state_data = {
                'state': self.state.value,
                'tag_info': self.tag_info,
                'pulse_info': self._serialize_pulse_info(),
                'breakout_level': self.breakout_level,
                'active_position': self.active_position,
                'history': self.history[-50:],  # Keep last 50 trades
            }

            with open(self.persistence_path, 'w') as f:
                json.dump(state_data, f, indent=2, default=str)

            logger.debug(f"Tag 'n Turn state saved: {self.state.value}")
        except Exception as e:
            logger.error(f"Failed to save Tag 'n Turn state: {e}")

    def _load_state(self) -> None:
        """Load state from JSON file if exists."""
        try:
            if not self.persistence_path.exists():
                logger.info("No Tag 'n Turn state file found, starting fresh")
                return

            with open(self.persistence_path, 'r') as f:
                data = json.load(f)

            self.state = TNTState(data.get('state', 'IDLE'))
            self.tag_info = data.get('tag_info')
            self.pulse_info = data.get('pulse_info')  # Bar object not restored
            self.breakout_level = data.get('breakout_level')
            self.active_position = data.get('active_position')
            self.history = data.get('history', [])

            logger.info(
                f"Tag 'n Turn state restored: {self.state.value}"
                + (f", position open" if self.active_position else "")
            )
        except Exception as e:
            logger.warning(f"Failed to load Tag 'n Turn state: {e}")
            self.state = TNTState.IDLE

    def _serialize_pulse_info(self) -> Optional[Dict]:
        """Serialize pulse_info for JSON, excluding Bar object."""
        if self.pulse_info is None:
            return None

        # Exclude the Bar object which isn't JSON serializable
        return {
            'direction': self.pulse_info.get('direction'),
            'bar_time': self.pulse_info.get('bar_time'),
            'high': self.pulse_info.get('high'),
            'low': self.pulse_info.get('low'),
            'close': self.pulse_info.get('close'),
        }
