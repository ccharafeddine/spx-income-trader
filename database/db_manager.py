import sqlite3
import time as _time
from datetime import datetime, date
from typing import List, Optional, Dict
import json
import logging
from pathlib import Path
import pytz

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Manage SQLite database for trade history and analytics"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path

        # Create database directory if it doesn't exist
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        # Initialize database
        self._init_db()

        logger.info(f"Database initialized at {db_path}")

    @classmethod
    def ensure_schema(cls, db_path: str):
        """Create the database file and initialize schema if it doesn't exist."""
        cls(db_path)
    
    def _get_connection(self):
        """Get database connection with WAL mode and busy timeout for concurrent access."""
        conn = sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn
    
    def _init_db(self):
        """Initialize database schema"""
        schema_file = Path(__file__).parent / 'schema.sql'

        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Read and execute schema
            if schema_file.exists():
                with open(schema_file, 'r') as f:
                    cursor.executescript(f.read())
            else:
                # Fallback: create basic schema
                cursor.executescript("""
                    CREATE TABLE IF NOT EXISTS trades (
                        id TEXT PRIMARY KEY,
                        entry_time TIMESTAMP NOT NULL,
                        exit_time TIMESTAMP,
                        direction TEXT NOT NULL,
                        status TEXT NOT NULL,
                        short_strike REAL NOT NULL,
                        long_strike REAL NOT NULL,
                        credit_received REAL NOT NULL,
                        entry_price REAL NOT NULL,
                        exit_price REAL,
                        pnl REAL,
                        quantity INTEGER NOT NULL,
                        notes TEXT
                    );
                """)

            conn.commit()

        # Run migrations for new columns
        self._run_migrations()

    def _run_migrations(self):
        """Run migrations to add new columns if they don't exist"""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Check existing columns
            cursor.execute("PRAGMA table_info(trades)")
            existing_columns = {row[1] for row in cursor.fetchall()}

            # New columns to add — must match schema.sql exactly so the
            # migration-only path (packaged app without schema.sql) produces
            # an identical table to a fresh schema.sql creation.
            new_columns = [
                ('strategy_type', "TEXT DEFAULT 'daily_income'"),
                # Spread details
                ('spread_width', 'REAL'),
                # Entry details
                ('entry_order_id', 'TEXT'),
                ('underlying_price_at_entry', 'REAL'),
                ('setup_bar_time', 'TIMESTAMP'),
                # Entry context
                ('spx_at_entry', 'REAL'),
                ('vix_at_entry', 'REAL'),
                ('vix_regime', 'TEXT'),
                ('day_open', 'REAL'),
                ('gap_pct', 'REAL'),
                ('intraday_move_at_entry', 'REAL'),
                # Slippage tracking
                ('theoretical_credit', 'REAL'),
                ('actual_credit', 'REAL'),
                ('slippage', 'REAL'),
                ('slippage_pct', 'REAL'),
                # Economic calendar context
                ('economic_events', 'TEXT'),
                # Enhanced trade metadata
                ('day_of_week', 'INTEGER'),
                ('day_of_week_name', 'TEXT'),
                ('entry_time_bucket', 'TEXT'),
                ('sma50', 'REAL'),
                ('sma200', 'REAL'),
                ('spx_vs_sma50', 'REAL'),
                ('spx_vs_sma50_pct', 'REAL'),
                ('spx_vs_sma200', 'REAL'),
                ('spx_vs_sma200_pct', 'REAL'),
                ('prior_day_high', 'REAL'),
                ('prior_day_low', 'REAL'),
                ('spx_vs_prior_range', 'TEXT'),
                # Exit details
                ('exit_order_id', 'TEXT'),
                ('exit_reason', 'TEXT'),
                ('spx_at_exit', 'REAL'),
                ('profit_captured_pct', 'REAL'),
                ('time_in_trade_minutes', 'INTEGER'),
                # Market conditions
                ('day_type', 'TEXT'),
                ('daily_move_pct', 'REAL'),
                # P&L
                ('pnl_percent', 'REAL'),
                ('max_profit', 'REAL'),
                ('max_risk', 'REAL'),
                # Position details
                ('expiration', 'TIMESTAMP'),
                # Analytics
                ('vix_at_exit', 'REAL'),
                ('bb_agreement', 'INTEGER'),
                # Commissions/fees
                ('commissions', 'REAL DEFAULT 0.0'),
            ]

            for col_name, col_type in new_columns:
                if col_name not in existing_columns:
                    try:
                        cursor.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}")
                        logger.info(f"Added column {col_name} to trades table")
                    except Exception as e:
                        logger.debug(f"Column {col_name} may already exist: {e}")

            # Create supporting tables if not exist
            cursor.executescript("""
                CREATE TABLE IF NOT EXISTS system_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    event_type TEXT NOT NULL,
                    message TEXT,
                    details TEXT
                );
                CREATE TABLE IF NOT EXISTS daily_journal (
                    date DATE PRIMARY KEY,
                    bars_built INTEGER DEFAULT 0,
                    pulse_bars_found INTEGER DEFAULT 0,
                    signals_evaluated INTEGER DEFAULT 0,
                    trades_entered INTEGER DEFAULT 0,
                    spx_open REAL,
                    spx_close REAL,
                    spx_change_pct REAL,
                    vix_level REAL,
                    vix_regime TEXT,
                    rejection_reasons TEXT,
                    market_context TEXT,
                    no_trade_summary TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS daily_stats (
                    date TEXT PRIMARY KEY,
                    bars_built INTEGER DEFAULT 0,
                    pulse_bars INTEGER DEFAULT 0,
                    signals INTEGER DEFAULT 0,
                    trades_count INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    pnl REAL DEFAULT 0.0
                );
                CREATE TABLE IF NOT EXISTS journal_notes (
                    trade_id TEXT PRIMARY KEY,
                    rating INTEGER DEFAULT NULL,
                    notes TEXT DEFAULT '',
                    tags TEXT DEFAULT '[]',
                    flagged INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Create indexes if not exist
            try:
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_strategy_type ON trades(strategy_type)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_day_type ON trades(day_type)")
            except Exception:
                pass

            conn.commit()
    
    def save_trade(self, trade, context: Optional[Dict] = None):
        """Save or update trade in database with optional context"""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Get context values or None
            ctx = context or {}

            cursor.execute(f"""
                INSERT OR REPLACE INTO trades (
                    id, entry_time, exit_time, direction, status,
                    strategy_type,
                    short_strike, long_strike, spread_width, credit_received,
                    entry_price, entry_order_id, underlying_price_at_entry,
                    spx_at_entry, vix_at_entry, vix_regime,
                    day_open, gap_pct, intraday_move_at_entry,
                    theoretical_credit, actual_credit, slippage, slippage_pct,
                    economic_events,
                    day_of_week, day_of_week_name, entry_time_bucket,
                    sma50, sma200,
                    spx_vs_sma50, spx_vs_sma50_pct,
                    spx_vs_sma200, spx_vs_sma200_pct,
                    prior_day_high, prior_day_low, spx_vs_prior_range,
                    exit_price, exit_order_id, exit_reason,
                    spx_at_exit, profit_captured_pct, time_in_trade_minutes,
                    day_type, daily_move_pct,
                    pnl, pnl_percent, max_profit, max_risk,
                    quantity, expiration, notes, setup_bar_time,
                    bb_agreement
                ) VALUES ({','.join(['?'] * 53)})
            """, (
                trade.id,
                trade.entry_time,
                trade.exit_time,
                trade.spread.direction.value,
                trade.status.value,
                ctx.get('strategy_type', 'daily_income'),
                trade.spread.short_leg.strike,
                trade.spread.long_leg.strike,
                trade.spread.spread_width,
                trade.spread.credit_received,
                trade.entry_price,
                trade.entry_order_id,
                trade.spread.underlying_price_at_entry,
                ctx.get('spx_at_entry'),
                ctx.get('vix_at_entry'),
                ctx.get('vix_regime'),
                ctx.get('day_open'),
                ctx.get('gap_pct'),
                ctx.get('intraday_move_at_entry'),
                ctx.get('theoretical_credit'),
                ctx.get('actual_credit'),
                ctx.get('slippage'),
                ctx.get('slippage_pct'),
                ctx.get('economic_events'),
                ctx.get('day_of_week'),
                ctx.get('day_of_week_name'),
                ctx.get('entry_time_bucket'),
                ctx.get('sma50'),
                ctx.get('sma200'),
                ctx.get('spx_vs_sma50'),
                ctx.get('spx_vs_sma50_pct'),
                ctx.get('spx_vs_sma200'),
                ctx.get('spx_vs_sma200_pct'),
                ctx.get('prior_day_high'),
                ctx.get('prior_day_low'),
                ctx.get('spx_vs_prior_range'),
                trade.exit_price,
                trade.exit_order_id,
                trade.exit_reason,
                ctx.get('spx_at_exit'),
                ctx.get('profit_captured_pct'),
                ctx.get('time_in_trade_minutes'),
                ctx.get('day_type'),
                ctx.get('daily_move_pct'),
                trade.pnl,
                trade.pnl_percent,
                trade.spread.max_profit,
                trade.spread.max_risk,
                trade.quantity,
                trade.spread.expiration,
                trade.notes,
                trade.setup_bar.timestamp if hasattr(trade, 'setup_bar') and trade.setup_bar else None,
                1 if getattr(trade, 'bb_agreement', None) is True else (0 if getattr(trade, 'bb_agreement', None) is False else None),
            ))

            conn.commit()
            logger.debug(f"Trade {trade.id} saved to database")

    def save_trade_with_retry(self, trade, context: Optional[Dict] = None,
                              max_attempts: int = 3,
                              delays: tuple = (0.1, 0.5, 1.0)):
        """Save trade with exponential backoff retries for lock contention.

        This wraps save_trade() with retries so that transient 'database is
        locked' errors from concurrent dashboard reads don't orphan live
        trades.  Raises the last exception if all attempts fail.
        """
        last_err = None
        for attempt in range(max_attempts):
            try:
                self.save_trade(trade, context=context)
                if attempt > 0:
                    logger.info(
                        f"save_trade succeeded on attempt {attempt + 1} "
                        f"for trade {trade.id}"
                    )
                return  # success
            except Exception as e:
                last_err = e
                if attempt < max_attempts - 1:
                    delay = delays[attempt] if attempt < len(delays) else delays[-1]
                    logger.warning(
                        f"save_trade attempt {attempt + 1}/{max_attempts} failed "
                        f"for trade {trade.id}: {e}. Retrying in {delay}s..."
                    )
                    _time.sleep(delay)
        raise last_err

    def update_trade_close(self, trade):
        """Update trade row on close/exit using UPDATE (not INSERT OR REPLACE).

        This preserves entry-context columns (strategy_type, spx_at_entry,
        vix_at_entry, vix_regime, SMA data, etc.) that were written at entry
        time.  Only the columns that genuinely change on exit are touched.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE trades SET
                    exit_time = ?,
                    status = ?,
                    exit_price = ?,
                    exit_order_id = ?,
                    exit_reason = ?,
                    pnl = ?,
                    pnl_percent = ?
                WHERE id = ?
            """, (
                trade.exit_time,
                trade.status.value,
                trade.exit_price,
                trade.exit_order_id,
                trade.exit_reason,
                trade.pnl,
                trade.pnl_percent,
                trade.id,
            ))
            conn.commit()
            logger.debug(f"Trade {trade.id} close fields updated (entry context preserved)")

    def update_trade_exit_context(self, trade_id: str, exit_context: Dict):
        """Update trade with exit context after closing"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE trades SET
                    spx_at_exit = ?,
                    profit_captured_pct = ?,
                    time_in_trade_minutes = ?,
                    day_type = ?,
                    daily_move_pct = ?,
                    vix_at_exit = ?
                WHERE id = ?
            """, (
                exit_context.get('spx_at_exit'),
                exit_context.get('profit_captured_pct'),
                exit_context.get('time_in_trade_minutes'),
                exit_context.get('day_type'),
                exit_context.get('daily_move_pct'),
                exit_context.get('vix_at_exit'),
                trade_id,
            ))
            conn.commit()
            logger.debug(f"Trade {trade_id} exit context updated")
    
    def update_trade_commissions(self, trade_id: str, commissions: float):
        """Update commissions/fees for a trade."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE trades SET commissions = ? WHERE id = ?",
                (round(commissions, 2), trade_id),
            )
            conn.commit()
            logger.debug(f"Trade {trade_id} commissions updated: ${commissions:.2f}")

    def update_trade_settlement_pnl(self, trade_id: str, pnl: float):
        """Update P&L for an expired trade after Schwab settlement reconciliation.

        Used when post-settlement transaction data reveals a discrepancy
        between the bot's calculated P&L and Schwab's actual settlement amount.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Get current credit_received for pnl_percent calculation
            row = cursor.execute(
                "SELECT credit_received, quantity FROM trades WHERE id = ?",
                (trade_id,)
            ).fetchone()
            pnl_percent = None
            if row:
                credit = row[0] or 0
                qty = row[1] or 1
                max_profit = credit * 100 * qty
                if max_profit > 0:
                    pnl_percent = round(pnl / max_profit * 100, 2)
            cursor.execute(
                "UPDATE trades SET pnl = ?, pnl_percent = ? WHERE id = ?",
                (round(pnl, 2), pnl_percent, trade_id),
            )
            conn.commit()
            logger.debug(
                f"Trade {trade_id} settlement P&L updated: ${pnl:.2f}"
            )

    def get_open_trades(self) -> List[Dict]:
        """Get all open trades"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM trades 
                WHERE status IN ('pending', 'active')
                ORDER BY entry_time DESC
            """)
            
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
    
    def get_trades_by_date(self, trade_date: date) -> List[Dict]:
        """Get all trades for a specific date"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM trades 
                WHERE SUBSTR(entry_time, 1, 10) = ?
                ORDER BY entry_time
            """, (trade_date,))
            
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
    
    def get_daily_counts_by_strategy(self, trade_date: date) -> Dict[str, int]:
        """Get trade counts grouped by strategy_type for a given date.

        Only counts trades that were actually placed (active, closed, expired).
        Excludes pending (order never filled) and cancelled records so they
        don't inflate daily trade limits after a crash/restart.

        Returns:
            Dict mapping strategy name to trade count, e.g.
            {'daily_income': 1, 'orb': 0, 'tag_n_turn': 0, 'bnb': 0}
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT strategy_type, COUNT(*) as cnt
                FROM trades
                WHERE SUBSTR(entry_time, 1, 10) = ?
                  AND status IN ('active', 'closed', 'expired')
                GROUP BY strategy_type
            """, (trade_date,))
            return {row[0]: row[1] for row in cursor.fetchall()}

    def get_daily_summary(self, trade_date: date) -> Dict:
        """Get trade count and realized P&L for a given date.

        Used to restore in-memory counters after a mid-day restart.
        Only counts trades that were actually placed (active, closed, expired).
        Pending/cancelled records are excluded so they don't inflate counters.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    COUNT(CASE WHEN status IN ('active', 'closed', 'expired') THEN 1 END) as trades_count,
                    COALESCE(SUM(CASE WHEN status = 'closed' THEN pnl ELSE 0 END), 0.0) as realized_pnl,
                    COALESCE(SUM(CASE WHEN status = 'closed' AND pnl > 0 THEN 1 ELSE 0 END), 0) as wins,
                    COALESCE(SUM(CASE WHEN status = 'closed' AND pnl < 0 THEN 1 ELSE 0 END), 0) as losses
                FROM trades
                WHERE SUBSTR(entry_time, 1, 10) = ?
            """, (trade_date,))
            row = cursor.fetchone()
            return {'trades_count': row[0], 'realized_pnl': row[1], 'wins': row[2], 'losses': row[3]}

    def update_daily_stats(self, trade_date: date):
        """Update daily statistics"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Calculate stats for the day
            cursor.execute("""
                SELECT 
                    COUNT(*) as trades_count,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                    SUM(pnl) as total_pnl,
                    MAX(pnl) as largest_win,
                    MIN(pnl) as largest_loss
                FROM trades
                WHERE SUBSTR(entry_time, 1, 10) = ? AND status = 'closed'
            """, (trade_date,))
            
            stats = cursor.fetchone()
            
            cursor.execute("""
                INSERT OR REPLACE INTO daily_stats 
                (date, trades_count, wins, losses, total_pnl, largest_win, largest_loss)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (trade_date, *stats))
            
            conn.commit()
            logger.debug(f"Daily stats updated for {trade_date}")
    
    def log_event(self, event_type: str, message: str, details: Optional[Dict] = None):
        """Log system event"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO system_events (event_type, message, details)
                VALUES (?, ?, ?)
            """, (event_type, message, json.dumps(details) if details else None))
            
            conn.commit()
    
    def get_performance_summary(self, days: int = 30) -> Dict:
        """Get performance summary for last N days"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                    SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
                    SUM(pnl) as total_pnl,
                    AVG(pnl) as avg_pnl,
                    MAX(pnl) as max_win,
                    MIN(pnl) as max_loss,
                    AVG(CASE WHEN pnl > 0 THEN pnl END) as avg_win,
                    AVG(CASE WHEN pnl < 0 THEN pnl END) as avg_loss
                FROM trades
                WHERE status = 'closed'
                AND entry_time >= datetime('now', '-' || ? || ' days')
            """, (days,))
            
            row = cursor.fetchone()
            columns = [desc[0] for desc in cursor.description]
            
            return dict(zip(columns, row)) if row else {}

    def get_strategy_stats(self, strategy_type: str, days: int = 90) -> Dict:
        """
        Get performance statistics for a specific strategy.
        Used for performance analytics and drawdown tracking.

        Args:
            strategy_type: Strategy identifier (e.g., 'daily_income', 'tag_n_turn')
            days: Number of days to look back (default 90)

        Returns:
            Dict with win_rate, avg_win, avg_loss, total_trades
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    COUNT(*) as total_trades,
                    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                    AVG(CASE WHEN pnl > 0 THEN pnl END) as avg_win,
                    AVG(CASE WHEN pnl < 0 THEN pnl END) as avg_loss
                FROM trades
                WHERE status = 'closed'
                AND strategy_type = ?
                AND entry_time >= datetime('now', '-' || ? || ' days')
            """, (strategy_type, days))

            row = cursor.fetchone()
            if not row or row[0] == 0:
                return {
                    'total_trades': 0,
                    'win_rate': 0.5,
                    'avg_win': 0.0,
                    'avg_loss': 0.0,
                }

            total_trades = row[0] or 0
            winning_trades = row[1] or 0
            avg_win = row[2] or 0.0
            avg_loss = row[3] or 0.0

            win_rate = winning_trades / total_trades if total_trades > 0 else 0.5

            return {
                'total_trades': total_trades,
                'win_rate': win_rate,
                'avg_win': avg_win,
                'avg_loss': avg_loss,
            }

    def get_active_trades_raw(self) -> List[Dict]:
        """Get active trades using a plain connection (no detect_types).

        This avoids sqlite3's TIMESTAMP converter choking on timezone-aware
        datetime strings like '2026-02-03 00:00:00-05:00'.
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status IN ('pending', 'active') "
                "ORDER BY entry_time DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def close_orphaned_trade(
        self,
        trade_id: str,
        pnl: float,
        pnl_percent: float,
        exit_reason: str,
        exit_time: Optional[datetime] = None,
    ):
        """Close an orphaned trade directly in the DB (expired while bot was offline)."""
        if exit_time is None:
            exit_time = datetime.now(pytz.timezone("America/New_York"))
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE trades SET
                    status = 'closed',
                    exit_price = 0.0,
                    exit_time = ?,
                    exit_reason = ?,
                    pnl = ?,
                    pnl_percent = ?
                WHERE id = ?
            """, (exit_time, exit_reason, pnl, pnl_percent, trade_id))
            conn.commit()
            logger.info(f"Orphaned trade {trade_id} resolved: P&L=${pnl:+.2f}")

    def save_daily_journal(self, journal_date: date, data: Dict):
        """Save or update a daily journal entry."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO daily_journal (
                    date, bars_built, pulse_bars_found, signals_evaluated,
                    trades_entered, spx_open, spx_close, spx_change_pct,
                    vix_level, vix_regime, rejection_reasons, market_context,
                    no_trade_summary, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (
                journal_date,
                data.get('bars_built', 0),
                data.get('pulse_bars_found', 0),
                data.get('signals_evaluated', 0),
                data.get('trades_entered', 0),
                data.get('spx_open'),
                data.get('spx_close'),
                data.get('spx_change_pct'),
                data.get('vix_level'),
                data.get('vix_regime'),
                json.dumps(data.get('rejection_reasons', [])),
                json.dumps(data.get('market_context', {})),
                data.get('no_trade_summary', ''),
            ))
            conn.commit()
            logger.debug(f"Daily journal saved for {journal_date}")

    def get_daily_journal(self, journal_date: date) -> Optional[Dict]:
        """Get daily journal entry for a specific date."""
        with self._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM daily_journal WHERE date = ?",
                (journal_date,)
            ).fetchone()
            if row:
                d = dict(row)
                # Parse JSON fields
                try:
                    d['rejection_reasons'] = json.loads(d.get('rejection_reasons') or '[]')
                except (json.JSONDecodeError, TypeError):
                    d['rejection_reasons'] = []
                try:
                    d['market_context'] = json.loads(d.get('market_context') or '{}')
                except (json.JSONDecodeError, TypeError):
                    d['market_context'] = {}
                return d
            return None

    def get_daily_journals_for_month(self, year: int, month: int) -> Dict[str, Dict]:
        """Get all daily journal entries for a given month. Returns dict keyed by date string."""
        first_day = f"{year:04d}-{month:02d}-01"
        if month == 12:
            last_day = f"{year + 1:04d}-01-01"
        else:
            last_day = f"{year:04d}-{month + 1:02d}-01"

        result = {}
        with self._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM daily_journal WHERE date >= ? AND date < ?",
                (first_day, last_day)
            ).fetchall()
            for row in rows:
                d = dict(row)
                raw_date = d.get('date', '')
                # detect_types may return datetime.date; convert to string key
                date_str = str(raw_date) if not isinstance(raw_date, str) else raw_date
                try:
                    d['rejection_reasons'] = json.loads(d.get('rejection_reasons') or '[]')
                except (json.JSONDecodeError, TypeError):
                    d['rejection_reasons'] = []
                try:
                    d['market_context'] = json.loads(d.get('market_context') or '{}')
                except (json.JSONDecodeError, TypeError):
                    d['market_context'] = {}
                result[date_str] = d
        return result