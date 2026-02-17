"""
Layered Drawdown Circuit Breakers (Weekly + Monthly + Consecutive Losses)

Extends the daily circuit breaker with longer-horizon loss limits.
Tracks cumulative realized P&L per ISO week (Mon-Fri) and calendar month,
triggering circuit breakers when losses exceed configurable thresholds.

Also tracks consecutive losing trades and pauses trading for a configurable
duration after hitting a streak limit. The consecutive-loss pause is time-based
and does NOT reset on period rollovers.

Design philosophy matches the daily breaker:
- Only REALIZED losses count (not unrealized)
- Percentage-based limits scale with account size
- Auto-resets on period rollover (new week / new month)
- Backward compatible: missing config = all breakers disabled
"""

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class DrawdownManager:
    """
    Tracks weekly and monthly realized P&L and enforces drawdown limits.

    Weekly period = ISO week (Monday through Friday).
    Monthly period = calendar month.

    State persisted to database/drawdown_state.json so it survives restarts.
    """

    def __init__(
        self,
        account_size: float = 50000.0,
        config: Optional[Dict] = None,
        persistence_path: Optional[Path] = None,
    ):
        self.account_size = account_size

        # Parse config with safe defaults (disabled if missing)
        config = config or {}
        weekly_cfg = config.get('weekly', {})
        monthly_cfg = config.get('monthly', {})

        self.weekly_enabled = weekly_cfg.get('enabled', False)
        self.weekly_max_loss_pct = weekly_cfg.get('max_loss_pct', 4.0)

        self.monthly_enabled = monthly_cfg.get('enabled', False)
        self.monthly_max_loss_pct = monthly_cfg.get('max_loss_pct', 8.0)

        # Consecutive loss pause config
        consec_cfg = config.get('consecutive_losses', {})
        self.consec_enabled = consec_cfg.get('enabled', False)
        self.max_consecutive_losses = consec_cfg.get('max_consecutive', 5)
        self.pause_hours = consec_cfg.get('pause_hours', 24)

        # Calculate dollar limits
        self._recalculate_limits()

        # State
        self.weekly_realized_pnl: float = 0.0
        self.monthly_realized_pnl: float = 0.0
        self.weekly_breaker_triggered: bool = False
        self.monthly_breaker_triggered: bool = False

        # Consecutive loss state
        self.consecutive_losses: int = 0
        self.consec_pause_until: Optional[datetime] = None

        # Period tracking (ISO week number + year, month + year)
        today = date.today()
        self.current_iso_year, self.current_iso_week, _ = today.isocalendar()
        self.current_month = today.month
        self.current_month_year = today.year

        # Persistence
        if persistence_path:
            self.persistence_path = persistence_path
        else:
            self.persistence_path = (
                Path(__file__).parent.parent.parent / 'database' / 'drawdown_state.json'
            )

        self._load_state()

        if self.weekly_enabled or self.monthly_enabled or self.consec_enabled:
            logger.info(
                f"DrawdownManager initialized: "
                f"weekly={'ON' if self.weekly_enabled else 'OFF'} "
                f"({self.weekly_max_loss_pct}% / ${self.weekly_max_loss:.0f}), "
                f"monthly={'ON' if self.monthly_enabled else 'OFF'} "
                f"({self.monthly_max_loss_pct}% / ${self.monthly_max_loss:.0f}), "
                f"consec_losses={'ON' if self.consec_enabled else 'OFF'} "
                f"(max={self.max_consecutive_losses}, pause={self.pause_hours}h)"
            )

    def _recalculate_limits(self):
        """Recalculate dollar limits from percentages and account size."""
        self.weekly_max_loss = self.account_size * (self.weekly_max_loss_pct / 100)
        self.monthly_max_loss = self.account_size * (self.monthly_max_loss_pct / 100)

    def record_realized_pnl(self, pnl: float):
        """
        Record a realized P&L amount (positive = win, negative = loss).
        Checks all breakers after update.

        Consecutive loss tracking:
        - pnl < 0: increment consecutive_losses
        - pnl > 0: reset consecutive_losses to 0
        - pnl == 0: no change (scratch trade)
        """
        self.weekly_realized_pnl += pnl
        self.monthly_realized_pnl += pnl

        # Track consecutive losses
        if pnl < 0:
            self.consecutive_losses += 1
            if (
                self.consec_enabled
                and self.consec_pause_until is None
                and self.consecutive_losses >= self.max_consecutive_losses
            ):
                self.consec_pause_until = datetime.now() + timedelta(hours=self.pause_hours)
                logger.warning(
                    f"CONSECUTIVE LOSS PAUSE: {self.consecutive_losses} losses in a row. "
                    f"Trading paused until {self.consec_pause_until.isoformat()}"
                )
        elif pnl > 0:
            self.consecutive_losses = 0
            # A win does NOT clear an active pause (must wait it out)

        # Check weekly breaker
        if (
            self.weekly_enabled
            and not self.weekly_breaker_triggered
            and self.weekly_realized_pnl <= -self.weekly_max_loss
        ):
            self.weekly_breaker_triggered = True
            logger.warning(
                f"WEEKLY DRAWDOWN BREAKER TRIGGERED: "
                f"Realized loss ${abs(self.weekly_realized_pnl):.2f} "
                f"exceeds {self.weekly_max_loss_pct}% limit (${self.weekly_max_loss:.0f})"
            )

        # Check monthly breaker
        if (
            self.monthly_enabled
            and not self.monthly_breaker_triggered
            and self.monthly_realized_pnl <= -self.monthly_max_loss
        ):
            self.monthly_breaker_triggered = True
            logger.warning(
                f"MONTHLY DRAWDOWN BREAKER TRIGGERED: "
                f"Realized loss ${abs(self.monthly_realized_pnl):.2f} "
                f"exceeds {self.monthly_max_loss_pct}% limit (${self.monthly_max_loss:.0f})"
            )

        self._save_state()

    def can_trade(self) -> Tuple[bool, str]:
        """
        Check whether trading is allowed under drawdown limits.

        Check order: consecutive loss pause > weekly > monthly.

        Returns:
            (allowed: bool, reason: str)
        """
        # Consecutive loss pause (time-based, checked first)
        if self.consec_enabled and self.consec_pause_until is not None:
            now = datetime.now()
            if now < self.consec_pause_until:
                remaining = self.consec_pause_until - now
                hours_left = remaining.total_seconds() / 3600
                return False, (
                    f"Consecutive loss pause: {self.consecutive_losses} losses in a row. "
                    f"Resumes in {hours_left:.1f}h "
                    f"({self.consec_pause_until.strftime('%Y-%m-%d %H:%M')})"
                )
            else:
                # Pause expired, clear it
                logger.info(
                    f"Consecutive loss pause expired. "
                    f"Streak was {self.consecutive_losses}, resuming trading."
                )
                self.consec_pause_until = None
                self._save_state()

        if self.weekly_enabled and self.weekly_breaker_triggered:
            return False, (
                f"Weekly drawdown limit reached: "
                f"${abs(self.weekly_realized_pnl):.2f} loss "
                f"exceeds {self.weekly_max_loss_pct}% (${self.weekly_max_loss:.0f})"
            )

        if self.monthly_enabled and self.monthly_breaker_triggered:
            return False, (
                f"Monthly drawdown limit reached: "
                f"${abs(self.monthly_realized_pnl):.2f} loss "
                f"exceeds {self.monthly_max_loss_pct}% (${self.monthly_max_loss:.0f})"
            )

        return True, "Drawdown limits OK"

    def check_period_rollovers(self):
        """
        Detect new week or new month and reset the appropriate counters.
        Called from PortfolioManager.reset_daily() at each market open.
        """
        today = date.today()
        iso_year, iso_week, _ = today.isocalendar()
        month = today.month
        month_year = today.year

        # Weekly rollover
        if iso_year != self.current_iso_year or iso_week != self.current_iso_week:
            logger.info(
                f"Weekly rollover: W{self.current_iso_week}/{self.current_iso_year} -> "
                f"W{iso_week}/{iso_year}. "
                f"Final weekly P&L: ${self.weekly_realized_pnl:.2f}"
            )
            self.weekly_realized_pnl = 0.0
            self.weekly_breaker_triggered = False
            self.current_iso_year = iso_year
            self.current_iso_week = iso_week

        # Monthly rollover
        if month_year != self.current_month_year or month != self.current_month:
            logger.info(
                f"Monthly rollover: {self.current_month}/{self.current_month_year} -> "
                f"{month}/{month_year}. "
                f"Final monthly P&L: ${self.monthly_realized_pnl:.2f}"
            )
            self.monthly_realized_pnl = 0.0
            self.monthly_breaker_triggered = False
            self.current_month = month
            self.current_month_year = month_year

        self._save_state()

    def update_account_size(self, new_size: float):
        """Update account size and recalculate dollar limits."""
        self.account_size = new_size
        self._recalculate_limits()
        logger.info(
            f"DrawdownManager account size updated to ${new_size:.2f}: "
            f"weekly limit=${self.weekly_max_loss:.0f}, "
            f"monthly limit=${self.monthly_max_loss:.0f}"
        )

    def get_status(self) -> dict:
        """Return status dict for dashboard/API."""
        consec_paused = (
            self.consec_enabled
            and self.consec_pause_until is not None
            and datetime.now() < self.consec_pause_until
        )
        return {
            'weekly': {
                'enabled': self.weekly_enabled,
                'realized_pnl': round(self.weekly_realized_pnl, 2),
                'max_loss_pct': self.weekly_max_loss_pct,
                'max_loss_dollars': round(self.weekly_max_loss, 2),
                'breaker_triggered': self.weekly_breaker_triggered,
                'iso_week': self.current_iso_week,
                'iso_year': self.current_iso_year,
            },
            'monthly': {
                'enabled': self.monthly_enabled,
                'realized_pnl': round(self.monthly_realized_pnl, 2),
                'max_loss_pct': self.monthly_max_loss_pct,
                'max_loss_dollars': round(self.monthly_max_loss, 2),
                'breaker_triggered': self.monthly_breaker_triggered,
                'month': self.current_month,
                'year': self.current_month_year,
            },
            'consecutive_losses': {
                'enabled': self.consec_enabled,
                'count': self.consecutive_losses,
                'max_consecutive': self.max_consecutive_losses,
                'pause_hours': self.pause_hours,
                'paused': consec_paused,
                'resume_time': (
                    self.consec_pause_until.isoformat()
                    if self.consec_pause_until else None
                ),
            },
        }

    # =========================================================================
    # PERSISTENCE
    # =========================================================================

    def _load_state(self):
        """Load persisted drawdown state from disk."""
        try:
            if not self.persistence_path.exists():
                return

            data = json.loads(self.persistence_path.read_text())

            # Validate period currency - stale data from a prior period is discarded
            today = date.today()
            iso_year, iso_week, _ = today.isocalendar()

            saved_iso_year = data.get('current_iso_year')
            saved_iso_week = data.get('current_iso_week')
            if saved_iso_year == iso_year and saved_iso_week == iso_week:
                self.weekly_realized_pnl = data.get('weekly_realized_pnl', 0.0)
                self.weekly_breaker_triggered = data.get('weekly_breaker_triggered', False)
            else:
                logger.info(
                    f"Drawdown: Weekly state stale "
                    f"(saved W{saved_iso_week}/{saved_iso_year}, "
                    f"now W{iso_week}/{iso_year}), resetting"
                )

            saved_month = data.get('current_month')
            saved_month_year = data.get('current_month_year')
            if saved_month_year == today.year and saved_month == today.month:
                self.monthly_realized_pnl = data.get('monthly_realized_pnl', 0.0)
                self.monthly_breaker_triggered = data.get('monthly_breaker_triggered', False)
            else:
                logger.info(
                    f"Drawdown: Monthly state stale "
                    f"(saved {saved_month}/{saved_month_year}, "
                    f"now {today.month}/{today.year}), resetting"
                )

            # Consecutive losses are NOT period-bound, always restore
            self.consecutive_losses = data.get('consecutive_losses', 0)
            pause_str = data.get('consec_pause_until')
            if pause_str:
                self.consec_pause_until = datetime.fromisoformat(pause_str)

        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"Drawdown state file corrupted, starting fresh: {e}")
        except Exception as e:
            logger.error(f"Failed to load drawdown state: {e}")

    def _save_state(self):
        """Persist drawdown state to disk."""
        try:
            data = {
                'weekly_realized_pnl': self.weekly_realized_pnl,
                'monthly_realized_pnl': self.monthly_realized_pnl,
                'weekly_breaker_triggered': self.weekly_breaker_triggered,
                'monthly_breaker_triggered': self.monthly_breaker_triggered,
                'current_iso_year': self.current_iso_year,
                'current_iso_week': self.current_iso_week,
                'current_month': self.current_month,
                'current_month_year': self.current_month_year,
                'consecutive_losses': self.consecutive_losses,
                'consec_pause_until': (
                    self.consec_pause_until.isoformat()
                    if self.consec_pause_until else None
                ),
                'last_updated': datetime.now().isoformat(),
            }
            self.persistence_path.parent.mkdir(exist_ok=True)
            self.persistence_path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.error(f"Failed to save drawdown state: {e}")
