-- Migration: Add strategy and context fields to trades table
-- Run this once to upgrade existing database

-- Strategy identification
ALTER TABLE trades ADD COLUMN strategy_type TEXT DEFAULT 'daily_income';

-- Entry context
ALTER TABLE trades ADD COLUMN spx_at_entry REAL;
ALTER TABLE trades ADD COLUMN vix_at_entry REAL;
ALTER TABLE trades ADD COLUMN day_open REAL;
ALTER TABLE trades ADD COLUMN gap_pct REAL;
ALTER TABLE trades ADD COLUMN intraday_move_at_entry REAL;

-- Exit context
ALTER TABLE trades ADD COLUMN spx_at_exit REAL;
ALTER TABLE trades ADD COLUMN profit_captured_pct REAL;
ALTER TABLE trades ADD COLUMN time_in_trade_minutes INTEGER;

-- Market conditions
ALTER TABLE trades ADD COLUMN day_type TEXT;
ALTER TABLE trades ADD COLUMN daily_move_pct REAL;

-- Create index for strategy filtering
CREATE INDEX IF NOT EXISTS idx_trades_strategy_type ON trades(strategy_type);
CREATE INDEX IF NOT EXISTS idx_trades_day_type ON trades(day_type);
