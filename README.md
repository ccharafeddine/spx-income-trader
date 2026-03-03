# The Daily Melt

![CI](https://github.com/ccharafeddine/spx-income-trader/actions/workflows/ci.yml/badge.svg) ![Python 3.11-3.13](https://img.shields.io/badge/python-3.11--3.13-blue) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Automated SPX 0DTE options income trading system. Runs parallel credit spread strategies on the S&P 500 index, autonomously from market open to close, with real-time risk management, a web dashboard, full backtesting, session recording, and multi-broker support (E\*TRADE, Charles Schwab, Interactive Brokers, dry-run simulation). Ships as a standalone desktop app for Windows and macOS.

---

## How It Works

1. **Scan** -- Monitors 30-minute SPX bars for pulse bar momentum patterns (price closing in the top or bottom 10% of the bar's range)
2. **Confirm** -- Waits for price to break the pulse bar's high or low, confirming directional momentum
3. **Execute** -- Enters an ATM credit spread ($5 wide, same-day expiration) in the confirmed direction. Partial fills are accepted and tracked at actual quantity; zero fills are rejected.
4. **Manage** -- Targets 80% of max profit. For PDT accounts (under $25k), applies 1 PM ET trend-based management rules to decide hold vs. close
5. **Repeat** -- Resets daily. No overnight exposure on 0DTE positions

The entire pipeline runs autonomously. The bot handles strike selection, order sizing, execution, monitoring, and exit management across all strategies simultaneously.

---

## Strategies

### Core Strategies

#### Daily Income (DI) -- 0DTE
The primary strategy. Detects 30-minute pulse bars during the 9:30-11:30 ET setup window. A pulse bar is one where price closes in the extreme top or bottom 10% of the bar's range, signaling strong directional momentum. After detection, the bot waits for a breakout confirmation (price breaking above the pulse bar high for bullish, below the low for bearish), then enters an ATM credit spread with same-day expiration. A morning bias filter blocks bearish entries on strong up days and bullish entries on strong down days, reducing counter-trend trades. Exits at 80% of max profit, via 1 PM management rules (PDT accounts only), or at expiration. Bollinger Band agreement is tracked as analytics-only metadata on each trade.

#### Tag 'n Turn (TNT) -- Swing
Bollinger Band mean reversion strategy with 3-7 DTE. Uses a state machine: detects when price tags a Bollinger Band (50-period, 2 std dev), waits for a pulse bar confirming reversal, then enters on breakout. Targets the opposite Bollinger Band. Holds up to 7 days. Runs in a separate swing slot alongside the 0DTE strategies.

### Experimental Strategies

These strategies are functional and included in backtesting, but are considered experimental. They may see changes to their entry logic, parameters, or role in future versions.

#### Bed & Breakfast (B&B) -- Directional Confluence
Detects pulse bars in the 3:00-4:00 PM ET window to generate an overnight directional signal. Only the final bar (15:30) creates a definitive signal; if both the 15:00 and 15:30 bars produce pulses in opposite directions, they cancel out. The signal persists overnight and is used the next morning as directional confluence for DI (informational-only, no independent trade entries). If the market gaps more than 0.3% against the signal direction overnight, the signal is automatically invalidated.

#### Opening Range Breakout (ORB)
Uses the first 30-minute bar of the day to define the opening range. Strong signals only: price must close in the top or bottom 10% of the range (weak signals are rejected). A minimum range filter (default 8.0 points) skips narrow, choppy days. When price breaks above the range high or below the range low, a confirmation delay (default 3 minutes) ensures the breakout holds before entry. Managed by DI's exit logic once entered.

### Position Limits
- 2 total position slots: 1 shared 0DTE (DI or ORB) + 1 swing (TNT)
- All strategies share a single daily loss circuit breaker
- DI and ORB share the 0DTE slot (max 1 per day)
- B&B provides directional confluence only (no entries, does not consume a slot)
- TNT uses the swing slot independently (max 1 per day)
- All entry strategies go through the same portfolio risk gates before entry
- Partial fills accepted with actual quantity tracking; zero fills rejected; Slack/Discord notification on partial fills

---

## Features

### Risk Management
- Budget-based position sizing: daily loss budget drives DI contract count, swing strategies use fixed size
- Per-strategy contract caps (daily_contracts for DI, swing_contracts for TNT/ORB)
- Global max contracts hard cap that can never be exceeded by any strategy
- 2% daily loss circuit breaker halts all new entries automatically
- Weekly and monthly drawdown limits with configurable thresholds
- Consecutive-loss circuit breaker with configurable pause duration
- Partial fill handling: accepts partial fills at actual quantity, rejects zero fills, sends notification on partial fills
- Credit quality gates reject low-premium setups
- Max position limits enforced across all strategies (2 total: 1 0DTE + 1 swing)
- PDT rule compliance: tracks day trades in a rolling 5-business-day window, blocks entries when slots exhausted (accounts under $25k). 1 PM trend-based management activates only in PDT mode.
- Morning bias filter: blocks counter-trend entries based on intraday market direction
- VIX regime awareness (low / normal / elevated / high / extreme)
- Win/loss streak tracking with frozen previous-streak display

### Broker Integration
- Multi-broker architecture with pluggable broker interface
- **E\*TRADE**: Full API integration with OAuth flow, token auto-renewal (90-min cycle), order preview/place/confirm pipeline
- **Charles Schwab**: schwab-py integration with OAuth2 authorization, automatic token refresh, and full options order support
- **Interactive Brokers**: ib_insync integration via TWS/IB Gateway. Snapshot market data, options chain discovery via reqSecDefOptParams, BAG combo orders for credit spreads, live/paper trading with automatic port selection. Dashboard connect/disconnect controls.
- **Dry-run mode** (default): Real market data via Yahoo Finance with simulated fills using Black-Scholes pricing. No broker credentials needed. Optional shadow mode compares simulated fills against a read-only Schwab feed.
- Reactive 401 handling with automatic token refresh and retry on E\*TRADE and Schwab
- Proactive token freshness checks before order placement
- Rate limit (429) protection with exponential backoff

### Database Separation
- Dry-run and live trading use separate databases (`trades_dryrun.db` / `trades_live.db`)
- Switching modes shows only trades from that mode
- Backtest results stored in a shared database (mode-independent)
- Schema auto-created on first access for new mode

### Chart & Visualization
- Real-time SPX candlestick chart with four timeframe toggles: 4h, 1h, 30m, 5m
- 5-minute bar aggregator (display-only, does not feed strategy logic)
- Scrollable price history: 30+ days on 30m, proportionally more on 1h/4h
- TradingView-style chart controls: drag chart area to pan, scroll wheel to zoom, drag x-axis strip to widen/narrow candles, drag y-axis strip to scale height
- Auto y-axis scaling: visible price range fits to the candles currently in view, updates live as you pan or zoom
- Bollinger Bands overlay on 1h and 4h timeframes
- Lazy loading: fetches older bars seamlessly as user scrolls left, no viewport jump
- Historical trade overlays: entry/exit markers and spread zones render across the full scrollable history, not just today
- Pulse bar highlight boxes on detection
- Entry markers: filled circles (green bullish / red bearish) at entry price
- Exit markers: X at exit price with P&L label and connecting line
- Spread visualization: horizontal dashed lines at short/long strikes with tinted fill zone
- Profit zone overlay showing where price needs to stay

### Dashboard
- Real-time web UI with account summary, candlestick charts, and position monitoring
- Layered audio synthesis via Web Audio API: detuned oscillators, ADSR envelopes, biquad filters, and inharmonic bell partials for 8 distinct sounds (market open/close bells, trade entry arpeggio, profit fanfare, loss tone, bar tick, pulse chime, alert). All mutable.
- Visual micro-interactions: button press spring, tab fade-in, P&L glow on value change, position card entrance animation, mute bounce, toggle squeeze, risk bar pulse, LED flash on state change, settings gear rotation, click ripple effect
- DB-backed strategy status LEDs for all four strategies (persists across page refreshes)
- Signal log showing every detected setup with strikes, credit, risk, and SPX price
- Five tabs: Overview, Trade Journal, Analytics, Backtest, Logs

### Trade Journal
- Two views: monthly calendar and filterable list, toggled with Calendar/List buttons
- Calendar view shows per-day P&L, trade count, strategy tags, no-trade reasons
- List view shows per-strategy summary cards (trades, win rate, P&L) and expandable trade rows
- Entry analysis with pulse bar checklist, strike rationale, market context
- Exit review with post-trade notes and performance rating
- Daily journal capturing bars built, pulse bars, signals evaluated, rejections with reasons
- Filterable by strategy, direction, outcome, day type, and date range
- CSV export for offline analysis

### Analytics
- Data source selector: view analytics for live trades or any historical backtest run
- Collapsible panels with Expand All / Collapse All toolbar (state persisted in localStorage)
- Disclaimer banners for simulated (backtest) and dry-run data sources
- Sharpe ratio, Sortino ratio, max drawdown, profit factor, expectancy
- Large dollar values auto-abbreviated ($10.2K, $1.5M, $2.3B) in summary cards
- Equity curve and drawdown series charts
- Monthly returns heatmap with yearly totals
- P&L distribution histogram
- Win rate breakdowns: by day of week, by time of day, by VIX regime
- Per-strategy trade breakdown (trades, wins, win rate, total P&L)
- Per-direction breakdown (bullish vs. bearish)
- Rolling win rate chart (7-day, 30-day, 90-day moving averages)
- Risk-adjusted metrics panel: Value at Risk (VaR), CVaR, tail ratio, win/loss streaks
- P&L attribution panel: decomposes trade P&L into theta, delta, vega, and residual components
- Market regime analysis: VIX regime performance, market direction performance, VIX transitions, market neutrality (correlation/beta to SPX)
- Direction drill-down: cross-tabulates market day type x trade direction to investigate regime-specific weaknesses
- BB agreement analysis: compares win rates and P&L for trades where the Bollinger Band filter agreed vs. disagreed with direction
- Trade duration analysis: per-strategy breakdown (DI vs. TNT), winner vs. loser duration comparison, duration distribution chart, win rate by duration bucket
- Execution quality: slippage summary, slippage by time bucket and VIX regime, exit reason breakdown with drill-down
- Portfolio Greeks: real-time delta, gamma, theta, vega exposure via Black-Scholes calculator
- Strategy tear sheet PDF export with monthly, weekly, and custom date range options
- Auto-loads completed backtest results without requiring app restart

### Backtesting
- Historical replay engine supporting all four strategies (DI, TNT, ORB, B&B)
- Strategy selection checkboxes: run any combination of the four strategies
- Automatic SPX and VIX data download from Yahoo Finance
- VIX + time-of-day slippage model (BidAskModel): $0.02/leg base scaled by VIX regime and intraday liquidity
- NYSE half-day calendar: early close days (1 PM ET) handled automatically
- Morning bias filter applied during simulation
- Backtest assumptions panel showing input parameters (date range, capital, strategies, thresholds) and simulation details (slippage model, pricing model, fill assumptions, flagged unusual credits)
- Configurable parameters: pulse threshold, spread width, profit target, min credit, max contracts, slippage, fill quality factor, max daily loss, initial capital
- Position sizing scales with simulated account growth over the backtest period
- Previous runs list with bulk select and delete
- Results auto-populate in the Analytics tab dropdown on completion
- Per-strategy trade breakdown and monthly return heatmaps

### Notifications
- Dynamic webhook list: up to 5 webhooks with per-webhook type (Slack, Discord, generic), URL, and minimum severity level
- Add, remove, reorder, and test individual webhooks from the Account page
- Backward-compatible migration from legacy fixed-section format
- Email and SMS (Twilio) support
- Hot-reloadable: changes take effect without restarting the bot (the only hot-reloadable setting)

### Demo Mode
- Record live trading sessions as JSONL event streams (14 event types)
- Replay recordings at accelerated speed (1x / 5x / 10x / 30x / 60x)
- Pre-built demo scenarios: winning day, losing day, no-setup day
- Dashboard looks identical during replay (same API routes, same UI)
- Play/pause, speed control, seek, and jump-to-next-event controls
- Privacy scrub removes account IDs, tokens, and API keys from recordings

### Session Recording
- Start and stop recording from the dashboard Overview tab while the bot is running
- Recordings saved as JSONL files in `demo_recordings/` with timestamped filenames
- Browse recent recordings with event count and duration metadata
- Load any recording as a demo file for replay
- Recording panel auto-hides in standalone mode and demo mode
- UI restores recording state on page refresh (pulsing indicator, elapsed timer, event count)
- Recording auto-closes when the bot stops

### Reconciliation
- Compares database trades against broker order fills
- Detects price and quantity mismatches (configurable tolerance)
- Reports matched count, discrepancies, and P&L delta
- On-demand reconciliation trigger from the dashboard
- Available in live mode only (skipped in dry-run and demo modes)

### Observability
- Structured JSON logging with credential masking
- Prometheus metrics endpoint (`/metrics`) with trade counters, P&L gauges, and latency histograms
- Health endpoint (`/api/health`) for uptime monitoring
- Price feed health monitoring with stale timeout and failure tracking
- Rotating log files with separate error-level log
- Crash log written to `~/spx_crash.log` for desktop app failures

### Desktop Application
- Native window via pywebview (not a browser wrapper)
- System tray with bot status indicator (green = running, red = stopped) and minimize-to-tray
- Windows (PyInstaller) and macOS (py2app) builds
- Headless mode (`--headless`) for running without a UI
- Dev mode (`--dev`) opens in system browser
- Bot start/stop controls from the dashboard and system tray context menu
- Bot crash detection with watchdog thread and notification

### Discord Notifications
- Bot startup: mode, equity, open positions, ET time
- Market open (9:30 AM ET): SPX price, VIX level and regime, carry-over positions
- Hourly market update (top of each hour during market hours): SPX price and session change, VIX, session high/low range, bars built, pulse bars detected, open position details (strikes, unrealized P&L, time held), next bar time
- Trade entry: strategy, direction, strikes, credit per contract, total credit, quantity, breakeven, max risk, expiry
- Trade exit: strategy, direction, strikes, P&L, exit reason, hold duration
- End of day summary: trades (W/L), daily/weekly/monthly P&L, SPX close and session range, equity, win rate, streak, open swing positions, no-trade reason with pulse bar count and VIX context
- Circuit breaker alert: loss amount vs limit, halted status
- Short strike breach warning: critical alert when SPX crosses the short strike
- Bot stopped: shutdown reason, uptime, trade count
- Watchdog auto-restart: crash reason, restart count
- All channels configurable (Slack, Discord, generic webhook) with per-channel min severity level

### Data & Storage
- SQLite database with WAL mode for concurrent read/write access
- Separate databases for dry-run and live trading modes
- OS keychain credential storage (Windows Credential Manager / macOS Keychain / Linux Secret Service)
- Automatic database migrations on startup
- Signal log rotation at 5,000 entries with atomic writes (tempfile + rename)
- CSRF protection via per-session API token on all state-changing requests
- Settings API allowlist prevents arbitrary key injection
- Sensitive values redacted in API responses

---

## Screenshots

**Full Application** -- Complete desktop application showing the sidebar, real-time SPX candlestick chart with trade overlays, open position panel, and trade plan section.

![Full App Window](screenshots/FullAppWindow.png)

**Dashboard — 1h Chart with Bollinger Bands** -- One-hour candlestick view with Bollinger Band overlay, historical trade entry/exit markers, and spread visualization across multiple days.

![Overview 1h Chart](screenshots/OverviewTab1hChart.png)

**Open Position** -- Active credit spread details showing strikes, credit received, SPX distance, estimated P&L, time to expiry, and quantity.

![Open Position](screenshots/OpenPosition.png)

**Trade Journal - Calendar View** -- Monthly calendar showing per-day P&L, trade count, strategy tags, no-trade reasons, weekends, and market holidays. Click any day to expand its trades.

![Trade Journal Calendar](screenshots/TradeJournalCalendar.png)

**Trade Journal - List View** -- Per-strategy summary cards, filterable trade list with expandable entry/exit analysis, CSV export.

![Trade Journal List](screenshots/TradeJournalList.png)

**Trade Journal - Trade Entry Detail** -- Entry analysis with a checklist of every gate the signal passed through, pulse bar details, strike rationale, market context, and trade details.

![Trade Journal Entry](screenshots/TradeJournalEntry.png)

**Trade Journal - Performance Overview** -- Aggregate performance metrics across all journal entries including win rate, P&L breakdown, and trade duration stats.

![Trade Performance Overview](screenshots/TradePerformanceOverview.png)

**Backtest — Full Run (2019–2025)** -- Long-run backtest example showing equity curve and key performance metrics across six years.

![Backtest Full Run](screenshots/Backtest_20190101-20251231_example.png)

**Analytics — Equity & Drawdown** -- Equity curve, rolling drawdown chart, and performance summary cards.

![Analytics 1](screenshots/Analytics_20190101-20251231_1.png)

**Analytics — Regime & Win Rate** -- VIX regime breakdown, market direction drilldown, win rate by day/time/regime, and direction comparison.

![Analytics 2](screenshots/Analytics_20190101-20251231_2.png)

**Analytics — P&L Attribution & Execution Quality** -- Theta/delta/vega decomposition, slippage analysis by time bucket and VIX regime, and exit reason breakdown.

![Analytics 3](screenshots/Analytics_20190101-20251231_3.png)

**Analytics — Risk Metrics** -- Calmar ratio, VaR 95/99, CVaR, tail ratio, win/loss streaks, and rolling win rate windows.

![Analytics 4](screenshots/Analytics_20190101-20251231_4.png)

---

## Architecture

```
Market Data
    |--- Yahoo Finance (backtest historical data, dry-run real-time)
    |--- E*TRADE Quotes API (live mode, 10s polling)
    |--- Schwab Quotes API (live mode, 10s polling)
    |
    v
Price Feed (health monitoring, stale timeout, TTL cache)
    |
    v
BarBuilder (30-min aggregation) + BarAggregator (5-min for charts)
    |
    v
Strategy Engine
    |--- Daily Income (0DTE credit spreads, pulse bar + breakout + morning bias)
    |--- Tag 'n Turn (BB mean reversion, 3-7 DTE swing)
    |--- Bed & Breakfast (directional confluence signal for DI) [experimental]
    |--- Opening Range Breakout (strong-only, range filter, confirmation delay) [experimental]
    |
    v
Portfolio Manager (2-slot limits, risk gates, circuit breaker, PDT gate)
    |
    v
Broker Interface
    |--- E*TRADE Broker (live orders via OAuth API)
    |--- Schwab Broker (live orders via schwab-py OAuth2)
    |--- IBKR Broker (live orders via ib_insync, TWS/Gateway)
    |--- Dry-Run Broker (real data, simulated fills via Black-Scholes)
    |
    v
Position Manager (P&L tracking, exit management, partial fill tracking, PDT-conditional 1pm)
    |
    +---> SQLite Database (mode-specific: dry-run / live)
    +---> Flask Dashboard (Overview, Journal, Analytics, Backtest, Logs)
    +---> Notification Manager (Slack, Discord, email, SMS, webhooks)
    +---> Prometheus Metrics (/metrics)
    +---> Event Recorder (session recording, demo JSONL capture)
    +---> Trade Reconciler (DB vs. broker fill comparison)
```

**Tech stack:** Python 3.13, Flask, SQLite (WAL), Yahoo Finance, E\*TRADE API, schwab-py, ib_insync, pywebview, PyInstaller/py2app, Prometheus | **Notifications:** Discord webhooks, Slack webhooks, generic webhooks, email, SMS | **Testing:** pytest (1170+ tests), GitHub Actions CI

---

## Settings

All settings are configured via `config/strategy_params.yaml` and the dashboard Settings page.

### What requires a restart
- Trading mode (dry-run / live)
- Active broker (schwab / etrade / ibkr / dry_run)
- Strategy enable/disable flags (DI, TNT, B&B, ORB)
- Pulse threshold, spread width, profit target
- Position sizing parameters (max contracts, risk per trade)
- Drawdown limits (daily, weekly, monthly)
- Setup window timing (morning start/end, afternoon window)
- Bollinger Band filter parameters
- PDT protection settings (including 1pm management mode)
- ORB range filter and confirmation delay
- B&B gap invalidation threshold
- Morning bias filter toggle

### What is hot-reloadable (no restart needed)
- Notification settings (Slack, Discord, webhooks, email, SMS)

### Key parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `strategy.pulse_threshold` | 10% | How extreme the bar close must be (top/bottom N%) |
| `strategy.spread_width` | $5 | Width of credit spreads |
| `strategy.profit_target_pct` | 80% | Exit when this percentage of max profit is reached |
| `portfolio.max_daily_loss_pct` | 2% | Circuit breaker: halt all entries for the day |
| `portfolio.daily_contracts` | 7 | Max contracts for Daily Income (budget-driven cap) |
| `portfolio.swing_contracts` | 2 | Fixed contracts for swing strategies (TNT) |
| `portfolio.max_contracts` | 10 | Global hard cap regardless of account size |
| `timing.morning_start` | 09:30 | Setup window open |
| `timing.morning_end` | 11:30 | Setup window close |
| `di_morning_bias_filter` | true | Block counter-trend entries based on intraday direction |
| `tag_n_turn.spread_width` | $10 | TNT spread width (wider for swing trades) |
| `tag_n_turn.min_dte` / `max_dte` | 3 / 7 | TNT expiration range |
| `orb.min_range_points` | 8.0 | Minimum opening range size in points (skip narrow days) |
| `orb.confirmation_minutes` | 3 | Minutes a breakout must hold before entry |
| `bnb.gap_invalidation_pct` | 0.3% | Overnight gap threshold that invalidates B&B signal |

---

## Quick Start

```bash
# Clone and install
git clone https://github.com/ccharafeddine/spx-income-trader.git
cd spx-income-trader
pip install -r requirements.txt

# Run in dry-run mode (real market data, simulated execution)
python src/main.py

# Or launch the desktop app
python app_desktop.py

# Pre-built desktop app (if available)
# Windows: dist/The Daily Melt/The Daily Melt.exe
# macOS:   dist/The Daily Melt.app
```

**Broker Setup:**

- **Schwab**: Launch the app, go to Settings, enter your Schwab API credentials, and complete the OAuth2 flow. The bot handles token refresh automatically. Set `broker.active: schwab` in `strategy_params.yaml`.
- **E\*TRADE**: Configure your consumer key and secret via the Settings page or OS keychain. OAuth token renewal runs on a 90-minute cycle. Set `broker.active: etrade`.
- **Interactive Brokers**: Start TWS or IB Gateway with API connections enabled (File > Global Configuration > API > Settings). Configure host, port, and client ID via the Setup page or Settings. Paper trading auto-selects port 7497. Set `broker.active: ibkr`.
- **Dry Run** (default): No broker config needed. Uses real market data with simulated fills.

**Configuration:**
- `config/strategy_params.yaml` for strategy parameters (thresholds, spread width, profit target, timing windows, broker selection, IBKR connection settings)
- Dashboard Settings page for credentials, trading mode (dry-run/live), and notifications
- Credentials stored in OS keychain, never in config files

---

## Building from Source

Requires **Python 3.13** (pythonnet does not support 3.14+).

**Windows (PyInstaller):**

```bash
py -3.13 -m venv .venv313
.venv313\Scripts\activate
pip install -r requirements.txt

# Build (output: dist/The Daily Melt/)
python build/build_windows.py --clean

# Debug build with console visible
python build/build_windows.py --clean --debug
```

**macOS (py2app):**

```bash
python3.13 -m venv .venv313
source .venv313/bin/activate
pip install -r requirements.txt

# Build (output: dist/The Daily Melt.app)
python build/build_macos.py --clean
```

**Packaged app data paths:**
- Windows: `%LOCALAPPDATA%\SPXIncomeTrader\SPXIncomeTrader\`
- macOS: `~/Library/Application Support/SPXIncomeTrader/`

The packaged app copies the database and seeds `strategy_params.yaml` on first run.

---

## Project Structure

```
src/
    main.py                  # TradingBot orchestrator and main loop
    core/
        strategy.py          # Daily Income strategy (pulse bar + breakout + morning bias)
        tag_n_turn.py        # Tag 'n Turn swing strategy (BB reversals)
        bnb_strategy.py      # B&B directional confluence signal [experimental]
        orb_strategy.py      # Opening Range Breakout strategy [experimental]
        portfolio_manager.py # Multi-strategy coordination and risk gates
        position_manager.py  # Trade lifecycle, P&L, exit management
        bar_builder.py       # 30-min bar aggregation from tick data
        bar_aggregator_5min.py # 5-min bars for dashboard charts (display-only)
        pulse_detector.py    # Pulse bar pattern detection
        bollinger_filter.py  # Bollinger Band filter and trend bias
        pdt_tracker.py       # Pattern Day Trader rule tracking
        drawdown_manager.py  # Drawdown tracking and circuit breakers
        reconciler.py        # Trade reconciliation (DB vs. broker fills)
    data/
        yahoo_finance.py     # Real-time SPX/VIX quotes
        market_data.py       # Market data abstraction
        price_feed.py        # Price feed abstraction with health monitoring
        vix_provider.py      # VIX data with multi-source fallback and regime classification
        economic_calendar.py # FOMC, CPI, and other high-impact event tracking
        sma_provider.py      # Simple moving average calculations
    brokers/
        base.py              # Abstract broker interface
        broker_factory.py    # Broker selection and instantiation
        etrade_broker.py     # E*TRADE live trading (OAuth, orders)
        etrade_auth.py       # E*TRADE OAuth token management
        schwab_broker.py     # Schwab live trading (schwab-py)
        schwab_auth.py       # Schwab OAuth2 token management
        ibkr_broker.py       # Interactive Brokers live trading (ib_insync)
        dry_run_broker.py    # Simulated execution with real data
    analytics/
        regime_analysis.py   # VIX/direction regime performance and drilldown
        pnl_attribution.py   # Theta/delta/vega P&L decomposition
        greeks.py            # Black-Scholes Greeks calculator
        tear_sheet.py        # PDF strategy tear sheet generator
    backtest/
        engine.py            # Multi-strategy backtest engine
        runner.py            # CLI backtest runner
        data_loader.py       # Yahoo Finance data download and CSV parsing
        sim_broker.py        # Simulated broker for backtesting
        report.py            # Backtest report generation
    demo/
        recorder.py          # JSONL event recorder (live sessions + dashboard recording)
        replay.py            # Replay engine with threaded playback
    models/
        bar.py               # Bar dataclass
        spread.py            # CreditSpread, OptionLeg models
        trade.py             # Trade record, TradeStatus enum
    utils/
        app_paths.py         # Cross-platform path resolution (dev vs. packaged)
        logging.py           # Structured logging with credential masking
        metrics.py           # Prometheus metrics
        notifications.py     # Slack, Discord, email, SMS, webhooks
        market_calendar.py   # NYSE calendar, half-days, holidays
        version.py           # Version management and update check

dashboard/
    app.py                   # Flask REST API and dashboard server (40+ routes)
    templates/
        index.html           # Main dashboard (5 tabs)
        settings.html        # Configuration UI
        setup.html           # Initial setup wizard
    static/                  # Icons and static assets

config/
    strategy_params.yaml     # All strategy and risk parameters
    settings.py              # Environment, paths, DB config, keyring integration

database/
    db_manager.py            # SQLite operations (WAL mode, migrations)
    schema.sql               # Database schema
    demo_recordings/         # Pre-built and session-recorded demo JSONL files

scripts/
    generate_demo_recording.py  # Fabricate demo scenarios
    run_dashboard.py            # Run dashboard standalone
    run_dry_run.py              # Quick-start dry-run mode
    validate_schwab.py          # Schwab credential validation
    health_check.py             # Health check script
    view_trades.py              # Trade database viewer

build/
    build_windows.py         # PyInstaller build script (Windows)
    build_macos.py           # py2app build script (macOS)

app_desktop.py               # Desktop app entry point (pywebview + Flask + session recording)
```

---

## Testing

1170+ tests covering:
- Strategy logic (pulse detection, breakout confirmation, setup windows, range filters, confirmation delays, morning bias filter, TNT weekend hold prevention)
- Multi-strategy backtest engine (DI, TNT, ORB, B&B parallel execution)
- Position management (sizing, P&L calculation, exit triggers, partial fill tracking)
- Risk gates (circuit breaker, position limits, credit quality, drawdown)
- Catastrophic scenario testing (max_contracts enforcement, B&B entry prevention, circuit breaker all-path coverage)
- PDT compliance tracking, entry gating, and 1PM management integration
- Bar building and market state management
- Consecutive loss tracking and streak counters
- VIX data provider multi-source fallback chains
- Price feed health monitoring and stale detection
- Daily journal persistence and API endpoints
- Notification delivery (Slack, Discord, email, webhook, hourly updates, EOD summary, SPX/VIX enrichment)
- Demo mode (recording, replay, Flask integration)
- Session recording (fixed filename mode, start/stop lifecycle, dashboard list/load routes, path traversal protection)
- Trade reconciliation (DB vs. broker comparison, mismatch detection)
- Monitoring and observability (Prometheus metrics, health endpoint)
- Schwab, E*TRADE, and IBKR broker integration (connect/disconnect, market data, order execution, position value)
- VIX + time-of-day bid-ask spread model (BidAskModel) and fill quality factor
- Slippage tracking and database migrations
- Database separation (dry-run vs live mode)
- Analytics computations (BB agreement, trade duration, direction drilldown, P&L attribution, regime analysis, execution quality)
- Backtest engine PDT mode and BB agreement tracking
- Chart data endpoints (unified bar history, paginated lazy loading, historical trade queries)
- Timestamp normalization across year boundaries (YYYY-MM-DD HH:MM format, correct sort order)

```bash
python -m pytest tests/ -v
```

CI runs on every push and pull request to `main`, testing Python 3.11, 3.12, and 3.13.

---

## Backtesting

Run backtests from the dashboard Backtest tab or via CLI:

```bash
python -m src.backtest.runner --start 2024-01-01 --end 2025-12-31

# With custom parameters
python -m src.backtest.runner \
    --start 2024-06-01 \
    --end 2025-06-01 \
    --capital 100000 \
    --pulse-threshold 10 \
    --spread-width 5 \
    --profit-target 80 \
    --max-contracts 5 \
    --slippage 0.02 \
    --output docs/backtest_report.md
```

The dashboard Backtest tab provides:
- Strategy selection checkboxes (any combination of DI, TNT, B&B, ORB)
- All parameters configurable in-UI
- Progress bar with current date indicator
- Results display with equity curve, drawdown, and monthly returns
- Backtest assumptions panel with input parameters and simulation details (slippage model, pricing model, fill assumptions, flagged credits)
- Previous runs list with bulk select and delete
- Completed backtests auto-appear in the Analytics tab dropdown
- VIX + time-of-day slippage: $0.02/leg base, scaled by VIX regime (1.0-2.5x) and intraday period (1.0-1.6x)
- Fill quality factor: conservative overlay reducing entry credit (default 1.0, configurable down to stress-test worse fills)
- PDT-aware simulation: 1pm management activates when initial capital < $25k
- Morning bias filter applied during simulation
- BB agreement tracked as analytics metadata on every backtest trade
- Disclaimer banner reminds that backtest results are simulated
- NYSE half-day calendar: early close days handled automatically

SPX and VIX data are downloaded automatically from Yahoo Finance. Pass `--csv` and `--vix-csv` to use existing data files.

---

## Demo Mode

Generate synthetic demo recordings and replay them through the dashboard:

```bash
# Generate all three demo scenarios
python scripts/generate_demo_recording.py --all

# Launch the desktop app in demo mode
python app_desktop.py --demo database/demo_recordings/demo_winning.jsonl
```

Three pre-built scenarios:
- **Winning day**: Bullish pulse, breakout confirmed, put spread entered, 80% profit target hit
- **Losing day**: Bullish pulse, breakout, reversal, stop loss hit
- **No-setup day**: Narrow range, no pulse detected, all windows expire

### Session Recording from the Dashboard

In desktop mode, you can record live bot sessions directly from the Overview tab:

1. Start the bot in dry-run or live mode
2. Click **Record** in the Session Recording panel
3. The panel shows a pulsing indicator, elapsed time, and event count
4. Click **Stop** to save the recording
5. Click **Load as Demo** to set it up for replay, then restart with `--demo`

Recordings are saved to `database/demo_recordings/` and can be browsed from the panel. The recording state persists across page refreshes. If the bot stops while recording, the recording is automatically closed and saved.

---

## Expansion Roadmap
- Text message (SMS) notifications via Twilio
- Tastytrade broker integration

---

## Disclaimer

This is a personal project. It is not financial advice.

- Options trading involves significant risk of loss, including the possibility of losing more than the initial investment
- Past performance, whether simulated or live, does not guarantee future results
- This software is provided as-is with no warranty
- Always validate thoroughly in dry-run mode before risking real capital

---

## License

MIT License - See [LICENSE](LICENSE) for details.
