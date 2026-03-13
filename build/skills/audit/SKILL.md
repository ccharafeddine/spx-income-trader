---
name: trading-bot-audit
description: Comprehensive security, logic, analytics, and strategy audit for The Daily Melt SPX options trading bot. Use when you need to verify strategies are working, settings propagate correctly, credentials are secure, analytics are accurate, the backtest is realistic, and the system is production-ready. Run this before going live, after major changes, or periodically for maintenance.
---

# Trading Bot Audit Skill

A systematic audit framework for The Daily Melt automated SPX options trading system. Covers nine domains: strategy correctness, settings propagation, security, data integrity, backtest realism, desktop app / demo mode, analytics & reporting, price feed architecture, and risk management. The dashboard has five tabs: Overview, Trade Journal, Analytics, Backtest, and Logs. Economic calendar events (FOMC, CPI, NFP, GDP, PCE) appear as badges on Trade Journal calendar day cells and in the Overview left panel.

## When To Use

- Before enabling live trading with real money
- After implementing new features or strategies
- After changing broker integrations
- Periodically (monthly) as a maintenance check
- When strategies appear to not be triggering
- When settings changes don't seem to take effect
- After backtest model changes (slippage, pricing, calendar)
- After adding or modifying analytics panels or calculations
- After changing risk metric formulas or P&L attribution logic
- After adding new broker integrations

## Audit Structure

The audit has nine parts that should always run in this order. Parts can be run independently if only a specific concern exists, but a full audit runs all nine.

---

## PART 1: Strategy Logic Audit

**Goal:** Verify every strategy can detect setups, trigger entries, manage positions, and exit correctly in all three modes (live, dry-run, backtest).

### 1A. Strategy Inventory

Identify all strategy classes in the codebase. For The Daily Melt, expect:
- **Daily Income (DI):** 0DTE pulse bar breakout credit spreads (src/core/strategy.py)
  - "30-min bars, NO-Indicators" — no technical indicator filtering on entries
  - Bollinger Band data tracked for analytics only (bb_agreement field), never gates entries
  - Morning bias filter (di_morning_bias_filter): blocks bearish entries on Up/Strong Up days
    - Based on gap/open direction at bar formation time — NOT a technical indicator
    - Configurable boolean in strategy config, defaults to True
    - Logs skip reason when blocked; tracked in analytics as "Direction Filter (Up Day)"
  - Two-phase entry: pulse detection → breakout confirmation
- **Tag 'n Turn (TNT):** Bollinger Band mean reversion, 3-7 DTE (src/core/tag_n_turn.py)
  - BB IS core to TNT (mean reversion requires BB tags)
  - Weekend hold prevention: profitable TNT positions (≥ configured threshold, default 30% of max profit)
    exited automatically after 3:00 PM ET on Fridays to avoid weekend gamma risk
  - Threshold and cutoff time are configurable; enabled/disabled via config flag
  - Target/stop exits take priority over weekend gate
- **B&B / Bed & Breakfast:** EOD signal enhancer (src/core/bnb_strategy.py)
  - Does NOT enter trades independently — provides directional confluence for DI
  - Scans 15:00-16:00 for pulse bars, generates overnight bias signal
  - get_bias() returns 'bullish'/'bearish'/None
  - validate_signal() checks gap invalidation
  - Does NOT consume 0DTE position slots
  - Enabled: false by default (experimental)
- **ORB / Opening Range Breakout:** Strong first-bar breakout (src/core/orb_strategy.py)
  - Strong signals only: close_position_pct in top/bottom 10%
  - Minimum range filter: 8.0 points
  - Confirmation delay: 3 minutes (breakout must hold)
  - Enabled: false by default (experimental)

For each strategy file, document:
```
Strategy: [name]
File: [path]
Entry conditions: [exact logic with thresholds]
Exit conditions: [profit target, max loss, time-based, expiration]
Position sizing: [how contracts are determined]
DTE range: [0DTE, 3-7 DTE, etc.]
Risk gates that apply: [circuit breaker, position limits, etc.]
Independent trader: [YES/NO — B&B is NO]
```

### 1B. Signal Path Trace

For EACH strategy, trace the complete signal lifecycle:

1. **Detection:** What market condition creates a setup?
2. **Confirmation:** What confirms the setup into a trade?
   - Breakout trigger? Time window? Next bar confirmation?
   - For ORB: does the 3-minute confirmation delay work?
3. **Entry:** How does the trade get placed?
   - Spread construction (strikes, width, credit)
   - Is bb_agreement recorded on the trade? (analytics-only, must NOT gate)
4. **Management:** How is the position monitored?
   - Profit target check (default 80%)
   - PDT-aware 1pm management (see 1F below)
   - TNT weekend hold prevention (see 1I below)
5. **Exit:** How does the trade close?
   - Exit reason normalization (category string, not verbose detail)

### 1C. Backtest Engine Strategy Coverage

Check src/backtest/engine.py:
- Are ALL strategies imported and instantiated?
- Does the backtest handle multi-day trades (TNT 3-7 DTE)?
- Does the backtest handle B&B as signal enhancer (bias tracking, not trade entry)?
- Does the NYSE half-day calendar filter bars correctly?
- Is bb_agreement populated on backtest trades?
- Is exit_reason normalized before storage?
- Is PDT mode applied based on starting_capital vs pdt_threshold?
- Is TNT weekend hold prevention applied in backtest (current_time parameter)?

**Morning bias filter in backtest engine (CRITICAL):**
- src/backtest/engine.py must apply di_morning_bias_filter the same way src/main.py does
- After fix: Direction Drill-Down Up Day Bearish count should be near zero

**Backtest UI defaults (enforced):**
- Daily Income: checked by default
- Tag 'n Turn: checked by default
- B&B: UNCHECKED by default, labeled "(Experimental)" in amber text
- ORB: UNCHECKED by default, labeled "(Experimental)" in amber text

### 1D. Parallel Execution in Live/Dry-Run Mode

Check src/main.py main loop:
- Are all enabled strategies evaluated each cycle?
- Position limit enforcement: max 1 swing (TNT), max 1 0DTE (DI/ORB), max 2 total
- B&B does NOT consume a position slot
- Can TNT and DI hold positions simultaneously?

### 1E. Create Targeted Tests

For any strategy that shows zero trades in backtest or dry-run:
- Create a unit test with fabricated bar data that SHOULD trigger the strategy
- If the test fails, the strategy logic is broken
- If the test passes, the integration with the engine/main loop is broken

### 1F. PDT-Aware 1PM Management

**Critical:** The 1pm auto-close is a PDT management tool, NOT a core strategy feature.

Verify:
- **PDT mode OFF (>= $25k):** 1pm check returns early with no action
- **PDT mode ON (< $25k):** Day trade counter is active, 1pm management rules apply
- **Config:** Settings live under pdt: section
  - monitoring.enable_1pm_check MUST NOT EXIST (removed)
  - pdt.enable_1pm_management EXISTS

### 1G. Bollinger Band Filter Verification

**Critical:** BB must NOT gate DI entries.

Verify:
- src/core/strategy.py evaluate_setup() does NOT check BB position/bias
- bb_agreement field IS populated on trades (analytics only)
- Tag 'n Turn BB logic is UNCHANGED (BB is core to TNT)

```bash
grep -n "bollinger\|bb_bias\|bb_direction" src/core/strategy.py
# Should only find analytics tracking, never conditional entry logic
```

### 1H. Morning Bias Filter Verification

**Critical:** Bearish DI entries must be blocked on Up/Strong Up market days.

Verify:
- Live path (src/main.py) blocks bearish DI on Up/Strong Up days
- Backtest engine (src/backtest/engine.py) applies the same filter
- Canary test: run 2019-2025 backtest, Direction Drill-Down shows near-zero Up Day Bearish trades

### 1I. TNT Weekend Hold Prevention

Verify:
- TNT checks Friday 3:00 PM ET gate when deciding whether to exit at profit
- Configurable threshold (default 30% of max profit): positions above threshold are exited
- Configurable cutoff time: default 15:00 ET
- Configurable enabled/disabled flag
- Target/stop exits still take priority — the weekend gate only activates if no normal exit triggered
- Backtest engine passes current_time to TNT evaluation so backtest matches live behavior
- Exit reason for weekend gate exits normalizes to "weekend_hold_prevention"

---

## PART 2: Settings Propagation Audit

**Goal:** Verify every setting in the UI actually controls the behavior it claims to control.

### 2A. Settings API Verification

Check the settings save/load cycle:
- GET /api/settings returns current values from strategy_params.yaml
- POST /api/settings writes changes back to strategy_params.yaml
- ALLOWED_SETTINGS_PATHS allowlist prevents arbitrary key injection (includes `developer_mode`)
- Sensitive values are redacted in GET response via _redact_secrets()

### 2B. Critical Settings That Must Hot-Reload

| Setting | Expected Effect | How to Verify |
|---------|----------------|---------------|
| max_daily_loss_pct | Changes daily circuit breaker limit | Check PortfolioManager.max_daily_loss reloads |
| drawdown_limits.weekly.max_loss_pct | Changes weekly circuit breaker limit | Check DrawdownManager.reload_config() called on save |
| drawdown_limits.monthly.max_loss_pct | Changes monthly circuit breaker limit | Check DrawdownManager.reload_config() called on save |
| profit_target_pct | Changes exit threshold | Bot reads on next position check |
| max_contracts | Changes position sizing cap | Next trade uses new cap |
| tnt.weekend_hold_prevention.enabled | Enables/disables Friday gate | TNT checks config each evaluation |
| tnt.weekend_hold_prevention.profit_threshold_pct | Changes Friday exit threshold | TNT reads on next evaluation |
| notifications.discord.webhook_url | Changes where notifications go | Next notification uses new URL |
| developer_mode | Hides/shows Backtest tab, Logs tab, Analytics backtest data source dropdown | Dashboard reads via /api/settings on page load |

**Developer Mode:** Top-level `developer_mode` setting in runtime_settings.json, included in ALLOWED_SETTINGS_PATHS. Toggled via Account > Trading Settings > EXPERIMENTAL. When off: Backtest tab hidden, Logs tab hidden, Analytics backtest data source dropdown hidden.

**Critical:** DrawdownManager.reload_config() must be called from the settings save handler in app.py whenever drawdown limit percentages change. If not called, the dashboard shows new limits but the engine enforces old ones.

### 2C. Removed Settings Verification

These settings MUST NOT exist in the codebase:
- monitoring.enable_1pm_check (moved to pdt.enable_1pm_management)
- monitoring.auto_1pm_close (removed)
- monitoring.trending_threshold (moved to pdt.trending_threshold)

```bash
grep -rn "monitoring.*enable_1pm\|monitoring.*auto_1pm\|monitoring.*trending" src/ config/
```

### 2D. Hot-Reload Verification

Flag any settings that:
- Cache the value at startup and never re-read
- Read from a module-level constant instead of the live config
- Use a stale reference after settings_changed fires

---

## PART 3: Security Audit

**Goal:** Ensure no credentials are exposed, the application is safe from common web vulnerabilities, and data is protected.

### 3A. Credential Handling

```bash
git log --all -p | grep -iE "(consumer_key|consumer_secret|app_key|app_secret|webhook_url)" | head -40
```

- Verify only variable NAMES appear, never actual key VALUES
- Verify .gitignore covers: .env*, *.db, database/*.json, tokens/, logs/
- Verify keyring is the primary credential storage mechanism

### 3B. Web Security

- Flask bound to 127.0.0.1 (not 0.0.0.0)?
- CSRF: Per-session API_TOKEN via secrets.token_hex, validated on all POST/PUT/DELETE
- CSRF-exempt routes: Only /setup and /auth/etrade/ (OAuth callbacks)
- Settings allowlist: ALLOWED_SETTINGS_PATHS prevents arbitrary key injection
- Settings redaction: GET /api/settings masks sensitive values via _redact_secrets()

### 3C. File Permissions

- Token files (schwab_token.json, etrade token): chmod 0o600?
- Log files: No account numbers, API keys, or tokens in output?

### 3D. Singleton Instance Lock

- Only one app instance can run at a time (app_desktop.py)
- Lock file at BASE_DIR/.app.lock stores PID and mode (live/dry-run)
- Live mode takes priority: evicts any running dry-run instance
- Auto-releases on crash via Windows file lock (msvcrt)
- Verify: launching a second instance while one is running is blocked (or evicts dry-run if new instance is live)

---

## PART 4: Data Integrity Audit

**Goal:** Verify data is stored safely, survives crashes, and displays correctly.

### 4A. Database Safety

- SQLite WAL mode enabled? (PRAGMA journal_mode=WAL)
- Crash recovery: If bot crashes with open positions, does restart find them?
- Daily counter restoration: Does _restore_daily_counters work on mid-day restart?
- Correct database used: dry-run reads/writes trades_dryrun.db, live reads/writes trades_live.db
- `save_trade_with_retry()`: 5 attempts with delays (0.2, 0.5, 1.0, 2.0, 4.0 seconds)
- `busy_timeout` on bot DB connections: 10000ms
- Dashboard expire-trade writes in `classify_trades()` use a separate short-lived connection via `_open_db()` instead of the main request connection (avoids holding write locks that block the bot)
- DB failure Discord alerts are deduplicated: only one alert per cooldown period via `_db_alert_sent` flag on PositionManager

### 4B. State File Integrity

- **portfolio_state.json:** Contains daily_realized_pnl, circuit_breaker_triggered
  - Written by PortfolioManager._save_state() after every trade close and daily reset
  - daily_realized_pnl resets to 0 on reset_daily() each morning
- **drawdown_state.json:** Contains weekly/monthly realized P&L, period markers, breaker flags
  - Written by DrawdownManager._save_state() after every record_realized_pnl() call
  - Schema must include: current_date (str), current_iso_week (int), current_iso_year (int),
    current_month (int), current_year (int), daily/weekly/monthly_realized_pnl (float),
    daily/weekly/monthly_breaker_triggered (bool), consecutive_losses (int), consec_pause_until (str|null)
  - All integers must be stored as int, not as date strings (prevents comparison failures)
- **Backfill on startup:** DrawdownManager._backfill_from_db() queries trades DB on init
  if state is stale or monthly_realized_pnl == 0 (mid-period state reset detection)

### 4C. Signal & Trade Logging

- Signal log rotation at configured limit (5000 entries)?
- Atomic writes (tempfile.mkstemp + os.replace pattern)?
- Exit reasons normalized to category strings?
- weekend_hold_prevention included in exit reason normalizer?

### 4D. Display Accuracy

- Dashboard P&L matches database records?
- Net P&L (gross - commissions) computed consistently across all views: `_compute_exit_analysis()`, `api_journal_calendar()`, Trade Journal, Analytics
- Calendar view shows correct trade/no-trade days?
- Risk Status bars read from correct state files (daily from portfolio_state.json, weekly/monthly from drawdown_state.json)?
- W/L streak query excludes pnl=0 (scratch trades don't break streaks)?
- TNT LED shows Active when DB has open TNT position, regardless of bot state?

---

## PART 5: Backtest Realism Audit

**Goal:** Verify the backtest engine produces realistic results that would translate to live trading.

### 5A. Slippage Model

- VIX-aware slippage tiers implemented ($0.02–$0.04/leg depending on regime)?
- Flat slippage backward compatible (single value in config still works)?
- Unusual credits (outside $1.50–$3.50) are flagged, not rejected?

### 5B. Calendar & Timing

- NYSE half-day calendar: shortened windows on half-days?
- Half-day settlement: uses 1PM close, not 4PM?
- Weekend exclusion: no bars generated on Saturday/Sunday?

### 5C. PDT-Aware Backtesting

- starting_capital < pdt_threshold activates PDT mode in backtest?
- At $50k starting capital: NO 1pm auto-closes?
- At $20k starting capital: HAS 1pm auto-closes?

### 5D. TNT Weekend Prevention in Backtest

- Backtest engine passes current_time (bar timestamp) to TNT evaluation?
- Friday bars after 3PM trigger weekend prevention check?
- Backtest exit_reason shows "weekend_hold_prevention" for these exits?

### 5E. Position Sizing Realism

- Backtest uses same sizing formula as live engine?
- Compound growth modeled correctly (account_size grows with P&L)?
- Max contracts cap applied identically in both engines?

---

## PART 6: Desktop App & Demo Mode Audit

**Goal:** Verify the packaged application launches correctly, system tray works, and demo playback is isolated.

### 6A. Multi-Broker Dashboard Controls

- IBKR connect/disconnect button visible when IBKR is active broker?
- Schwab auth status shows token expiry correctly?
- E*TRADE OAuth flow accessible from dashboard?
- Broker switch in settings takes effect without full restart?

### 6B. Demo/Replay Mode

- Demo banner appears at top of dashboard?
- Play/pause/speed controls work?
- Demo mode does NOT write to real database?
- Bar model compatibility: getattr(bar, 'tick_count', 0) used in recorder hooks?

### 6C. Singleton Instance Lock

- app_desktop.py enforces single-instance via BASE_DIR/.app.lock
- Lock file contains PID and mode (live/dry-run)
- Live mode evicts running dry-run instances; dry-run cannot evict live
- Windows file lock via msvcrt auto-releases on crash
- Verify: start dry-run, then start live — dry-run should be evicted

### 6D. Developer Mode Toggle

- Account > Trading Settings > EXPERIMENTAL section
- When off: Backtest tab, Logs tab, and Analytics backtest data source dropdown are hidden
- Setting: `developer_mode` (top-level) in runtime_settings.json
- Dashboard reads setting via /api/settings on page load

### 6E. Build System

- **Windows:** PyInstaller via build/build_windows.py, output in dist/The Daily Melt/
- **macOS:** py2app via build/build_macos.py, output in dist/The Daily Melt.app/
- **Python version:** MUST use 3.13 (pythonnet incompatible with 3.14)
- Packaged app uses platformdirs DATA_DIR (not project root) for database, config, logs
- Headless mode (--headless) launches bot without UI?

---

## PART 7: Analytics & Reporting Audit

**Goal:** Verify all analytics calculations are mathematically correct, handle edge cases safely, and display accurate results.

### 7A. Portfolio Greeks

- Black-Scholes Greeks calculated correctly (delta, gamma, theta, vega, rho)?
- Spread-level netting: short leg − long leg?
- Portfolio aggregation across all open positions?
- Dashboard panel shows net delta, gamma, theta/hour in dollars, vega?

### 7B. Risk Metrics

- Calmar Ratio: annualized return / max drawdown?
- VaR at 95% and 99% confidence computed from daily P&L distribution?
- CVaR: average of worst 5%/1% of days?
- Tail Ratio: top 5% wins vs bottom 5% losses?
- Minimum data gate: N/A returned with fewer than 20 trades?
- Streak calculation uses daily P&L series (not trade-level consecutive):
  - Groups trades by calendar day, sums daily P&L
  - $0 P&L days are neutral — do NOT break a streak

### 7C. P&L Attribution

- theta_pnl = net_theta * hours_held / 6.5
- delta_pnl = net_delta * (exit_spx - entry_spx) * quantity
- vega_pnl = net_vega * (exit_iv - entry_iv) * quantity
- residual = actual_pnl - theta_pnl - delta_pnl - vega_pnl
- Null SPX/VIX inputs handled safely (returns N/A, not crash)
- Auto-generated interpretation identifies dominant component

### 7D. Execution Quality

- Slippage: theoretical_credit - actual_credit per trade
- Breakdown by time bucket and VIX regime
- Exit reason normalization maps to ~10 categories:
  - "weekend_hold_prevention" → "Weekend Hold Prevention" (new)
  - All existing normalizations preserved
- Click-to-expand drill-down shows individual trades per category

### 7E. Regime Analysis

- VIX regime performance: Calm (<15), Normal (15–20), Elevated (20–30), Crisis (>30)
- Market direction: Strong Down, Down, Flat, Up, Strong Up
- SPX correlation and beta with color-coded indicator

### 7F. Tear Sheet PDF Export

- Period filter applied to ALL sections (not just summary)?
- Equity curve uses rolling equity (not fixed initial capital)?
- Monthly return percentages use rolling equity denominator?
- Currency abbreviated format: $30.5M, $450K (not $30,500,000)?
- "P&L" renders without double-escaping in PDF?
- Strategy abbreviations in trade log: DI, TNT, ORB, B&B?

### 7G. Trade History & Performance Panel

- Syncs with Trade Journal view: shows data for the calendar month being viewed or the list period dropdown
- Title updates dynamically to reflect the selected period
- Removed from periodic refreshAll() tasks (updates only on Trade Journal navigation)

### 7H. Economic Calendar Integration

- FRED API events (FOMC, CPI, NFP, GDP, PCE) appear as badges on Trade Journal calendar day cells
- Standalone Calendar tab removed (merged into Trade Journal)
- Today's events still shown in Overview left panel

### 7I. Chart Visualization

- Four timeframes render correctly: 4h, 1h, 30m, 5m?
- Bollinger Bands overlay on 1h and 4h only (not 30m/5m)?
- Scrollable history: 35 days (30m/5m), 90 days (1h), 180 days (4h)?
- Lazy loading: older bars fetch seamlessly on scroll-left, no viewport jump?
- Historical trade overlays (entry/exit markers, spread zones) appear across full scrollable range?
- Auto y-axis scales to visible candles; skips when open positions need strike line visibility?
- Timestamps stored as YYYY-MM-DD HH:MM (sorts correctly across year boundaries)?
- 5-minute bar aggregator feeds CHART ONLY, NOT strategy logic?

---

## PART 8: Price Feed Audit

**Goal:** Verify live trading uses broker-sourced price data and that failure handling is safe.

### 8A. Feed Source Verification

- Live mode: broker price feed (EtradePriceFeed or SchwabPriceFeed), NOT Yahoo?
- Dry-run mode: YahooPriceFeed?
- IBKR live mode: IBKR price feed via ib_insync snapshot data?
- Backtest: unaffected (uses historical bar data, not price feed)?

### 8B. Health Monitoring

- /api/status exposes price_feed.source, price_feed.healthy, price_feed.consecutive_failures?
- get_health_status() returns dict with: source, healthy, last_update_secs_ago, consecutive_failures?
- Stale cache fallback: on fetch failure, returns cached price with warning log (not None)?

### 8C. price_feed_state.json

- Written to database/ directory?
- source field matches active broker?
- Survives bot restart (loaded on init)?

---

## PART 9: Risk Management Audit

**Goal:** Verify all three circuit breakers are independently enforced, display correctly, and reset on the correct schedule.

### 9A. Multi-Timeframe Circuit Breaker Verification

Three independent breakers must each block new entries when triggered:

| Breaker | Limit Source | Reset Schedule | State File |
|---------|-------------|----------------|------------|
| Daily | portfolio.max_daily_loss_pct | Midnight ET daily | portfolio_state.json |
| Weekly | drawdown_limits.weekly.max_loss_pct | Monday 00:00 ET | drawdown_state.json |
| Monthly | drawdown_limits.monthly.max_loss_pct | 1st of month 00:00 ET | drawdown_state.json |

Verify for each breaker:
- Only REALIZED losses (closed trades, pnl < 0) count — not unrealized
- Winning trades do NOT reduce the accumulated loss counter
- When triggered, can_enter_position() returns False with specific reason string
- When one breaker is triggered, the other two continue tracking independently

### 9B. Period Reset Mechanism

- check_and_apply_resets(now) called at START of can_trade() (lazy reset)
- check_and_apply_resets(now) called at market open in main.py (proactive reset)
- Uses pytz America/New_York timezone for midnight/Monday boundary detection
- Idempotent: calling multiple times in the same period has no effect
- reset_daily() in PortfolioManager does NOT touch DrawdownManager weekly/monthly counters

### 9C. Startup Backfill

- DrawdownManager._backfill_from_db() runs on init if state is stale or zero
- Backfill trigger: stored period != current period OR monthly_realized_pnl == 0.0
- Queries trades DB for sum of losses (pnl < 0) since current period start
- Backfill skipped if state is valid and non-zero (real-time tracking is authoritative)
- db_path passed correctly from PortfolioManager to DrawdownManager

### 9D. Dashboard Risk Status Panel Verification

- Daily bar reads from portfolio_state.json (daily_realized_pnl key)
- Weekly bar reads from drawdown_state.json (weekly_realized_pnl key)
- Monthly bar reads from drawdown_state.json (monthly_realized_pnl key)
- Limits computed from same YAML keys as the engine (account_size × pct/100)
- Bar fill: 0% on winning/flat days; abs(loss)/limit % on loss days
- LED color: green (under limit), yellow (>50%), red (>80% or triggered)
- W/L streak: queries closed trades with pnl != 0 AND pnl IS NOT NULL
  - Scratch trades (pnl = 0) excluded from both query and loop logic
  - Active streak counts consecutive wins or losses from most recent trade
- TNT LED: reads data.account.positions from DB first — shows Active if open TNT position
  exists, regardless of bot state, market hours, or enabled flag

### 9E. Settings → Engine Alignment

- Max Daily Loss slider → portfolio.max_daily_loss_pct → PortfolioManager.max_daily_loss
- Max Weekly Loss slider → drawdown_limits.weekly.max_loss_pct → DrawdownManager.weekly_max_loss_pct
- Max Monthly Loss slider → drawdown_limits.monthly.max_loss_pct → DrawdownManager.monthly_max_loss_pct
- After settings save: DrawdownManager.reload_config() called so new limits take effect immediately

### 9F. Notification Verification

Verify all notification types fire correctly and contain accurate data:

| Notification | Required Fields |
|-------------|----------------|
| Bot startup | Mode, equity, open positions, ET time (not UTC) |
| Market open | SPX opening price, VIX + regime, carry-over positions |
| Hourly update | SPX price + session change %, VIX + regime, session H/L, bars built, pulse bars, open position details, next bar time |
| Trade entry | Strategy, direction, strikes, credit, quantity, breakeven, max risk, expiry |
| Trade exit | Strategy, direction, strikes, P&L, exit reason, hold duration |
| EOD summary | W/L count, daily/weekly/monthly P&L, SPX close + session change %, session H/L, equity, win rate, streak, no-trade reason with pulse bar count and VIX |
| Circuit breaker | Which breaker triggered (daily/weekly/monthly), loss amount vs limit |
| Short strike breach | Critical alert when SPX crosses short strike |

All timestamps in ET (America/New_York), 12-hour format with AM/PM.
Hourly updates color-coded: blue (flat/up), yellow (down >0.5%), red (down >1%).

**DB failure alert deduplication:** `_send_db_failure_alert()` checks `self._db_alert_sent` flag on PositionManager. Only one alert fires per cooldown period — subsequent failures within the same cooldown are suppressed. Verify the flag resets correctly after the cooldown expires.

---

## Audit Report Template

Document findings in docs/full_system_audit.md using this structure:

### Section 1: Strategy Status

| Strategy | Backtest Works | Dry-Run Works | Live Ready | Issues Found |
|----------|---------------|---------------|------------|--------------|
| Daily Income | PASS/FAIL | PASS/FAIL | PASS/FAIL | ... |
| Tag 'n Turn | PASS/FAIL | PASS/FAIL | PASS/FAIL | ... |
| B&B | PASS/FAIL | PASS/FAIL | N/A | ... |
| ORB | PASS/FAIL | PASS/FAIL | PASS/FAIL | ... |

### Section 2: Settings Propagation

| Setting | Saves Correctly | Hot-Reloads | Notes |
|---------|----------------|-------------|-------|
| S-1 | ... | ... | ... |

### Section 3: Security

| Check | Status | Notes |
|-------|--------|-------|
| Credentials not in git | PASS/FAIL | ... |
| Flask bound to localhost | PASS/FAIL | ... |
| CSRF protection | PASS/FAIL | ... |
| Singleton instance lock | PASS/FAIL | ... |

### Section 4: Data Integrity

| Check | Status | Notes |
|-------|--------|-------|
| WAL mode | PASS/FAIL | ... |
| portfolio_state.json schema | PASS/FAIL | ... |
| drawdown_state.json schema | PASS/FAIL | ... |
| Backfill on startup | PASS/FAIL | ... |
| save_trade_with_retry (5 attempts) | PASS/FAIL | ... |
| busy_timeout 10000ms | PASS/FAIL | ... |
| classify_trades separate DB conn | PASS/FAIL | ... |
| DB alert deduplication | PASS/FAIL | ... |
| Net P&L consistency across views | PASS/FAIL | ... |

### Section 5: Backtest Realism

| Check | Status | Notes |
|-------|--------|-------|
| VIX-aware slippage | PASS/FAIL | ... |
| Half-day calendar | PASS/FAIL | ... |
| PDT-aware backtesting | PASS/FAIL | ... |
| TNT weekend prevention in backtest | PASS/FAIL | ... |

### Section 6: Desktop App & Demo

| Check | Status | Notes |
|-------|--------|-------|
| Windows build + launch | PASS/FAIL | ... |
| System tray icon | PASS/FAIL | ... |
| Demo mode isolation | PASS/FAIL | ... |
| Headless mode | PASS/FAIL | ... |
| Singleton instance lock | PASS/FAIL | ... |
| Developer mode toggle | PASS/FAIL | ... |

### Section 7: Analytics & Reporting

| Component | Math Correct | Edge Cases | Dashboard Renders | Tests Pass |
|-----------|-------------|------------|-------------------|------------|
| Greeks | PASS/FAIL | PASS/FAIL | PASS/FAIL | PASS/FAIL |
| Risk Metrics | PASS/FAIL | PASS/FAIL | PASS/FAIL | PASS/FAIL |
| P&L Attribution | PASS/FAIL | PASS/FAIL | PASS/FAIL | PASS/FAIL |
| Execution Quality | PASS/FAIL | PASS/FAIL | PASS/FAIL | PASS/FAIL |
| Regime Analysis | PASS/FAIL | PASS/FAIL | PASS/FAIL | PASS/FAIL |
| Tear Sheet PDF | PASS/FAIL | PASS/FAIL | N/A (download) | PASS/FAIL |
| Chart (4 timeframes) | N/A | N/A | PASS/FAIL | PASS/FAIL |
| Chart (scrollable/lazy) | N/A | N/A | PASS/FAIL | PASS/FAIL |
| Chart (Bollinger Bands) | N/A | N/A | PASS/FAIL | PASS/FAIL |
| Trade History & Perf sync | N/A | N/A | PASS/FAIL | PASS/FAIL |
| Economic calendar badges | N/A | N/A | PASS/FAIL | PASS/FAIL |

### Section 8: Price Feed

| Check | Status | Notes |
|-------|--------|-------|
| Live mode uses broker feed | PASS/FAIL | ... |
| Dry-run uses Yahoo | PASS/FAIL | ... |
| Health monitoring in /api/status | PASS/FAIL | ... |
| Graceful degradation | PASS/FAIL | ... |

### Section 9: Risk Management

| Check | Status | Notes |
|-------|--------|-------|
| Daily circuit breaker blocks entries | PASS/FAIL | ... |
| Weekly circuit breaker blocks entries | PASS/FAIL | ... |
| Monthly circuit breaker blocks entries | PASS/FAIL | ... |
| Period resets fire on correct schedule | PASS/FAIL | ... |
| Startup backfill from DB | PASS/FAIL | ... |
| Dashboard bars read correct state files | PASS/FAIL | ... |
| Streak excludes $0 trades | PASS/FAIL | ... |
| TNT LED wired to DB positions | PASS/FAIL | ... |
| Settings sliders → engine alignment | PASS/FAIL | ... |
| Notifications contain correct data | PASS/FAIL | ... |
| DB failure alert deduplication | PASS/FAIL | ... |

### Section 10: Fixes Required

Ordered by severity:
- **CRITICAL:** Strategies not running, orders not placing, circuit breakers not blocking entries, morning bias filter missing from backtest engine, wrong DB path for streak query
- **HIGH:** Settings not propagating to engine, DrawdownManager not reloading on settings save, backfill not firing, period resets using wrong timezone
- **MEDIUM:** Display inaccuracies, missing error handling, realism gaps
- **LOW:** Code quality, documentation gaps

### Section 11: Fix Implementation

1. All CRITICAL fixes first
2. All HIGH fixes
3. Run full test suite after each fix (1,467+ tests expected as of Mar 2026)
4. Update the audit report with fix status

---

## Running the Audit

Feed this to your AI assistant:

```
Read the audit skill at build/skills/audit/SKILL.md and perform a full
9-part audit of the codebase. Document everything in
docs/full_system_audit.md first, then implement all CRITICAL and
HIGH fixes. Run the test suite after each fix.
```

For a strategy-only audit:

```
Read the audit skill at build/skills/audit/SKILL.md and perform only
PART 1 (Strategy Logic Audit). Focus on DI has no BB gating, 1pm
management is PDT-only, B&B is signal-only, morning bias filter
applies in BOTH live and backtest engine (section 1H), TNT weekend
hold prevention fires correctly on Fridays (section 1I), and
B&B/ORB default to unchecked with Experimental labels in backtest UI.
```

For a risk management audit:

```
Read the audit skill at build/skills/audit/SKILL.md and perform only
PART 9 (Risk Management Audit). Verify all three circuit breakers
independently block entries, period resets fire on correct ET schedule,
startup backfill reconstructs losses from DB, dashboard bars and
streaks reflect real data, and notification payloads contain correct
fields including ET timestamps.
```

For a security-only check:

```
Read the audit skill at build/skills/audit/SKILL.md and perform only
PART 3 (Security Audit). This is a pre-release security review.
```

For an analytics audit:

```
Read the audit skill at build/skills/audit/SKILL.md and perform only
PART 7 (Analytics & Reporting Audit). Verify calculations are correct,
all four chart timeframes render with correct data, Bollinger Bands
show on 1h/4h only, scrollable lazy-loading history works, tear sheet
period filter applies to all sections, and weekend_hold_prevention
appears in exit reason normalizer.
```

For a price feed check:

```
Read the audit skill at build/skills/audit/SKILL.md and perform only
PART 8 (Price Feed Audit). Verify live uses broker feed, dry-run uses
Yahoo, IBKR live uses IBKR snapshot data, health monitoring in
/api/status, and graceful degradation on failure.
```

For a desktop app check:

```
Read the audit skill at build/skills/audit/SKILL.md and perform only
PART 6 (Desktop App & Demo Mode Audit). Verify build, tray, headless
mode, and demo playback isolation.
```
