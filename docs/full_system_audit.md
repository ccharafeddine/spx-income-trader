# Full System Audit - The Daily Melt

**Date**: 2026-02-19
**Test Suite**: 572 tests passing (before and after all fixes)
**Scope**: Strategy logic, settings propagation, security, data integrity

---

## Section 1: Strategy Status

| Strategy | Backtest | Dry-Run | Live Ready | Issues |
|----------|----------|---------|------------|--------|
| Daily Income | PASS | PASS | PASS | Backtest missing afternoon window (LOW) |
| Tag 'n Turn | PASS | PASS | PASS | None |
| B&B | PASS | PASS | PASS | Pulse detection less strict than DI's PulseBarDetector (LOW) |
| ORB | PASS | PASS | PASS | Spread width hardcoded to $5 in live mode (LOW) |

### Strategy Inventory

**Daily Income (DI)** - `src/core/strategy.py`
- 0DTE pulse bar breakout credit spreads
- Entry: 30-min bar pulse (close in top/bottom 10% of range) -> breakout confirmation
- Exit: 80% profit target, 1PM management rules, 4PM expiration
- Position sizing: percent_risk method via PortfolioManager

**Tag 'n Turn (TNT)** - `src/core/tag_n_turn.py`
- Bollinger Band mean reversion, 3-7 DTE swings
- Entry: State machine (tag BB -> pulse confirmation -> breakout)
- Exit: Opposite BB target, stop beyond entry band + $5, max 7 days

**B&B (Bed & Breakfast)** - `src/core/bnb_strategy.py`
- EOD pulse signal -> next morning entry at 9:30-10:00
- Exit: Just Breakfast (10:00 exit) or aggressive roll to DI management

**ORB (Opening Range Breakout)** - `src/core/orb_strategy.py`
- First 30-min bar range breakout, 10-40% threshold
- Managed by DI's exit logic once entered

### Backtest Engine Coverage

All 4 strategies are properly imported, instantiated, and evaluated each bar in `src/backtest/engine.py`. Strategy checkboxes from the dashboard now correctly pass through to the engine (fixed this session).

### Issues Found

| ID | Severity | Finding | Status |
|----|----------|---------|--------|
| S-1 | MEDIUM | Backtest position sizing uses different formula than live (max_daily_loss_pct vs risk_per_trade_pct) | DOCUMENTED |
| S-2 | LOW | Backtest checks signals per-bar, live checks per-tick (inherent to bar-based backtesting) | DOCUMENTED |
| S-3 | LOW | Backtest missing afternoon setup window for DI | DOCUMENTED |
| S-4 | LOW | B&B's `_is_pulse_bar()` is less strict than DI's PulseBarDetector (no open/close direction check) | DOCUMENTED |
| S-5 | LOW | ORB/B&B spread width hardcoded to $5 in live mode `src/main.py:1436,1468` | DOCUMENTED |

---

## Section 2: Settings Propagation

| Setting | YAML Path | Hot-Reload | Works | Issue |
|---------|-----------|------------|-------|-------|
| trading_mode | (keyring) | NO (restart) | YES | Correct behavior |
| active_broker | broker.active | NO (restart) | YES | Correct behavior |
| pulse_threshold | strategy.pulse_threshold | NO | YES | Set at init |
| spread_width | strategy.spread_width | NO | YES | Set at init |
| profit_target_pct | strategy.profit_target_pct | NO | YES | Set at init |
| morning_start | timing.morning_start | NO | **YES (FIXED)** | Was hardcoded, now reads from config |
| morning_end | timing.morning_end | NO | **YES (FIXED)** | Was hardcoded, now reads from config |
| afternoon_enabled | timing.afternoon_enabled | NO | YES | |
| max_daily_loss_pct | portfolio.max_daily_loss_pct | NO | YES | Set at init, API change requires restart |
| weekly_drawdown | portfolio.drawdown_limits.weekly.* | NO | YES | Checked before every trade entry |
| monthly_drawdown | portfolio.drawdown_limits.monthly.* | NO | YES | Checked before every trade entry |
| max_consecutive_losses | portfolio.drawdown_limits.consecutive_losses.* | NO | YES | Count tracked, pause activates |
| max_contracts | portfolio.position_sizing.max_contracts | NO | YES | Per-strategy override read at trade time |
| account_size | portfolio.account_size | NO | YES | Used in percent_risk calculations |
| tnt.enabled | tag_n_turn.enabled | NO | YES | |
| bnb.enabled | bnb.enabled | NO | YES | |
| orb.enabled | orb.enabled | NO | YES | |
| notifications.* | notifications.* | **YES** | YES | Only truly hot-reloadable setting |

### Critical Findings

| ID | Severity | Finding | Status |
|----|----------|---------|--------|
| P-1 | **HIGH** | `timing.morning_start/morning_end` were hardcoded as `time(9,30)` and `time(11,30)` in TradingBot. Config values were ignored. | **FIXED** |
| P-2 | HIGH | Runtime settings (POST /api/settings) save to `runtime_settings.json` but bot reads `STRATEGY_PARAMS` from YAML at startup. Only notifications hot-reload. | DOCUMENTED - all other settings require restart |
| P-3 | MEDIUM | `daily_income.enabled` flag exists in YAML but is never checked. DI always runs. | DOCUMENTED |
| P-4 | MEDIUM | Dead settings: `risk.max_position_size`, `risk.max_daily_loss`, `risk.max_account_risk_pct`, `execution.min_credit_pct`, `execution.max_slippage`, `monitoring.check_interval`, `timing.timezone`, `timing.market_close` | DOCUMENTED |

---

## Section 3: Security Findings

**Overall Score: 8.5/10**

| ID | Severity | Finding | File:Line | Status |
|----|----------|---------|-----------|--------|
| SEC-1 | HIGH | Flask binds to 127.0.0.1 (not 0.0.0.0) | app_desktop.py:302 | PASS |
| SEC-2 | HIGH | CSRF protection via per-session API_TOKEN on all POST/PUT/DELETE | dashboard/app.py:59-81 | PASS |
| SEC-3 | HIGH | Credentials in OS keyring, not YAML | config/settings.py:26-88 | PASS |
| SEC-4 | HIGH | All SQL queries use parameterized ? placeholders | Multiple | PASS |
| SEC-5 | HIGH | Token files created with 0o600 permissions | etrade_auth.py:274, schwab_auth.py:250 | PASS |
| SEC-6 | HIGH | _redact_secrets() masks sensitive values in API responses | dashboard/app.py:3968-3988 | PASS |
| SEC-7 | HIGH | No |safe filters or {% autoescape false %} in templates | All templates | PASS |
| SEC-8 | HIGH | Flask secret_key is os.urandom(32) per session | dashboard/app.py:51 | PASS |
| SEC-9 | **MEDIUM** | `keyring` used pervasively but was missing from requirements.txt | requirements.txt | **FIXED** |
| SEC-10 | LOW | CSRF comparison uses != instead of hmac.compare_digest() | dashboard/app.py:79 | WARNING (localhost-only) |
| SEC-11 | LOW | account_number not in _redact_secrets() set | dashboard/app.py:3970 | WARNING |
| SEC-12 | LOW | yfinance>=1.2.0 uses floor constraint (unpinned) | requirements.txt:16 | WARNING |

---

## Section 4: Data Integrity

| Check | Status | Notes |
|-------|--------|-------|
| WAL mode | PASS | Enabled in db_manager.py:34 and all connection paths |
| Transactions | PASS | All writes use context manager with conn.commit() |
| Crash recovery | PASS | resolve_expired_trades() on startup, _restore_daily_counters() |
| Daily counter restore | PASS | src/main.py:803-829, works on mid-day restart |
| Signal log rotation | PASS | MAX_SIGNALS=5000, atomic writes via tempfile+os.replace |
| Trade journal | PASS | Daily entries for all trading days including zero-trade days |
| Rejection tracking | PASS | Accumulated per-day, flushed at EOD with reason+detail |
| DB separation (dry-run/live) | **FIXED** | Was dead code; `get_database_path()` now wired to `DATABASE_PATH` |
| Dashboard DB paths | **FIXED** | 7 hardcoded `trades.db` replaced with `DATABASE_PATH` or `DB_PATH` |
| Portfolio manager DB path | **FIXED** | Hardcoded relative path replaced with `DATABASE_PATH` import |
| Backtest data storage | PASS | backtest_runs in shared DB (DB_PATH), separate from mode-specific trades |
| Analytics accuracy | PASS | P&L, streaks, breakdowns all read from correct DB fields |

### DB Separation Architecture (Post-Fix)

- **Trade data** (save_trade, analytics, journal, streaks): Uses `DATABASE_PATH` which resolves to `trades_dryrun.db` or `trades_live.db` based on `TRADING_MODE`
- **Backtest results** (backtest_runs table): Uses `DB_PATH` (`trades.db`) since backtest data is mode-independent
- **Switching modes**: Restart required. Each mode sees only its own trades.

---

## Section 5: Fixes Implemented (This Session)

### HIGH Priority (Implemented)

1. **DB separation wired up** - `config/settings.py:326`: `DATABASE_PATH` now calls `get_database_path()` returning mode-specific DB path
2. **Dashboard hardcoded paths fixed** - 7 locations in `dashboard/app.py` replaced with `DATABASE_PATH` (trade data) or `DB_PATH` (backtest data)
3. **Portfolio manager path fixed** - `src/core/portfolio_manager.py:590`: replaced hardcoded relative path with `DATABASE_PATH` import
4. **Morning window config wired** - `src/main.py`: 3 locations replaced hardcoded `time(9,30)`/`time(11,30)` with `self.morning_start`/`self.morning_end` from timing config
5. **Strategy checkboxes wired** - `dashboard/templates/index.html` + `dashboard/app.py`: Frontend sends checkbox states, backend passes `strategies` param to `BacktestEngine`
6. **Analytics dropdown auto-refresh** - `index.html`: `loadAnalyticsSourceOptions(force, selectRunId)` refreshes and auto-selects new backtest on completion
7. **Missing dependencies added** - `requirements.txt`: Added `keyring>=24.0.0` and `platformdirs>=4.0.0`

### MEDIUM Priority (Documented, Not Implemented)

- Runtime settings don't reach running bot (architectural - requires settings reload handler expansion)
- `daily_income.enabled` flag not checked (DI always runs)
- Dead YAML settings (8 settings with no consumers)
- Backtest position sizing formula differs from live

### LOW Priority (Documented)

- B&B pulse detection less strict than PulseBarDetector
- ORB/B&B spread width hardcoded to $5 in live mode
- Backtest missing afternoon window support
- CSRF timing attack (theoretical, localhost-only)
- account_number not redacted in API response

---

## Section 6: Test Results

**Before fixes**: 572 passed, 0 failed, 1 warning
**After all fixes**: 572 passed, 0 failed, 1 warning

No regressions introduced.
