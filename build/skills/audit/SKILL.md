---
name: trading-bot-audit
description: Comprehensive security, logic, analytics, and strategy audit for The Daily Melt SPX options trading bot. Use when you need to verify strategies are working, settings propagate correctly, credentials are secure, analytics are accurate, the backtest is realistic, and the system is production-ready. Run this before going live, after major changes, or periodically for maintenance.
---

# Trading Bot Audit Skill

A systematic audit framework for The Daily Melt automated SPX options trading system. Covers eight domains: strategy correctness, settings propagation, security, data integrity, backtest realism, desktop app / demo mode, analytics & reporting, and price feed architecture.

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

## Audit Structure

The audit has eight parts that should always run in this order. Parts can be run independently if only a specific concern exists, but a full audit runs all eight.

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
   - Find the method that scans for setups (e.g., check_for_setups, evaluate)
   - What inputs does it need? (bars, price, Bollinger Bands, etc.)
   - What thresholds must be met?

2. **Confirmation:** What confirms the setup into a trade?
   - Breakout trigger? Time window? Next bar confirmation?
   - Where is the pending setup stored?
   - What causes it to expire unused?
   - For ORB: does the 3-minute confirmation delay work?

3. **Entry:** How does the trade get placed?
   - Spread construction (strikes, width, credit)
   - Order type (CREDIT, LIMIT)
   - Broker method called
   - Is bb_agreement recorded on the trade? (analytics-only, must NOT gate)
   - Is B&B confluence logged? (informational, must NOT block)

4. **Management:** How is the position monitored?
   - Profit target check (default 80%)
   - Max loss check
   - PDT-aware 1pm management (see 1F below)

5. **Exit:** How does the trade close?
   - Close order construction
   - Expiration handling (4PM for 0DTE, 1PM for half-days, actual DTE for swings)
   - Exit reason normalization (category string, not verbose detail)

### 1C. Backtest Engine Strategy Coverage

Check src/backtest/engine.py specifically:
- Are ALL strategies imported and instantiated?
- Are all strategy evaluate/check methods called each bar?
- Does the backtest handle multi-day trades (TNT 3-7 DTE)?
- Does the backtest handle B&B as signal enhancer (bias tracking, not trade entry)?
- Does the backtest results report include per-strategy breakdown?
- Does the NYSE half-day calendar filter bars correctly on half-days?
- Does expiration use the correct close time (1PM vs 4PM)?
- Is bb_agreement populated on backtest trades?
- Is exit_reason normalized before storage (exit_detail has verbose text)?
- Is PDT mode applied based on starting_capital vs pdt_threshold?

**Test:** Run a backtest over 1+ years and check:
- DI and TNT produce trades
- B&B produces zero trades (it's a signal enhancer, not a trader)
- ORB produces zero trades if disabled (default), valid trades if enabled with strong signals
- Backtest at $50k starting capital has NO 1pm auto-closes (above PDT threshold)
- Backtest at $20k starting capital HAS 1pm auto-closes (below PDT threshold)

**Morning bias filter in backtest engine (CRITICAL):**
- src/backtest/engine.py must apply di_morning_bias_filter the same way src/main.py does
- If filter=True, no bearish DI setups should be created on Up/Strong Up days in the backtest
- Verify by running 2019-2025 backtest and checking Direction Drill-Down: Up Day Bearish count should be near zero
- Known bug history: the filter was initially implemented only in the live path (main.py) and missed in the backtest engine — the Direction Drill-Down showing 177 Up Day Bearish trades at 7.9% WR is the canary that reveals the filter is not applied
- After fix: trade count drops ~177 (1,696 → ~1,519), overall win rate and DI win rate improve meaningfully

**Backtest UI defaults (enforced):**
- Daily Income: checked by default
- Tag 'n Turn: checked by default
- B&B: UNCHECKED by default, labeled "(Experimental)" in amber text
- ORB: UNCHECKED by default, labeled "(Experimental)" in amber text

### 1D. Parallel Execution in Live/Dry-Run Mode

Check src/main.py main loop:
- Are all enabled strategies evaluated each cycle?
- Does portfolio_manager route signals from all strategies?
- Position limit enforcement: max 1 swing (TNT), max 1 0DTE (DI/ORB), max 2 total
- B&B does NOT consume a position slot (verify it never calls portfolio_manager.enter_trade)
- Can TNT and DI hold positions simultaneously?

### 1E. Create Targeted Tests

For any strategy that shows zero trades in backtest or dry-run:
- Create a unit test with fabricated bar data that SHOULD trigger the strategy
- If the test fails, the strategy logic is broken
- If the test passes, the integration with the engine/main loop is broken

### 1F. PDT-Aware 1PM Management

**Critical:** The 1pm auto-close is a PDT management tool, NOT a core strategy feature.

Verify:
- **PDT mode detection:** Account equity < $25,000 activates PDT mode
  - Live mode: reads equity from broker
  - Dry-run mode: uses starting_capital or force_pdt_mode config override
  - Null/error broker equity response fails safe (PDT ON, not OFF)
- **PDT mode OFF (>= $25k):**
  - 1pm check_1pm_management() returns early with no action
  - 1pm assessment is logged informationally but does NOT close positions
  - No day trade counting
  - Trades run to profit target or expiration only
- **PDT mode ON (< $25k):**
  - Day trade counter is active
  - 1pm management rules apply:
    - Trending (any direction): hold to expiration
    - Non-trending + favorable + >40% profit: close (uses day trade slot)
    - Non-trending + unfavorable + >10% loss: close (uses day trade slot)
  - Before closing, verify day trade slots are available
  - If no day trade slots available, hold to expiration regardless
- **Dashboard:** 1pm Management section replaced with read-only PDT status indicator
  - Shows "PDT Mode: ACTIVE" (amber) or "PDT Mode: OFF" (green)
  - NOT toggleable by the user
- **Config:** 1pm settings live under pdt: section, NOT monitoring:
  - monitoring.enable_1pm_check REMOVED
  - monitoring.auto_1pm_close REMOVED
  - monitoring.trending_threshold REMOVED
  - pdt.enable_1pm_management EXISTS
  - pdt.trending_threshold EXISTS

### 1G. Bollinger Band Filter Verification

**Critical:** BB must NOT gate DI entries.

Verify:
- src/core/strategy.py evaluate_setup() does NOT check BB position/bias
- src/core/strategy.py construct_spread() does NOT reject based on BB
- bb_agreement field IS populated on trades (True if BB agreed with direction)
- bb_agreement appears in analytics/trade records but NEVER in any if/else that gates entry
- Tag 'n Turn BB logic is UNCHANGED (BB is core to TNT)
- config/strategy_params.yaml: BB settings marked as "analytics only, not entry filter" for DI

```bash
# Quick check: search for BB gating in DI entry path
grep -n "bollinger\|bb_bias\|bb_direction" src/core/strategy.py
# Should only find analytics tracking, never conditional entry logic
```

### 1H. Morning Bias Filter Verification

**Critical:** Bearish DI entries must be blocked on Up/Strong Up market days. This is NOT a technical indicator — it is a directional alignment gate based on observed market bias at bar formation time.

Verify:
- src/core/strategy.py evaluate_setup() checks morning bias when direction == "bearish"
- Block condition: daily_move_pct > +0.25% (Up classification) or > +1.0% (Strong Up classification)
- Bullish entries are NEVER blocked by this filter regardless of morning bias
- Flat/Down/Strong Down days: bearish entries proceed normally
- Skip is logged clearly: e.g. "DI bearish setup skipped — morning bias is Up (SPX +0.42%)"
- Skip reason tracked in analytics as "Direction Filter (Up Day)"
- Filter uses the SAME classification thresholds as regime analysis (Part 7E) — single implementation, no duplicate logic
- config di_morning_bias_filter=False completely disables the filter in both backtest and live trading

```bash
# Quick check: morning bias filter only in DI, not other strategies
grep -n "morning_bias\|di_morning_bias" src/core/strategy.py         # should exist
grep -n "morning_bias\|di_morning_bias" src/core/tag_n_turn.py       # should be empty
grep -n "morning_bias\|di_morning_bias" src/core/orb_strategy.py     # should be empty
grep -n "morning_bias\|di_morning_bias" src/core/bnb_strategy.py     # should be empty
```

**Backtest verification:** Run backtest over 2019-2025 with di_morning_bias_filter=True:
- No bearish DI trades appear on days classified as "Up" or "Strong Up"
- All bullish DI trades on Up days still execute (filter is bearish-only)
- Comparing filter=True vs filter=False: ~177 fewer trades, meaningfully higher overall win rate
- Direction Drill-Down panel: Up Day Bearish count near zero when filter is active
- If Direction Drill-Down still shows 177 Up Day Bearish trades at 7.9% WR → the filter is NOT applied in backtest engine (known bug — fix required before baseline numbers are valid)

**Test cases to verify exist:**
```
TestMorningBiasBlocksBearishOnUpDay
TestMorningBiasBlocksBearishOnStrongUpDay
TestMorningBiasBearishProceedsOnFlatDay
TestMorningBiasBearishProceedsOnDownDay
TestMorningBiasBearishProceedsOnStrongDownDay
TestMorningBiasBullishAlwaysProceeds
TestMorningBiasSkipLogged
TestMorningBiasDisabledViaConfig
```

---

## PART 2: Settings Propagation Audit

**Goal:** Verify every user-configurable setting actually affects bot behavior.

### 2A. Settings Inventory

Map every setting in strategy_params.yaml and the Settings dashboard page. For each:

```
Setting: [name]
YAML path: [e.g., portfolio.position_sizing.max_contracts]
UI location: [Settings page panel name, or N/A]
Read by: [file:line where the value is consumed]
Hot-reloadable: [YES if .settings_changed flag is checked / NO if read once at startup]
Verified working: [YES/NO with evidence]
```

### 2B. Critical Settings to Verify

These MUST work correctly for safe live trading:

**Trading Mode:**
- dry-run -> live: Does the broker switch from DryRunBroker to real broker?
- live -> dry-run: Does it switch back?
- Is restart required? Document clearly.

**Active Broker:**
- Switching between schwab/etrade/dry_run: Does broker_factory create the correct class?
- Restart required? Document.

**Strategy Parameters (hot-reload critical):**
- pulse_threshold: Change value -> does PulseDetector use new value on next bar?
- spread_width: Change -> does construct_spread use it?
- profit_target: Change -> does position_manager use it for next exit check?
- setup_start_time / setup_end_time: Do they actually gate setup creation?

**Risk Parameters:**
- max_daily_loss_pct: Change -> does circuit breaker use new threshold?
- weekly_drawdown_limit: Is it checked? Where?
- monthly_drawdown_limit: Is it checked? Where?
- max_consecutive_losses: Is count tracked? Does pause activate?
- max_contracts: Change -> does next trade use new value?

**PDT Configuration:**
- pdt_threshold: Change -> does PDT mode activation threshold update?
- force_pdt_mode: Does override work in dry-run?
- enable_1pm_management: Does toggling under pdt: section enable/disable 1pm checks?
- trending_threshold: Under pdt: section, does it affect the $15 move classification?

**Morning Bias Filter:**
- di_morning_bias_filter: Change to False -> does DI stop blocking bearish entries on Up days?
- di_morning_bias_filter: Change to True -> does blocking resume immediately on next bar?
- Hot-reloadable: YES (must be read each bar, not cached at startup)
- Must apply in BOTH live/dry-run (src/main.py) AND backtest engine (src/backtest/engine.py)

**Account/Position Sizing:**
- account_size: Used for daily loss budget calculation (account_size * max_daily_loss_pct)?
- Dual balance (dry-run vs live): Does switching modes swap balances?

**Notifications:**
- Enable/disable channels: Does sending start/stop?
- Webhook URL change: Picked up immediately?
- Min level change: Does filtering update?

### 2C. Removed Settings Verification

These settings should NO LONGER EXIST in the codebase:
- monitoring.enable_1pm_check (moved to pdt.enable_1pm_management)
- monitoring.auto_1pm_close (removed — behavior driven by PDT mode)
- monitoring.trending_threshold (moved to pdt.trending_threshold)

```bash
# Verify removed settings don't appear in code (except legacy fallback)
grep -rn "monitoring.*enable_1pm\|monitoring.*auto_1pm\|monitoring.*trending" src/ config/
# Should find only legacy fallback reads, not primary config paths
```

### 2D. Hot-Reload Verification

Find every place in the codebase where settings are read. Flag any that:
- Cache the value at startup and never re-read
- Read from a module-level constant instead of the live config
- Use a stale reference after settings_changed fires

---

## PART 3: Security Audit

**Goal:** Ensure no credentials are exposed, the application is safe from common web vulnerabilities, and data is protected.

### 3A. Credential Handling

```bash
# Check git history for leaked secrets
git log --all -p | grep -iE "(consumer_key|consumer_secret|app_key|app_secret|ETRADE_SANDBOX_KEY|webhook_url)" | head -40
```

- Verify only variable NAMES appear, never actual key VALUES
- Verify .gitignore covers: .env*, *.db, database/*.json, tokens/, logs/
- Verify strategy_params.yaml in the repo has empty credential fields
- Verify keyring is the primary credential storage mechanism
- Check log output for credential masking (search for `_redact` patterns)

### 3B. Web Security

- **Authentication:** Dashboard has no auth (document as known limitation for single-user local app)
- **Binding:** Flask server bound to 127.0.0.1 (not 0.0.0.0)?
- **CSRF:** Per-session API_TOKEN (secrets.token_hex) embedded in templates via `{{ api_token }}`
- **CSRF validation:** All POST/PUT/DELETE validated via X-API-Token header in @app.before_request
- **CSRF-exempt routes:** Only /setup (initial form) and /auth/etrade/ (OAuth callbacks)
- **XSS:** Dynamic values in templates use {{ value }} (auto-escaped by Jinja2)?
- **innerHTML:** Any use of innerHTML with user/API data? Must be escaped.
- **API endpoints:** Do /api/health, /metrics, /api/status leak account numbers, credentials, or tokens?
- **Analytics endpoints:** Do /api/analytics/*, /api/analytics/risk, /api/analytics/attribution, /api/analytics/regime, /api/analytics/execution return only computed metrics (no raw credentials or account numbers)?
- **Tear sheet export:** Does /api/export/tearsheet sanitize date parameters? Can crafted query params cause path traversal or injection?
- **Settings allowlist:** Does ALLOWED_SETTINGS_PATHS prevent arbitrary key injection?
- **Settings redaction:** Does GET /api/settings mask sensitive values via _redact_secrets()?
- **Backtest delete:** Does POST /api/backtest/delete validate X-API-Token and sanitize IDs?

### 3C. File Permissions

- Token files (schwab_token.json, etrade token file): Created with chmod 0o600?
- Database file: Contains trade data but no raw credentials?
- Log files: Scan for account numbers, API keys, tokens in log output
- Single-instance lock: OS-level file lock (msvcrt/fcntl) with PID check?

### 3D. Dependency Security

```bash
pip-audit  # Check for known vulnerabilities
```

- Flag any critical or high severity findings
- Note any unpinned dependencies in requirements.txt
- Check reportlab and matplotlib versions for known CVEs

---

## PART 4: Data Integrity Audit

**Goal:** Verify data is stored safely, survives crashes, and displays correctly.

### 4A. Database Safety

- SQLite WAL mode enabled? (Check db_manager.py for PRAGMA journal_mode=WAL)
- Writes use proper transactions? (Look for conn.commit() patterns)
- Crash recovery: If bot crashes with open positions, does restart find them?
- Daily counter restoration: Does _restore_daily_counters work on mid-day restart?
- Database migration: Do new fields (vix_at_exit, bb_agreement, exit_detail, spx_at_entry, spx_at_exit, entry_vix) have proper nullable migration?

### 4B. Signal & Trade Logging

- Signal log rotation at configured limit (5000 entries)?
- Atomic writes (tempfile.mkstemp + os.replace pattern)?
- Trade journal: Daily entries created for all trading days?
- Rejection reasons: Captured with correct detail?
- Exit reasons: Normalized to category strings (profit_target, 1pm_close_non_trending, etc.)?
- Exit details: Verbose text stored separately in exit_detail field?

### 4C. Display Accuracy

- Dashboard P&L matches database records?
- Calendar view shows correct trade/no-trade days?
- Analytics metrics match manual calculation from trades table?
- Backtest results display matches the stored JSON report?
- Backtest assumptions panel renders correctly from report.assumptions?
- Risk status streaks: active streak uses `win_streak`/`loss_streak`, previous uses `prev_win_streak`/`prev_loss_streak`?
- Exit reason breakdown shows ~7-10 normalized categories (not hundreds of unique strings)?
- Click-to-expand drill-down shows individual trades per exit category?

### 4D. Backtest Data Management

- Backtest runs stored in backtest_runs table with full_results JSON?
- History API (GET /api/backtest/history) returns all runs (no artificial LIMIT)?
- Delete API (POST /api/backtest/delete) removes rows by ID correctly?
- UI checkboxes, select-all, and bulk delete work end-to-end?
- After deletion, history list refreshes and assumptions panel clears if active run was deleted?
- New fields (bb_agreement, exit_detail, vix_at_exit) included in backtest trade records?

---

## PART 5: Backtest Realism Audit

**Goal:** Verify the backtest execution model produces realistic results and clearly discloses its assumptions and limitations.

### 5A. Slippage Model (BidAskModel)

Check src/brokers/dry_run_broker.py (BidAskModel class) and src/backtest/sim_broker.py:

- **BidAskModel (default):** Shared class used by both DryRunBroker and BacktestBroker
  - BASE_SPREAD = $0.02/leg
  - VIX regime multipliers: low (<15) 1.0x, normal (15-20) 1.3x, elevated (20-30) 1.8x, high (>30) 2.5x
  - Time-of-day multipliers: open (9:30-10) 1.4x, morning (10-11:30) 1.0x, midday (11:30-1) 1.1x, afternoon (1-3) 1.2x, close (3-4) 1.6x
  - Per-leg spread = BASE_SPREAD * vix_mult * time_mult
  - Total per-contract slippage = per_leg * 2 (short + long leg)
  - Range: $0.04-$0.10/contract in typical conditions
- **Flat override:** Constructor `slippage` param used as-is when not None (backward compat)
- **Applied correctly:** Entry subtracts slippage from credit, exit adds slippage to debit
- **Dashboard default:** api_backtest_run() defaults to slippage=None (BidAskModel), user can override
- **Backtest form:** Empty slippage field = auto (BidAskModel), placeholder shows "Auto (VIX + time)"

Verify with tests:
```
TestBidAskModel - 13 unit tests for VIX/time multiplier arithmetic
TestDryRunBrokerDynamicSlippage - 3 integration tests
TestBacktestBrokerDynamicSlippage - 6 integration tests
TestVIXAwareSlippage - backward compat tests
```

### 5A-bis. Fill Quality Factor

Check src/backtest/sim_broker.py place_spread_order() and dashboard/app.py:

- **Parameter:** fill_quality_factor in BacktestBroker constructor (default 1.0)
- **Application:** After slippage: `fill_price = max(0.01, (credit - slippage) * fill_quality_factor)`
- **Entry-only:** close_spread() is NOT affected by fill_quality_factor
- **Engine pass-through:** BacktestEngine accepts fill_quality_factor, passes to BacktestBroker
- **Dashboard integration:** POST /api/backtest/run accepts fill_quality_factor from UI form
- **Report inclusion:** assumptions dict includes fill_quality_factor value
- **Config default:** backtest.fill_quality_factor: 1.0 in strategy_params.yaml
- **UI rendering:** Amber highlight in assumptions panel when factor < 1.0
- **Use case:** 0.90 = 10% worse fills (conservative stress test), 1.0 = theoretical mid (default)

Verify with tests:
```
TestFillQualityFactor - 5 tests: default passthrough, 0.90 factor, 0.50 factor, BidAskModel interaction, close unaffected
```

### 5B. NYSE Half-Day Calendar

Check src/backtest/engine.py _nyse_half_days():

- **Coverage:** Jul 3 (day before Independence Day), Black Friday (day after 4th Thursday of Nov), Dec 24 (if weekday)
- **Bar filtering:** _process_day() skips bars at or after 13:00 on half-days
- **Settlement time:** _expire_0dte_position() receives close_time (13:00 or 16:00)
- **Expiration label:** Dynamic ("Expiration (1:00 PM)" vs "Expiration (4:00 PM)")
- **Edge cases:** Jul 4 on weekend (no half-day), Dec 24 on weekend (no half-day)

Verify with tests:
```
TestNYSEHalfDays - 5 tests covering 2024, 2025, weekend exclusions
```

### 5C. Credit Validation & Flagging

Check src/backtest/engine.py:

- **0DTE trades:** Flag if fill credit > $3.50 or < $1.50
- **TNT trades:** Flag if fill credit > $5.00 or < $2.00
- **Counter:** _flagged_credit_count is cumulative across entire backtest
- **Logging:** Warnings logged for each flagged trade
- **Report:** flagged_credit_count included in engine results and report.assumptions

### 5D. Position Sizing Realism

Check src/core/portfolio_manager.py and src/backtest/engine.py:

- **Daily Income sizing:** contracts = floor(daily_loss_budget / max_risk_per_contract), capped by daily_contracts and max_contracts
  - daily_loss_budget = account_balance * (max_daily_loss_pct / 100)
  - max_risk_per_contract = spread_width * 100 - credit
- **Swing sizing (TNT/ORB/B&B):** Fixed at swing_contracts, clamped to max_contracts
- **Config params:**
  - daily_contracts: 7 (max DI cap, budget-driven)
  - swing_contracts: 2 (fixed for swing strategies)
  - spread_width: 5.0 (used in DI budget calculation)
  - min_contracts: 1 (hard floor)
  - max_contracts: 10 (hard ceiling, never exceeded)
- **Compound scaling:** As balance grows, DI contracts scale up to daily_contracts cap. Cap prevents unbounded growth.
- **Known limitation:** No liquidity modeling. Real SPX options market cannot absorb 100+ contracts at theoretical mid without significant market impact.
- **Known limitation:** No partial fill modeling. All orders fill instantly at full quantity.
- **Known limitation:** No margin requirements modeled. Large positions may exceed buying power in reality.

Flag results with max_contracts > 50 as having significant realism concerns.

### 5E. Pricing Model

Check src/backtest/sim_broker.py get_options_chain():

- **Model:** Black-Scholes approximation with VIX as implied volatility proxy
- **Limitations:** No skew/smile (all strikes use same IV), no term structure, no intraday IV changes
- **Bid-ask spread:** Fixed $0.20 spread, unrealistic for far OTM/ITM strikes
- **Known issue:** Synthetic pricing degrades for large position sizes (not modeled)

### 5F. Assumptions Transparency

Check src/backtest/report.py generate_report() and dashboard/templates/index.html:

- **Report dict:** assumptions key includes slippage_model, slippage_detail, fill_model, pricing_model, fill_quality_factor, half_day_calendar, flagged_credit_count
- **Dashboard panel:** bt-assumptions-panel renders between metrics cards and equity curve
- **Rendering:** renderBtAssumptions() shows 2-column grid with slippage, fill, pricing, fill quality factor, flagged credits
- **Fill quality color:** Amber (#f59e0b) when factor < 1.0, default text color when 1.0
- **Flagged credit color:** Amber (#f59e0b) when count > 0, gray (#6b7280) when 0

### 5G. PDT-Aware Backtesting

Check src/backtest/engine.py:

- **Starting capital:** starting_capital parameter determines PDT mode
- **PDT threshold:** If starting_capital < pdt_threshold (default $25,000), PDT mode activates
- **PDT mode ON in backtest:** 1pm auto-close rules apply, day trade counting active
- **PDT mode OFF in backtest:** No 1pm auto-closes, trades run to profit target or expiration
- **Default:** $50,000 starting capital = PDT mode OFF = no forced 1pm closures

Verify by comparing backtest results:
- $50k starting capital: zero "1pm_close_non_trending" exit reasons
- $20k starting capital: some "1pm_close_non_trending" exit reasons present

### 5H. Morning Bias Filter in Backtest Engine

Check src/backtest/engine.py _check_for_setup() or equivalent:

- **Filter applied:** di_morning_bias_filter config read each bar (not cached at startup)
- **Block logic:** If direction == 'bearish' AND daily_move_pct > +0.25% → skip setup
- **Implementation:** Uses same _classify_direction_regime() from regime_analysis.py as live path — NOT a duplicate implementation
- **SPX open tracking:** Engine stores spx_open_for_day at bar formation time for classification
- **Counted:** direction_filter_skips counter in backtest report
- **Canary:** Direction Drill-Down showing 177 Up Day Bearish trades at 7.9% WR = filter NOT applied

```bash
# Verify filter applied in backtest engine
grep -n "morning_bias\|di_morning_bias\|direction_filter" src/backtest/engine.py
# Must find references — empty result = filter missing from backtest
```

---

## PART 6: Desktop App & Demo Mode Audit

**Goal:** Verify the desktop application, system tray, and demo/replay mode work correctly.

### 6A. Desktop Application (app_desktop.py)

- **Window:** PyWebView "The Daily Melt" (1400x900, min 1024x700, bg #0a0e17)
- **Port fallback:** Tries ports 5000-5010 until one is available
- **System tray:** pystray icon with green/red status dot overlay (bottom-right corner)
- **Tray updates:** Green dot when connected/running, red dot on error/disconnect
- **CLI flags:** --headless, --dev, --no-tray, --demo [file]
- **Single instance:** OS-level file lock prevents duplicate processes

Verify:
- App launches without console window (--noconsole in PyInstaller)
- Tray icon appears and updates status correctly
- Window close minimizes to tray (not exit), tray quit actually exits
- --headless runs Flask without PyWebView window
- --dev enables Flask debug mode

### 6B. Demo/Replay Mode

**Recorder (src/demo/recorder.py):**
- Append-only JSONL format, thread-safe writes
- 30-second price_tick throttle to limit file size
- Privacy scrub: strips account numbers, real credentials
- 14 event types recorded

**Replay (src/demo/replay.py):**
- State machine with threaded playback
- Controls: play, pause, speed (1x/2x/5x/10x), seek, jump to event
- All 14 event types replayed

**Generator (scripts/generate_demo_recording.py):**
- `--all` flag creates 3 scenarios in database/demo_recordings/
- Scenarios: demo_winning.jsonl, demo_losing.jsonl, demo_mixed.jsonl

**Dashboard integration:**
- 5 routes get demo guards (return canned data instead of live)
- 2 demo-specific routes: /api/demo/info, /api/demo/control
- Jinja {% if demo_mode %} banner + controls with cyan accent (#06b6d4)
- 500ms polling for state updates during playback

**Launch:** `python app_desktop.py --demo database/demo_recordings/demo_winning.jsonl --dev`

Verify:
- Demo banner appears at top of dashboard
- Play/pause/speed controls work
- Events replay in correct order with correct timing
- Demo mode does NOT affect real database or trades
- Bar model compatibility: always use getattr(bar, 'tick_count', 0) in recorder hooks

### 6C. Build System

- **Windows:** PyInstaller via build/build_windows.py, output in dist/The Daily Melt/
- **macOS:** py2app via build/build_macos.py, output in dist/The Daily Melt.app/
- **Python version:** MUST use 3.13 (pythonnet incompatible with 3.14)
- **Bundled assets:** templates (index.html, settings.html, setup.html), config/strategy_params.yaml, assets/icon.ico, assets/icon.png, dashboard/static/icon.png
- **icon.ico:** Multi-resolution (16-256px), built manually with struct+PNG (Pillow append_images produces tiny files)
- **macOS iconset:** Must include 512x512@2x.png (1024x1024) for valid .icns

Verify:
- `build/build_windows.py --clean` produces working exe
- Verification step at end of build confirms all bundled files present
- Packaged app uses platformdirs DATA_DIR (not project root) for database, config, logs
- macOS `clean()` only deletes app bundle, not entire dist/ (would destroy Windows build)

---

## PART 7: Analytics & Reporting Audit

**Goal:** Verify all analytics calculations are mathematically correct, handle edge cases safely, and display accurate results on the dashboard.

### 7A. Greeks Calculator (src/analytics/greeks.py)

**Input validation:**
- Negative implied volatility: should handle gracefully (clamp or return N/A)
- Zero DTE: uses 1/365 as floor (not zero, which would cause division errors)
- Zero spot price or strike: should not crash scipy.stats.norm calls
- Extreme IV (>500%): verify Black-Scholes doesn't produce NaN/Inf

**Spread Greeks:**
- Formula: (long leg Greeks - short leg Greeks) * multiplier
- Sign convention: short credit spread should show positive theta (collecting decay)
- Verify net delta direction matches trade direction expectation

**Portfolio aggregation:**
- Sum across all open positions
- Empty positions returns zeroes (not error)
- Dashboard panel: color-coded delta risk (green < 20, yellow 20-50, red > 50)

**Tests to verify exist:**
```
TestBlackScholes, TestSpreadGreeks, TestPortfolioGreeks, TestEdgeCases
```

### 7B. Risk Metrics (dashboard/app.py analytics helpers)

**VaR calculation:**
- Historical percentile method (not parametric)
- VaR 95% = 5th percentile of daily P&L series
- VaR 99% = 1st percentile of daily P&L series
- Verify numpy.percentile is called correctly (5th and 1st, not 95th and 99th)

**CVaR (Expected Shortfall):**
- CVaR 95% = mean of all daily P&L values below VaR 95%
- CVaR 99% = mean of all daily P&L values below VaR 99%
- Edge case: if no values below VaR threshold, handle gracefully

**Calmar Ratio:**
- Formula: annualized return / max drawdown
- Division by zero: if max drawdown is 0, return N/A (not infinity)
- Uses the equity_curve and calmar_ratio passed from backtest/live data

**Tail Ratio:**
- Average of top 5% daily P&L / abs(average of bottom 5% daily P&L)
- Edge case: fewer than 20 data points returns N/A
- Edge case: bottom 5% average is zero, handle division

**Streaks:**
- Consecutive wins/losses tracked correctly
- Edge cases: all wins, all losses, alternating, single trade

**Insufficient data gate:**
- Fewer than 20 trading days: all metrics return N/A
- Dashboard displays "N/A" not misleading partial calculations

**Tests to verify exist:**
```
TestVaR, TestCVaR, TestCalmar, TestTailRatio, TestStreaks, TestInsufficientData
```

### 7C. P&L Attribution (src/analytics/pnl_attribution.py)

**Attribution math:**
- theta_pnl = net_theta * hours_held / 6.5
- delta_pnl = net_delta * (exit_spx - entry_spx) * quantity
- vega_pnl = net_vega * (exit_iv - entry_iv) * quantity
- residual = actual_pnl - theta_pnl - delta_pnl - vega_pnl

**Input safety:**
- Null/zero entry_spx_price or exit_spx_price: returns N/A attribution
- Null/zero entry_vix or exit_vix (vix_at_entry, vix_at_exit): vega_pnl = 0
- Zero hours_held (entry_time == exit_time): theta_pnl = 0
- Zero actual_pnl: percentage calculations don't divide by zero

**Batch aggregation:**
- Empty trade list: returns zeroes
- Mixed attributed + unattributed trades: counts separately
- "X of Y trades attributed" displayed correctly

**Dashboard interpretation:**
- Auto-generated text identifies dominant component
- High residual on 0DTE trades is expected and noted (gamma-driven)

**Tests to verify exist:**
```
TestAttributionMath, TestThetaPositive, TestComponentsSum, TestBatch, TestMissingData, TestZeroPnL
```

### 7D. Execution Quality (dashboard/app.py)

**Slippage calculation:**
- theoretical_credit - actual_credit = slippage per trade
- Aggregation: total cost, average, percentage of credit
- Breakdown by time bucket: Mid-Morning (10-11), Midday (11-1), Close (3-4)
- Breakdown by VIX regime: Low (<15), Normal (15-20), Elevated (20-30), High (>30)

**Exit reason normalization:**
- "Profit target reached: $X (target: $Y)" → "profit_target"
- "1PM CHECK: NON-TRENDING ..." → "1pm_close_non_trending"
- "1PM CHECK: TRENDING ..." → "1pm_hold_trending"
- "Expiration (4:00 PM)" / "Expiration reached (4:00 PM EST)" → "expiration"
- "Expiration (1:00 PM)" → "expiration_1pm"
- "TNT:target_hit" / "TNT: target_hit" → "tnt_target_hit"
- "TNT:stop_hit" → "tnt_stop_hit"
- "TNT:max_hold_exceeded" / "TNT: max_hold_exceeded" → "tnt_max_hold"
- "Expired (resolved at startup, ...)" → "expired_at_startup"
- "Direction Filter (Up Day)" → "direction_filter_up_day" (DI bearish entry blocked by morning bias filter)
- Unknown strings: pass through unchanged

**Display labels:** Normalized keys map to readable labels (e.g., "profit_target" → "Profit Target (80%)", "direction_filter_up_day" → "Direction Filter (Up Day)")

**Drill-down:** Click-to-expand shows individual trades: Date | Direction | Credit | P&L | SPX Entry → Exit

**Tests to verify exist:**
```
TestDashboardNormalization, TestBacktestNormalization, TestExitReasonLabels, TestDateRange, TestGroupedAggregation, TestExecutionEndpoint
```

### 7E. Regime Analysis (src/analytics/regime_analysis.py)

**VIX regime classification:**
- Calm: VIX < 15
- Normal: VIX 15-20
- Elevated: VIX 20-30
- Crisis: VIX > 30

**Direction regime classification:**
- Strong Down: daily SPX return < -1%
- Down: -1% to -0.25%
- Flat: -0.25% to +0.25%
- Up: +0.25% to +1%
- Strong Up: > +1%

**Correlation calculation:**
- Uses numpy.corrcoef on daily P&L vs SPX daily return
- Edge case: fewer than 2 data points returns N/A (not NaN)
- Edge case: zero variance in either series returns N/A

**Beta calculation:**
- Covariance(P&L, SPX return) / Variance(SPX return)
- Division by zero if SPX variance is zero: return N/A

**Market neutrality interpretation:**
- |correlation| < 0.2: "low correlation" (green)
- |correlation| 0.2-0.5: "moderate correlation" (yellow)
- |correlation| > 0.5: "high correlation" (red)

**Edge cases:**
- All trades in single regime: works but no comparison possible
- Fewer than 5 trades: returns N/A for that regime
- Empty trade list: returns empty results (not crash)

**Tests to verify exist:**
```
TestVIXRegime, TestDirectionRegime, TestCorrelation, TestBeta, TestSingleRegime, TestFewTrades
```

### 7F. Tear Sheet PDF Export (src/analytics/tear_sheet.py)

**Generation:**
- Monthly, weekly, and custom date range periods
- Returns valid PDF bytes (starts with %PDF header)
- Empty period (no trades): generates PDF with "No trades" message, does NOT crash

**Modal UX (all three period modes must work correctly):**
- **Monthly:** shows month/year range picker (start month/year + end month/year)
  - Defaults to the FULL DATE RANGE of the active data source (e.g. Jan 2019 – Dec 2025 for full backtest)
  - Pre-populated by reading the `an-date-range` span already rendered in the Analytics tab header
  - Falls back to current calendar month only if no data source is loaded
  - Allows selecting a range of months, not just a single month
  - Must NOT default to current month when a data source is loaded (known past bug)
- **Weekly:** shows date range picker (start date + end date)
- **Custom:** shows start date + end date pickers
- All four fields set: ts-start-year, ts-start-month, ts-end-year, ts-end-month

**Period filter applied to ALL sections:**
- Trade list filtered once at top of _build_pdf() using selected start/end dates
- SAME filtered list passed to: _compute_analytics(), _compute_risk_metrics(), _compute_attribution(), _compute_execution()
- Known past bug: only trade log was filtered; summary metrics, equity curve, and monthly table used full dataset
- After fix: every section must reflect only trades within the selected period

**Metrics pass-through from backtest report:**
- Sharpe, Sortino, Max Drawdown, Calmar, Win Rate, Profit Factor, Best Streak, Worst Streak passed through from backtest report JSON — NOT recalculated
- VaR and CVaR: recalculated from filtered trade list (not in backtest report)
- Fallback: if no report available (live trades), all metrics recalculated from trade list
- Streak calculation uses _compute_daily_streaks() with daily P&L series:
  - Groups trades by calendar day, sums daily P&L
  - $0 P&L days are neutral — do NOT break a streak
  - Known past bug: trade-level consecutive wins used instead of daily streaks (produced wrong numbers like 141/119 instead of 22/4)

**Monthly return percentages:**
- Formula: monthly_pnl / equity_at_start_of_month * 100
- Running equity starts from initial_capital (read from backtest report, default $50,000)
- After each month: equity += monthly_pnl before next month's calculation
- Known past bug: monthly_pnl / initial_capital (fixed denominator) produced absurd values like 2012% in Dec 2021

**Currency formatting:**
- Large values use abbreviated format: $30.5M, $450K, $1.2B
- _fmt_dollar_abbrev() helper:
  - >= $1B: {sign}${val/1B:.1f}B
  - >= $1M: {sign}${val/1M:.1f}M
  - >= $1K: {sign}${val/1K:.1f}K
  - else: {sign}${val:.2f}
- Applied to: Total P&L summary card, VaR, CVaR in risk metrics, equity curve Y-axis labels
- Values under $10K show full precision

**Exit reason display labels:**
- Normalized key → display label mapping applied to BOTH Top Exit Reasons table AND individual trade log rows
- Mapping: profit_target → "Profit Target (80%)", tnt_stop_hit → "TNT Stop Loss", expiration → "Held to Expiration", tnt_target_hit → "TNT Target Hit", tnt_max_hold → "TNT Max Hold (7 days)", direction_filter_up_day → "Direction Filter (Up Day)"
- Fallback: unknown keys → title-case with underscores replaced by spaces
- Known past bug: trade log rows showed raw keys while summary table showed labels

**Strategy name abbreviations in trade log:**
- daily_income → "DI", tag_n_turn → "TNT", orb → "ORB", bnb → "B&B"
- Applied before rendering trade log table rows
- Prevents column overflow wrapping ("daily_incom / e" was a known past bug)

**P&L ampersand rendering:**
- "P&L" must render as plain text in PDF — use "P&amp;L" in reportlab Paragraph() XML mode
- Known past bug: double-escaping produced "P&L;" with stray semicolon throughout

**Error surfacing:**
- If PDF generation fails, modal displays the actual error message
- Backend: Flask route returns structured JSON error on failure, not HTTP 500
- Frontend: fetch handler checks response.ok and renders error text inside modal

**Content sections:**
1. Header: "The Daily Melt — Performance Report", period label, data source label, generation date
2. Performance summary cards (7 cards): Total P&L, Win Rate, Profit Factor, Sharpe, Sortino, Max DD, Trades
3. Equity curve chart (reportlab native drawing, dark theme — NOT matplotlib)
4. Monthly returns table (rolling equity percentages)
5. Risk metrics table (Calmar, VaR, CVaR, Tail Ratio, Best/Worst Streak)
6. Top Exit Reasons table (display labels, not raw keys)
7. P&L Attribution table (Theta/Delta/Vega/Residual)
8. Execution Quality summary (avg slippage, total cost abbreviated)
9. Trade log table (DI/TNT/ORB/B&B abbreviations, display reason labels)
10. Disclaimer footer: "Simulated Results — Uses synthetic pricing, instant fills at theoretical mid. Results reflect backtested performance, not live execution."

**Download mechanism (PyWebView compatible):**
- Backend: saves PDF to Downloads folder, returns JSON {success: true, filename: "..."}
- Frontend: shows green toast notification with filename
- Known past bug: blob URL download silently ignored by PyWebView's embedded browser

**Tests to verify exist:**
```
TestMonthlyPDF (with month range params), TestWeeklyPDF, TestCustomPDF,
TestEmptyPeriod, TestChartEdgeCases, TestEndpoint,
TestMonthlyModalHasDateRange, TestErrorResponseOnFailure,
TestDataSourceLabelInHeader, TestPeriodFilterAllSections,
TestMetricsPassthroughFromReport, TestRollingEquityMonthlyReturns,
TestAbbreviatedCurrencyFormat, TestExitReasonLabelsInTradeLog,
TestStrategyAbbreviationsInTradeLog, TestStreakFromDailyPnL
```

### 7I. New Analytics Panels (BB Agreement, Trade Duration, Direction Drill-Down)

**BB Agreement Analysis:**
- Counts DI trades where BB direction agreed vs. disagreed with the trade direction at entry
- BB Agreed panel: win rate + total P&L + trade count displayed
- BB Disagreed panel: win rate + total P&L + trade count displayed
- Panel note rendered: "BB filter is analytics-only. It never gates DI entry."
- Edge case: 0 disagreed trades → shows 0.0% win rate / $0.00 P&L (not divide-by-zero crash)
- Expected result with current setup: ~100% of DI trades in BB Agreed bucket (BB and pulse bars use similar momentum logic, so disagreement is rare in practice)

**Trade Duration:**
- Calculates avg winner duration, avg loser duration, fastest winner, longest loser
- Duration distribution histogram with bins: 0-1h, 1-2h, 2-3h, 3-4h, 4-5h, 5-6h, 6h+ — wins and losses shown as split bars
- Win rate by duration table with avg P&L per bin
- Interpretation note rendered: "Winners exit quickly via profit target. Losers tend to be held longer."
- Edge case: empty duration data (no exit times recorded) → shows N/A labels, not crash
- Edge case: single trade → histogram renders correctly with one bar

**Direction Drill-Down:**
- Cross-tab of market day classification (Strong Down / Down / Flat / Up / Strong Up) × trade direction (Bull / Bear)
- Shows: trade count, win rate, avg P&L for each combination
- Highlight boxes for key pairings (e.g., "Up Day Bullish" and "Up Day Bearish") with color-coded win rates
- Auto-generated insight: flags when bearish win rate on Up days drops below 20%, surfacing the "Up day problem"
- When di_morning_bias_filter is enabled: panel note reads "Morning bias filter active — bearish DI entries blocked on Up/Strong Up days"
- Edge case: no trades in a direction/day combination → displays "--" not zero (zero would be misleading)
- Edge case: empty trade set → panel renders with all "--" values, not crash

**Tests to verify exist:**
```
TestBBAgreementPanel
TestBBAgreementZeroDisagreed
TestBBAgreementEdgeCaseAllAgreed
TestTradeDurationPanel
TestTradeDurationEmptyData
TestTradeDurationSingleTrade
TestDirectionDrillDownCrossTab
TestDirectionDrillDownInsightNote
TestDirectionDrillDownFilterActiveNote
TestDirectionDrillDownEmptyCombo
```

### 7G. 5-Minute Bar Aggregator (src/core/bar_aggregator_5min.py)

**Display-only verification:**
- CRITICAL: 5-min aggregator must NEVER feed into strategy logic
- Verify it is NOT imported by strategy.py, tag_n_turn.py, orb_strategy.py, or bnb_strategy.py
- Verify it is NOT passed to PulseBarDetector or any signal detection method
- Only imported by main.py (for tick feeding) and dashboard/app.py (for API endpoint)

```bash
# Quick check: 5-min aggregator should only be imported by main.py and app.py
grep -rn "bar_aggregator_5min\|BarAggregator5Min" src/ dashboard/ --include="*.py"
# Flag any imports in strategy files as CRITICAL violations
```

**Memory safety:**
- MAX_BARS constant limits stored bars (expected: 24 = 2 hours of 5-min bars)
- Auto-eviction: oldest bars removed when capacity reached
- Reset on new trading day

**Market hours:**
- Ticks outside 9:30-16:00 ET filtered
- Handles gaps (no ticks for extended period)
- Handles market open/close boundaries

**Tests to verify exist:**
```
TestOHLC, TestBoundaryCompletion, TestAutoEviction, TestMarketHours, TestReset, TestAPIEndpoint
```

### 7H. Chart Visualization Data

**Entry markers:**
- Bullish entries: green filled circle at entry price
- Bearish entries: red filled circle at entry price

**Exit markers:**
- Winner: green X with P&L label
- Loser: red X with P&L label
- Connecting line from entry circle to exit X

**Spread visualization:**
- Horizontal dashed lines at short and long strike prices
- Tinted fill between strikes (green if winning, red if losing)

**Timeframe toggle:**
- 30m / 5m segmented control
- Switching fetches correct bar data
- Markers render at correct positions on both timeframes
- Pulse bar highlighting only on 30m view (pulse bars are 30-min)

---

## PART 8: Price Feed Audit

**Goal:** Verify that live trading uses broker-sourced price data, not Yahoo Finance. Yahoo Finance is appropriate only for backtest historical data and dry-run simulation.

### 8A. Price Feed Architecture

The system uses a PriceFeed abstraction layer (src/data/price_feed.py):

```
PriceFeed (ABC)
├── YahooPriceFeed     — dry-run mode (30s poll, yfinance)
├── EtradePriceFeed    — live mode + E*TRADE broker (10s poll)
└── SchwabPriceFeed    — live mode + Schwab broker (10s poll)
```

**Factory function:** create_price_feed(trading_mode, broker) in src/data/price_feed.py
- live + EtradeBroker → EtradePriceFeed
- live + SchwabBroker → SchwabPriceFeed
- dry-run (any broker) → YahooPriceFeed
- Any other mode → YahooPriceFeed

**Base class features (_BasePriceFeed):**
- TTL caching with stale-cache fallback (10s for broker feeds, 30s for Yahoo)
- Consecutive failure tracking (increments on each failed quote call)
- is_healthy(): returns False after 3 consecutive failures
- get_health_status(): returns dict with source, healthy, last_update_secs_ago, consecutive_failures
- Graceful degradation: returns last cached value on failure (not None/crash)

### 8B. Live Trading Verification

Check src/main.py:
- price_feed created via create_price_feed() during initialization
- _update_market_state() calls price_feed.get_latest_price() — NOT broker.get_current_price("SPX") directly
- get_latest_bar_data() uses price_feed for open/prev_close data
- Yahoo Finance NOT called directly in the live trading loop
- BnB strategy's day-end price check also uses price_feed

```bash
# Quick check: no direct Yahoo calls in live trading path
grep -n "yfinance\|yahoo_finance\|get_current_price" src/main.py
# Should find price_feed references, not raw yfinance calls
```

### 8C. Health Monitoring

- price_feed_state.json persisted to disk (survives bot restart — feed health preserved)
- /api/status endpoint includes price_feed key:
  ```json
  {
    "price_feed": {
      "source": "schwab" | "etrade" | "yahoo",
      "healthy": true | false,
      "last_update": "2026-02-21T14:32:01",
      "consecutive_failures": 0
    }
  }
  ```
- WARNING logged when feed has been unhealthy > 2 minutes during market hours
- CRITICAL logged after 3 consecutive failures

### 8D. Data Source Rules by Mode

| Mode | Broker | Price Source | Poll Interval |
|------|--------|-------------|---------------|
| backtest | N/A | Yahoo historical (unchanged) | N/A |
| dry-run | any | YahooPriceFeed | 30s |
| live | E*TRADE | EtradePriceFeed | 10s |
| live | Schwab | SchwabPriceFeed | 10s |

**VIX data:** Always from Yahoo Finance regardless of mode (brokers don't provide VIX). This is expected and correct.

**get_latest_bar_data():** Returns Yahoo fallback for open/prev_close on broker feeds — broker quote endpoints provide last price but not OHLCV. Bar builder assembles OHLCV from ticks so this is acceptable.

### 8E. Weekend/After-Hours Behavior

- During market close (weekends, after 4PM ET): price_feed returns last cached value
- consecutive_failures does NOT increment outside market hours (stale data is expected)
- is_healthy() remains true during market close if last successful fetch was recent
- First Monday morning check: verify /api/status shows correct source and healthy=true at 9:30 ET open

### 8F. Symbol Formats

- E*TRADE: "SPX" (verify exact symbol the E*TRADE quote API expects)
- Schwab: "$SPX.X" (schwab-py format)
- Yahoo Finance: "^GSPC" (existing behavior, unchanged)

**Tests to verify exist:**
```
TestYahooPriceFeedReturnsFloat
TestYahooPriceFeedReturnsNoneOnFailure
TestEtradePriceFeedCallsBrokerQuote
TestSchwabPriceFeedCallsBrokerQuote
TestCreatePriceFeedLiveEtrade
TestCreatePriceFeedLiveSchwab
TestCreatePriceFeedDryRun
TestConsecutiveFailuresIncrement
TestIsHealthyFalseAfterThreeFailures
TestGracefulFallbackOnFailure
TestHealthStatusDict
TestPriceFeedStateJson
```

---

## Deliverable Format

Create docs/full_system_audit.md with these sections:

### Section 1: Strategy Status

| Strategy | Backtest Works | Dry-Run Works | Live Ready | Issues Found |
|----------|---------------|---------------|------------|--------------|
| Daily Income | PASS/FAIL | PASS/FAIL | PASS/FAIL | description |
| Tag 'n Turn | PASS/FAIL | PASS/FAIL | PASS/FAIL | description |
| B&B (signal enhancer) | PASS/FAIL | PASS/FAIL | N/A (no trades) | description |
| ORB (experimental) | PASS/FAIL | PASS/FAIL | PASS/FAIL | description |

### Section 2: Settings Propagation

| Setting | YAML Path | Hot-Reload | Works | Issue |
|---------|-----------|------------|-------|-------|
| trading_mode | trading_mode | NO (restart) | YES | -- |
| pulse_threshold | ... | ... | ... | ... |

### Section 3: Security Findings

| ID | Severity | Finding | File:Line | Status |
|----|----------|---------|-----------|--------|
| S-1 | ... | ... | ... | PASS/FAIL |

### Section 4: Data Integrity

| Check | Status | Notes |
|-------|--------|-------|
| WAL mode | PASS/FAIL | ... |
| Crash recovery | PASS/FAIL | ... |
| Exit reason normalization | PASS/FAIL | ... |

### Section 5: Backtest Realism

| Check | Status | Notes |
|-------|--------|-------|
| VIX-aware slippage tiers | PASS/FAIL | ... |
| Flat slippage backward compat | PASS/FAIL | ... |
| Half-day bar filtering | PASS/FAIL | ... |
| Half-day settlement time | PASS/FAIL | ... |
| Credit flagging counter | PASS/FAIL | ... |
| Assumptions panel renders | PASS/FAIL | ... |
| PDT-aware backtesting | PASS/FAIL | ... |
| Position sizing realism | DOCUMENTED | Known limitations flagged |

### Section 6: Desktop App & Demo

| Check | Status | Notes |
|-------|--------|-------|
| Windows build + launch | PASS/FAIL | ... |
| System tray icon | PASS/FAIL | ... |
| Demo mode playback | PASS/FAIL | ... |
| Demo isolation (no real DB writes) | PASS/FAIL | ... |

### Section 7: Analytics & Reporting

| Component | Math Correct | Edge Cases | Dashboard Renders | Tests Pass |
|-----------|-------------|------------|-------------------|------------|
| Greeks | PASS/FAIL | PASS/FAIL | PASS/FAIL | PASS/FAIL |
| Risk Metrics (VaR/CVaR/Calmar) | PASS/FAIL | PASS/FAIL | PASS/FAIL | PASS/FAIL |
| P&L Attribution | PASS/FAIL | PASS/FAIL | PASS/FAIL | PASS/FAIL |
| Execution Quality | PASS/FAIL | PASS/FAIL | PASS/FAIL | PASS/FAIL |
| Regime Analysis | PASS/FAIL | PASS/FAIL | PASS/FAIL | PASS/FAIL |
| Tear Sheet PDF | PASS/FAIL | PASS/FAIL | N/A (download) | PASS/FAIL |
| BB Agreement / Duration / Direction Drill-Down | PASS/FAIL | PASS/FAIL | PASS/FAIL | PASS/FAIL |
| 5-Min Aggregator | PASS/FAIL | PASS/FAIL | PASS/FAIL | PASS/FAIL |
| Chart Visualization | N/A | N/A | PASS/FAIL | PASS/FAIL |

### Section 8: Price Feed

| Check | Status | Notes |
|-------|--------|-------|
| Live mode uses broker feed (not Yahoo) | PASS/FAIL | ... |
| Dry-run mode uses Yahoo | PASS/FAIL | ... |
| Backtest unaffected | PASS/FAIL | ... |
| Health monitoring in /api/status | PASS/FAIL | ... |
| Graceful degradation on failure | PASS/FAIL | ... |
| price_feed_state.json persisted | PASS/FAIL | ... |

### Section 9: Fixes Required

Ordered by severity:
- **CRITICAL:** Strategies not running, orders not placing correctly, 5-min aggregator feeding strategy logic, morning bias filter not blocking bearish entries on Up days in BOTH live AND backtest engine
- **HIGH:** Settings not propagating, security vulnerabilities, backtest model errors, analytics math errors, tear sheet export failure (period filter not applied to all sections, wrong metrics, wrong monthly returns), price feed using Yahoo in live mode, new analytics panels crashing or showing wrong data
- **MEDIUM:** Display inaccuracies, missing error handling, realism gaps, edge case failures
- **LOW:** Code quality, documentation gaps

### Section 10: Fix Implementation

After documenting all findings, implement fixes in this order:
1. All CRITICAL fixes first (strategy wiring, order lifecycle, isolation violations, morning bias filter in backtest)
2. All HIGH fixes (settings propagation, security, backtest model, analytics math, tear sheet, price feed)
3. Run full test suite after each fix (1010+ tests expected as of Feb 2026)
4. Update the audit report with fix status

---

## Running the Audit

Feed this to your AI assistant:

```
Read the audit skill at build/skills/audit/SKILL.md and perform a full
8-part audit of the codebase. Document everything in
docs/full_system_audit.md first, then implement all CRITICAL and
HIGH fixes. Run the test suite after each fix.
```

For a partial audit (e.g., strategy-only):

```
Read the audit skill at build/skills/audit/SKILL.md and perform only
PART 1 (Strategy Logic Audit). Focus on verifying DI has no BB
gating, 1pm management is PDT-only, B&B is signal-only, the
morning bias filter correctly blocks bearish DI entries on Up/Strong
Up days in BOTH live and backtest engine (section 1H), and B&B/ORB
default to unchecked with Experimental labels in the backtest UI.
```

For a security-only check:

```
Read the audit skill at build/skills/audit/SKILL.md and perform only
PART 3 (Security Audit). This is a pre-release security review.
```

For an analytics audit:

```
Read the audit skill at build/skills/audit/SKILL.md and perform only
PART 7 (Analytics & Reporting Audit). Verify all calculations are
mathematically correct, edge cases are handled, the 5-min aggregator
is properly isolated from strategy logic, the tear sheet export
generates successfully with correct modal UX for all three period
types (with default date range pre-populated from active data source),
period filter applied to ALL sections, metrics passed through from
backtest report, rolling equity used for monthly returns, abbreviated
currency format, display labels in trade log, strategy abbreviations
in trade log, and daily P&L streaks (section 7F). Also verify the
BB Agreement / Trade Duration / Direction Drill-Down panels (section 7I)
render correctly without crashes.
```

For a backtest realism review:

```
Read the audit skill at build/skills/audit/SKILL.md and perform only
PART 5 (Backtest Realism Audit). Verify slippage model, half-day
calendar, credit flagging, PDT-aware backtesting, position sizing
realism, and morning bias filter applied in backtest engine (section 5H).
Canary test: run 2019-2025 backtest and verify Direction Drill-Down
shows near-zero Up Day Bearish trades when di_morning_bias_filter=True.
```

For a price feed check:

```
Read the audit skill at build/skills/audit/SKILL.md and perform only
PART 8 (Price Feed Audit). Verify live trading uses broker-sourced
price data (not Yahoo Finance), dry-run uses Yahoo, health monitoring
is exposed in /api/status, and graceful degradation works correctly.
On Monday morning at market open, verify /api/status shows
price_feed.source = "etrade" or "schwab" and price_feed.healthy = true.
```

For a desktop app check:

```
Read the audit skill at build/skills/audit/SKILL.md and perform only
PART 6 (Desktop App & Demo Mode Audit). Verify build, tray, and
demo playback work correctly.
```
