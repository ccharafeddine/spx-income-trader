"""
Pattern Day Trader (PDT) Rule Protection System

Tracks day trades over a rolling 5-business-day window to prevent PDT rule violations
for accounts under $25,000.

A "day trade" is defined as a position that was BOTH opened and actively closed by
the bot on the same calendar day. Positions that expire at end of day (0DTE options
expiring worthless or at max profit without an explicit close order) do NOT count.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional
import sqlite3

logger = logging.getLogger(__name__)


# NYSE market holidays for 2024-2026 (approximate - should be updated annually)
NYSE_HOLIDAYS = {
    # 2024
    date(2024, 1, 1),   # New Year's Day
    date(2024, 1, 15),  # MLK Day
    date(2024, 2, 19),  # Presidents Day
    date(2024, 3, 29),  # Good Friday
    date(2024, 5, 27),  # Memorial Day
    date(2024, 6, 19),  # Juneteenth
    date(2024, 7, 4),   # Independence Day
    date(2024, 9, 2),   # Labor Day
    date(2024, 11, 28), # Thanksgiving
    date(2024, 12, 25), # Christmas
    # 2025
    date(2025, 1, 1),   # New Year's Day
    date(2025, 1, 20),  # MLK Day
    date(2025, 2, 17),  # Presidents Day
    date(2025, 4, 18),  # Good Friday
    date(2025, 5, 26),  # Memorial Day
    date(2025, 6, 19),  # Juneteenth
    date(2025, 7, 4),   # Independence Day
    date(2025, 9, 1),   # Labor Day
    date(2025, 11, 27), # Thanksgiving
    date(2025, 12, 25), # Christmas
    # 2026
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Presidents Day
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3),   # Independence Day (observed)
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 26), # Thanksgiving
    date(2026, 12, 25), # Christmas
}

# Exit reasons that count as "active close" (day trade)
ACTIVE_CLOSE_REASONS = [
    'profit_target',
    'profit target',
    '80% profit target',
    'early exit',
    'manual close',
    'stop loss',
    '1pm check',
    '1pm trend check',
    '1PM CHECK',
    'unfavorable position',
]


def is_business_day(d: date) -> bool:
    """Check if a date is a business day (weekday and not NYSE holiday)."""
    if d.weekday() >= 5:  # Saturday = 5, Sunday = 6
        return False
    if d in NYSE_HOLIDAYS:
        return False
    return True


def get_business_days_ago(n: int, from_date: Optional[date] = None) -> date:
    """Get the date that was N business days ago from from_date (or today)."""
    if from_date is None:
        from_date = date.today()

    count = 0
    current = from_date
    while count < n:
        current -= timedelta(days=1)
        if is_business_day(current):
            count += 1
    return current


def get_rolling_window_start(window_days: int = 5, from_date: Optional[date] = None) -> date:
    """Get the start date for the rolling window (N business days ago)."""
    return get_business_days_ago(window_days - 1, from_date)


class PDTTracker:
    """
    Tracks day trades for Pattern Day Trader rule compliance.

    PDT rule: If you execute 4+ day trades in 5 business days with an account
    under $25,000, your account may be restricted.

    This tracker limits day trades to 3 per 5-day rolling window when
    account equity is below the threshold.
    """

    def __init__(
        self,
        db_path: str,
        enabled: bool = True,
        threshold: float = 25000.0,
        max_day_trades: int = 3,
        window_days: int = 5,
        get_account_equity: Optional[callable] = None,
    ):
        """
        Initialize PDT tracker.

        Args:
            db_path: Path to SQLite database
            enabled: Master switch for PDT protection
            threshold: Account equity threshold (default $25,000)
            max_day_trades: Max day trades allowed in window (default 3)
            window_days: Rolling window in business days (default 5)
            get_account_equity: Callback to get current account equity
        """
        self.db_path = db_path
        self.enabled = enabled
        self.threshold = threshold
        self.max_day_trades = max_day_trades
        self.window_days = window_days
        self._get_account_equity = get_account_equity

        # Cache account equity check (refreshed daily)
        self._cached_equity: Optional[float] = None
        self._cached_equity_date: Optional[date] = None

        # Initialize database table
        self._init_db()

        logger.info(
            f"PDTTracker initialized: enabled={enabled}, "
            f"threshold=${threshold:,.0f}, max_trades={max_day_trades}, "
            f"window={window_days} business days"
        )

    def _get_connection(self):
        """Get database connection."""
        return sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES)

    def _init_db(self):
        """Initialize PDT tracking table."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pdt_day_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT NOT NULL,
                    trade_date DATE NOT NULL,
                    exit_reason TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(trade_id)
                )
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS idx_pdt_trade_date
                ON pdt_day_trades(trade_date)
            """)
            conn.commit()

    def _get_current_equity(self) -> float:
        """Get current account equity, with daily caching."""
        today = date.today()

        # Return cached value if checked today
        if self._cached_equity_date == today and self._cached_equity is not None:
            return self._cached_equity

        # Get fresh equity value
        if self._get_account_equity:
            try:
                self._cached_equity = self._get_account_equity()
                self._cached_equity_date = today
                logger.debug(f"PDT: Account equity refreshed: ${self._cached_equity:,.2f}")
                return self._cached_equity
            except Exception as e:
                logger.warning(f"PDT: Failed to get account equity: {e}")

        # Default to under threshold (conservative)
        return 0.0

    def is_pdt_restricted(self) -> bool:
        """
        Check if PDT restrictions apply.

        Returns True if:
        - PDT protection is enabled
        - Account equity is below threshold
        """
        if not self.enabled:
            return False

        equity = self._get_current_equity()
        return equity < self.threshold

    def record_day_trade(self, trade_id: str, trade_date: date, exit_reason: str) -> bool:
        """
        Record a day trade (same-day open and ACTIVE close).

        Only call this for trades that were actively closed (profit target,
        stop loss, 1pm check, etc.), NOT for trades that expired.

        Args:
            trade_id: Unique trade identifier
            trade_date: Date the day trade occurred
            exit_reason: Why the trade was closed

        Returns:
            True if recorded, False if already exists
        """
        # Check if exit reason qualifies as active close
        is_active_close = any(
            reason.lower() in exit_reason.lower()
            for reason in ACTIVE_CLOSE_REASONS
        )

        if not is_active_close:
            logger.debug(
                f"PDT: Trade {trade_id[:8]} exit '{exit_reason}' "
                f"not counted as day trade (expiration/not active close)"
            )
            return False

        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT OR IGNORE INTO pdt_day_trades (trade_id, trade_date, exit_reason)
                    VALUES (?, ?, ?)
                """, (trade_id, trade_date, exit_reason))
                conn.commit()

                if cursor.rowcount > 0:
                    logger.info(
                        f"PDT: Day trade recorded - {trade_id[:8]} on {trade_date} "
                        f"(reason: {exit_reason})"
                    )
                    return True
                return False

        except Exception as e:
            logger.error(f"PDT: Failed to record day trade: {e}")
            return False

    def get_day_trades_in_window(self, from_date: Optional[date] = None) -> List[Dict]:
        """
        Get all day trades in the rolling window.

        Args:
            from_date: End date of window (default: today)

        Returns:
            List of day trade records
        """
        if from_date is None:
            from_date = date.today()

        window_start = get_rolling_window_start(self.window_days, from_date)

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, trade_id, trade_date, exit_reason, created_at
                FROM pdt_day_trades
                WHERE trade_date >= ? AND trade_date <= ?
                ORDER BY trade_date DESC
            """, (window_start, from_date))

            columns = ['id', 'trade_id', 'trade_date', 'exit_reason', 'created_at']
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def get_day_trades_count(self, from_date: Optional[date] = None) -> int:
        """Get count of day trades in the rolling window."""
        return len(self.get_day_trades_in_window(from_date))

    def get_remaining_day_trades(self, from_date: Optional[date] = None) -> int:
        """
        Get how many day trades are remaining in the rolling window.

        Returns:
            Number of day trades remaining (0 if at limit)
        """
        used = self.get_day_trades_count(from_date)
        remaining = max(0, self.max_day_trades - used)
        return remaining

    def can_close_early(self) -> bool:
        """
        Check if an early close is allowed under PDT rules.

        Returns True if:
        - PDT protection is disabled
        - Account equity is at or above threshold
        - Day trade slots are available
        """
        if not self.enabled:
            logger.debug("PDT: Protection disabled, early close allowed")
            return True

        equity = self._get_current_equity()
        if equity >= self.threshold:
            logger.debug(
                f"PDT: Account ${equity:,.2f} >= ${self.threshold:,.0f}, "
                f"early close allowed (unlimited day trades)"
            )
            return True

        remaining = self.get_remaining_day_trades()
        if remaining > 0:
            logger.debug(f"PDT: {remaining} day trade slot(s) available, early close allowed")
            return True

        logger.info(
            f"PDT: No day trade slots available ({self.max_day_trades}/{self.max_day_trades} used). "
            f"Early close blocked."
        )
        return False

    def get_next_slot_frees_on(self, from_date: Optional[date] = None) -> Optional[date]:
        """
        Get the date when the next day trade slot will free up.

        Returns:
            Date when oldest day trade exits the rolling window, or None if slots available
        """
        if self.get_remaining_day_trades(from_date) > 0:
            return None

        trades = self.get_day_trades_in_window(from_date)
        if not trades:
            return None

        # Oldest trade in window
        oldest_trade = min(trades, key=lambda t: t['trade_date'])
        oldest_date = oldest_trade['trade_date']

        # It exits the window N business days after it occurred
        # Add window_days business days to find when it drops off
        count = 0
        check_date = oldest_date
        while count < self.window_days:
            check_date += timedelta(days=1)
            if is_business_day(check_date):
                count += 1

        return check_date

    def get_pdt_status(self) -> Dict:
        """
        Get comprehensive PDT status for dashboard display.

        Returns:
            Dict with PDT status information
        """
        equity = self._get_current_equity()
        is_restricted = self.is_pdt_restricted()
        used = self.get_day_trades_count()
        remaining = self.get_remaining_day_trades()
        next_frees = self.get_next_slot_frees_on()

        return {
            'enabled': self.enabled,
            'account_value': equity,
            'threshold': self.threshold,
            'is_restricted': is_restricted,
            'day_trades_used': used,
            'day_trades_remaining': remaining,
            'max_day_trades': self.max_day_trades,
            'window_days': self.window_days,
            'next_slot_frees_on': next_frees.isoformat() if next_frees else None,
            'can_close_early': self.can_close_early(),
        }

    def check_and_record_day_trade(
        self,
        trade_id: str,
        entry_date: date,
        exit_date: date,
        exit_reason: str,
    ) -> bool:
        """
        Check if a trade qualifies as a day trade and record it if so.

        A day trade is when entry and exit occur on the same calendar day
        AND the exit was an active close (not expiration).

        Args:
            trade_id: Trade identifier
            entry_date: Date trade was opened
            exit_date: Date trade was closed
            exit_reason: Why trade was closed

        Returns:
            True if recorded as day trade, False otherwise
        """
        # Must be same calendar day
        if entry_date != exit_date:
            logger.debug(
                f"PDT: Trade {trade_id[:8]} not a day trade "
                f"(entry={entry_date}, exit={exit_date})"
            )
            return False

        return self.record_day_trade(trade_id, entry_date, exit_reason)

    def refresh_account_equity(self):
        """Force refresh of account equity cache."""
        self._cached_equity = None
        self._cached_equity_date = None
        if self._get_account_equity:
            self._get_current_equity()
