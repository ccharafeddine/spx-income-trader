# SPX Income Trading System

Automated 0-DTE SPX credit spread trading system based on the **Production Line Trading** strategy. Detects pulse bar setups on 30-minute charts, confirms entries on breakout, and manages positions with profit targets and a 1pm trend-based management check.

## Strategy Overview

The system sells ATM credit spreads on SPX index options that expire the same day (0-DTE). It uses intraday price action on 30-minute bars to identify high-probability entry points and manages risk through defined spread widths, daily trade limits, and systematic exit rules.

### Signal Detection: Pulse Bars

A **pulse bar** is a 30-minute bar where the close is in the extreme 10% of the bar's range:

- **Bullish pulse**: Close is in the **top 10%** of the bar range (strong buying pressure). Triggers a **put credit spread** (sell put, buy lower put).
- **Bearish pulse**: Close is in the **bottom 10%** of the bar range (strong selling pressure). Triggers a **call credit spread** (sell call, buy higher call).

The close position percentage is calculated as `((close - low) / (high - low)) * 100`. A bullish pulse requires this to be >= 90%, bearish <= 10%.

### Two-Step Entry: Setup + Breakout

Pulse bars are **setups**, not immediate entries. The actual entry triggers on breakout confirmation:

1. **Setup phase** (9:30-11:30 AM ET): When a completed 30-min bar is a pulse bar, it becomes a **pending setup**. The trigger price is the bar's high (bullish) or low (bearish).
2. **Breakout phase**: The system monitors subsequent price action. Entry fires when:
   - **Bullish**: `current_price > setup_bar.high`
   - **Bearish**: `current_price < setup_bar.low`

If no breakout occurs by 11:30 AM ET, the pending setup expires. A new pulse bar replaces any existing pending setup.

### Spread Construction

On breakout confirmation:
- Selects the nearest ATM strike (rounded to $5 increments)
- Constructs a $5-wide credit spread
- Rejects spreads with credit below the $1.00 minimum floor
- Classifies credit quality against expected ranges by moneyness (OTM: $2.40-$2.60, ATM: $2.50-$2.80, ITM: $2.80-$3.00)

### Exit Rules

Three exit checks run in priority order:

1. **80% profit target**: Closes when current profit reaches 80% of the spread's max profit.
2. **1pm ET management check** (configurable mode):
   - Evaluates whether the day is **trending** (`|SPX move| >= $15 threshold`) or non-trending
   - Evaluates whether the move is **favorable** to the position direction
   - Decision matrix by mode:
     | Condition | AGGRESSIVE | MODERATE | CONSERVATIVE |
     |-----------|-----------|----------|--------------|
     | Trending (any) | Hold | Hold | Hold |
     | Non-trending + favorable | Hold | Hold | Close early |
     | Non-trending + unfavorable | Hold | Close early | Close early |
3. **Expiration**: Positions expire at 4:00 PM ET (0-DTE).

## Architecture

```
config/
  settings.py               # Environment config, paths, credentials
  strategy_params.yaml      # Strategy parameters (thresholds, risk, timing)
src/
  main.py                   # TradingBot - main loop, pending setup logic
  brokers/
    base.py                 # Abstract BrokerInterface
    dry_run_broker.py       # Real Yahoo Finance data, simulated execution
    paper_trader.py         # Simulated data + execution with slippage
    etrade_broker.py        # E*TRADE live trading integration
  core/
    strategy.py             # SPXIncomeStrategy - setup eval, spread construction, exits
    pulse_detector.py       # PulseBarDetector - 10% bar threshold analysis
    bar_builder.py          # BarBuilder - 30-min bar aggregation from ticks
    position_manager.py     # PositionManager - trade lifecycle, P&L, 1pm wiring
  models/
    bar.py                  # Bar dataclass (OHLC, range, close position %)
    spread.py               # CreditSpread, OptionLeg, TradeDirection
    trade.py                # Trade, TradeStatus
  data/
    yahoo_finance.py        # Real-time SPX/VIX quotes (60s cache)
database/
  schema.sql                # SQLite schema (trades, daily_stats, events, journal_notes)
  db_manager.py             # Database CRUD operations
dashboard/
  app.py                    # Flask web dashboard
  templates/index.html      # Single-page dark-themed UI
scripts/
  run_dry_run.py            # Start bot in dry-run mode
  run_dashboard.py          # Start web dashboard
  health_check.py           # Bot health monitoring
```

## Broker Modes

| Mode | Market Data | Options Chain | Order Execution | Use Case |
|------|------------|---------------|----------------|----------|
| **Dry Run** | Real (Yahoo Finance) | Simulated (Black-Scholes approx from real SPX/VIX) | Simulated | Validate strategy with real prices |
| **Paper** | Simulated (random walk) | Simulated (simplified formula) | Simulated with slippage | Test without market dependency |
| **E*TRADE** | Real (E*TRADE API) | Real | Live orders | Production trading |

## Dashboard

Web dashboard at `http://127.0.0.1:5000` with four tabs:

- **Overview**: Bot status (with heartbeat staleness detection), current SPX price, account summary, today's bar chart, daily stats
- **Signals**: All trade signals from the signal log with direction, strikes, credit, VIX at signal time, pulse bar OHLC
- **History**: All historical trades with P&L, duration, entry/exit details
- **Trade Journal**: Per-trade analysis cards with:
  - Entry analysis (pulse bar details, strike rationale, credit quality vs expectations)
  - Market context (SPX at entry, SPX open, VIX level)
  - Exit analysis (reason, P&L, duration)
  - Editable post-trade review (1-5 star rating, notes, "what would I do differently", news catalyst)

## Configuration

Key parameters in `config/strategy_params.yaml`:

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
  min_credit: 1.00             # Hard floor - reject spreads below $1.00

monitoring:
  enable_1pm_check: true       # Enable 1pm trend management
  trending_threshold: 15.0     # Dollar move threshold for trending day
  management_mode: "MODERATE"  # AGGRESSIVE / MODERATE / CONSERVATIVE
```

## Running

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and configure environment
cp .env.example .env

# Start in dry-run mode (real data, no orders)
python -m src.main --dry-run --no-confirm

# Start the dashboard
python scripts/run_dashboard.py
```

## Database

SQLite with four tables:
- **trades**: Full trade lifecycle (entry/exit prices, strikes, P&L, setup bar time, breakout time)
- **daily_stats**: Aggregated daily performance (wins, losses, total P&L)
- **system_events**: Bot events log (starts, stops, 1pm management decisions)
- **journal_notes**: Persistent trade review annotations (ratings, notes)

## Signal Logging

All dry-run signals are written to `logs/dry_run_signals.json` with:
- Trade direction, strikes, credit received, max risk
- SPX price and VIX level at signal time
- Pulse bar OHLC and close position percentage
- Setup bar time and breakout confirmation time
- Credit quality classification

## Main Loop Flow

1. **Pre-market**: Poll every 60s waiting for 9:30 AM ET
2. **Market open**: Switch to 30s loop, fetch SPX prices, build 30-min bars
3. **Setup window (9:30-11:30)**: Detect pulse bars, store as pending setups, monitor for breakout
4. **Active trading**: Execute on breakout, monitor positions for profit target
5. **1pm check**: Evaluate trend and decide hold/close per management mode
6. **Expiration (4pm)**: Close remaining positions, log daily stats
7. **Post-market**: Return to 60s polling
