"""
ORB (Opening Range Breakout) Strategy - Production Line Trading p.22

Concept: Use the first 30-minute bar's range as entry trigger with relaxed threshold.

Rules from PDF:
- Uses first 30-minute bar high-low range
- More flexible threshold: 10% to 40% (vs strict 10% for main strategy)
- Good for when you can't watch the screen - set and forget
- Entry on breakout of opening range
- Can run alongside daily income strategy
"""

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, time
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
    direction_bias: Optional[str]  # bullish/bearish/bullish_weak/bearish_weak


class ORBStrategy:
    """
    Opening Range Breakout strategy.

    Uses first 30-min bar with relaxed 10-40% threshold.
    Good for set-and-forget trading.
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
        self.max_threshold = config.get('max_threshold', 40.0)
        self.contracts_per_trade = config.get('contracts_per_trade', 3)

        # State
        self.opening_range: Optional[ORBRange] = None
        self.triggered_today = False
        self.position_open = False
        self.entry_price: Optional[float] = None
        self.entry_direction: Optional[str] = None

        # Persistence
        if persistence_path:
            self.persistence_path = persistence_path
        else:
            self.persistence_path = Path(__file__).parent.parent.parent / 'database' / 'orb_state.json'

        self._load_state()

        logger.info(
            f"ORBStrategy initialized: enabled={self.enabled}, "
            f"threshold={self.min_threshold}-{self.max_threshold}%"
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

        close_position = (first_bar.close - first_bar.low) / bar_range * 100

        # Check if close is in the threshold zone (bullish or bearish)
        direction_bias = None

        # Bullish: close in top portion of range
        if close_position >= (100 - self.max_threshold):
            if close_position >= (100 - self.min_threshold):
                direction_bias = "bullish"  # Strong (top 10%)
            else:
                direction_bias = "bullish_weak"  # Relaxed (10-40%)

        # Bearish: close in bottom portion of range
        elif close_position <= self.max_threshold:
            if close_position <= self.min_threshold:
                direction_bias = "bearish"  # Strong (bottom 10%)
            else:
                direction_bias = "bearish_weak"  # Relaxed (10-40%)

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
            strength = "STRONG" if direction_bias in ('bullish', 'bearish') else "WEAK"
            logger.info(
                f"ORB: Opening range set - High=${first_bar.high:.2f}, "
                f"Low=${first_bar.low:.2f}, Close={close_position:.1f}%, "
                f"Bias={direction_bias.upper()} ({strength})"
            )
        else:
            logger.info(
                f"ORB: Opening range set - High=${first_bar.high:.2f}, "
                f"Low=${first_bar.low:.2f}, Close={close_position:.1f}% (no bias)"
            )

        return self.opening_range

    def check_breakout(self, current_price: float) -> Optional[Dict]:
        """
        Check if price has broken out of the opening range.

        Only triggers once per day.
        """
        if not self.enabled or not self.opening_range or self.triggered_today:
            return None

        orb = self.opening_range

        # Need a directional bias to trigger
        if not orb.direction_bias:
            return None

        # Bullish breakout: price breaks above opening range high
        if 'bullish' in orb.direction_bias and current_price > orb.high:
            self.triggered_today = True
            self.position_open = True
            self.entry_price = current_price
            self.entry_direction = 'bullish'
            self._save_state()

            logger.info(
                f"ORB BREAKOUT: BULLISH - Price ${current_price:,.2f} > "
                f"ORB high ${orb.high:.2f}"
            )

            return {
                "strategy": "orb",
                "action": "enter",
                "direction": "bullish",
                "entry_price": current_price,
                "opening_range_high": orb.high,
                "opening_range_low": orb.low,
                "bias_strength": "strong" if orb.direction_bias == "bullish" else "weak",
                "trigger": f"Break above ORB high ${orb.high:.2f}"
            }

        # Bearish breakout: price breaks below opening range low
        if 'bearish' in orb.direction_bias and current_price < orb.low:
            self.triggered_today = True
            self.position_open = True
            self.entry_price = current_price
            self.entry_direction = 'bearish'
            self._save_state()

            logger.info(
                f"ORB BREAKOUT: BEARISH - Price ${current_price:,.2f} < "
                f"ORB low ${orb.low:.2f}"
            )

            return {
                "strategy": "orb",
                "action": "enter",
                "direction": "bearish",
                "entry_price": current_price,
                "opening_range_high": orb.high,
                "opening_range_low": orb.low,
                "bias_strength": "strong" if orb.direction_bias == "bearish" else "weak",
                "trigger": f"Break below ORB low ${orb.low:.2f}"
            }

        return None

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
