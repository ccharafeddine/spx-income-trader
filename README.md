# SPX Income Trading System

Automated 0-DTE and swing SPX options trading system implementing four parallel strategies coordinated by a portfolio risk manager: Daily Income (core 0DTE), Tag 'n Turn (swing), B&B (overnight signals), and ORB (opening range breakout).

## Strategy Overview

The system trades SPX index options using intraday price action on 30-minute bars. It coordinates multiple strategies through a Portfolio Manager that enforces position limits, daily risk caps, and a circuit breaker.

### Architecture

```
Market Data (Yahoo Finance / E*TRADE)
  -> BarBuilder (30-min bar aggregation)
     -> Four Parallel Strategies:
        1. Daily Income (0DTE credit spreads, morning pulse bars)
        2. Tag 'n Turn (3-7 DTE swing trades, BB reversals)
        3. B&B (overnight signals for next-day entry)
        4. ORB (opening range breakout)
     -> PortfolioManager (coordinates all strategies)
        -> Position limits (max 3 total, max 2 0DTE)
        -> Daily risk tracking (5% max at risk)
        -> Circuit breaker (2% realized loss limit)
     -> Order Execution -> Position Tracking -> Exit Management
```

## Implemented Strategies

### 1. Daily Income Strategy (Core 0DTE)

The primary strategy: sells ATM credit spreads on SPX that expire the same day.

**Entry Signals:**
- **Pulse Bar Detection**: Bar where close is in extreme 10% of range
  - Bullish: `((close - low) / (high - low)) >= 90%`
  - Bearish: `((close - low) / (high - low)) <= 10%`
- **Breakout Confirmation**: Price breaks pulse bar high (bullish) or low (bearish)
- **Setup Window**: 9:30 AM - 11:30 AM ET

**Position Structure:**
- Bullish pulse -> Put credit spread (sell put, buy lower put)
- Bearish pulse -> Call credit spread (sell call, buy higher call)
- $5-wide spreads, ATM strikes rounded to nearest $5
- Minimum $1.00 credit required

**Exit Rules:**
- 80% profit target
- 1pm trend management check
- Expiration at close

**1pm Management Modes:**

| Day Type | AGGRESSIVE | MODERATE | CONSERVATIVE |
|----------|-----------|----------|--------------|
| Trending (>$15 move) | Hold | Hold | Hold |
| Non-trending + favorable | Hold | Hold | Close early |
| Non-trending + unfavorable | Hold | Close early | Close early |

### 2. Tag 'n Turn Strategy (Swing)

Multi-day swing strategy using Bollinger Band mean reversion (p.30-33).

**Entry Signals:**
1. Price tags upper or lower Bollinger Band (50-period, 2 std)
2. Pulse bar forms in **reversal** direction (opposite to band tagged)
3. Next bar breaks pulse bar high/low (confirmation)

**State Machine:**
```
IDLE -> TAG_DETECTED -> PULSE_CONFIRMED -> AWAITING_BREAKOUT -> POSITION_OPEN -> IDLE
```

**Position Structure:**
- 3-7 DTE credit spreads or directional options
- $10-wide spreads for swing trades
- Smaller size (2 contracts default) for multi-day risk

**Exit Rules:**
- Target: Opposite Bollinger Band
- Stop: Close beyond entry band
- Max hold: 7 days

### 3. B&B Strategy (Bed & Breakfast)

Overnight signal strategy detecting end-of-day setups for next-morning entry (p.23-26).

**Signal Detection (15:00-16:00 ET):**
- Pulse bar in final hour of trading
- Signal stored overnight with direction and trigger price

**Next-Day Entry (09:30 ET):**
- If signal still valid, enter at market open
- Just Breakfast mode: Roll into daily income if first bar confirms direction

**Configuration:**
- `aggressive_roll: true` - Enable Just Breakfast rolling
- Window: 15:00-16:00 ET for signal detection

### 4. ORB Strategy (Opening Range Breakout)

First-bar breakout strategy with relaxed thresholds (p.22).

**Entry Signals:**
- First 30-min bar establishes opening range
- Breakout above high or below low triggers entry
- Threshold: 10-40% (more flexible than strict pulse bar)

**Position Structure:**
- 0DTE credit spreads
- Separate from daily income trade limit

## Portfolio Risk Manager

Coordinates all strategies to prevent over-allocation:

**Position Limits:**
- Max 3 total positions across all strategies
- Max 2 same-day (0DTE) positions
- Strategy priority: B&B > Daily Income > ORB > Tag 'n Turn

**Risk Controls:**
- Max 5% of account at risk at any time
- Circuit breaker: 2% realized daily loss stops new entries
- Dollar limits scale with account size ($50k @ 2% = $1000 max loss)

**Daily Reset:**
- Clears daily P&L and risk tracking at market open
- Removes expired 0DTE positions
- Keeps swing positions (Tag 'n Turn)

## Configuration

All parameters in `config/strategy_params.yaml`:

```yaml
# Portfolio-wide settings
portfolio:
  account_size: 50000.0        # Account size for risk calculations
  max_total_positions: 3       # Max concurrent positions
  max_0dte_positions: 2        # Max same-day positions
  max_daily_risk_pct: 5.0      # Max % at risk
  max_daily_loss_pct: 2.0      # Circuit breaker (realized loss)
  priority:                    # Entry priority when slots limited
    - bnb
    - daily_income
    - orb
    - tag_n_turn

# Daily Income Strategy
strategy:
  pulse_threshold: 10.0        # % threshold for pulse bar
  spread_width: 5.0            # Spread width ($)
  profit_target_pct: 80.0      # Exit at 80% of max profit
  max_daily_trades: 1          # Max daily income trades
  contracts_per_trade: 5       # Contracts per entry

timing:
  morning_start: "09:30"       # Setup window start (ET)
  morning_end: "11:30"         # Setup window end (ET)

monitoring:
  enable_1pm_check: true       # 1pm trend management
  trending_threshold: 15.0     # $ move for trending day
  management_mode: "MODERATE"  # AGGRESSIVE/MODERATE/CONSERVATIVE

filters:
  bollinger_enabled: true      # BB filter for daily income
  bollinger_period: 50
  bollinger_std: 2.0
  extreme_move_override_pct: 1.5  # Override BB on big moves

# Tag 'n Turn Strategy
tag_n_turn:
  enabled: true
  bb_period: 50
  bb_std: 2.0
  pulse_threshold: 10.0
  max_hold_days: 7
  use_credit_spreads: true
  contracts_per_trade: 2
  spread_width: 10.0
  min_credit: 2.00
  min_dte: 3
  max_dte: 7

# B&B Strategy
bnb:
  enabled: true
  pulse_threshold: 10.0
  contracts_per_trade: 3
  aggressive_roll: true        # Just Breakfast mode

# ORB Strategy
orb:
  enabled: true
  min_threshold: 10.0
  max_threshold: 40.0
  contracts_per_trade: 3
  separate_daily_limit: true
```

Environment variables (`.env`):
- `TRADING_MODE`: `paper` or `live`
- `ETRADE_CONSUMER_KEY`, `ETRADE_CONSUMER_SECRET`: E*TRADE API credentials
- `DASHBOARD_HOST`, `DASHBOARD_PORT`: Dashboard bind address/port

## Web Dashboard

Flask-based dashboard at `http://127.0.0.1:5000`:

### Overview Tab
- **Bot Status**: Running state, heartbeat with market-aware staleness
- **SPX Price**: Real-time price with tick flash animation
- **Strategy Status Panel**: Live state of all 4 strategies
  - Daily Income: Idle / Watching / Pending / Done
  - Tag 'n Turn: IDLE / TAG_DETECTED / PULSE_CONFIRMED / AWAITING_BREAKOUT / POSITION
  - B&B: Idle / Scanning / Signal / Pending
  - ORB: Idle / Building / Range Set / Triggered / Position
- **Account Summary**: Equity, cash, unrealized/realized P&L

### Today Tab
- Live 30-min candlestick chart with pulse bar highlighting
- Bar building progress and countdown
- Today's signals and trades
- Open position zone visualization (SAFE/WARNING/DANGER)

### Trade Journal Tab
- **Strategy Breakdown**: Stats per strategy (trades, win rate, P&L)
- **Advanced Filters**: Strategy, direction, outcome, day type
- **Trade Cards**: Expandable with full context
  - Strategy tag (DAILY, TNT, B&B, ORB)
  - Entry context (SPX, VIX, gap, intraday move)
  - Exit analysis (reason, duration, capture %)
  - Market conditions (day type, daily move)
  - Editable notes and ratings
- **CSV Export**: Download all trade data

## Running

```bash
# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your settings

# Start bot in dry-run mode (real data, simulated execution)
python -m src.main --dry-run --no-confirm

# Start web dashboard
cd dashboard && python app.py

# Or run both (separate terminals)
python -m src.main --dry-run --no-confirm &
python -m dashboard.app &

# View logs
tail -f logs/trading.log
```

**Command-line flags:**
- `--dry-run`: Real Yahoo data, simulated options chain, no orders
- `--no-confirm`: Skip interactive prompts (for unattended runs)
- `--mode paper|live`: Broker mode selection
- `--log-level DEBUG|INFO|WARNING|ERROR`: Log verbosity

## Project Structure

```
config/
  settings.py                 # Environment config, paths
  strategy_params.yaml        # All strategy parameters

src/
  main.py                     # TradingBot orchestrator
  brokers/
    base.py                   # Abstract BrokerInterface
    dry_run_broker.py         # Real data + simulated execution
    paper_trader.py           # Simulated everything
    etrade_broker.py          # E*TRADE live trading
  core/
    strategy.py               # Daily Income strategy
    pulse_detector.py         # Pulse bar detection
    bar_builder.py            # 30-min bar aggregation
    bollinger_filter.py       # BB filter and bias tracking
    tag_n_turn.py             # Tag 'n Turn swing strategy
    bnb_strategy.py           # B&B overnight signals
    orb_strategy.py           # Opening Range Breakout
    portfolio_manager.py      # Multi-strategy coordination
    position_manager.py       # Trade lifecycle, P&L
  models/
    bar.py                    # Bar dataclass
    spread.py                 # CreditSpread, OptionLeg
    trade.py                  # Trade, TradeStatus
  data/
    yahoo_finance.py          # Real-time SPX/VIX quotes
    market_data.py            # Market data wrapper

database/
  schema.sql                  # SQLite schema
  db_manager.py               # Database operations
  migrations/                 # Schema migrations

dashboard/
  app.py                      # Flask app (REST API)
  templates/index.html        # Single-page UI

logs/
  trading.log                 # Main log file
  dry_run_signals.json        # Signal history (dry-run)
```

## Database

SQLite with tables:
- **trades**: Full trade records with strategy context
  - Entry: strategy_type, spx_at_entry, vix, gap, intraday_move
  - Exit: spx_at_exit, profit_captured_pct, time_in_trade, day_type
- **daily_stats**: Aggregated daily performance
- **system_events**: Bot events log
- **journal_notes**: Trade review annotations

Auto-migration adds new columns to existing databases on startup.

## Broker Modes

| Mode | Market Data | Options Chain | Execution | Use Case |
|------|------------|---------------|-----------|----------|
| Dry Run | Real (Yahoo) | Simulated (BS) | Simulated | Validate with real prices |
| Paper | Simulated | Simulated | Simulated | Test without market |
| E*TRADE | Real | Real | Live orders | Production trading |

## Risk Disclaimer

This software is for educational purposes. Trading options involves substantial risk of loss. Past performance does not guarantee future results. Use at your own risk.

## License

MIT License - See LICENSE file for details.
