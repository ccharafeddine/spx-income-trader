---
name: pre-live-checklist
description: Operational readiness check for The Daily Melt before enabling live trading with real money. Run this after the full audit has already passed and you are ready to flip from dry-run to live. This is NOT a code audit — it is a configuration, connectivity, and risk gate verification pass. Read-only: no code changes, no test runs.
---

# Pre-Live Trading Checklist

A focused operational readiness check for The Daily Melt. Verifies that configuration, broker authentication, risk gates, database, notifications, and price feed are all correctly set up for the first live session.

## When To Use

- Immediately before switching from dry-run to live trading
- After any broker re-authentication (token expiry)
- After changing brokers (E*TRADE ↔ Schwab)
- After a long gap between live sessions (verify token still valid)
- After any settings changes that affect live trading behavior
- As a periodic Monday morning sanity check before market open

## When NOT To Use

- For code correctness issues → use the audit skill instead
- For backtest model verification → use the audit skill Part 5
- For strategy logic debugging → use the audit skill Part 1

---

## Running the Checklist

Feed this to your AI assistant:

```
Read the pre-live checklist skill at build/skills/pre-live-checklist/SKILL.md
and run the full checklist against the current codebase and config.
This is a read-only verification pass — do not make any code changes
and do not run the test suite. Output PASS, FAIL, or WARN for each
check with a one-line explanation. End with the GO/NO-GO summary table.
```

For a quick re-run after fixing FAILs:

```
Read the pre-live checklist skill at build/skills/pre-live-checklist/SKILL.md
and re-run only the sections that previously FAILed:
[list the section numbers that failed]
Confirm they now PASS before giving a GO verdict.
```

---

## Checklist

### Section 1: Configuration Review

Read `config/strategy_params.yaml` and verify:

1. `trading_mode` is set to `live` (not `dry-run`)
2. `broker.active` is set to `etrade` or `schwab` (not `dry_run`)
3. `strategies.daily_income.enabled = true`
4. `strategies.tag_n_turn.enabled = false` ← DI-only for first sessions
5. `strategies.orb.enabled = false`
6. `strategies.bnb.enabled = false`
7. `di_morning_bias_filter = true`
8. `portfolio.daily_contracts` is set (expected: 7) ← DI budget-driven cap
9. `portfolio.swing_contracts` is set (expected: 2) ← fixed swing size
10. `portfolio.spread_width` is set (expected: 5.0) ← DI spread width for sizing
11. `portfolio.max_contracts <= 10` ← hard cap, conservative first session
12. `portfolio.min_contracts = 1`
13. `risk.max_daily_loss_pct` is set (expected: 2.0%)
14. `risk.drawdown_limits.weekly.max_loss_pct` is set (expected: 4.0%)
15. `risk.drawdown_limits.monthly.max_loss_pct` is set (expected: 8.0%)
16. `risk.consecutive_losses.max_consecutive` is set (expected: 5)
13. `pdt.enable_1pm_management` exists under `pdt:` section
14. `monitoring.enable_1pm_check` does NOT exist (removed setting — if present, flag FAIL)
15. `spread_width = 5` (standard $5 wide spreads)
16. `min_credit >= 1.00`
17. `profit_target_pct = 80`

**Operator note:** Items 1, 2, and trading mode are intentionally left as `dry_run` in the repo default — they must be changed via the Settings page or keyring before going live. FAILs on these items are expected on a fresh setup and are resolved by the operator, not by code changes.

---

### Section 2: Broker Connectivity

18. **OAuth token file exists and is not expired**
    - Schwab: `database/schwab_token.json` exists and timestamp is recent
    - E*TRADE: token file exists and is within session window
    - WARN if file not found (expected on first-ever live session — resolve by running broker auth flow)

19. **Broker class imports without error**
    - `EtradeBroker`, `SchwabBroker`, and `broker_factory.get_broker()` importable from `src/main.py`

20. **`price_feed_state.json` exists**
    - Located in `database/` or platform data dir
    - WARN if not found (auto-created on first run — expected for fresh setup)

21. **Price feed source matches active broker**
    - If `price_feed_state.json` exists: `source` field matches `broker.active`
    - WARN if cannot verify (acceptable if file not yet created)

---

### Section 3: Risk Gate Verification

Read `src/core/portfolio_manager.py` and `src/main.py`:

22. **Circuit breaker fires before new entries**
    - `_check_daily_loss_circuit_breaker()` called before any strategy entry logic each cycle

23. **Max contracts cap respected**
    - `calculate_position_size()` clamps to `min(contracts, self.max_contracts)`
    - Per-strategy override applied after global cap

24. **PDT threshold check present**
    - Account equity compared to `pdt_threshold` (default $25,000) to set PDT mode
    - Null/error broker equity response fails safe (PDT ON, not OFF)

25. **Max 2 simultaneous positions enforced**
    - `can_enter_position()` checks `len(active_positions) >= max_total_positions`
    - `max_total_positions: 2` in config

26. **Duplicate DI entry guard**
    - `max_0dte_positions: 1` enforced — cannot open two DI positions on same day

---

### Section 4: Database Readiness

27. **Live trading database exists (or will auto-create)**
    - `database/trades_live.db` exists, OR
    - WARN if not found (auto-created on first connection — expected for first live session)

28. **WAL mode enabled**
    - `db_manager.py` contains `PRAGMA journal_mode=WAL`

29. **No open positions in live database**
    - If `trades_live.db` exists: query `open_positions` table — must be empty
    - If DB doesn't exist yet: PASS (clean start)
    - If stale open positions found: FAIL — must be resolved before starting

30. **Daily counters clean**
    - `PortfolioManager.reset_daily()` runs at start of each trading day
    - Verify the reset logic exists in `main.py`

---

### Section 5: Notification System

31. **At least one notification channel configured**
    - Check `notifications:` section in `strategy_params.yaml`
    - At least one of: `slack.enabled: true`, `discord.enabled: true`, `webhook.enabled: true`
    - FAIL if all channels disabled — live trading without notifications is not recommended

32. **Test notification fires**
    - If a channel is enabled: verify the send method exists and webhook URL is non-empty
    - FAIL if channel enabled but URL is empty or placeholder

33. **Trade entry notifications enabled**
    - `notifier.send("Trade Entered/...")` call exists in strategy entry path

34. **Trade exit notifications enabled**
    - `notifier.send("Trade Closed: ...")` call exists in position exit path

35. **Daily summary notification enabled**
    - `notifier.send_eod_summary(...)` call exists in end-of-day logic

**Operator note:** Notifications are optional for the system to function but strongly recommended for live trading. A FAIL on #31 is a NO-GO recommendation but not a hard block — operator can acknowledge and proceed at their own discretion.

---

### Section 6: Price Feed Readiness

36. **`src/data/price_feed.py` exists**
    - Contains `PriceFeed` ABC + `_BasePriceFeed` + `YahooPriceFeed` + `EtradePriceFeed` + `SchwabPriceFeed` + `create_price_feed()`

37. **Factory returns correct feed for live mode**
    - `create_price_feed()`: if `'Schwab'` in broker class → `SchwabPriceFeed`
    - `create_price_feed()`: if `'ETrade'` in broker class → `EtradePriceFeed`
    - `create_price_feed()`: dry-run → `YahooPriceFeed`

38. **Stale cache fallback implemented**
    - On fetch failure: returns `self._cached_price` with warning log (not None/crash)

39. **Health monitoring dict correct**
    - `get_health_status()` returns dict with keys: `source`, `healthy`, `last_update_secs_ago`, `consecutive_failures`

---

### Section 7: First Session Parameters

These are the conservative settings required for session 1 specifically:

40. `max_contracts = 10` — verify matches config
41. `daily_contracts = 7` and `swing_contracts = 2` — verify matches config
42. Only Daily Income active — TNT, ORB, B&B all disabled
42. `di_morning_bias_filter = true`
43. `spread_width = 5`
44. `min_credit = 1.00`
45. `profit_target_pct = 80`
46. Config confirms live mode intent (broker not `dry_run`)

**After session 1:** Once comfortable with live execution, TNT can be re-enabled. Max contracts can be scaled up as account grows. The conservative first-session settings are intentional — they limit exposure while validating broker connectivity and fill behavior with real money.

---

### Section 8: Known Limitations (Acknowledge Only)

These are not FAILs — they are documented limitations the operator must be aware of:

| # | Limitation | Status |
|---|-----------|--------|
| 47 | Backtest uses synthetic Black-Scholes pricing (not real option chain data) | ACK |
| 48 | Position sizing compounds aggressively at scale — 20 contract cap mitigates for first sessions | ACK |
| 49 | Price feed uses 10s polling, not WebSocket streaming — acceptable for 30-min bar strategy | ACK |
| 50 | No margin requirement modeling in backtest — real buying power may differ | ACK |
| 51 | SPX 0DTE liquidity assumed — real fills at theoretical mid not guaranteed at scale | ACK |

---

### Section 9: Final GO/NO-GO

Produce this summary table:

| Section | Checks | Passed | Failed | Warned |
|---------|--------|--------|--------|--------|
| 1. Configuration | 21 | | | |
| 2. Broker Connectivity | 4 | | | |
| 3. Risk Gates | 5 | | | |
| 4. Database | 4 | | | |
| 5. Notifications | 5 | | | |
| 6. Price Feed | 4 | | | |
| 7. First Session Params | 8 | | | |
| **TOTAL** | **51** | | | |

**GO criteria:** Zero FAILs in Sections 1–4 and 6–7. Notification FAILs (Section 5) are strongly recommended to fix but operator may acknowledge and proceed.

**NO-GO criteria:** Any FAIL in Sections 1, 2, 3, or 7.

**WARN items:** All expected for a fresh first-live setup. WARNs are self-resolving after broker auth and first run.

Output one of:
- ✅ **GO** — All critical checks pass. Ready for live trading.
- ⚠️ **GO WITH CAUTION** — Critical checks pass but warnings or notification FAILs present. Acknowledge before proceeding.
- ❌ **NO-GO** — Critical FAILs present. Resolve before going live.

---

## Common Action Items After NO-GO

When the checklist returns NO-GO, the most common fixes required (in order):

1. **Switch broker:** Change `broker.active` from `dry_run` to `schwab` or `etrade` via Settings page
2. **Disable TNT for session 1:** Set `tag_n_turn.enabled: false` in strategy_params.yaml
3. **Run broker OAuth flow:** Launch app and complete auth flow to create token file
4. **Set trading mode to live:** Change via Settings page after broker is authenticated
5. **Configure notification webhook:** Add Slack or Discord webhook URL in strategy_params.yaml under `notifications:`
6. **Re-run checklist:** After completing action items, re-run to confirm clean GO

## Monday Morning Routine

Before each week's first live session:

```
Read build/skills/pre-live-checklist/SKILL.md and run a quick
Monday morning readiness check. Focus on: broker token still valid
(Section 2), no stale positions (check #29), price feed healthy
at market open (/api/status shows price_feed.healthy: true and
source matches active broker).
```

At 9:30 ET market open specifically:
- Check `/api/status` in the dashboard
- Confirm `price_feed.source` = "etrade" or "schwab" (not "yahoo")
- Confirm `price_feed.healthy` = true
- Confirm `price_feed.consecutive_failures` = 0
- Watch first bar build — prices should match a real-time SPX chart
