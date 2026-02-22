# Pre-Live Trading Audit Report

**Date:** 2026-02-17
**Auditor:** Automated code audit
**Scope:** Full codebase review for live E*TRADE/Schwab deployment
**Test Suite:** 433/433 passed (0 failures, 1 unrelated deprecation warning)

---

## Executive Summary

The trading system is architecturally sound with comprehensive risk management, multi-layer authentication safety, and correct order lifecycle handling. No critical showstoppers were found that would cause immediate financial loss. All HIGH, MEDIUM, and LOW findings have been resolved.

**Recommendation: GO** -- ready for live deployment.

---

## Findings Summary

| Severity | Count | Status |
|----------|-------|--------|
| CRITICAL | 0 | -- |
| HIGH | 4 | **All fixed** |
| MEDIUM | 5 | **All fixed** |
| LOW | 3 | **All fixed** |
| PASS | 38 | Verified correct |

---

## 1. Order Lifecycle

### 1.1 Spread Construction

**[PASS] Credit spread leg ordering**
Both brokers correctly assign SELL_OPEN to the short leg and BUY_OPEN to the long leg for opens, and BUY_CLOSE/SELL_CLOSE for closes.
- E*TRADE: `etrade_broker.py:564-636` (`_build_spread_order`)
- Schwab: `schwab_broker.py:443-525` (uses schwab-py `bear_call_vertical_open` / `bull_put_vertical_open` templates)

**[PASS] LIMIT orders only**
Both brokers use LIMIT order type exclusively. No market orders exist anywhere in the codebase.
- E*TRADE: `etrade_broker.py:587` (`LIMIT`)
- Schwab: schwab-py `OrderBuilder` enforces LIMIT via `set_price()`

**[PASS] Credit quality gate**
Non-positive credits are rejected before order placement.
- `strategy.py:168` - `if credit <= 0` rejects debit spreads
- `main.py:1327-1331` - Same guard in `_construct_strategy_spread`
- `main.py:1332-1336` - Minimum credit floor enforced

**[PASS] OCC symbol format**
- E*TRADE: Uses standard SPX root (`etrade_broker.py:536-560`)
- Schwab: Uses SPXW root via schwab-py `OptionSymbol` (`schwab_broker.py:395-418`)

**[PASS] Strike selection sanity check**
ATM rounded to nearest $5 with 1000-20000 range validation.
- `strategy.py:112-117` - Sanity bounds on computed strikes

**[PASS] E*TRADE preview-then-place pipeline**
Orders go through preview before placement. If preview fails, no order is placed.
- `etrade_broker.py:809-889` (`place_spread_order`)

### 1.2 Order Status Normalization

**[PASS] E*TRADE status mapping**
E*TRADE returns 'EXECUTED'; broker normalizes to 'filled' via `_STATUS_MAP`.
- `etrade_broker.py:970-978` - Maps EXECUTED->filled, OPEN->open, CANCEL_REQUESTED->open, CANCELLED->cancelled, etc.

**[PASS] Schwab status mapping**
Schwab returns 'FILLED'; broker normalizes to 'filled' via `_SCHWAB_STATUS_MAP`.
- `schwab_broker.py:70-95` - Maps FILLED->filled, WORKING->open, CANCELED->cancelled, etc.

### 1.3 Fill Handling

**[HIGH] H-1: Partial fill quantity mismatch**
When E*TRADE returns a partial fill, `_wait_for_fill()` treats it as a successful fill and returns the actual `filled_quantity`. However, `position_manager.enter_trade()` at line 186 records `quantity=quantity` (the *requested* amount), not the actual filled amount. If 5 contracts are requested and 3 fill, the trade record says 5 contracts but only 3 exist at the broker. When closing, the bot would attempt to close 5 contracts.

- `etrade_broker.py:748-762` - PARTIAL treated as EXECUTED, returns actual filled_quantity
- `position_manager.py:186` - Uses requested quantity, ignores fill response
- `position_manager.py:171-175` - Checks `order_status['status']` but not `order_status.get('filled_quantity')`

**Risk:** SPX index options are highly liquid, making partial fills extremely rare. But if one occurs, the portfolio tracker and close order would have a quantity mismatch.

**Fix:** In `enter_trade()`, after line 175, read `filled_quantity` from `order_status` and use it instead of the requested `quantity` if it differs. Log a warning when partial fill detected.

**[PASS] Schwab partial fill handling**
Schwab's `_SCHWAB_STATUS_MAP` does not include 'PARTIALLY_FILLED', so a partial fill status would not match 'filled' and the poller would keep waiting (treating it as still open). This is actually safer than E*TRADE's approach -- it waits for full fill or timeout.
- `schwab_broker.py:674-726` (`_poll_order_fill`)

### 1.4 Exit Paths

**[PASS] Profit target exit (80%)**
Checked every monitoring cycle via `strategy.should_exit()`.
- `strategy.py:189-201` - `check_profit_target()`
- `position_manager.py:340-345` - Calls should_exit in monitor loop

**[PASS] 1PM management check**
Trend-based management at 1:00 PM ET, auto-close for non-trending conditions.
- `strategy.py:203-271` - `check_1pm_management()`

**[PASS] 4PM expiration handling**
Expiration check uses `now >= expiration` where expiration is set to 16:00 ET.
- `strategy.py:273-282` - `check_expiration()`
- `position_manager.py:389-404` - Intrinsic value calculation at expiration (European-style, correct)

**[PASS] Close order retry**
If close order fails to fill, `_close_pending` is reset to False and the position is retried on the next monitoring cycle (30s interval).
- `position_manager.py:420-436` - Reset and retry logic

**[HIGH] H-2: No max retry count on close attempts**
If a close order repeatedly fails to fill (price moved away from limit, API issues), the bot will retry every 30-second cycle indefinitely. There is no retry counter, escalation to market order, or alerting after N failures.

- `position_manager.py:309-311` - `_close_pending` flag check
- `position_manager.py:420-436` - Reset on failure, no counter

**Risk:** An unfilled close order on a 0DTE spread near expiration could leave the position open past the intended exit window. The position would eventually expire and settle, but the intended exit timing would be lost.

**Fix:** Add a retry counter to each trade. After N retries (e.g., 5), either widen the limit price by an additional increment or log a HIGH-priority alert. Consider market order fallback as last resort near expiration.

**[PASS] Order timeout with cancel**
E*TRADE orders that don't fill within 30 seconds are automatically cancelled.
- `etrade_broker.py:775-791` - Cancel on timeout, logs ALERT if cancel fails

### 1.5 Quantity and Sizing

**[PASS] Position sizing enforcement**
`calculate_position_size()` in portfolio_manager enforces global `max_contracts` cap (20) and per-strategy overrides.
- `portfolio_manager.py:287-342` - `calculate_position_size()`

**[PASS] Risk gate before entry**
`can_enter_position()` checks circuit breaker, daily loss, weekly/monthly drawdown, position limits, and total risk before allowing entry.
- `portfolio_manager.py:178-242` - `can_enter_position()`

---

## 2. Token & Auth Management

### 2.1 E*TRADE OAuth 1.0a

**[PASS] Triple-layer token renewal**
1. Background thread renews every 90 minutes (`main.py:222-252`)
2. Proactive renewal at 110 minutes in `_request()` (`etrade_broker.py:142-166`)
3. Reactive 401 retry with token renewal (`etrade_broker.py:180-205`)

**[PASS] Token file permissions**
Token files written with `os.chmod(0o600)` for owner-only read/write.
- `etrade_auth.py:268-276` - `_save_tokens()`

**[PASS] Conservative reload check**
Tokens older than 1.5 hours trigger renewal on load (conservative vs 2-hour expiry).
- `etrade_auth.py:290-308` - `_load_tokens()`

**[PASS] Background renewal thread safety**
Token renewal thread is daemon thread, only touches token state (no trading logic).
- `main.py:235-252` - `_token_renewal_loop()`

### 2.2 Schwab OAuth2

**[PASS] Automatic access token refresh**
schwab-py's `client_from_token_file()` handles access token refresh transparently.
- `schwab_auth.py:78-112` - `get_client()`

**[PASS] 7-day refresh token tracking**
Warnings emitted at 48h and 12h before refresh token expiry.
- `schwab_auth.py:148-190` - `get_token_status()`

**[PASS] Token metadata permissions**
Schwab token metadata saved with `os.chmod(0o600)`.
- `schwab_auth.py:126-140` - `_write_metadata()`

**[PASS] Reactive 401 retry (Schwab)**
Re-creates client on 401, retries the request.
- `schwab_broker.py:188-247` - `_call_api()`

### 2.3 Credential Storage

**[PASS] OS keyring for secrets**
Both E*TRADE and Schwab credentials stored in OS keyring, never in files.
- E*TRADE: `config/settings.py:130-153` - `save_etrade_credentials()` uses keyring service `spx-income-trader`
- Schwab: `config/settings.py:180-200` - `save_schwab_credentials()` uses keyring service `spx-income-trader-schwab`

---

## 3. P&L Calculations

### 3.1 SPX Contract Properties

**[PASS] $100 multiplier**
Correctly applied throughout.
- `spread.py:53` - `max_profit = credit_received * 100`
- `spread.py:58` - `max_risk = (spread_width - credit_received) * 100`
- `trade.py:58` - `update_pnl()`: `(entry_price - current_price) * 100 * quantity`

**[PASS] European-style settlement**
Expiration P&L calculated from intrinsic value at underlying price, not from live option prices.
- `spread.py:68-87` - `profit_at_price()` uses correct European cash-settlement math
- `position_manager.py:391-403` - Expiration path calls `spread.profit_at_price(underlying_price)`

**[PASS] Cash-settled / PM-settled**
No physical delivery logic exists. Settlement is purely cash-based at expiration.

### 3.2 P&L Flow

**[PASS] Single source of truth**
`portfolio.daily_realized_pnl` is the sole P&L tracker. No duplicate tracking.
- `main.py:777-797` - `_check_daily_loss_circuit_breaker()` reads from `portfolio.daily_realized_pnl`
- `main.py:953-957` - `_drain_recently_closed()` feeds realized P&L to portfolio

**[PASS] Circuit breaker uses realized P&L only**
Unrealized P&L does not trigger the circuit breaker.
- `portfolio_manager.py:193-198` - Checks `daily_realized_pnl` vs `max_daily_loss`

**[PASS] get_position_value() returns per-share**
Both brokers return per-share mid-price from the options chain. Callers multiply by 100 and quantity.
- `etrade_broker.py:1035-1062` - Per-share mid-price
- `schwab_broker.py:744-768` - Same approach

**[PASS] Daily reset at market open**
`reset_daily()` clears daily P&L, removes stale 0DTE positions, keeps swing positions.
- `portfolio_manager.py:137-176` - `reset_daily()`
- `main.py:503-520` - Called at `daily_reset_done` gate

**[PASS] Restore counters on restart**
Trade counts and realized P&L restored from database on bot restart.
- `main.py:669-695` - `_restore_daily_counters()`

---

## 4. Risk Gates

### 4.1 Daily Loss Circuit Breaker

**[PASS] 2% daily loss limit**
Default `max_daily_loss_pct: 2.0` ($1,000 at $50k account).
- `strategy_params.yaml:20` - `max_daily_loss_pct: 2.0`
- `portfolio_manager.py:193-198` - Enforced in `can_enter_position()`

**[PASS] Re-check before breakout entry**
Circuit breaker re-checked in `_check_breakout_trigger()` because parallel strategy exits in `_update_market_state()` may have pushed P&L past the limit.
- `main.py:1533-1537` - Re-checks circuit breaker before DI entry

### 4.2 Drawdown Manager

**[PASS] Weekly drawdown limit (4%)**
ISO-week-based tracking with auto-reset on period rollover.
- `drawdown_manager.py:98-130` - Weekly drawdown check and reset

**[PASS] Monthly drawdown limit (8%)**
Calendar-month-based tracking.
- `drawdown_manager.py:132-165` - Monthly drawdown check and reset

**[PASS] Consecutive loss tracking**
5 consecutive losses trigger a 24-hour trading pause.
- `drawdown_manager.py:167-198` - Consecutive loss counter and time-based pause
- `strategy_params.yaml:31-32` - `max_consecutive_losses: 5`, `cooldown_hours: 24`

**[PASS] State persistence**
Drawdown state saved to `database/drawdown_state.json`, survives restarts.
- `drawdown_manager.py:285-320` - JSON serialization/deserialization

### 4.3 Position Limits

**[PASS] Total position limit (2)**
`max_total_positions: 2` enforced.
- `portfolio_manager.py:203-208` - Checked in `can_enter_position()`

**[PASS] 0DTE position limit (1)**
`max_0dte_positions: 1` prevents multiple same-day expirations.
- `portfolio_manager.py:210-215` - Checked when `is_0dte=True`

**[PASS] Per-strategy position check**
Daily Income only enters when it has no existing position.
- `main.py:1539` - `has_position_for_strategy(StrategyType.DAILY_INCOME)`
- `main.py:1610` - Same check in `_check_for_setups()`

### 4.4 PDT Compliance

**[PASS] Rolling 5-business-day window**
Tracks day trades with NYSE holiday awareness.
- `pdt_tracker.py:85-130` - Rolling window calculation

**[PASS] Entry gate**
New trades blocked if no day trade slots available.
- `main.py:1400-1409` - `_execute_strategy_trade()` checks `can_open_trade()`
- `main.py:1675-1684` - `_execute_setup()` checks `can_open_trade()`

**[PASS] Exit gate**
Early exits blocked if closing would consume the last PDT slot.
- `pdt_tracker.py:180-220` - `can_close_early()`

**[LOW] L-1: PDT holidays hardcoded through 2026**
NYSE holiday list covers 2024-2026. Will need 2027 holidays added.
- `pdt_tracker.py:40-82` - `NYSE_HOLIDAYS` dict

### 4.5 Live Mode Safety Gates

**[PASS] --confirm-live required**
Live mode start requires explicit `--confirm-live` CLI flag.
- `main.py:1976-1982` - Blocks without flag, prints warning

**[PASS] Pre-flight checks**
Before live trading starts, verifies SPX quote, options chain availability, and account balance.
- `main.py:2016-2053` - Three checks, all must pass

**[PASS] Safe default broker**
`broker.active: "dry_run"` in strategy_params.yaml. Must be explicitly changed.
- `strategy_params.yaml:37` - Default value

**[PASS] Broker factory validation**
`get_broker()` raises ValueError for unknown broker type.
- `broker_factory.py:25-42` - Explicit validation

**[PASS] DryRunBroker isolation**
DryRunBroker never calls any real broker API. Uses Yahoo Finance for data, simulates fills locally.
- `dry_run_broker.py` - All methods are local simulations

---

## 5. Edge Cases & Failure Modes

### 5.1 Network/API Failures

**[PASS] HTTP request timeout**
E*TRADE: 30-second timeout on every HTTP request.
- `etrade_broker.py:107` - `REQUEST_TIMEOUT = 30`
- `etrade_broker.py:168-172` - Applied to GET/POST/PUT

**[PASS] 429 rate limiting**
Both brokers implement exponential backoff on 429 responses.
- `etrade_broker.py:189-196` - Up to 3 retries with exponential backoff
- `schwab_broker.py:223-232` - Same pattern

**[PASS] 5xx server error retry**
Both brokers retry on 500/502/503/504.
- `etrade_broker.py:198-207` - Retry with backoff
- `schwab_broker.py:234-243` - Retry with backoff

**[PASS] Connection error handling**
Network failures caught and logged with retry.
- `etrade_broker.py:209-222` - Catches `requests.exceptions.RequestException`

### 5.2 Race Conditions

**[PASS] Single-threaded trading logic**
Main loop is single-threaded. No race conditions possible in trading decisions.
- `main.py:460-667` - `_run_main_loop()` is sequential

**[PASS] Instance lock**
OS-level file lock prevents multiple bot instances.
- `main.py:118-176` - `_acquire_instance_lock()` using msvcrt/fcntl

**[PASS] Token renewal isolation**
Background renewal thread only touches token state, never trading state.
- `main.py:235-252` - Daemon thread, isolated scope

### 5.3 Market Edge Cases

**[FIXED] M-1: Early close days not handled**
Added `src/utils/market_calendar.py` with `EARLY_CLOSE_DATES` set and `get_market_close_time()` helper. On half-days (July 3, Black Friday, Christmas Eve), expiration is now set to 13:00 ET. `_is_market_open()` in main.py uses the dynamic close time. Both `strategy.py:construct_spread()` and `main.py:_construct_strategy_spread()` use the helper for expiration.

**[FIXED] M-2: Stale price / trading halt detection**
Added `_stale_price_count` and `_last_spx_price` tracking to TradingBot. When SPX price is unchanged for 3+ consecutive cycles, a WARNING is logged identifying a possible data feed issue or trading halt. Counter resets when price changes.

**[FIXED] M-3: Bid-ask sanity check on spread construction**
Both `strategy.py:construct_spread()` and `main.py:_construct_strategy_spread()` now validate that `short_bid > 0` and `long_ask > 0` before proceeding. Zero/negative prices reject the spread as stale. Additionally, each leg's bid-ask spread is checked; if wider than 50% of mid-price, a warning is logged.

### 5.4 Crash Recovery

**[PASS] Orphaned trade resolution**
`resolve_expired_trades()` runs at startup, settles orphaned trades using historical SPX close.
- `position_manager.py:534-607` - Queries DB for active trades past expiration

**[PASS] Position reconciliation**
On restart, compares DB positions vs broker positions, logs warnings for mismatches.
- `main.py:697-733` - `_reconcile_positions()` (warning-only, no auto-fix)

---

## 6. Configuration Safety

**[FIXED] H-3: validate_settings() only checks E*TRADE**
`validate_settings()` now checks `broker.active` and validates the corresponding broker's credentials. E*TRADE: checks consumer_key, consumer_secret, account_id. Schwab: checks app_key, app_secret. Unknown broker types are rejected. `dry_run` skips credential checks.

**[PASS] Trading mode validation**
`TRADING_MODE` validated as either 'dry-run' or 'live'.
- `config/settings.py:381-382` - Rejects invalid modes
- `config/settings.py:273` - `save_trading_mode()` only accepts 'dry-run' or 'live'

**[PASS] Credential priority: keyring > env > default**
Both brokers use keyring first, fall back to environment variables.
- `config/settings.py:42-88` - E*TRADE priority chain
- `config/settings.py:203-229` - Schwab priority chain (keyring > YAML)

**[PASS] Strategy params from file**
`load_strategy_params()` loads from YAML with sensible defaults if file missing.
- `config/settings.py:319-347` - YAML loader with fallback defaults

---

## 7. Logging & Monitoring

### 7.1 Order Logging

**[PASS] Trade entry logging**
Full details logged: direction, strikes, credit, quantity, sizing method, slippage.
- `main.py:1771-1783` - Signal log entry
- `position_manager.py:152-156` - Credit quality logged
- `position_manager.py:260-270` - Slippage calculation and logging

**[PASS] Trade exit logging**
Exit reason, P&L, duration, exit context all logged.
- `position_manager.py:382-384` - Exit reason
- `position_manager.py:449-451` - P&L and duration
- `position_manager.py:462-490` - Exit context (SPX at exit, profit captured %, time in trade, daily move)

**[PASS] System event logging**
Bot start/stop, circuit breaker triggers, PDT blocks logged to database.
- `main.py:1897-1904` - Shutdown event
- `main.py:1404-1408` - PDT block event

**[PASS] Signal rotation**
Signal log rotated at 5,000 entries with archive.
- `main.py:1866-1872` - Rotation logic

### 7.2 Error Handling

**[FIXED] H-4: Bare except in etrade_broker.py**
Replaced `except:` at line 222 with `except (ValueError, KeyError, TypeError):` so only expected JSON parsing errors are caught.

**[FIXED] L-2: Bare except in dry_run_broker.py**
Replaced `except:` in `get_signal_log()` with `except Exception:`.

**[FIXED] M-4: schwab-py float deprecation warning**
All schwab-py order calls now pass `str(limit_price)` instead of float. Test file also updated. Deprecation warnings dropped from 10 to 0 (1 remaining is unrelated websockets warning).

### 7.3 Monitoring Gaps

**[FIXED] M-5: Close-order retry counter logging**
Addressed by H-2 fix. Close retries are now counted per trade, logged with attempt number, and emit CRITICAL alert after 10 failures.

---

## 8. Database Integrity

**[PASS] WAL mode**
SQLite WAL mode enabled for concurrent read/write access.
- `db_manager.py:45-47` - `PRAGMA journal_mode=WAL` in `_get_connection()`

**[PASS] Connection timeout**
10-second timeout prevents indefinite blocking on locked database.
- `db_manager.py:42` - `timeout=10`

**[PASS] Auto migrations**
New columns added automatically via migration files.
- `db_manager.py:65-95` - `_run_migrations()` applies pending migrations

**[PASS] Comprehensive trades table**
52 fields in `save_trade()` covering all entry/exit context, analytics, and metadata.
- `db_manager.py:137-252` - `save_trade()` with INSERT OR REPLACE

**[PASS] Proper indexes**
Indexes on `entry_time`, `status`, `date` for query performance.
- `schema.sql:75-80` - Index definitions

**[PASS] Backup before cleanup**
Database backed up before cleanup operations (resolve_expired_trades).
- `db_manager.py:470-480` - Backup logic

**[FIXED] L-3: Signal log file I/O now atomic**
`_log_signal()` now uses write-to-temp-then-rename pattern via `tempfile.mkstemp()` + `os.replace()`. Prevents corruption from interrupted writes.

---

## Additional Observations

### Non-Interactive Trade Confirmation
In live mode, `_confirm_trade()` auto-accepts when `sys.stdin.isatty()` returns False. This means trades execute without human confirmation when running from:
- Desktop app (pywebview, non-terminal)
- Background services / cron jobs
- Systemd units

This is by design for autonomous operation, but operators should be aware. The `--auto-trade` flag controls this for terminal sessions.
- `main.py:1826` - `if self.skip_confirm or not sys.stdin.isatty(): return True`

### Position Reconciliation is Warning-Only
`_reconcile_positions()` compares broker positions vs DB but only logs warnings -- it does not auto-correct mismatches. This is a conservative design choice.
- `main.py:697-733`

---

## Fixes Applied

All 12 findings have been resolved:

| # | ID | Fix | Files Changed |
|---|-----|-----|---------------|
| 1 | H-1 | Partial fill quantity tracking | `position_manager.py` |
| 2 | H-2 | Close order retry limit with escalation | `position_manager.py` |
| 3 | H-3 | Schwab validation in validate_settings() | `config/settings.py` |
| 4 | H-4 | Typed exception in etrade_broker.py | `etrade_broker.py` |
| 5 | M-1 | Early close day calendar (2025-2027) | `src/utils/market_calendar.py` (new), `main.py`, `strategy.py` |
| 6 | M-2 | Stale price detection (3+ cycles) | `main.py` |
| 7 | M-3 | Bid-ask sanity check on spread construction | `main.py`, `strategy.py` |
| 8 | M-4 | schwab-py price as string | `schwab_broker.py`, `test_schwab_broker.py` |
| 9 | M-5 | Close retry counter with escalation | (covered by H-2) |
| 10 | L-1 | 2027 NYSE holidays added | `pdt_tracker.py` |
| 11 | L-2 | Typed exception in dry_run_broker.py | `dry_run_broker.py` |
| 12 | L-3 | Atomic signal log writes | `main.py` |

---

## Test Suite Results

```
Platform: Windows 10 Pro (Python 3.14)
Tests:    433 passed, 0 failed, 0 errors
Duration: 17.89 seconds
Warnings: 1 (unrelated websockets deprecation)
```

All test categories passing:
- Strategy logic (pulse detection, breakout, setup windows)
- Position management (sizing, P&L, exit triggers)
- Risk gates (circuit breaker, position limits, credit quality)
- PDT compliance tracking and entry gating
- Bar building and market state management
- Drawdown management and consecutive loss tracking
- VIX provider multi-source fallback
- Schwab broker order building and status mapping
- E*TRADE broker status normalization
- Slippage tracking and DB migration
- Live validation pre-flight checks

---

## GO/NO-GO Assessment

### GO (Unconditional)

All audit findings have been resolved. The system is ready for live deployment.

**Recommended first-week practices:**
1. **Start with 1-2 contracts** -- minimize exposure while validating live fills
2. **Monitor first 5 trades manually** -- verify fill prices, quantities, and exit behavior match expectations
3. **Use --auto-trade cautiously** -- consider running first few sessions with manual trade confirmation

### What makes this a GO:
- Order lifecycle is correct (LIMIT only, preview-before-place, status normalization)
- Risk management is comprehensive (circuit breaker, drawdown, PDT, position limits)
- Triple-layer token renewal prevents auth failures
- Pre-flight checks catch configuration problems at startup
- Safe defaults (dry_run, --confirm-live required)
- All 433 tests pass with 0 relevant warnings
- Crash recovery handles orphaned trades
- Credentials stored securely in OS keyring
- Partial fills tracked by actual quantity (H-1 fixed)
- Close order retries limited with escalation (H-2 fixed)
- Early close days handled automatically (M-1 fixed)
- Stale price / trading halt detection (M-2 fixed)
- Bid-ask sanity checks prevent stale chain entries (M-3 fixed)
- All bare except clauses replaced with typed exceptions (H-4, L-2 fixed)
- Signal log writes are atomic (L-3 fixed)
