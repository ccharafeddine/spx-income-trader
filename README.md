# SPX Income Trading System

Automated 0-DTE SPX credit spread trading system implementing Phil Newton's **Production Line Trading** strategy. Detects pulse bar setups on 30-minute charts, confirms entries on breakout, filters with Bollinger Band directional bias, and manages positions with profit targets and a 1pm trend-based management check.

## Strategy Overview

The system sells ATM credit spreads on SPX index options that expire the same day (0-DTE). It uses intraday price action on 30-minute bars to identify high-probability setups and manages risk through defined spread widths, daily trade limits, and systematic exit rules.

### Architecture

```
Market Data (Yahoo Finance / E*TRADE)
  → BarBuilder (30-min bar aggregation from ticks)
    → PulseBarDetector (10% threshold close-in-range check)
      → BollingerFilter (directional bias gate)
        → Pending Setup (stored, waiting for breakout)
          → Breakout Confirmation (price > bar high or < bar low)
            → Spread Construction (ATM $5-wide credit spread)
              → Credit Quality Check + Order Execution
                → PositionManager (P&L tracking, exit logic)
                  → 1pm Management Check / 80% Profit Target / Expiration
```

## Implemented Features

### Core Trading Logic
- **30-minute bar building** from live tick data via Yahoo Finance
- **Pulse bar detection**: Bar where close is in the extreme 10% of its range (top 10% = bullish, bottom 10% = bearish). Calculated as `((close - low) / (high - low)) * 100`
- **Two-phase entry**: Pulse bar creates a *pending setup* (not an immediate trade). Entry triggers only when the next price breaks the setup bar's high (bullish) or low (bearish)
- **ATM $5-wide credit spreads**: Bullish pulse = put credit spread, bearish pulse = call credit spread. Strikes rounded to nearest $5
- **$1.00 minimum credit floor**: Spreads below this threshold are rejected
- **80% profit target**: Exits when current profit reaches 80% of max spread profit
- **Daily limits**: Max 1 trade per day, $1,000 max daily loss
- **Morning setup window**: 9:30-11:30 AM ET. Pending setups expire if no breakout by window close

### 1pm In-Trade Management (p.19-20)
At 1pm ET, evaluates whether the day is trending (`|SPX move from entry| >= $15`) and whether the move is favorable to the position direction. Runs once per trade.

| Condition | AGGRESSIVE | MODERATE | CONSERVATIVE |
|-----------|-----------|----------|--------------|
| Trending (any direction) | Hold | Hold | Hold |
| Non-trending + favorable | Hold | Hold | Close early |
| Non-trending + unfavorable | Hold | Close early | Close early |

Default mode: **MODERATE**

### Credit Quality Classification (p.20)
Each trade entry classifies credit received against expected benchmarks by moneyness:
- OTM: $2.40-$2.60
- ATM: $2.50-$2.80
- ITM: $2.80-$3.00

Classification (GOOD / ACCEPTABLE / BELOW_EXPECTED) and a simulated-pricing warning flag (`< $1.50`) are logged per trade and stored in signal data.

### Bollinger Band Filter — Tag 'n Turn (p.30-33)
50-period Bollinger Bands on 30-minute bars (non-standard setting from the book). Establishes directional bias:
- Price tags lower band -> bullish bias
- Price tags upper band -> bearish bias
- Bias persists until the opposite band is tagged

Pulse bar setups are **blocked** if they conflict with the current bias. If no bias is established (insufficient data or no recent tag), all setups are allowed. Seeded with 10 days of historical data from yfinance at startup.

Configurable: can be disabled via `filters.bollinger_enabled: false`.

### Afternoon Setup Window — Bed & Breakfast (p.23-26)
Optional second setup window in the last 30 minutes of trading (15:00-15:30 ET). Uses the same pulse bar logic, breakout confirmation, and filters as the morning window. Pending setups from this window expire at 15:30 ET.

**Disabled by default.** Enable via `timing.afternoon_enabled: true`.

### Web Dashboard
Flask-based single-page dashboard at `http://127.0.0.1:5000` with four tabs:

- **Overview**: Bot status with heartbeat staleness detection, SPX price, account summary, strategy parameters, market status with next-bar countdown
- **Today**: Live 30-min candlestick chart with pulse bar highlighting, bar building progress, today's signals and trades, open position status (SAFE/WARNING/DANGER distance to short strike)
- **History**: Daily stats, trade-level history with cumulative P&L, win rate, average win/loss
- **Trade Journal**: Per-trade analysis cards with:
  - Entry checklist (pulse detected, threshold met, window, credit minimum, limits, position clear)
  - Pulse bar OHLC details and close position %
  - Market context (SPX at entry, SPX open, VIX level)
  - Strike analysis (rationale, credit vs expected range, distance to short)
  - Exit analysis (reason, duration, % of max profit)
  - Editable post-trade review: 1-5 star rating, "what would I do differently", notes, news catalyst (persisted in SQLite)

### Dry-Run Mode
Uses real SPX/VIX prices from Yahoo Finance with a simulated options chain (Black-Scholes approximation using live VIX for implied volatility). No real orders placed. All signals logged to `logs/dry_run_signals.json` with full metadata: pulse bar OHLC, VIX at signal, setup bar time, breakout time, credit quality.

### Security
- Dashboard defaults to localhost-only (`127.0.0.1`), overridable via `DASHBOARD_HOST` env var
- `.gitignore` covers `.env`, `tokens/`, `*.db`, `*.pdf`, signal logs
- PID lockfile prevents duplicate bot instances

## Configuration

All strategy parameters live in `config/strategy_params.yaml`:

```yaml
strategy:
  pulse_threshold: 10.0        # % threshold for pulse bar detection
  spread_width: 5.0            # Credit spread width ($)
  profit_target_pct: 80.0      # Exit at 80% of max profit
  max_daily_trades: 1          # Max trades per day
  contracts_per_trade: 5       # Contracts per entry

risk:
  max_daily_loss: 1000         # Stop trading if down $1000/day
  max_account_risk_pct: 2.0    # Max risk per trade (% of account)

execution:
  min_credit: 1.00             # Hard floor — reject spreads below $1.00
  max_slippage: 0.10           # Max slippage tolerance ($)

timing:
  morning_start: "09:30"       # Setup window start (ET)
  morning_end: "11:30"         # Setup window end (ET)
  afternoon_enabled: false     # Bed & Breakfast window (p.23-26)
  afternoon_start: "15:00"     # Afternoon window start
  afternoon_end: "15:30"       # Afternoon window end

monitoring:
  enable_1pm_check: true       # 1pm trend management
  trending_threshold: 15.0     # $ move threshold for trending day
  management_mode: "MODERATE"  # AGGRESSIVE / MODERATE / CONSERVATIVE

filters:
  bollinger_enabled: true      # Tag 'n Turn BB filter (p.30-33)
  bollinger_period: 50         # BB period (30-min bars)
  bollinger_std: 2.0           # BB standard deviations
```

Environment variables (`.env`):
- `TRADING_MODE`: `paper` or `live`
- `ETRADE_CONSUMER_KEY`, `ETRADE_CONSUMER_SECRET`: E\*TRADE API credentials
- `ETRADE_SANDBOX`: `true` for sandbox, `false` for production
- `DASHBOARD_HOST`, `DASHBOARD_PORT`: Dashboard bind address and port

## Running

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env

# Start in dry-run mode (real market data, simulated execution)
python -m src.main --dry-run --no-confirm

# Start the web dashboard
python -m dashboard.app

# Paper trading (simulated data + execution)
python -m src.main --mode paper

# View bot logs
tail -f logs/trading.log
```

Flags:
- `--dry-run`: Real Yahoo Finance data, simulated options chain, no orders placed
- `--no-confirm`: Skip interactive trade confirmation prompts (for unattended runs)
- `--mode paper|live`: Select broker mode
- `--log-level DEBUG|INFO|WARNING|ERROR`: Override log verbosity

## Project Structure

```
config/
  settings.py                 # Environment config, paths, credentials
  strategy_params.yaml        # Strategy parameters
src/
  main.py                     # TradingBot orchestrator and main loop
  brokers/
    base.py                   # Abstract BrokerInterface
    dry_run_broker.py          # Real data + simulated execution
    paper_trader.py            # Simulated data + execution with slippage
    etrade_broker.py           # E*TRADE live trading integration
    etrade_auth.py             # E*TRADE OAuth2 authentication
  core/
    strategy.py               # SPXIncomeStrategy (setup eval, exits, 1pm mgmt)
    pulse_detector.py          # PulseBarDetector (10% bar threshold)
    bar_builder.py             # 30-min bar aggregation from ticks
    bollinger_filter.py        # Tag 'n Turn BB directional bias filter
    position_manager.py        # Trade lifecycle, P&L, credit quality
  models/
    bar.py                     # Bar dataclass (OHLC, range, close %)
    spread.py                  # CreditSpread, OptionLeg, TradeDirection
    trade.py                   # Trade, TradeStatus
  data/
    yahoo_finance.py           # Real-time SPX/VIX quotes (60s cache)
    market_data.py             # Market data feed wrapper
  utils/
    logging.py                 # Log configuration
    notifications.py           # Email/SMS notifications
database/
  schema.sql                  # SQLite schema
  db_manager.py               # Database CRUD
dashboard/
  app.py                      # Flask dashboard (7 API endpoints)
  templates/index.html        # Single-page dark-themed UI
scripts/
  run_dry_run.py              # Convenience launcher
  run_dashboard.py            # Dashboard launcher
  health_check.py             # Bot health monitoring
```

## Database

SQLite with four tables:
- **trades**: Full trade lifecycle (entry/exit, strikes, P&L, setup bar time, breakout time)
- **daily_stats**: Aggregated daily performance (wins, losses, total P&L)
- **system_events**: Bot events log (starts, stops, 1pm management decisions)
- **journal_notes**: Persistent trade review annotations (star ratings, notes)

## Broker Modes

| Mode | Market Data | Options Chain | Execution | Use Case |
|------|------------|---------------|-----------|----------|
| Dry Run | Real (Yahoo Finance) | Simulated (BS approx from live VIX) | Simulated | Validate strategy with real prices |
| Paper | Simulated (random walk) | Simulated (simplified) | Simulated + slippage | Test without market dependency |
| E\*TRADE | Real (E\*TRADE API) | Real | Live orders | Production trading |

## Planned / Not Yet Implemented

- **Backtesting engine**: Replay historical data through the strategy
- **ORB30 filter**: Opening Range Breakout on first 30-min bar as additional directional filter
- **Just Breakfast system**: Morning-only variant with tighter exit rules
- **Unit/integration tests**: No test suite currently exists
- **Live E\*TRADE execution**: Broker interface is wired but not production-tested
- **Multi-day position tracking**: Currently assumes all positions are 0-DTE
- **SMS/email notifications**: Utility module exists but not fully integrated
