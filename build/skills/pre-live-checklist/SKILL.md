---
name: pre-live-checklist
description: Operational readiness check for The Daily Melt before enabling live trading with real money. Run this after the full audit has already passed and you are ready to flip from dry-run to live. This is NOT a code audit — it is a configuration, connectivity, and risk gate verification pass. Read-only: no code changes, no test runs.
---

# Pre-Live Trading Checklist

A focused operational readiness check for The Daily Melt. Verifies that configuration, broker authentication, risk gates, database, notifications, circuit breakers, and price feed are all correctly set up for the first live session.

## When To Use

- Immediately before switching from dry-run to live trading
- After any broker re-authentication (token expiry)
- After changing brokers (E*TRADE ↔ Schwab ↔ IBKR)
- After a long gap between live sessions (verify token still valid)
- After any settings changes that affect live trading behavior
- As a periodic Monday morning sanity check before market open

## When NOT To Use

- For code correctness issues → use the audit skill instead
- For backtest model verification → use the audit skill Part 5
- For strategy logic debugging → use the audit skill Part 1
- For risk management wiring verification → use the audit skill Part 9

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
2. `broker.active` is set to `etrade`, `schwab`, or `ibkr` (not `dry_run`)
3. `strategies.daily_income.enabled = true`
4. `strategies.tag_n_turn.enabled = false` ← DI-only for first sessions
5. `strategies.orb.enabled = false`
6. `strategies.bnb.enabled = false`
7. `di_morning_bias_filter = true`
8. `portfolio.daily_contracts` is set (expected: 7) ← DI budget-driven cap
9. `portfolio.swing_contracts` is set (expected: 2) ← fixed swing size
10. `portfolio.spread_width` is set (expected: 5.0)
11. `portfolio.max_contracts <= 10` ← hard cap, conservative first session
12. `portfolio.min_contracts = 1`
13. `portfolio.max_daily_loss_pct` is set (expected: 3.0%) ← daily circuit breaker
14. `drawdown_limits.weekly.max_loss_pct` is set (expected: 6.0%) ← weekly breaker
15. `drawdown_limits.monthly.max_loss_pct` is set (expected: 12.0%) ← monthly breaker
16. `drawdown_limits.weekly.enabled = true`
17. `drawdown_limits.monthly.enabled = true`
18. `risk.consecutive_losses.max_consecutive` is set (expected: 5)
19. `pdt.enable_1pm_management` exists under `pdt:` section
20. `monitoring.enable_1pm_check` does NOT exist (removed — flag FAIL if present)
21. `spread_width = 5`
22. `min_credit >= 1.00`
23. `profit_target_pct = 80`
24. `tnt.weekend_hold_prevention.enabled` exists (true or false — document value)
25. `tnt.weekend_hold_prevention.profit_threshold_pct` is set (expected: 30)

**Operator note:** Items 1, 2, and trading mode are intentionally left as `dry_run` in the repo default — they must be changed via the Settings page before going live. FAILs on these items are expected on a fresh setup and are resolved by the operator, not by code changes.

---

### Section 2: Broker Connectivity

26. **OAuth token file exists and is not expired**
    - Schwab: `database/schwab_token.json` exists and timestamp is recent
    - E*TRADE: token file exists and is within session window
    - IBKR: TWS or IB Gateway is running and accessible on the configured port
      - Live trading: TWS port 7496 (default) or IB Gateway port 4001
      - Paper trading: TWS port 7497 or IB Gateway port 4002
    - WARN if file not found (expected on first-ever live session — resolve by running broker auth flow)

27. **Broker class imports without error**
    - `EtradeBroker`, `SchwabBroker`, `IBKRBroker`, and `broker_factory.get_broker()` importable from `src/main.py`

28. **`price_feed_state.json` exists**
    - Located in `database/` or platform data dir
    - WARN if not found (auto-created on first run)

29. **Price feed source matches active broker**
    - If `price_feed_state.json` exists: `source` field matches `broker.active`

---

### Section 3: Multi-Timeframe Circuit Breaker Verification

Read `src/core/drawdown_manager.py` and `src/core/portfolio_manager.py`:

30. **Daily circuit breaker wired correctly**
    - PortfolioManager.max_daily_loss = account_size × (max_daily_loss_pct / 100)
    - Realized losses accumulate in daily_realized_pnl
    - When abs(daily_realized_pnl) >= max_daily_loss → circuit_breaker_triggered = True
    - can_enter_position() blocks when circuit_breaker_triggered is True

31. **Weekly circuit breaker wired correctly**
    - DrawdownManager.weekly_max_loss = account_size × (weekly_max_loss_pct / 100)
    - record_realized_pnl() accumulates in weekly_realized_pnl
    - When abs(weekly_realized_pnl) >= weekly_max_loss → weekly_breaker_triggered = True
    - can_trade() blocks when weekly_breaker_triggered is True

32. **Monthly circuit breaker wired correctly**
    - DrawdownManager.monthly_max_loss = account_size × (monthly_max_loss_pct / 100)
    - record_realized_pnl() accumulates in monthly_realized_pnl
    - When abs(monthly_realized_pnl) >= monthly_max_loss → monthly_breaker_triggered = True
    - can_trade() blocks when monthly_breaker_triggered is True

33. **Period resets fire on correct schedule**
    - check_and_apply_resets() uses pytz America/New_York timezone
    - Daily: resets at midnight ET (current_date changes)
    - Weekly: resets Monday 00:00 ET (current_iso_week changes)
    - Monthly: resets 1st of month 00:00 ET (current_month changes)
    - check_and_apply_resets() called at start of can_trade() AND at market open in main.py

34. **Startup backfill from database**
    - DrawdownManager._backfill_from_db() runs on init if state is stale or monthly_realized_pnl == 0
    - Queries trades DB for sum of losses in current period
    - Correctly reconstructs prior losses even after a restart or state file reset

35. **Settings sliders → engine alignment**
    - Max Daily Loss slider writes to portfolio.max_daily_loss_pct
    - Max Weekly Loss slider writes to drawdown_limits.weekly.max_loss_pct
    - Max Monthly Loss slider writes to drawdown_limits.monthly.max_loss_pct
    - DrawdownManager.reload_config() called after settings save (new limits take effect immediately)

---

### Section 4: Risk Gate Verification

Read `src/core/portfolio_manager.py` and `src/main.py`:

36. **All three circuit breakers checked before entries**
    - Daily breaker checked via circuit_breaker_triggered in can_enter_position()
    - Weekly and monthly checked via drawdown_manager.can_trade() before entry
    - Both checks happen before any strategy entry logic each cycle

37. **Max contracts cap respected**
    - calculate_position_size() clamps to min(contracts, self.max_contracts)

38. **PDT threshold check present**
    - Account equity compared to pdt_threshold (default $25,000) to set PDT mode
    - Null/error broker equity response fails safe (PDT ON, not OFF)

39. **Max 2 simultaneous positions enforced**
    - can_enter_position() checks len(active_positions) >= max_total_positions

40. **Duplicate DI entry guard**
    - max_0dte_positions: 1 enforced — cannot open two DI positions on same day

---

### Section 5: Database Readiness

41. **Live trading database exists (or will auto-create)**
    - `database/trades_live.db` exists, OR WARN (auto-created on first connection)

42. **WAL mode enabled**
    - db_manager.py contains `PRAGMA journal_mode=WAL`

43. **No open positions in live database**
    - If trades_live.db exists: open_positions must be empty
    - If stale open positions found: FAIL — must resolve before starting

44. **drawdown_state.json schema valid**
    - Contains: current_date (str), current_iso_week (int), current_iso_year (int),
      current_month (int), current_year (int), daily/weekly/monthly_realized_pnl (float),
      daily/weekly/monthly_breaker_triggered (bool)
    - All period markers are integers (not date strings)
    - WARN if file does not exist (backfill will reconstruct on first startup)

---

### Section 6: Notification System

45. **At least one notification channel configured**
    - At least one of: discord.enabled: true, slack.enabled: true, webhook.enabled: true
    - FAIL if all channels disabled

46. **Discord webhook URL is non-empty if enabled**
    - FAIL if discord.enabled = true but webhook_url is empty or placeholder

47. **Notification payload fields verified**
    - Bot startup notification includes ET time (not UTC)
    - Market open notification includes SPX opening price field
    - EOD summary includes SPX close, session range, pulse bar count in no-trade reason
    - Hourly update method (send_hourly_update) exists in notifications.py
    - Circuit breaker notification specifies which breaker (daily/weekly/monthly)

48. **Trade entry/exit notifications enabled**
    - send_trade_entered() and send_trade_closed() calls exist in strategy entry/exit paths

49. **EOD summary notification enabled**
    - send_eod_summary() call exists in end-of-day logic

**Operator note:** Notifications are strongly recommended but not a hard block. FAIL on #45 is a NO-GO recommendation that operator can acknowledge.

---

### Section 7: Price Feed Readiness

50. **`src/data/price_feed.py` exists**
    - Contains PriceFeed ABC + YahooPriceFeed + EtradePriceFeed + SchwabPriceFeed + IBKRPriceFeed + create_price_feed()

51. **Factory returns correct feed for live mode**
    - Schwab broker → SchwabPriceFeed
    - E*TRADE broker → EtradePriceFeed
    - IBKR broker → IBKRPriceFeed (ib_insync snapshot)
    - Dry-run → YahooPriceFeed

52. **Stale cache fallback implemented**
    - On fetch failure: returns cached price with warning log (not None/crash)

53. **Health monitoring dict correct**
    - get_health_status() returns: source, healthy, last_update_secs_ago, consecutive_failures

---

### Section 8: First Session Parameters

Conservative settings required for session 1:

54. `max_contracts = 10` — verify matches config
55. `daily_contracts = 7` and `swing_contracts = 2` — verify matches config
56. Only Daily Income active — TNT, ORB, B&B all disabled
57. `di_morning_bias_filter = true`
58. `spread_width = 5`
59. `min_credit = 1.00`
60. `profit_target_pct = 80`
61. Config confirms live mode intent (broker not `dry_run`)

**After session 1:** TNT can be re-enabled. Max contracts can scale up as account grows. When TNT is re-enabled, verify `tnt.weekend_hold_prevention.enabled = true` and threshold is set.

---

### Section 9: Dashboard Visual Verification

Before going live, confirm these dashboard indicators show correct live data:

62. **Risk Status bars**
    - Daily bar reads from portfolio_state.json (resets each morning)
    - Weekly bar reads from drawdown_state.json (resets each Monday)
    - Monthly bar reads from drawdown_state.json (resets each 1st)
    - All three bars show $0 fill on clean days (no losses)
    - Limits shown match what the engine enforces (same YAML source)

63. **W/L Streak**
    - Streak count matches Trade Journal closed trade history
    - Scratch trades ($0 P&L) do not break active streaks

64. **TNT LED**
    - Shows ACTIVE (green) if open TNT position exists in DB
    - Correct even when bot is stopped or market is closed

65. **Strategy LEDs**
    - DI: IDLE when no position and market closed; WATCHING during setup window
    - TNT: ACTIVE when position open (from DB); IDLE otherwise
    - B&B and ORB: IDLE (disabled by default for first session)

---

### Section 10: Known Limitations (Acknowledge Only)

| # | Limitation | Status |
|---|-----------|--------|
| 66 | Backtest uses synthetic Black-Scholes pricing (not real option chain data) | ACK |
| 67 | Position sizing compounds aggressively at scale — 10 contract cap for first session | ACK |
| 68 | Price feed uses 10s polling, not WebSocket streaming — acceptable for 30-min bar strategy | ACK |
| 69 | No margin requirement modeling in backtest — real buying power may differ | ACK |
| 70 | SPX 0DTE liquidity assumed — real fills at theoretical mid not guaranteed at scale | ACK |
| 71 | Close order retry is unbounded — no limit-price widening after N failures | ACK |
| 72 | Orphaned positions (crash between order and DB write) require manual review on restart | ACK |

---

### Section 11: Final GO/NO-GO

Produce this summary table:

| Section | Checks | Passed | Failed | Warned |
|---------|--------|--------|--------|--------|
| 1. Configuration | 25 | | | |
| 2. Broker Connectivity | 4 | | | |
| 3. Circuit Breakers | 6 | | | |
| 4. Risk Gates | 5 | | | |
| 5. Database | 4 | | | |
| 6. Notifications | 5 | | | |
| 7. Price Feed | 4 | | | |
| 8. First Session Params | 8 | | | |
| 9. Dashboard Visuals | 4 | | | |
| **TOTAL** | **65** | | | |

**GO criteria:** Zero FAILs in Sections 1–5, 7–9. Notification FAILs (Section 6) are strongly recommended to fix but operator may acknowledge and proceed.

**NO-GO criteria:** Any FAIL in Sections 1, 2, 3, 4, 7, or 8.

**WARN items:** Expected for fresh first-live setup. WARNs are self-resolving after broker auth and first run.

Output one of:
- ✅ **GO** — All critical checks pass. Ready for live trading.
- ⚠️ **GO WITH CAUTION** — Critical checks pass but warnings or notification FAILs present. Acknowledge before proceeding.
- ❌ **NO-GO** — Critical FAILs present. Resolve before going live.

---

## Common Action Items After NO-GO

1. **Switch broker:** Change `broker.active` from `dry_run` to `schwab`, `etrade`, or `ibkr` via Settings page
2. **Disable TNT for session 1:** Set `tag_n_turn.enabled: false` in strategy_params.yaml
3. **Run broker OAuth flow:** Launch app and complete auth flow to create token file
4. **For IBKR:** Start TWS or IB Gateway, confirm paper/live port matches config
5. **Set trading mode to live:** Change via Settings page after broker is authenticated
6. **Configure notification webhook:** Add Discord or Slack webhook URL in strategy_params.yaml
7. **Verify circuit breaker limits:** Check that daily/weekly/monthly pct values match intended risk tolerance
8. **Re-run checklist:** After completing action items, re-run to confirm clean GO

---

## Monday Morning Routine

Before each week's first live session:

```
Read build/skills/pre-live-checklist/SKILL.md and run a quick
Monday morning readiness check. Focus on: broker token still valid
(Section 2), no stale positions (check #43), drawdown_state.json
weekly counter reset to 0 (Monday reset fired correctly), price feed
healthy at market open (/api/status shows price_feed.healthy: true
and source matches active broker).
```

At 9:30 ET market open specifically:
- Check `/api/status` in the dashboard
- Confirm `price_feed.source` = "etrade", "schwab", or "ibkr" (not "yahoo")
- Confirm `price_feed.healthy` = true
- Confirm `price_feed.consecutive_failures` = 0
- Check Risk Status panel: Weekly bar should show $0 (reset fired this morning)
- Watch first bar build — prices should match a real-time SPX chart

## First-of-Month Routine

On the 1st of each month before market open:
- Confirm Monthly bar in Risk Status panel shows $0 (reset fired at midnight ET)
- If monthly bar still shows prior month's losses, the reset did not fire — investigate
  check_and_apply_resets() call in main.py market open logic
