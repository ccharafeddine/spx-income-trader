-- SPX Income Trading System Database Schema

CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    entry_time TIMESTAMP NOT NULL,
    exit_time TIMESTAMP,
    direction TEXT NOT NULL,
    status TEXT NOT NULL,

    -- Strategy identification
    strategy_type TEXT DEFAULT 'daily_income',  -- daily_income, tag_n_turn, bnb, orb

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

    -- Entry context
    spx_at_entry REAL,
    vix_at_entry REAL,
    day_open REAL,
    gap_pct REAL,
    intraday_move_at_entry REAL,

    -- Exit details
    exit_price REAL,
    exit_order_id TEXT,
    exit_reason TEXT,
    spx_at_exit REAL,
    profit_captured_pct REAL,
    time_in_trade_minutes INTEGER,

    -- Market conditions
    day_type TEXT,  -- trending_up, trending_down, choppy, flat
    daily_move_pct REAL,

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

-- PDT (Pattern Day Trader) day trade tracking
-- Records trades that were opened AND actively closed on the same day
-- Expiration (0DTE running to close) does NOT count as a day trade
CREATE TABLE IF NOT EXISTS pdt_day_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id TEXT NOT NULL,
    trade_date DATE NOT NULL,
    exit_reason TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(trade_id)
);

CREATE INDEX IF NOT EXISTS idx_pdt_trade_date ON pdt_day_trades(trade_date);