"""
Layered Drawdown Circuit Breakers (Daily + Weekly + Monthly + Consecutive Losses)

Three independent loss limits -- Daily, Weekly, Monthly -- each on their own
reset schedule.  If ANY ONE reaches its configured limit, the bot cannot enter
new trades until that period resets.

Also tracks consecutive losing trades and pauses trading for a configurable
duration after hitting a streak limit. The consecutive-loss pause is time-based
and does NOT reset on period rollovers.

Design philosophy:
- Only REALIZED losses count (not unrealized)
- Percentage-based limits scale with account size
- Auto-resets on period rollover (new day / new week / new month)
- Winning trades do NOT reduce accumulated loss -- only a period reset clears it
- Startup backfill reconstructs state from the database when persisted state
  is stale or missing
"""

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class DrawdownManager:
    """
    Tracks daily, weekly, and monthly realized P&L and enforces drawdown limits.

    Daily period  = calendar day (midnight ET).
    Weekly period = ISO week (Monday through Friday).
    Monthly period = calendar month.

    State persisted to database/drawdown_state.json so it survives restarts.
    """

    def __init__(
        self,
        account_size: float = 50000.0,
        config: Optional[Dict] = None,
        persistence_path: Optional[Path] = None,
        max_daily_loss_pct: float = 2.0,
        db_path: Optional[str] = None,
    ):
        self.account_size = account_size
        self.db_path = str(db_path) if db_path else None

        # Parse config with safe defaults (disabled if missing)
        config = config or {}
        weekly_cfg = config.get('weekly', {})
        monthly_cfg = config.get('monthly', {})

        self.weekly_enabled = weekly_cfg.get('enabled', False)
        self.weekly_max_loss_pct = weekly_cfg.get('max_loss_pct', 4.0)

        self.monthly_enabled = monthly_cfg.get('enabled', False)
        self.monthly_max_loss_pct = monthly_cfg.get('max_loss_pct', 8.0)

        self.daily_max_loss_pct = max_daily_loss_pct

        # Consecutive loss pause config
        consec_cfg = config.get('consecutive_losses', {})
        self.consec_enabled = consec_cfg.get('enabled', False)
        self.max_consecutive_losses = consec_cfg.get('max_consecutive', 5)
        self.pause_hours = consec_cfg.get('pause_hours', 24)

        # Calculate dollar limits
        self._recalculate_limits()

        # State -- daily
        self.daily_realized_pnl: float = 0.0
        self.daily_breaker_triggered: bool = False

        # State -- weekly / monthly
        self.weekly_realized_pnl: float = 0.0
        self.monthly_realized_pnl: float = 0.0
        self.weekly_breaker_triggered: bool = False
        self.monthly_breaker_triggered: bool = False

        # Consecutive loss state
        self.consecutive_losses: int = 0
        self.consec_pause_until: Optional[datetime] = None

        # Period tracking
        today = date.today()
        self.current_date: str = str(today)
        self.current_iso_year, self.current_iso_week, _ = today.isocalendar()
        self.current_month = today.month
        self.current_month_year = today.year

        # Persistence
        if persistence_path:
            self.persistence_path = persistence_path
        else:
            from src.utils.app_paths import DATA_DIR
            self.persistence_path = DATA_DIR / 'database' / 'drawdown_state.json'

        self._load_state()

        # Startup backfill from database when state is stale or missing
        if self._needs_backfill() and self.db_path:
            self.check_and_apply_resets()
            self._backfill_from_db()

        if self.weekly_enabled or self.monthly_enabled or self.consec_enabled:
            logger.info(
                f"DrawdownManager initialized: "
                f"daily={self.daily_max_loss_pct}% (${self.daily_max_loss:.0f}), "
                f"weekly={'ON' if self.weekly_enabled else 'OFF'} "
                f"({self.weekly_max_loss_pct}% / ${self.weekly_max_loss:.0f}), "
                f"monthly={'ON' if self.monthly_enabled else 'OFF'} "
                f"({self.monthly_max_loss_pct}% / ${self.monthly_max_loss:.0f}), "
                f"consec_losses={'ON' if self.consec_enabled else 'OFF'} "
                f"(max={self.max_consecutive_losses}, pause={self.pause_hours}h)"
            )

    def _recalculate_limits(self):
        """Recalculate dollar limits from percentages and account size."""
        self.daily_max_loss = self.account_size * (self.daily_max_loss_pct / 100)
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
        self.daily_realized_pnl += pnl
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

        # Check daily breaker
        if (
            not self.daily_breaker_triggered
            and self.daily_realized_pnl <= -self.daily_max_loss
        ):
            self.daily_breaker_triggered = True
            logger.warning(
                f"DAILY DRAWDOWN BREAKER TRIGGERED: "
                f"Realized loss ${abs(self.daily_realized_pnl):.2f} "
                f"exceeds {self.daily_max_loss_pct}% limit (${self.daily_max_loss:.0f})"
            )

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
        Check whether trading is allowed under all drawdown limits.

        Lazy reset: calls check_and_apply_resets() first so period resets
        fire even if the bot has been idle.

        Check order: resets > consecutive loss pause > daily > weekly > monthly.

        Returns:
            (allowed: bool, reason: str)
        """
        # Lazy reset before checking breakers
        self.check_and_apply_resets()

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

        # Daily breaker
        if self.daily_breaker_triggered:
            return False, (
                f"Daily loss limit reached: "
                f"${abs(self.daily_realized_pnl):.2f} loss "
                f"exceeds {self.daily_max_loss_pct}% (${self.daily_max_loss:.0f})"
            )

        # Weekly breaker
        if self.weekly_enabled and self.weekly_breaker_triggered:
            return False, (
                f"Weekly loss limit reached: "
                f"${abs(self.weekly_realized_pnl):.2f} loss "
                f"exceeds {self.weekly_max_loss_pct}% (${self.weekly_max_loss:.0f})"
            )

        # Monthly breaker
        if self.monthly_enabled and self.monthly_breaker_triggered:
            return False, (
                f"Monthly loss limit reached: "
                f"${abs(self.monthly_realized_pnl):.2f} loss "
                f"exceeds {self.monthly_max_loss_pct}% (${self.monthly_max_loss:.0f})"
            )

        return True, "Drawdown limits OK"

    def check_and_apply_resets(self, now=None):
        """
        Detect new day, new week, or new month and reset the appropriate
        counters.  Called lazily from can_trade() and proactively from
        PortfolioManager.reset_daily() at market open.

        Args:
            now: datetime or date to use as "current time".
                 If None, uses datetime.now(ET).
        """
        if now is None:
            import pytz
            now = datetime.now(pytz.timezone('America/New_York'))

        if isinstance(now, datetime):
            today = now.date()
        else:
            today = now

        today_str = str(today)
        any_reset = False

        # Daily reset
        if today_str != self.current_date:
            logger.info(
                f"Daily rollover: {self.current_date} -> {today_str}. "
                f"Final daily P&L: ${self.daily_realized_pnl:.2f}"
            )
            self.daily_realized_pnl = 0.0
            self.daily_breaker_triggered = False
            self.current_date = today_str
            any_reset = True

        # Weekly reset
        iso_year, iso_week, _ = today.isocalendar()
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
            any_reset = True

        # Monthly reset
        if today.year != self.current_month_year or today.month != self.current_month:
            logger.info(
                f"Monthly rollover: {self.current_month}/{self.current_month_year} -> "
                f"{today.month}/{today.year}. "
                f"Final monthly P&L: ${self.monthly_realized_pnl:.2f}"
            )
            self.monthly_realized_pnl = 0.0
            self.monthly_breaker_triggered = False
            self.current_month = today.month
            self.current_month_year = today.year
            any_reset = True

        if any_reset:
            self._save_state()

    def check_period_rollovers(self):
        """Legacy wrapper: uses date.today() for backward compatibility.

        Called from older code paths and tests that mock date.today().
        """
        self.check_and_apply_resets(now=date.today())

    def update_account_size(self, new_size: float):
        """Update account size and recalculate dollar limits."""
        self.account_size = new_size
        self._recalculate_limits()
        logger.info(
            f"DrawdownManager account size updated to ${new_size:.2f}: "
            f"daily limit=${self.daily_max_loss:.0f}, "
            f"weekly limit=${self.weekly_max_loss:.0f}, "
            f"monthly limit=${self.monthly_max_loss:.0f}"
        )

    def reload_config(self):
        """Reload config from strategy_params.yaml and recalculate limits."""
        try:
            from config.settings import load_strategy_params
            params = load_strategy_params()
            portfolio_cfg = params.get('portfolio', {})
            dd_cfg = portfolio_cfg.get('drawdown_limits', {})

            weekly_cfg = dd_cfg.get('weekly', {})
            monthly_cfg = dd_cfg.get('monthly', {})

            self.weekly_enabled = weekly_cfg.get('enabled', False)
            self.weekly_max_loss_pct = weekly_cfg.get('max_loss_pct', 4.0)
            self.monthly_enabled = monthly_cfg.get('enabled', False)
            self.monthly_max_loss_pct = monthly_cfg.get('max_loss_pct', 8.0)
            self.daily_max_loss_pct = portfolio_cfg.get('max_daily_loss_pct', 2.0)

            self._recalculate_limits()
            logger.info(
                f"DrawdownManager config reloaded: "
                f"daily={self.daily_max_loss_pct}%/${self.daily_max_loss:.0f}, "
                f"weekly={self.weekly_max_loss_pct}%/${self.weekly_max_loss:.0f}, "
                f"monthly={self.monthly_max_loss_pct}%/${self.monthly_max_loss:.0f}"
            )
        except Exception as e:
            logger.error(f"Failed to reload drawdown config: {e}")

    def get_status(self) -> dict:
        """Return status dict for dashboard/API."""
        consec_paused = (
            self.consec_enabled
            and self.consec_pause_until is not None
            and datetime.now() < self.consec_pause_until
        )
        return {
            'daily': {
                'realized_pnl': round(self.daily_realized_pnl, 2),
                'max_loss_pct': self.daily_max_loss_pct,
                'max_loss_dollars': round(self.daily_max_loss, 2),
                'breaker_triggered': self.daily_breaker_triggered,
            },
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
            today = date.today()
            today_str = str(today)
            iso_year, iso_week, _ = today.isocalendar()

            stored_date = data.get('current_date')
            stored_iso_week = data.get('current_iso_week')
            stored_iso_year = data.get('current_iso_year')
            stored_month = data.get('current_month')
            stored_month_year = data.get('current_month_year')

            # Daily (only if date matches)
            if stored_date == today_str:
                self.daily_realized_pnl = data.get('daily_realized_pnl', 0.0)
                self.daily_breaker_triggered = data.get('daily_breaker_triggered', False)
                self.current_date = stored_date
            else:
                if stored_date is not None:
                    logger.info(
                        f"Drawdown: Daily state stale "
                        f"(saved {stored_date}, now {today_str}), resetting"
                    )

            # Weekly (only if period matches)
            if stored_iso_year == iso_year and stored_iso_week == iso_week:
                self.weekly_realized_pnl = data.get('weekly_realized_pnl', 0.0)
                self.weekly_breaker_triggered = data.get('weekly_breaker_triggered', False)
            else:
                logger.info(
                    f"Drawdown: Weekly state stale "
                    f"(saved W{stored_iso_week}/{stored_iso_year}, "
                    f"now W{iso_week}/{iso_year}), resetting"
                )

            # Monthly (only if period matches)
            if stored_month_year == today.year and stored_month == today.month:
                self.monthly_realized_pnl = data.get('monthly_realized_pnl', 0.0)
                self.monthly_breaker_triggered = data.get('monthly_breaker_triggered', False)
            else:
                logger.info(
                    f"Drawdown: Monthly state stale "
                    f"(saved {stored_month}/{stored_month_year}, "
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
                'current_date': self.current_date,
                'daily_realized_pnl': self.daily_realized_pnl,
                'daily_breaker_triggered': self.daily_breaker_triggered,
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

    def _needs_backfill(self) -> bool:
        """Decide whether startup backfill from the database is needed.

        Triggers backfill when:
        1. Any period tracker is stale (date/week/month doesn't match today).
        2. Weekly or monthly realized_pnl is 0.0 -- catches the case where the
           state file was freshly created (or reset) mid-period after losses
           had already occurred.  Safe because _backfill_from_db() queries the
           DB for actual losses; if there are none, values stay at 0.0.
        """
        today = date.today()
        if today.month != self.current_month or today.year != self.current_month_year:
            return True
        iso_year, iso_week, _ = today.isocalendar()
        if iso_week != self.current_iso_week or iso_year != self.current_iso_year:
            return True
        if str(today) != self.current_date:
            return True
        # Also backfill if stored pnl is zero but DB may have losses this period
        if self.monthly_realized_pnl == 0.0 or self.weekly_realized_pnl == 0.0:
            return True
        return False

    # =========================================================================
    # STARTUP BACKFILL
    # =========================================================================

    def _backfill_from_db(self):
        """Reconstruct period P&L from the database after a stale or missing state file."""
        if not self.db_path:
            return
        try:
            db_file = Path(self.db_path)
            if not db_file.exists():
                logger.info("Backfill skipped: database file does not exist")
                return

            conn = sqlite3.connect(str(db_file), timeout=10)
            today = date.today()

            # Daily losses (since midnight today)
            daily_sum = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) FROM trades "
                "WHERE LOWER(status) = 'closed' AND pnl < 0 "
                "AND exit_time >= ?",
                (str(today),),
            ).fetchone()[0]
            self.daily_realized_pnl = daily_sum

            # Weekly losses (since Monday of current ISO week)
            iso_year, iso_week, _ = today.isocalendar()
            monday = date.fromisocalendar(iso_year, iso_week, 1)
            weekly_sum = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) FROM trades "
                "WHERE LOWER(status) = 'closed' AND pnl < 0 "
                "AND exit_time >= ?",
                (str(monday),),
            ).fetchone()[0]
            self.weekly_realized_pnl = weekly_sum

            # Monthly losses (since 1st of current month)
            first_of_month = date(today.year, today.month, 1)
            monthly_sum = conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) FROM trades "
                "WHERE LOWER(status) = 'closed' AND pnl < 0 "
                "AND exit_time >= ?",
                (str(first_of_month),),
            ).fetchone()[0]
            self.monthly_realized_pnl = monthly_sum

            conn.close()

            # Re-check breakers after backfill
            if self.daily_realized_pnl <= -self.daily_max_loss:
                self.daily_breaker_triggered = True
            if self.weekly_enabled and self.weekly_realized_pnl <= -self.weekly_max_loss:
                self.weekly_breaker_triggered = True
            if self.monthly_enabled and self.monthly_realized_pnl <= -self.monthly_max_loss:
                self.monthly_breaker_triggered = True

            self._save_state()
            logger.info(
                f"Backfill complete: "
                f"daily=${self.daily_realized_pnl:.2f}, "
                f"weekly=${self.weekly_realized_pnl:.2f}, "
                f"monthly=${self.monthly_realized_pnl:.2f}"
            )
        except Exception as e:
            logger.error(f"Failed to backfill from database: {e}")
