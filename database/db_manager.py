import sqlite3
from datetime import datetime, date
from typing import List, Optional, Dict
import json
import logging
from pathlib import Path

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
    
    def _get_connection(self):
        """Get database connection"""
        return sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    
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

            # New columns to add
            new_columns = [
                ('strategy_type', "TEXT DEFAULT 'daily_income'"),
                ('spx_at_entry', 'REAL'),
                ('vix_at_entry', 'REAL'),
                ('day_open', 'REAL'),
                ('gap_pct', 'REAL'),
                ('intraday_move_at_entry', 'REAL'),
                ('spx_at_exit', 'REAL'),
                ('profit_captured_pct', 'REAL'),
                ('time_in_trade_minutes', 'INTEGER'),
                ('day_type', 'TEXT'),
                ('daily_move_pct', 'REAL'),
            ]

            for col_name, col_type in new_columns:
                if col_name not in existing_columns:
                    try:
                        cursor.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}")
                        logger.info(f"Added column {col_name} to trades table")
                    except Exception as e:
                        logger.debug(f"Column {col_name} may already exist: {e}")

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

            cursor.execute("""
                INSERT OR REPLACE INTO trades (
                    id, entry_time, exit_time, direction, status,
                    strategy_type,
                    short_strike, long_strike, spread_width, credit_received,
                    entry_price, entry_order_id, underlying_price_at_entry,
                    spx_at_entry, vix_at_entry, day_open, gap_pct, intraday_move_at_entry,
                    exit_price, exit_order_id, exit_reason,
                    spx_at_exit, profit_captured_pct, time_in_trade_minutes,
                    day_type, daily_move_pct,
                    pnl, pnl_percent, max_profit, max_risk,
                    quantity, expiration, notes, setup_bar_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                ctx.get('day_open'),
                ctx.get('gap_pct'),
                ctx.get('intraday_move_at_entry'),
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
            ))

            conn.commit()
            logger.debug(f"Trade {trade.id} saved to database")

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
                    daily_move_pct = ?
                WHERE id = ?
            """, (
                exit_context.get('spx_at_exit'),
                exit_context.get('profit_captured_pct'),
                exit_context.get('time_in_trade_minutes'),
                exit_context.get('day_type'),
                exit_context.get('daily_move_pct'),
                trade_id,
            ))
            conn.commit()
            logger.debug(f"Trade {trade_id} exit context updated")
    
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
                WHERE DATE(entry_time) = ?
                ORDER BY entry_time
            """, (trade_date,))
            
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
    
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
                WHERE DATE(entry_time) = ? AND status = 'closed'
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
        Used for Kelly position sizing calculations.

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
        conn = sqlite3.connect(self.db_path)
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
            exit_time = datetime.now()
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