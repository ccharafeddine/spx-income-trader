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
    
    def save_trade(self, trade):
        """Save or update trade in database"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT OR REPLACE INTO trades (
                    id, entry_time, exit_time, direction, status,
                    short_strike, long_strike, spread_width, credit_received,
                    entry_price, entry_order_id, underlying_price_at_entry,
                    exit_price, exit_order_id, exit_reason,
                    pnl, pnl_percent, max_profit, max_risk,
                    quantity, expiration, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade.id,
                trade.entry_time,
                trade.exit_time,
                trade.spread.direction.value,
                trade.status.value,
                trade.spread.short_leg.strike,
                trade.spread.long_leg.strike,
                trade.spread.spread_width,
                trade.spread.credit_received,
                trade.entry_price,
                trade.entry_order_id,
                trade.spread.underlying_price_at_entry,
                trade.exit_price,
                trade.exit_order_id,
                trade.exit_reason,
                trade.pnl,
                trade.pnl_percent,
                trade.spread.max_profit,
                trade.spread.max_risk,
                trade.quantity,
                trade.spread.expiration,
                trade.notes
            ))
            
            conn.commit()
            logger.debug(f"Trade {trade.id} saved to database")
    
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