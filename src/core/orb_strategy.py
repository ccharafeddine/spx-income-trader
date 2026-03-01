"""
ORB (Opening Range Breakout) Strategy - Production Line Trading p.22

Concept: Use the first 30-minute bar's range as entry trigger with strong-only threshold.

Rules from PDF:
- Uses first 30-minute bar high-low range
- Strong threshold only: close in top/bottom 10% of range
- Minimum range filter: skip days with narrow opening ranges
- Confirmation delay: breakout must hold for N minutes before entry
- Entry on confirmed breakout of opening range
- Can run alongside daily income strategy
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


@dataclass
class ORBRange:
    """The opening range for the day"""
    date: str
    high: float
    low: float
    close: float
    range_size: float
    close_position_pct: float  # Where close is within range (0-100)
    direction_bias: Optional[str]  # bullish, bearish, or None


class ORBStrategy:
    """
    Opening Range Breakout strategy.

    Uses first 30-min bar with strong 10% threshold only.
    Includes range filter and confirmation delay.
    """

    FIRST_BAR_END = time(10, 0)

    def __init__(self, config: Dict[str, Any], persistence_path: Optional[Path] = None):
        """
        Initialize ORB strategy.

        Args:
            config: Configuration dict from strategy_params.yaml orb section
            persistence_path: Path to JSON file for state persistence
        """
        self.config = config
        self.enabled = config.get('enabled', False)
        self.min_threshold = config.get('min_threshold', 10.0)
        self.min_range_points = config.get('min_range_points', 8.0)
        self.confirmation_minutes = config.get('confirmation_minutes', 3)
        # State
        self.opening_range: Optional[ORBRange] = None
        self.triggered_today = False
        self.position_open = False
        self.entry_price: Optional[float] = None
        self.entry_direction: Optional[str] = None
        self._breakout_pending: Optional[Dict] = None

        # Persistence
        if persistence_path:
            self.persistence_path = persistence_path
        else:
            from src.utils.app_paths import DATA_DIR
            self.persistence_path = DATA_DIR / 'database' / 'orb_state.json'

        self._load_state()

        logger.info(
            f"ORBStrategy initialized: enabled={self.enabled}, "
            f"threshold={self.min_threshold}%, "
            f"min_range={self.min_range_points}pts, "
            f"confirmation={self.confirmation_minutes}min"
        )

    def _load_state(self):
        """Load state from persistence file"""
        try:
            if not self.persistence_path.exists():
                return

            data = json.loads(self.persistence_path.read_text())
            today = datetime.now().strftime('%Y-%m-%d')

            # Only restore if same day
            if data.get('date') == today:
                if data.get('opening_range'):
                    self.opening_range = ORBRange(**data['opening_range'])
                self.triggered_today = data.get('triggered_today', False)
                self.position_open = data.get('position_open', False)
                self.entry_price = data.get('entry_price')
                self.entry_direction = data.get('entry_direction')
                self._breakout_pending = data.get('breakout_pending')
                logger.info(f"ORB: State restored for {today}")

        except Exception as e:
            logger.error(f"Failed to load ORB state: {e}")

    def _save_state(self):
        """Persist state to disk"""
        try:
            data = {
                'date': datetime.now().strftime('%Y-%m-%d'),
                'opening_range': asdict(self.opening_range) if self.opening_range else None,
                'triggered_today': self.triggered_today,
                'position_open': self.position_open,
                'entry_price': self.entry_price,
                'entry_direction': self.entry_direction,
                'breakout_pending': self._breakout_pending,
            }
            self.persistence_path.parent.mkdir(exist_ok=True)
            self.persistence_path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.error(f"Failed to save ORB state: {e}")

    def set_opening_range(self, first_bar) -> Optional[ORBRange]:
        """
        Set the opening range from the first 30-min bar (09:30-10:00).

        Returns ORBRange if it qualifies, None otherwise.
        """
        if not self.enabled:
            return None

        bar_range = first_bar.high - first_bar.low
        if bar_range <= 0:
            logger.debug("ORB: First bar has zero range, skipping")
            return None

        if bar_range < self.min_range_points:
            logger.info(
                f"ORB: Range too small ({bar_range:.1f} < {self.min_range_points}), skipping"
            )
            return None

        close_position = (first_bar.close - first_bar.low) / bar_range * 100

        # Only strong signals: close in top/bottom min_threshold% of range
        direction_bias = None
        if close_position >= (100 - self.min_threshold):
            direction_bias = "bullish"
        elif close_position <= self.min_threshold:
            direction_bias = "bearish"

        self.opening_range = ORBRange(
            date=first_bar.timestamp.strftime('%Y-%m-%d'),
            high=first_bar.high,
            low=first_bar.low,
            close=first_bar.close,
            range_size=bar_range,
            close_position_pct=round(close_position, 1),
            direction_bias=direction_bias
        )

        self._save_state()

        if direction_bias:
            logger.info(
                f"ORB: Opening range set - High=${first_bar.high:.2f}, "
                f"Low=${first_bar.low:.2f}, Close={close_position:.1f}%, "
                f"Bias={direction_bias.upper()}"
            )
        else:
            logger.info(
                f"ORB: Opening range set - High=${first_bar.high:.2f}, "
                f"Low=${first_bar.low:.2f}, Close={close_position:.1f}% (no bias)"
            )

        return self.opening_range

    def check_breakout(self, current_price: float, current_time: datetime) -> Optional[Dict]:
        """
        Check if price has broken out of the opening range.

        Uses confirmation delay: breakout must hold for confirmation_minutes
        before firing. Only triggers once per day.
        """
        if not self.enabled or not self.opening_range or self.triggered_today:
            return None

        orb = self.opening_range

        # Need a directional bias to trigger
        if not orb.direction_bias:
            return None

        # Check for pending breakout confirmation
        if self._breakout_pending:
            elapsed = (current_time - datetime.fromisoformat(self._breakout_pending['trigger_time']))
            if hasattr(elapsed, 'total_seconds'):
                elapsed_minutes = elapsed.total_seconds() / 60
            else:
                elapsed_minutes = 0

            if elapsed_minutes >= self.confirmation_minutes:
                # Check if price still beyond breakout level
                direction = self._breakout_pending['direction']
                if direction == 'bullish' and current_price > orb.high:
                    return self._fire_breakout(direction, current_price)
                elif direction == 'bearish' and current_price < orb.low:
                    return self._fire_breakout(direction, current_price)
                else:
                    # Price retreated - cancel breakout
                    logger.info("ORB: Breakout failed confirmation")
                    self._breakout_pending = None
                    self.triggered_today = True
                    self._save_state()
                    return None
            else:
                # Still waiting for confirmation
                return None

        # No pending breakout - check for new breakout
        if orb.direction_bias == 'bullish' and current_price > orb.high:
            self._breakout_pending = {
                'direction': 'bullish',
                'trigger_time': current_time.isoformat(),
                'trigger_price': current_price,
            }
            self._save_state()
            logger.info(
                f"ORB: Bullish breakout pending confirmation "
                f"(${current_price:,.2f} > ${orb.high:.2f})"
            )
            return None

        if orb.direction_bias == 'bearish' and current_price < orb.low:
            self._breakout_pending = {
                'direction': 'bearish',
                'trigger_time': current_time.isoformat(),
                'trigger_price': current_price,
            }
            self._save_state()
            logger.info(
                f"ORB: Bearish breakout pending confirmation "
                f"(${current_price:,.2f} < ${orb.low:.2f})"
            )
            return None

        return None

    def _fire_breakout(self, direction: str, current_price: float) -> Dict:
        """Fire confirmed breakout signal."""
        orb = self.opening_range
        self.triggered_today = True
        self.position_open = True
        self.entry_price = current_price
        self.entry_direction = direction
        self._breakout_pending = None
        self._save_state()

        level = "high" if direction == "bullish" else "low"
        level_price = orb.high if direction == "bullish" else orb.low
        logger.info(
            f"ORB BREAKOUT: {direction.upper()} - Price ${current_price:,.2f} "
            f"confirmed beyond ORB {level} ${level_price:.2f}"
        )

        return {
            "strategy": "orb",
            "action": "enter",
            "direction": direction,
            "entry_price": current_price,
            "opening_range_high": orb.high,
            "opening_range_low": orb.low,
            "trigger": f"Break {'above' if direction == 'bullish' else 'below'} ORB {level} ${level_price:.2f}"
        }

    def rollback_entry(self):
        """Rollback premature state changes when trade execution fails.

        check_breakout() sets triggered_today/position_open BEFORE returning
        the signal dict. If the caller's execution pipeline fails (bad spread,
        portfolio block, broker reject), call this to restore the strategy to
        a retryable state.
        """
        self.triggered_today = False
        self.position_open = False
        self.entry_price = None
        self.entry_direction = None
        self._breakout_pending = None
        self._save_state()
        logger.info("ORB: Entry rolled back (execution failed), will retry on next tick")

    def on_position_closed(self, exit_price: float, reason: str):
        """Called when ORB position is closed"""
        if self.position_open:
            pnl_direction = "profit" if (
                (self.entry_direction == 'bullish' and exit_price > self.entry_price) or
                (self.entry_direction == 'bearish' and exit_price < self.entry_price)
            ) else "loss"

            logger.info(
                f"ORB EXIT: {reason} @ ${exit_price:.2f} "
                f"(entry ${self.entry_price:.2f}, {pnl_direction})"
            )

            self.position_open = False
            self.entry_price = None
            self.entry_direction = None
            self._save_state()

    def reset_daily(self):
        """Reset for new trading day"""
        self.opening_range = None
        self.triggered_today = False
        self.position_open = False
        self.entry_price = None
        self.entry_direction = None
        self._breakout_pending = None
        self._save_state()
        logger.debug("ORB: Daily reset")

    def get_status(self) -> Dict:
        """Return current ORB status for dashboard"""
        return {
            'enabled': self.enabled,
            'opening_range': asdict(self.opening_range) if self.opening_range else None,
            'triggered_today': self.triggered_today,
            'position_open': self.position_open,
            'entry_price': self.entry_price,
            'entry_direction': self.entry_direction,
        }
