# Full System Audit - The Daily Melt

**Date**: 2026-02-18
**Test Suite**: 543 tests passing at audit start
**Scope**: Strategy logic, settings propagation, security, data integrity

---

## Section 1: Strategy Status

| Strategy | Backtest | Dry-Run | Live Ready | Issues |
|----------|----------|---------|------------|--------|
| Daily Income | PASS | PASS | PASS | None |
| Tag 'n Turn | **FAIL** | PASS | PASS | Not wired into backtest engine |
| Bed & Breakfast | **FAIL** | PASS | PASS | Not wired into backtest engine |
| Opening Range Breakout | **FAIL** | PASS | PASS | Not wired into backtest engine |

### Root Cause: Backtest Engine Only Runs Daily Income

`src/backtest/engine.py` has 6 architectural gaps:

1. **No strategy instantiation** (line 126-131): Only `SPXIncomeStrategy` is created. `TagNTurnStrategy`, `BnBStrategy`, `ORBStrategy` are never imported.

2. **Single position tracking** (line 140): `self._active_trade: Optional[Trade] = None` tracks one position. Live trading supports 2 concurrent (1 swing + 1 0DTE).

3. **Hardcoded 1-trade-per-day limit** (line 268-269): `if self._daily_trades >= 1: continue` blocks all strategies after any entry. Live allows 1 per slot (DI/ORB/B&B share 0DTE slot, TNT has swing slot).

4. **Hardcoded 9:30-11:30 setup window** (line 275-279): Only Daily Income's window. TNT needs all-day bar feeding, B&B needs 15:00-16:00, ORB needs first bar only.

5. **No bar feeding to parallel strategies**: `tag_n_turn.on_bar_complete()`, `bnb_strategy.on_bar_complete()`, `orb_strategy.set_opening_range()` are never called.

6. **No cross-day state**: B&B generates signals at EOD for next-morning entry. Backtest processes days independently.

### Strategy Entry/Exit/Risk Summary

**Daily Income (DI)** - 0DTE credit spreads
- Entry: Pulse bar (10% threshold) in 9:30-11:30 window, then breakout confirmation
- Exit: 80% profit target, 1pm management check, or expiration
- Sizing: `portfolio.position_sizing.method` (percent_risk default), capped by `max_contracts_override`
- Risk gates: Circuit breaker, 0DTE slot limit, portfolio `can_enter_position()`, PDT tracker
- Works in: backtest, dry-run, live

**Tag 'n Turn (TNT)** - Multi-day swing via BB mean reversion
- Entry: Price tags Bollinger Band -> reversal pulse bar -> breakout confirmation
- Exit: Opposite BB (target), entry BB (stop), or 7-day max hold
- Sizing: 2 contracts default (`max_contracts_override`), $10 spread, $2 min credit, 3-7 DTE
- Risk gates: Circuit breaker, TNT slot limit (separate from 0DTE), portfolio gates, PDT
- Works in: dry-run, live. **FAILS in backtest** (not instantiated)

**Bed & Breakfast (B&B)** - Overnight signal for next-morning entry
- Entry: Pulse bar detected 15:00-16:00 -> signal stored overnight -> entry at 9:30 next day
- Exit: 30-min "Just Breakfast" exit at 10:00, or roll to daily if first bar confirms
- Sizing: 3 contracts default, shares 0DTE slot with DI/ORB
- Risk gates: Circuit breaker, 0DTE slot limit, portfolio gates, PDT
- Works in: dry-run, live. **FAILS in backtest** (not instantiated, cross-day state missing)

**Opening Range Breakout (ORB)** - First bar range breakout
- Entry: First 30-min bar sets opening range. Breakout above high (bullish) or below low (bearish). 10-40% threshold zone.
- Exit: Held to expiration (same as DI exit rules)
- Sizing: 3 contracts default, shares 0DTE slot with DI/B&B
- Risk gates: Circuit breaker, 0DTE slot limit, portfolio gates, PDT
- Works in: dry-run, live. **FAILS in backtest** (not instantiated)

---

## Section 2: Settings Propagation

| Setting | YAML Path | Hot-Reload | Works | Issue |
|---------|-----------|------------|-------|-------|
| Trading Mode | OS keyring | NO | YES | Requires restart |
| Active Broker | `broker.active` | NO | YES | Requires restart |
| Pulse Threshold | `strategy.pulse_threshold` | NO | YES | Cached at init |
| Spread Width | `strategy.spread_width` | NO | YES | Cached at init |
| Profit Target | `strategy.profit_target_pct` | NO | YES | Cached at init |
| Morning Setup Start | `timing.morning_start` | NO | **PARTIAL** | Hardcoded 9:30 in strategy.py |
| Morning Setup End | `timing.morning_end` | NO | **PARTIAL** | Hardcoded 11:30 in strategy.py |
| Afternoon Start | `timing.afternoon_start` | NO | YES | Cached at init |
| Afternoon End | `timing.afternoon_end` | NO | YES | Cached at init |
| Max Daily Loss % | `portfolio.max_daily_loss_pct` | NO | YES | Dollar amount cached at init |
| Weekly Drawdown | `portfolio.drawdown_limits.weekly.max_loss_pct` | NO | YES | DrawdownManager cached |
| Monthly Drawdown | `portfolio.drawdown_limits.monthly.max_loss_pct` | NO | YES | DrawdownManager cached |
| Max Contracts | `portfolio.position_sizing.max_contracts` | NO | YES | Cached at init |
| Account Size | `portfolio.account_size` | NO | PARTIAL | Editable in dry-run only |
| Slack/Discord/Webhook | `notifications.*` | **YES*** | **NO** | `reload_config()` exists but bot never calls it |
| TNT Enabled | `tag_n_turn.enabled` | NO | NO | Cannot enable mid-session |
| B&B Enabled | `bnb.enabled` | NO | NO | Cannot enable mid-session |
| ORB Enabled | `orb.enabled` | NO | NO | Cannot enable mid-session |
| Stop Loss % | `strategy.stop_loss_pct` | NO | **NO** | In allowlist but NOT implemented in code |
| Max Daily Trades | `strategy.max_daily_trades` | NO | **NO** | In allowlist but NOT implemented in code |
| 1pm Check | `monitoring.enable_1pm_check` | NO | YES | Cached at init |
| 1pm Auto Close | `monitoring.auto_1pm_close` | NO | YES | Cached at init |
| Trending Threshold | `monitoring.trending_threshold` | NO | YES | Cached at init |

\* = `reload_config()` method exists in NotificationManager but bot never calls it

### Key Issues

1. **`.settings_changed` file created but never monitored** (`dashboard/app.py:3961` creates, bot never checks)
2. **`runtime_settings.json` written but never read by bot** (only dashboard merges it for display)
3. **Morning window hardcoded** in `SPXIncomeStrategy._is_setup_window()` (`strategy.py:100-110`), ignoring `timing.morning_start/end` YAML
4. **`stop_loss_pct` and `max_daily_trades` in allowlist** (`app.py:3879-3880`) but no code uses them

---

## Section 3: Security Findings

### Credential Handling: STRONG

| Component | Storage | Status |
|-----------|---------|--------|
| E*TRADE keys | OS keyring (`spx-income-trader`) | PASS |
| E*TRADE tokens | File with `chmod 0o600` | PASS |
| Schwab keys | OS keyring (`spx-income-trader-schwab`) | PASS |
| Schwab tokens | File with `chmod 0o600` | PASS |
| .env file | Gitignored, contains sandbox creds only | PASS |
| SMTP password | Env var only (not keyring) | MEDIUM |
| Twilio token | Env var only (not keyring) | MEDIUM |
| Log output | No credential leakage found | PASS |
| Git history | No secrets in committed files | PASS |

### Web Security: STRONG

| Component | Status | Notes |
|-----------|--------|-------|
| CSRF protection | PASS | Per-session `secrets.token_hex(32)`, validated on all POST/PUT/DELETE |
| XSS prevention | PASS | Jinja2 autoescaping enabled, no `|safe` filters |
| Settings allowlist | PASS | Credentials excluded from editable paths |
| Secret redaction | PASS | `_redact_secrets()` masks API key/secret/token fields |
| Localhost binding | PASS | Flask binds to `127.0.0.1` only |
| `/api/health` | PASS | No sensitive data exposed |
| `/metrics` | PASS | No sensitive data exposed |
| `/api/settings` GET | PASS | Secrets redacted before response |
| Input validation | MEDIUM | No length checks on setup form |
| Trading mode toggle | MEDIUM | No audit logging on mode change |

### Data Integrity: STRONG

| Component | Status | Notes |
|-----------|--------|-------|
| SQLite WAL mode | PASS | Crash recovery enabled (`db_manager.py:29`) |
| Transactions | PASS | Context manager pattern, auto-rollback |
| Parameterized SQL | PASS | All queries use `?` placeholders |
| Signal log | PASS | Atomic writes (tempfile + rename), rotation at 5000 entries |
| Trade journal | PASS | Daily finalization at 16:00, captures rejections |
| Counter restoration | PASS | `_restore_daily_counters()` from DB on restart |
| Position monitoring | PASS | Never skipped (runs before daily limit gates) |

### Dependency Security: GOOD

- All packages pinned to exact versions in `requirements.txt`
- No `yaml.load()` (only `yaml.safe_load()`)
- No `pickle`, `eval`, `exec` usage
- No known critical CVEs in pinned versions

---

## Section 4: Fixes Required

### CRITICAL: Backtest engine missing 3 strategies - FIXED

**Impact**: Backtests only show Daily Income trades, making performance analysis incomplete and misleading.

**Fix applied**: Rewrote `src/backtest/engine.py` to support all 4 strategies:
1. Imported and instantiated TagNTurnStrategy, BnBStrategy, ORBStrategy
2. Replaced single `_active_trade` with 2-slot system: `_0dte_trade` (DI/ORB/B&B shared) + `_tnt_trade` (swing)
3. Per-slot daily trade limits (1 0DTE + 1 swing per day)
4. All bars fed to all strategies every cycle (not just DI during 9:30-11:30)
5. ORB opening range set on first bar of each day
6. B&B cross-day signal passing via `on_day_end()`/`on_day_start()`
7. Strategy-specific exit logic (DI profit target, TNT target/stop/max_hold, B&B Just Breakfast)
8. `strategy_type` tracked on every BacktestTrade
9. Added `strategies` parameter (backward compatible - DI-only when omitted)
10. 14 new tests in `TestMultiStrategyEngine` (39 total backtest tests)

### HIGH: Notification settings not hot-reloadable - FIXED

**Impact**: Changing webhook URLs in dashboard has no effect until bot restart.

**Fix applied**: Added `.settings_changed` file monitoring in `_run_main_loop()` (`src/main.py`). When detected, deletes the file and calls `notifier.reload_config()`.

### HIGH: `stop_loss_pct` and `max_daily_trades` in allowlist but unimplemented - FIXED

**Impact**: Users can change these settings thinking they'll take effect, but nothing happens.

**Fix applied**: Removed `strategy.stop_loss_pct` and `strategy.max_daily_trades` from `ALLOWED_SETTINGS_PATHS` in `dashboard/app.py`.

### HIGH: Morning setup window hardcoded - FIXED

**Impact**: Changing `timing.morning_start`/`timing.morning_end` in settings has no effect.

**Fix applied**: `SPXIncomeStrategy.__init__()` now reads `timing.morning_start`/`timing.morning_end` from `STRATEGY_PARAMS` and stores as `self._morning_start`/`self._morning_end`. `_is_setup_window()` uses these instead of hardcoded `time(9, 30)` and `time(11, 30)`.

### MEDIUM: Email/SMS credentials in env vars

**Impact**: Env vars less secure than keyring for SMTP password and Twilio token.

**Fix**: Add keyring storage functions for SMTP/Twilio credentials.

### MEDIUM: No audit logging on trading mode toggle

**Impact**: No record of who/when changed from dry-run to live.

**Fix**: Add `logger.critical()` audit log on mode change.

### LOW: Input validation on setup form

**Impact**: No length checks on credential fields could allow very large inputs.

**Fix**: Add max length validation.
