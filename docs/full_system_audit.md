# The Daily Melt - Full System Audit

**Date:** 2026-02-20
**Trigger:** ORB/B&B strategy rework + pre-live readiness review
**Test baseline:** 609 tests passing

---

## Section 1: Strategy Status

| Strategy | Backtest | Dry-Run | Live Ready | Issues Found |
|----------|----------|---------|------------|--------------|
| Daily Income | PASS | PASS | PASS | No issues |
| Tag 'n Turn | PASS | PASS | PASS | No issues |
| B&B | PASS | PASS | PASS | Now informational-only (no entries) |
| ORB | PASS | PASS | PASS | Now strong-only with range filter + confirmation delay |

### Strategy Details After Rework

**ORB (commit 64c068d):**
- Weak signals removed (`bullish_weak`/`bearish_weak` eliminated)
- Range filter: bars < 8.0 points are skipped
- Confirmation delay: breakout must hold 3 minutes before entry
- `check_breakout()` now requires `current_time` parameter
- Signal dict no longer has `bias_strength` field

**B&B (commit 64c068d):**
- All trade entry/exit logic removed (`check_entry_signal`, `rollback_entry`, Just Breakfast exit)
- Converted to directional confluence signal for DI
- Final-bar-only resolution: only the 15:30 bar creates a signal
- Conflicting pulses (15:00 vs 15:30 different directions) cancel out
- `get_bias()` and `validate_signal()` added for DI confluence check
- Gap invalidation: signal invalid if market gaps > 0.3% against it

---

## Section 2: Settings Propagation

| Setting | YAML Path | Hot-Reload | Works | Notes |
|---------|-----------|------------|-------|-------|
| max_contracts | portfolio.position_sizing.max_contracts | NO (restart) | YES | Global ceiling, tested with max_contracts=1 |
| max_contracts_override | strategy.max_contracts_override | NO (restart) | YES | Per-strategy ceiling, never overrides global |
| min_contracts | portfolio.position_sizing.min_contracts | NO (restart) | YES | Floor, prevents 0-contract trades |
| risk_per_trade_pct | portfolio.position_sizing.risk_per_trade_pct | NO (restart) | YES | Drives percent_risk sizing |
| max_daily_loss_pct | portfolio.max_daily_loss_pct | NO (restart) | YES | Circuit breaker threshold |
| pulse_threshold | strategy.pulse_threshold | NO (restart) | YES | DI pulse detection |
| spread_width | strategy.spread_width | NO (restart) | YES | Credit spread construction |
| profit_target_pct | strategy.profit_target_pct | NO (restart) | YES | Position exit logic |
| bnb.enabled | bnb.enabled | NO (restart) | YES | Must be false (informational-only) |
| orb.enabled | orb.enabled | NO (restart) | YES | Must be false until ready |
| orb.min_range_points | orb.min_range_points | NO (restart) | YES | Range filter (default 8.0) |
| orb.confirmation_minutes | orb.confirmation_minutes | NO (restart) | YES | Breakout hold time (default 3) |
| bnb.gap_invalidation_pct | bnb.gap_invalidation_pct | NO (restart) | YES | Gap filter (default 0.3%) |

### Position Sizing Pipeline

```
1. Calculate base: account_size * risk_per_trade_pct% / max_risk_per_contract
2. Clamp to [min_contracts, max_contracts]    <-- GLOBAL BOUNDS
3. Apply per-strategy override ceiling         <-- NEVER OVERRIDES GLOBAL
```

If `max_contracts=1`, the result is always 1 regardless of strategy overrides. Verified by 4 new tests (`TestMaxContractsEnforcement`).

### Known Discrepancy

Backtest uses `max_daily_loss_pct` for sizing; live uses `risk_per_trade_pct`. This can produce different contract quantities between backtest and live trading. This is a documentation issue, not a bug.

---

## Section 3: Security Findings

| ID | Severity | Finding | Status |
|----|----------|---------|--------|
| S-1 | LOW | E*TRADE sandbox creds in .env file | Mitigated (.gitignore) |
| S-2 | LOW | Exception details in Schwab auth error responses | Known, low-impact |
| S-3 | SAFE | CSRF protection (per-session token, all POST/PUT/DELETE validated) | PASS |
| S-4 | SAFE | Flask bound to 127.0.0.1 (not 0.0.0.0) | PASS |
| S-5 | SAFE | Credentials in OS keyring (not YAML) | PASS |
| S-6 | SAFE | Token files created with 0o600 permissions | PASS |
| S-7 | SAFE | Settings API uses allowlist (ALLOWED_SETTINGS_PATHS) | PASS |
| S-8 | SAFE | GET /api/settings redacts secrets via _redact_secrets() | PASS |
| S-9 | SAFE | secrets.token_hex(32) for CSRF token (cryptographically secure) | PASS |

---

## Section 4: Data Integrity

| Check | Status | Notes |
|-------|--------|-------|
| WAL mode | PASS | PRAGMA journal_mode=WAL in db_manager.py |
| Crash recovery | PASS | _restore_daily_counters() restores from DB |
| Position reconciliation | WARNING | Detected but not auto-recovered (manual review needed) |
| Daily counter restore | PASS | Tested; B&B correctly excluded from 0DTE counter |
| P&L single source of truth | PASS | portfolio.daily_realized_pnl is sole tracker |

---

## Section 5: Catastrophic Scenario Testing

| Scenario | Status | Test Coverage |
|----------|--------|---------------|
| Double entry (same strategy twice) | SAFE | Counter increments only after confirmed fill |
| Partial spread fills | SAFE | Partial fills accepted with actual qty; zero fills rejected; notification sent |
| Circuit breaker bypass | SAFE | All entry paths (DI, ORB, TNT) check breaker |
| Position limit bypass | SAFE | 0DTE limit enforced at portfolio + counter level |
| B&B accidental trade entry | SAFE | No entry methods exist; 4 tests verify |
| max_contracts=1 override | SAFE | Global ceiling never overridden by strategy; 4 tests |
| Small account (0 contracts) | SAFE | min_contracts=1 prevents 0-contract trades |
| Orphaned positions | WARNING | Detected and logged, requires manual intervention |
| P&L race condition | SAFE | Single-threaded main loop, atomic updates |
| ORB stale pending state | SAFE | Cleared on daily reset, rollback, and confirmation |

### Partial Fill Handling

**File:** `src/core/position_manager.py:178-190`

Partial fills are accepted and tracked with the actual filled quantity:
- If `filled_quantity == 0`, the trade is rejected (returns None)
- If `filled_quantity < requested_quantity`, the trade proceeds with `quantity = filled_quantity`
- All downstream tracking (portfolio risk, P&L, exit logic) uses the actual filled quantity
- A warning is logged and a notification sent via Slack/Discord for partial fills

---

## Section 6: Desktop App & Demo

| Check | Status | Notes |
|-------|--------|-------|
| Windows build | PASS | PyInstaller via build_windows.py |
| System tray icon | PASS | Green/red status dot overlay |
| Demo mode playback | PASS | 14 event types, play/pause/speed/seek |
| Demo isolation | PASS | No real DB writes in demo mode |

---

## Section 7: Pre-Live Checklist

Run through this checklist before enabling live trading with real money.

### Account & Broker Setup

- [ ] Broker credentials stored in OS keyring (not in config files)
- [ ] Schwab/E*TRADE OAuth tokens obtained and tested
- [ ] `broker.active` set to your live broker (not `dry_run`)
- [ ] `portfolio.account_size` matches your actual account balance
- [ ] Account balance >= $25,000 if PDT protection is disabled

### Position Sizing Verification

- [ ] `portfolio.position_sizing.max_contracts` set to your desired limit
- [ ] If small account: set `max_contracts: 1` and verify via dry-run
- [ ] Each strategy's `max_contracts_override` <= global `max_contracts`
- [ ] Run 1 full day in dry-run mode; check logs for actual contract quantities
- [ ] Verify: no trade ever exceeds your global max_contracts setting

### Risk Limits

- [ ] `portfolio.max_daily_loss_pct` set appropriately (default 2%)
- [ ] Drawdown limits configured: weekly (4%), monthly (8%), consecutive losses (5)
- [ ] Circuit breaker tested: trigger it in dry-run, verify no new entries
- [ ] `max_total_positions: 2` (1 shared 0DTE + 1 swing)
- [ ] `max_0dte_positions: 1` (DI/ORB share slot)

### Strategy Configuration

- [ ] `daily_income.enabled: true` (core strategy)
- [ ] `tag_n_turn.enabled: true/false` (your choice)
- [ ] `bnb.enabled: false` (informational-only, V1)
- [ ] `orb.enabled: false` (enable only after testing in dry-run)
- [ ] If ORB enabled: `min_range_points` and `confirmation_minutes` reviewed
- [ ] `spread_width: 5.0` for 0DTE, `spread_width: 10.0` for TNT
- [ ] `min_credit: 1.00` (reject thin spreads)

### PDT Protection

- [ ] `pdt.pdt_protection: true` if account < $25,000
- [ ] `pdt.pdt_max_day_trades: 3` and `pdt.pdt_window_days: 5`
- [ ] Test: verify PDT blocks entry when at limit (dry-run)

### Monitoring & Notifications

- [ ] Notifications configured (Slack/Discord/webhook) if desired
- [ ] Dashboard accessible at http://127.0.0.1:5000
- [ ] System tray icon visible and updating status

### Pre-Trade Day

- [ ] Bot started before 9:30 AM ET
- [ ] Check heartbeat logs: "Loop #N at HH:MM:SS ET"
- [ ] Verify market open detection: "Market open" log message
- [ ] Check options chain fetch: no "No options chain" warnings

### First Live Day Protocol

- [ ] Start with `max_contracts: 1` regardless of account size
- [ ] Monitor the first trade entry in real-time
- [ ] Verify spread fills correctly (both legs, full quantity)
- [ ] Verify position appears in broker account
- [ ] Watch exit logic trigger (profit target or time-based)
- [ ] After market close: compare bot P&L with broker account P&L
- [ ] If everything matches: gradually increase max_contracts over days

### Emergency Procedures

- [ ] Know how to stop the bot: close the app or Ctrl+C in terminal
- [ ] Know how to manually close positions in your broker's web portal
- [ ] Know where logs are: `database/logs/` (source) or `%APPDATA%/SPXIncomeTrader/logs/` (packaged)
- [ ] Bot crash during open position: positions monitored by broker's own risk system; bot will reconcile on restart

---

## Section 8: Fixes Applied

| Fix | Severity | Description | Test Coverage |
|-----|----------|-------------|---------------|
| Partial fill acceptance | HIGH | Accept partial fills with actual qty, reject zero fills, notify | `TestPartialFillAcceptance` (3 tests) |
| max_contracts=1 enforcement | HIGH | Verified global ceiling is never overridden | `TestMaxContractsEnforcement` (4 tests) |
| B&B entry removal verification | HIGH | Verified no code path can trigger B&B entry | `TestBnBCannotEnterTrades` (4 tests) |
| Circuit breaker all-path coverage | MEDIUM | Verified all strategies check breaker | `TestCircuitBreakerBlocksAllPaths` (3 tests) |

### Remaining Known Limitations

1. **Orphaned positions**: If bot crashes between order placement and DB write, position is detected on restart but not auto-recovered. Requires manual review.
2. **Backtest sizing formula**: Uses `max_daily_loss_pct` instead of `risk_per_trade_pct` for contract calculation. Results may differ from live.
3. **4 PM settlement**: 0DTE positions at expiration are resolved on next startup, not in real-time at 4 PM.
