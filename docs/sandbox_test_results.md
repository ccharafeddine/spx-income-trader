# E*TRADE Sandbox API Test Results

**Date:** 2026-02-09
**Environment:** Sandbox (`apisb.etrade.com`)
**Credential Source:** `.env` file
**Token Status:** Valid (1 hour old, renewal successful)

---

## 1. Configuration Verification

| Check | Status | Notes |
|-------|--------|-------|
| Consumer Key | PASS | Set in `.env` (not placeholder) |
| Consumer Secret | PASS | Set in `.env` (not placeholder) |
| Account ID | PASS | `****5980` (MARGIN account) |
| Sandbox Mode | PASS | `ETRADE_SANDBOX=true` |
| Base URL | PASS | `https://apisb.etrade.com` |
| Credential Source | PASS | Loaded from `.env` via `dotenv` |
| Keyring fallback | N/A | Not tested (env credentials found first) |

**TRADING_MODE** is set to `dry-run`. Switching to `live` requires changing `.env` and passing `--confirm-live` flag to `main.py`.

---

## 2. OAuth Authentication

| Check | Status | Notes |
|-------|--------|-------|
| Token file exists | PASS | `tokens/sandbox_tokens.json` |
| Token load | PASS | Tokens loaded from file |
| Token renewal | PASS | `GET /oauth/renew_access_token` returned 200 |
| Token age | OK | ~1 hour (within 2-hour validity) |
| Session creation | PASS | `OAuth1Session` created successfully |

### Token Lifecycle Notes
- Tokens expire after **2 hours of inactivity** or at **midnight ET**
- `etrade_auth.py` implements `_renew_token()` which extends validity
- Dashboard has auto-renewal via `_try_renew_etrade_token()` on each status check
- **GAP:** `main.py` (headless bot) has NO automatic token renewal thread. If a trading session runs >2 hours without the dashboard, tokens will expire mid-session and API calls will fail.

---

## 3. Account & Connection Tests

| Test | Status | Result |
|------|--------|--------|
| `broker.connect()` | PASS | Connected, loaded 4 accounts |
| `broker.get_accounts()` | PASS | 4 accounts (MARGIN, 2x INDIVIDUAL, CASH) |
| `broker.get_account_balance()` | PASS* | All values $0.00 |

*Sandbox accounts return $0 balances. This is a **known E*TRADE sandbox limitation** - the sandbox is for API structure testing, not balance simulation. Real balances will appear in production.

**Account matching:** Bot's configured account `823145980` correctly matched to the MARGIN account (first in list). `account_id_key` resolved correctly.

---

## 4. Market Data Tests

### 4a. SPX Quote (`get_current_price`)

| Check | Status | Notes |
|-------|--------|-------|
| API call | PASS | Returned data |
| Price value | **FAIL** | Returned **$577.51** (should be ~$6,000+) |
| Symbol mapping | **FAIL** | `$SPX.X` resolved to **GOOG** data |

**Root cause:** The E*TRADE sandbox **does not support index symbols** (`$SPX.X`). It maps unknown symbols to test data (currently Google stock). This is a **well-documented sandbox limitation**.

**Impact:** SPX price data from the sandbox is **unusable** for strategy testing. The production API correctly supports `$SPX.X`.

### 4b. VIX Quote

| Check | Status | Notes |
|-------|--------|-------|
| `get_quote('VIX')` | **FAIL** | Returned GOOG data ($577.51) |
| `get_quote('$VIX.X')` | **FAIL** | Same - returned GOOG data |

**Root cause:** Same sandbox limitation. VIX is not available in sandbox.

**Impact:** VIX data used for:
- Synthetic chain volatility estimation (DryRunBroker)
- Trade journal context fields (`vix_at_signal`)
- Not directly used by ETradeBroker for trading decisions

**Note:** ETradeBroker does NOT have a `get_vix()` method. The DryRunBroker gets VIX via `YahooFinanceProvider.get_vix_quote()`. When switching to live, VIX data for the trade journal would need to come from either Yahoo Finance (supplementary) or an E*TRADE `get_quote('$VIX.X')` call.

### 4c. Detailed Quote Fields

| Field | Value | Expected |
|-------|-------|----------|
| `lastTrade` | 577.51 | ~6,000+ (SPX) |
| `bid` | 574.04 | Valid for GOOG |
| `ask` | 579.73 | Valid for GOOG |
| `high` | 0.00 | Sandbox: stale |
| `low` | 0.00 | Sandbox: stale |
| `open` | 0.00 | Sandbox: stale |
| `volume` | 0 | Sandbox: no volume |

**Conclusion:** Quote API structure is correct. Response parsing works. Only the data itself is wrong due to sandbox symbol limitations.

---

## 5. Options Data Tests

### 5a. Option Expirations

| Check | Status | Notes |
|-------|--------|-------|
| API call | PASS | Returned 7 expirations |
| Dates current | **FAIL** | All dates are 2010-2015 |
| 0DTE available | **FAIL** | No current-day expirations |

**Returned dates:** 2010-01-16, 2011-01-22, 2012-01-21, 2013-01-19, 2013-06-22, 2014-01-18, 2015-01-17

**Root cause:** Sandbox options data is **frozen historical test data**. No current expirations exist.

### 5b. Option Chain

| Check | Status | Notes |
|-------|--------|-------|
| API call | PASS | Chain returned for 2015-01-17 |
| Strikes returned | **PARTIAL** | Only 1 strike (485) |
| Chain format | PASS | All required keys present |
| Bid/Ask data | PASS* | Values present but stale |

**Chain format verification:**
```
Required keys present: call_bid, call_ask, put_bid, put_ask  [PASS]
Extra keys present:    call_last, call_volume, call_oi, call_symbol,
                       put_last, put_volume, put_oi, put_symbol  [PASS]
```

The option chain format from E*TRADE **exactly matches** what `strategy.py` and `position_manager.py` expect. The key names (`call_bid`, `call_ask`, `put_bid`, `put_ask`) are compatible. No translation layer needed.

---

## 6. Order Lifecycle (Preview Only)

**Not tested** - sandbox option chain data is too stale to construct a realistic spread. A preview order would require valid current-day option symbols.

**What the code does:**
1. `place_spread_order()` builds a `PreviewOrderRequest` with LIMIT pricing
2. Posts to `/v1/accounts/{id}/orders/preview`
3. If `dry_run=True`, returns `PREVIEW-{preview_id}` without placing
4. If `dry_run=False`, converts preview to `PlaceOrderRequest` with `preview_id`
5. Polls `get_order_status()` until filled or timeout
6. Auto-cancels unfilled orders after 30s

**Safety features verified in code review:**
- All orders are LIMIT (never MARKET)
- Preview required before placement
- Full order details logged before submission
- Exponential backoff retry for transient failures
- Unfilled order auto-cancel on timeout

---

## 7. Critical Gaps Found

### GAP 1: Order Status Mismatch (CRITICAL)

**File:** `src/core/position_manager.py:125`

```python
# position_manager expects:
if order_status['status'] != 'filled':    # lowercase "filled"

# DryRunBroker returns:
{'status': 'filled', ...}                 # lowercase - WORKS

# ETradeBroker returns:
{'status': 'EXECUTED', ...}               # uppercase E*TRADE enum - BREAKS
```

**Impact:** When switching to live trading, `enter_trade()` will ALWAYS think the order failed because `'EXECUTED' != 'filled'`. Same issue in `_exit_trade()` at line 283.

**Fix:** Either:
- (a) Normalize ETradeBroker's `get_order_status()` to return `'filled'` instead of `'EXECUTED'`
- (b) Change position_manager to accept both `'filled'` and `'EXECUTED'`

### GAP 2: No Token Auto-Renewal in Headless Mode (HIGH)

**File:** `src/main.py` (entire file)

The bot's main loop can run for 6.5 hours (9:30 AM - 4:00 PM). E*TRADE tokens expire after 2 hours of inactivity. The dashboard has `_try_renew_etrade_token()` but `main.py` does NOT set up any renewal thread.

**Impact:** After ~2 hours, all API calls will fail with auth errors. The bot will hit consecutive error limits and shut down.

**Fix:** Add a background thread in `TradingBot` that calls `auth._renew_token()` every 90 minutes. Or renew before each API call if token age > 1.5 hours.

### GAP 3: Sandbox Returns Wrong Symbol Data (EXPECTED)

The sandbox maps `$SPX.X` to GOOG test data. This is a **known E*TRADE limitation** and NOT a code bug. The code correctly handles the SPX -> `$SPX.X` symbol mapping.

**Impact:** Cannot validate pricing accuracy in sandbox. Must be tested with production credentials during market hours.

### GAP 4: `place_spread_order` Signature Difference (LOW)

**Base interface:**
```python
def place_spread_order(self, spread, quantity, limit_price=None, metadata=None) -> str
```

**ETradeBroker:**
```python
def place_spread_order(self, spread, quantity, limit_price=None, metadata=None, dry_run=False) -> str
```

The extra `dry_run` parameter has a default value so it won't break calls, but `position_manager.py` never passes it. The ETradeBroker always places real orders when called via position_manager. This is actually correct behavior - the dry_run flag is only for manual testing.

### GAP 5: Missing `connect()` Call in Main Loop (MEDIUM)

`main.py` authenticates via `ETradeAuth()` and creates `ETradeBroker(auth=auth)`, but never calls `broker.connect()`. The `connect()` method loads accounts and resolves `account_id_key`.

Looking at the code: `connect()` calls `_load_accounts()`, which is also called lazily by methods that need `account_id_key`. However, it's **only called lazily in order methods** (`place_spread_order`, `close_position`), not in `get_current_price` or `get_options_chain`. The pre-flight checks in main.py would work because they call `get_current_price` (which doesn't need `account_id_key`) and `get_options_chain` (also doesn't need it). But `get_account_balance` DOES need it, and it has its own lazy `_load_accounts()` call.

**Impact:** Low - lazy loading covers most cases, but an explicit `connect()` call would be cleaner and fail-fast.

### GAP 6: VIX Data Source in Live Mode (LOW)

DryRunBroker gets VIX via `YahooFinanceProvider`. ETradeBroker has no VIX-specific method. The strategy doesn't require VIX for trading decisions, but the trade journal logs `vix_at_signal` for context.

**Impact:** Trade journal will lack VIX context in live mode unless supplementary Yahoo Finance data is used.

### GAP 7: `close_spread` Default Limit Price (LOW)

**Base interface:** `limit_price=0.20`
**ETradeBroker:** `limit_price=0.05`

Position manager calculates its own limit price: `limit_price = min(current_value + 0.10, 0.50)` and passes it explicitly. The default difference doesn't matter since it's always overridden.

---

## 8. Interface Compliance Matrix

| Method | Base Interface | DryRunBroker | ETradeBroker | Used By |
|--------|:---:|:---:|:---:|---------|
| `get_current_price(symbol)` | Required | Implemented | Implemented | main.py, position_manager |
| `get_options_chain(symbol, exp)` | Required | Implemented | Implemented | main.py |
| `place_spread_order(spread, qty, ...)` | Required | Implemented | Implemented | position_manager |
| `get_order_status(order_id)` | Required | Implemented | **Implemented (status mismatch)** | position_manager |
| `close_spread(spread, qty, price)` | Required | Implemented | Implemented | position_manager |
| `get_position_value(spread)` | Required | Implemented | Implemented | position_manager |
| `get_account_balance()` | Required | Implemented | Implemented | main.py |
| `connect()` | Not in base | N/A | Implemented | test script only |
| `get_accounts()` | Not in base | N/A | Implemented | test script only |
| `get_quote(symbol)` | Not in base | N/A | Implemented | test script only |
| `get_option_expirations(symbol)` | Not in base | N/A | Implemented | test script only |
| `get_orders(status)` | Not in base | N/A | Implemented | not used yet |

---

## 9. Dashboard Token Management

The dashboard (`dashboard/app.py`) has a complete OAuth management UI:

| Feature | Status | Notes |
|---------|--------|-------|
| OAuth flow start (`/auth/etrade/start`) | Implemented | Opens browser popup |
| Verifier code exchange (`/auth/etrade/callback`) | Implemented | Exchanges for access token |
| Token status display (`/auth/etrade/status`) | Implemented | Shows age, expiration |
| Token renewal (`_try_renew_etrade_token`) | Implemented | Auto-renews on status check |
| Disconnect (`/auth/etrade/disconnect`) | Implemented | Revokes and deletes tokens |
| Setup wizard (`/setup`) | Implemented | First-run credential entry |
| Connection test (`/api/test-connection`) | Implemented | Validates API connectivity |
| Settings page indicators | Implemented | Token age, expiry, auto-renew status |

**Dashboard token indicators work with sandbox connection.** Tested: token file loads, renewal succeeds, status endpoint returns correct data.

---

## 10. Recommendations - Priority Ordered

### Must Fix Before Live Trading

1. **Fix order status mismatch** (`position_manager.py:125, 283`)
   - Normalize `get_order_status()` return values across brokers
   - Suggested: ETradeBroker should return `'filled'` when status is `'EXECUTED'`
   - Effort: ~10 lines changed

2. **Add token auto-renewal to TradingBot** (`src/main.py`)
   - Add a daemon thread that renews tokens every 90 minutes
   - Or check token age before each API call and renew if > 1.5 hours
   - Effort: ~30 lines added

3. **Test with production credentials during market hours**
   - Sandbox cannot validate SPX pricing, option chains, or order previews
   - Use production API with `dry_run=True` to preview without placing orders
   - This is the only way to validate the full lifecycle

### Should Fix

4. **Add explicit `broker.connect()` in main.py** after creating ETradeBroker
   - Fail-fast instead of relying on lazy loading
   - Already done in pre-flight checks via `get_account_balance()` which triggers it

5. **Add VIX data source for live mode**
   - Either add `get_vix_price()` to ETradeBroker (using `$VIX.X`)
   - Or keep YahooFinance as supplementary data source for journal context

### Nice to Have

6. **Add order preview test** to the sandbox test script
   - Even with stale data, test that the preview API endpoint accepts the request format
   - Validates the order XML/JSON structure

7. **Add connection health monitoring**
   - Periodic `get_quote()` call to detect lost connectivity
   - Alert user if API calls start failing

---

## 11. Summary

| Category | Verdict |
|----------|---------|
| Configuration | PASS - credentials set, sandbox mode active |
| OAuth Authentication | PASS - token load, renewal, session creation work |
| Account Access | PASS - 4 accounts found, correct one selected |
| SPX Quotes | PASS (structure) / FAIL (data) - sandbox returns GOOG |
| Option Chains | PASS (structure/format) / FAIL (data) - stale 2015 data |
| Option Chain Compatibility | PASS - key names match strategy expectations |
| Order Lifecycle | NOT TESTED - need production API for real validation |
| Token Management | PASS (dashboard) / GAP (headless bot) |
| Interface Compliance | 1 CRITICAL gap (order status), 1 HIGH gap (token renewal) |

**Bottom line:** The E*TRADE integration code is well-structured with good safety features. Two issues must be fixed before going live: the order status string mismatch and the missing token auto-renewal. Full validation requires testing with production credentials during market hours, as the sandbox has fundamental data limitations.
