# The Daily Melt

![CI](https://github.com/ccharafeddine/spx-income-trader/actions/workflows/ci.yml/badge.svg) ![Python 3.11-3.13](https://img.shields.io/badge/python-3.11--3.13-blue) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Automated SPX 0DTE options income trading system. Executes credit spread strategies on the S&P 500 index using Phil Newton's Production Line methodology. Four parallel strategies run autonomously from market open to close with real-time risk management, a web dashboard, and multi-broker support (E*TRADE, Charles Schwab, dry-run). Ships as a standalone desktop app for Windows, macOS, and Linux.

---

## How It Works

1. **Scan** - Monitors 30-minute SPX bars for pulse bar momentum patterns (price closing in the top or bottom 10% of the bar's range)
2. **Confirm** - Waits for the next bar to break the pulse bar's high or low, confirming directional momentum
3. **Execute** - Enters an ATM credit spread ($5 wide, same-day expiration) in the direction of the signal
4. **Manage** - Targets 80% of max profit. At 1:00 PM ET, applies trend-based management rules to decide whether to hold or close early
5. **Repeat** - Resets daily. No overnight exposure on 0DTE positions

The entire pipeline runs autonomously. The bot handles strike selection, order sizing, execution, monitoring, and exit management.

---

## Features

**Strategy Engine**
- Four parallel strategies: Daily Income (core 0DTE), Tag 'n Turn (swing), Bed & Breakfast (overnight signals), Opening Range Breakout
- 2-slot position system: 1 shared 0DTE slot (DI/ORB/B&B) + 1 swing slot (TNT)
- Pulse bar detection with configurable thresholds
- Bollinger Band filtering for trend context (TNT)
- 1:00 PM automated position management based on trending vs. non-trending conditions
- All four strategies supported in backtesting

**Risk Management**
- Dynamic position sizing via percent-risk method (scales with account growth)
- 2% daily loss circuit breaker halts new entries automatically
- Weekly and monthly drawdown limits with configurable thresholds
- Credit quality gates reject low-premium setups
- Max position limits enforced across all strategies (2 total: 1 0DTE + 1 swing)
- PDT rule compliance: tracks day trades in a rolling 5-business-day window, blocks entries when slots exhausted (accounts under $25k)
- VIX regime awareness (low/normal/elevated/high/extreme classifications)
- Win/loss streak tracking with consecutive-loss circuit breaker

**Broker Integration**
- Multi-broker architecture with pluggable broker interface
- **E*TRADE**: Full API integration with OAuth flow, token auto-renewal (90-min cycle), order preview/place/confirm pipeline
- **Charles Schwab**: schwab-py integration with OAuth2 authorization, automatic token refresh, and full options order support
- Broker selection via `strategy_params.yaml` (`broker.active: schwab` or `broker.active: etrade`)
- Reactive 401 handling with automatic token refresh and retry on both brokers
- Proactive token freshness checks before order placement
- Rate limit (429) protection with exponential backoff
- Dry-run mode for paper trading with real market data and simulated fills (Black-Scholes pricing)

**Dashboard**
- Real-time web UI with account summary, candlestick charts, and position monitoring
- Audio alerts via Web Audio API: market open/close bell chimes, trade entry/exit tones, bar completion pings, and danger alerts (all mutable)
- Signal log showing every detected setup with strikes, credit, risk, and SPX price
- Trade journal with monthly calendar view, entry analysis (pulse bar checklist), market context, and exit review
- Daily journal capturing bars built, pulse bars, signals, rejections, and no-trade summaries
- Performance analytics tab with Sharpe ratio, Sortino ratio, max drawdown, equity curve, and monthly returns
- Risk status panel with drawdown tracking and win/loss streak counters
- Filterable by strategy, direction, outcome, day type, and date range
- PDT status badge with real-time slot count and next-frees date display
- CSV export for offline analysis

**Backtesting**
- Historical replay engine supporting all four strategies (DI, TNT, ORB, B&B)
- Automatic SPX and VIX data download from Yahoo Finance
- Equity curve, Sharpe/Sortino/Calmar ratios, max drawdown, profit factor
- Per-strategy trade breakdown and monthly return heatmaps
- Configurable parameters (threshold, spread width, capital, slippage)

**Notifications**
- Slack, Discord, and generic webhook integrations
- Email and SMS (Twilio) support
- Configurable notification levels (info, warning, critical)
- Hot-reloadable: changes take effect without restarting the bot

**Demo Mode**
- Record live trading sessions as JSONL event streams
- Replay recordings at accelerated speed (1x/5x/10x/30x/60x)
- Pre-built demo scenarios: winning day, losing day, no-setup day
- Dashboard looks identical during replay (same API routes, same UI)

**Observability**
- Structured JSON logging with credential masking
- Prometheus metrics endpoint (`/metrics`) with trade counters, P&L gauges, and latency histograms
- Health endpoint (`/api/health`) for uptime monitoring
- Rotating log files with separate error-level log

**Desktop Application**
- Native window via pywebview (not a browser wrapper)
- System tray with bot status indicator (green=running, red=stopped) and minimize-to-tray support
- Available for Windows (PyInstaller), macOS (py2app), and Linux (PyInstaller + optional AppImage)
- Headless mode (`--headless`) for running without a UI
- Dev mode (`--dev`) opens in system browser

**Data & Storage**
- SQLite database with WAL mode for concurrent read/write access
- OS keychain credential storage (Windows Credential Manager / macOS Keychain / Linux Secret Service)
- Automatic database migrations on startup
- Signal log rotation at 5,000 entries
- Atomic writes (tempfile + rename) for crash safety

---

## Screenshots

**Dashboard Overview** - Bot status, strategy parameters, risk drawdown meters, SPX candlestick chart with pulse bar highlights, account summary, signal log, and trade history.

![Dashboard Overview](screenshots/overview.png)

**Trade Journal - Calendar View** - Monthly calendar showing per-day P&L, trade count, strategy tags, no-trade reasons, weekends, and market holidays. Click any day to expand its trades below.

![Trade Journal Calendar](screenshots/journal_calendar.png)

**Trade Journal - Trade Detail** - Entry analysis with a checklist of every gate the signal passed through, pulse bar details, strike rationale, market context, trade details (strikes, credit, risk/reward, duration), and exit analysis with post-trade review.

![Trade Detail](screenshots/trade_detail.png)

---

## Architecture

```
Market Data (Yahoo Finance)
    |
    v
BarBuilder (30-min aggregation)
    |
    v
Strategy Engine
    |--- Daily Income (0DTE credit spreads, pulse bar + breakout)
    |--- Tag 'n Turn (BB mean reversion, 3-7 DTE swing)
    |--- Bed & Breakfast (overnight signal -> morning entry)
    |--- Opening Range Breakout (first-bar range breakout)
    |
    v
Portfolio Manager (2-slot limits, risk gates, circuit breaker, PDT gate)
    |
    v
Broker Interface
    |--- E*TRADE Broker (live orders via OAuth API)
    |--- Schwab Broker (live orders via schwab-py OAuth2)
    |--- Dry-Run Broker (real data, simulated fills via Black-Scholes)
    |
    v
Position Manager (P&L tracking, exit management, 1pm check)
    |
    +---> SQLite Database + Flask Dashboard
    +---> Notification Manager (Slack, Discord, email, SMS, webhooks)
    +---> Prometheus Metrics (/metrics)
    +---> Event Recorder (demo JSONL capture)
```

**Tech stack:** Python, Flask, SQLite, Yahoo Finance, E*TRADE API, schwab-py, pywebview, PyInstaller/py2app, Prometheus

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
# Linux:   dist/SPXIncomeTrader/The Daily Melt
# macOS:   dist/The Daily Melt.app
```

**Broker Setup:**

- **Schwab**: Run through the OAuth2 authorization flow via the Settings page. The bot handles token refresh automatically. Set `broker.active: schwab` in `strategy_params.yaml`.
- **E*TRADE**: Configure your consumer key and secret via the Settings page or OS keychain. OAuth token renewal runs on a 90-minute cycle. Set `broker.active: etrade` in `strategy_params.yaml`.
- **Dry Run** (default): No broker config needed. Uses real market data with simulated fills.

**Configuration:**
- `config/strategy_params.yaml` for strategy parameters (thresholds, spread width, profit target, timing windows, broker selection)
- Dashboard Settings page for credentials, trading mode (dry-run/live), and account settings
- Credentials stored in OS keychain, never in files

---

## Building from Source

Requires **Python 3.13** (pythonnet does not support 3.14+).

**Windows (PyInstaller):**

```bash
# Create a Python 3.13 virtual environment
py -3.13 -m venv .venv313
.venv313\Scripts\activate
pip install -r requirements.txt

# Build (output: dist/The Daily Melt/)
python build/build_windows.py --clean

# Debug build with console visible
python build/build_windows.py --clean --debug
```

**Linux (PyInstaller):**

```bash
# Install system dependencies (Ubuntu/Debian)
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1
sudo apt install libayatana-appindicator3-dev

# Create a Python 3.13 virtual environment
python3.13 -m venv .venv313
source .venv313/bin/activate
pip install -r requirements.txt

# Build (output: dist/SPXIncomeTrader/)
python build/build_linux.py --clean

# Optional: package as AppImage
python build/build_linux.py --appimage
```

**macOS (py2app):**

```bash
# Create a Python 3.13 virtual environment
python3.13 -m venv .venv313
source .venv313/bin/activate
pip install -r requirements.txt

# Build (output: dist/The Daily Melt.app)
python build/build_macos.py --clean
```

**Packaged app data paths:**
- Windows: `%LOCALAPPDATA%\SPXIncomeTrader\SPXIncomeTrader\`
- Linux: `~/.local/share/SPXIncomeTrader/`
- macOS: `~/Library/Application Support/SPXIncomeTrader/`

The packaged app copies the database and seeds `strategy_params.yaml` on first run.

---

## Project Structure

```
src/
    main.py                  # TradingBot orchestrator and main loop
    core/
        strategy.py          # Daily Income strategy (pulse bar + breakout)
        tag_n_turn.py        # Tag 'n Turn swing strategy (BB reversals)
        bnb_strategy.py      # B&B overnight signal strategy
        orb_strategy.py      # Opening Range Breakout strategy
        portfolio_manager.py # Multi-strategy coordination and risk gates
        position_manager.py  # Trade lifecycle, P&L, exit management
        bar_builder.py       # 30-min bar aggregation from tick data
        pulse_detector.py    # Pulse bar pattern detection
        bollinger_filter.py  # Bollinger Band filter and trend bias
        pdt_tracker.py       # Pattern Day Trader rule tracking and entry gating
        drawdown_manager.py  # Drawdown tracking and circuit breakers
    data/
        yahoo_finance.py     # Real-time SPX/VIX quotes
        market_data.py       # Market data abstraction
        vix_provider.py      # VIX data with multi-source fallback
    brokers/
        base.py              # Abstract broker interface
        broker_factory.py    # Broker selection and instantiation
        etrade_broker.py     # E*TRADE live trading (OAuth, orders)
        etrade_auth.py       # E*TRADE OAuth token management
        schwab_broker.py     # Charles Schwab live trading (schwab-py)
        schwab_auth.py       # Schwab OAuth2 token management
        dry_run_broker.py    # Simulated execution with real data
    backtest/
        engine.py            # Multi-strategy backtest engine
        runner.py            # CLI backtest runner
        data_loader.py       # Yahoo Finance data download and CSV parsing
        sim_broker.py        # Simulated broker for backtesting
        report.py            # Backtest report generation (Markdown + charts)
    demo/
        recorder.py          # JSONL event recorder for live sessions
        replay.py            # Replay engine with threaded playback
    models/
        bar.py               # Bar dataclass
        spread.py            # CreditSpread, OptionLeg models
        trade.py             # Trade record, TradeStatus enum
    utils/
        app_paths.py         # Cross-platform path resolution
        logging.py           # Structured logging with credential masking
        metrics.py           # Prometheus metrics (counters, gauges, histograms)
        notifications.py     # Slack, Discord, email, SMS, webhook notifications
        version.py           # Version management and update checks

dashboard/
    app.py                   # Flask REST API and dashboard server
    templates/
        index.html           # Overview + chart + signal log + analytics
        settings.html        # Configuration UI
        setup.html           # Initial setup wizard

config/
    strategy_params.yaml     # All strategy and risk parameters
    settings.py              # Environment and path configuration

database/
    db_manager.py            # SQLite operations (WAL mode, migrations)
    schema.sql               # Database schema
    migrations/              # Auto-applied schema migrations
    demo_recordings/         # Pre-built demo JSONL files

scripts/
    generate_demo_recording.py  # Fabricate demo scenarios
    run_dashboard.py            # Run dashboard standalone
    run_dry_run.py              # Quick-start dry-run mode
    validate_schwab.py          # Schwab credential validation
    health_check.py             # Health check script
    view_trades.py              # Trade database viewer

build/
    build_windows.py         # PyInstaller build script (Windows)
    build_linux.py           # PyInstaller build script (Linux)
    build_macos.py           # py2app build script (macOS)

.github/
    workflows/
        ci.yml               # CI pipeline: lint, test (3.11-3.13), security

tests/                       # 557 unit tests

app_desktop.py               # Desktop app entry point (pywebview + Flask)
```

---

## Testing

557 tests covering:
- Strategy logic (pulse detection, breakout confirmation, setup windows)
- Multi-strategy backtest engine (DI, TNT, ORB, B&B parallel execution)
- Position management (sizing, P&L calculation, exit triggers)
- Risk gates (circuit breaker, position limits, credit quality, drawdown)
- PDT compliance tracking and entry gating
- Bar building and market state management
- Consecutive loss tracking and streak counters
- VIX data provider multi-source fallback chains
- Daily journal persistence and API endpoints
- Notification delivery (Slack, Discord, email, webhook)
- Demo mode (recording, replay, Flask integration)
- Monitoring and observability (Prometheus metrics, health endpoint)
- Schwab and E*TRADE broker integration
- Slippage tracking and database migrations

```bash
python -m pytest tests/ -v
```

CI runs on every push and pull request to `main`, testing Python 3.11, 3.12, and 3.13. See `.github/workflows/ci.yml`.

---

## Backtesting

Run a historical backtest with automatic data download from Yahoo Finance:

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

Produces a Markdown report with:
- Total return, annualized return, Sharpe ratio, Sortino ratio, Calmar ratio
- Max drawdown (dollar and percent), profit factor, expectancy per trade
- Win rate, average win/loss, max consecutive wins/losses
- Equity curve and monthly return breakdown

SPX and VIX data are downloaded automatically from Yahoo Finance on first run. Pass `--csv` and `--vix-csv` to use existing data files.

---

## Demo Mode

Generate synthetic demo recordings and replay them through the dashboard:

```bash
# Generate all three demo scenarios
python scripts/generate_demo_recording.py --all

# Launch the desktop app in demo mode (uses latest recording)
python app_desktop.py --demo

# Or specify a recording file
python app_desktop.py --demo database/demo_recordings/demo_winning.jsonl
```

Demo mode replays recorded events at accelerated speed through the standard dashboard. The UI is identical to live trading: same charts, signals, trades, and risk displays. Use the on-screen controls to play/pause, change speed (1x-60x), seek, or jump to the next event.

Three pre-built scenarios are included:
- **Winning day**: Bullish pulse, breakout confirmed, put spread entered, 80% profit target hit (+$340)
- **Losing day**: Bullish pulse, breakout, reversal, stop loss hit (-$160)
- **No-setup day**: Narrow range, no pulse detected, all windows expire ($0)

---

## Dry-Run Results

Validation over 6 trading days (Feb 3-13, 2026) on a $50,000 simulated account:

| Metric | Value |
|--------|-------|
| Trading Days | 6 |
| Trades | 6 (5W / 1L) |
| Win Rate | 83.3% |
| Total P&L | +$3,915 |
| Return | +7.83% |
| Avg Win | +$918 |
| Avg Loss | -$675 |

These are simulated results using real-time SPX market data with Black-Scholes modeled option pricing. Live results will differ based on actual fills, slippage, and market conditions.

---

## Disclaimer

This is a personal project built for educational purposes. It is not financial advice.

- Options trading involves significant risk of loss, including the possibility of losing more than the initial investment
- Past performance, whether simulated or live, does not guarantee future results
- This software is provided as-is with no warranty
- Always validate thoroughly in dry-run mode before risking real capital

---

## License

MIT License - See [LICENSE](LICENSE) for details.
