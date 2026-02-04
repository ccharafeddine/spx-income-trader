-- SPX Income Trading System Database Schema

CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    entry_time TIMESTAMP NOT NULL,
    exit_time TIMESTAMP,
    direction TEXT NOT NULL,
    status TEXT NOT NULL,
    
    -- Spread details
    short_strike REAL NOT NULL,
    long_strike REAL NOT NULL,
    spread_width REAL NOT NULL,
    credit_received REAL NOT NULL,
    
    -- Entry details
    entry_price REAL NOT NULL,
    entry_order_id TEXT,
    underlying_price_at_entry REAL,
    setup_bar_time TIMESTAMP,
    
    -- Exit details
    exit_price REAL,
    exit_order_id TEXT,
    exit_reason TEXT,
    
    -- P&L
    pnl REAL,
    pnl_percent REAL,
    max_profit REAL,
    max_risk REAL,
    
    -- Position details
    quantity INTEGER NOT NULL,
    expiration TIMESTAMP NOT NULL,
    
    -- Notes
    notes TEXT,
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date DATE PRIMARY KEY,
    trades_count INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    total_pnl REAL DEFAULT 0,
    largest_win REAL DEFAULT 0,
    largest_loss REAL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    event_type TEXT NOT NULL,
    message TEXT,
    details TEXT
);

CREATE TABLE IF NOT EXISTS journal_notes (
    trade_id TEXT PRIMARY KEY,
    rating INTEGER DEFAULT NULL,         -- 1-5 star rating
    what_differently TEXT DEFAULT NULL,
    review_notes TEXT DEFAULT NULL,
    news_catalyst TEXT DEFAULT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (trade_id) REFERENCES trades(id)
);

CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_daily_stats_date ON daily_stats(date);