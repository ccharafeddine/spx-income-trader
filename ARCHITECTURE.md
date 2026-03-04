# The Daily Melt — Architecture Reference

**Version**: 1.0.0  
**Last Updated**: 2026-03-04  
**Test Count**: 1,245 passing  
**Status**: Dry-run operational, awaiting Schwab Level 3 approval for live

---

## Purpose of This Document

This document exists to prevent architectural drift as the codebase grows. Every Claude Code session should read this file before making changes. It defines:

- What each file/module owns and is responsible for
- Boundaries that must never be crossed
- Key decisions made and why
- The authoritative data flow for a complete trade lifecycle
- Known gotchas that have caused bugs

---

## Repository Structure

```
spx-income-trader/
├── src/                          # Trading engine (pure Python, no Flask)
│   ├── core/
│   │   ├── position_manager.py   # Trade entry/exit, DB persistence
│   │   ├── drawdown_manager.py   # Circuit breaker state (flags only)
│   │   ├── bar_builder.py        # 30-min bar aggregation from ticks
│   │   ├── greeks_calculator.py  # Black-Scholes analytics
│   │   └── pdt_tracker.py        # PDT rule compliance
│   ├── strategies/
│   │   ├── daily_income.py       # Primary strategy (pulse → breakout → spread)
│   │   ├── tag_n_turn.py         # Bollinger Band swing strategy
│   │   ├── orb.py                # Opening Range Breakout (experimental)
│   │   └── bnb.py                # B&B signal enhancer (not standalone)
│   ├── brokers/
│   │   ├── base.py               # AbstractBroker interface
│   │   ├── etrade.py             # E*TRADE OAuth integration
│   │   ├── schwab.py             # Schwab OAuth2 via schwab-py
│   │   ├── ibkr.py               # Interactive Brokers via ib_insync
│   │   └── dry_run.py            # Simulated broker (Yahoo + Black-Scholes)
│   ├── data/
│   │   ├── economic_calendar.py  # Calendar lookup (wraps fred_calendar.py)
│   │   ├── fred_calendar.py      # FRED API live feed (4h cache, static fallback)
│   │   ├── price_feed.py         # Real-time SPX price (broker or Yahoo)
│   │   └── vix_provider.py       # VIX level + regime classification
│   ├── shadow/
│   │   └── comparator.py         # Read-only Schwab price validation (dry-run only)
│   ├── notifications/
│   │   └── discord.py            # Webhook notifications
│   └── main.py                   # Bot main loop, orchestration
├── database/
│   ├── db_manager.py             # All SQLite operations
│   ├── trades.db                 # Dry-run trade records
│   └── trades_live.db            # Live trade records (separate, no contamination)
├── dashboard/
│   ├── app.py                    # Flask app, all API endpoints
│   └── templates/
│       ├── index.html            # Single-page dashboard (all tabs)
│       └── settings.html         # Broker/account configuration
├── config/
│   ├── strategy_params.yaml      # All runtime parameters
│   ├── settings.py               # Credential loading from OS keychain
│   └── economic_calendar.json    # Static fallback calendar (FOMC/CPI/NFP/GDP/PCE)
├── tests/                        # 1,245 pytest tests
├── build/
│   ├── build_windows.py          # PyInstaller packaging
│   └── build_mac.py              # py2app packaging
└── app_desktop.py                # PyWebView launcher
```

---

## Domain Boundaries (Critical — Do Not Cross)

### Rule 1: `src/` never imports from `dashboard/`
The trading engine is completely independent of the web dashboard. This allows headless operation and clean testing.

### Rule 2: `dashboard/app.py` never contains business logic
Flask routes are thin: validate input → call DB or src → return JSON. No trade decisions, no strategy logic, no risk calculations in app.py.

### Rule 3: `db_manager.py` owns all SQL
No raw SQLite queries outside `database/db_manager.py`. All DB access goes through its methods. Exception: test fixtures that create test databases directly.

### Rule 4: Strategy files never write to DB directly
Strategies call `position_manager.enter_trade()` and `position_manager.exit_trade()`. The position manager handles all DB writes. Strategies are pure logic.

### Rule 5: `drawdown_manager.py` owns circuit breaker FLAGS only
Weekly/monthly P&L is always computed from the DB (`status IN ('closed', 'expired')`). The drawdown state file (`drawdown_state.json`) stores only boolean breaker flags and their reset timestamps — never P&L values. This was fixed 2026-03-04 after a bug where stale state file P&L was overriding real DB values.

### Rule 6: Credentials never in config files
All API keys (broker, FRED, etc.) stored via OS keychain through `config/settings.py`. Never in YAML, `.env`, or any file that could be committed.

---

## Authoritative Trade Lifecycle

This is the single source of truth for how a trade flows through the system. Any change to this flow must update this document.

```
1. BAR CLOSES (30-min bar completes)
   └── bar_builder.py aggregates tick data → emits bar event

2. STRATEGY EVALUATES
   └── daily_income.py checks:
       - Pulse bar? (close in top/bottom 10% of range)
       - Setup window? (9:30–11:30 ET)
       - Breakout confirmed? (price crosses pulse bar high/low)
       - Credit quality? (>= $1.00 minimum)
       - Risk gates clear? (drawdown limits, position limits)
   └── If all pass → calls position_manager.enter_trade()

3. TRADE ENTRY (position_manager.py:295)
   └── save_trade() called IMMEDIATELY with status='active'
       ← Trade is in DB from this moment
       ← If bot crashes here, record exists as 'active' — recoverable
   └── broker.place_order() called
   └── save_trade() updated with fill data (price, quantity)

4. POSITION MONITORED (main.py heartbeat loop)
   └── Checks every 30 seconds:
       - 80% profit target hit?
       - 1pm management triggered? (PDT accounts <$25k only)
       - Circuit breaker triggered?
       - Expiration approaching?

5. TRADE EXIT
   └── position_manager.exit_trade() called
   └── db_manager.update_trade_close() called
       ← CRITICAL: Uses UPDATE not INSERT OR REPLACE
       ← Preserves all entry context (strategy_type, spx_at_entry, VIX, etc.)
       ← Only updates: exit_time, status, exit_price, pnl, exit_reason
   └── Status becomes 'closed'

6. CRASH / ORPHAN PATH
   └── If bot crashes mid-trade, trade remains 'active' in DB
   └── On next startup OR via dashboard EOD resolution:
       └── _resolve_expired_trade() called
       └── Sets status='expired', records spx_at_exit from current price
       └── 'expired' treated identically to 'closed' in ALL queries

7. DASHBOARD QUERIES
   └── ALL P&L queries use: status IN ('closed', 'expired')
   └── ALL drawdown queries use: status IN ('closed', 'expired')
   └── ALL chart annotation queries use: status IN ('closed', 'expired')
   └── Trade counters (ODTE/Swing) use: DATE(entry_time) = today_et
```

---

## Data Sources — Source of Truth Hierarchy

| Data | Source of Truth | Never Use |
|------|----------------|-----------|
| Daily/Weekly/Monthly P&L | DB query | drawdown_state.json P&L values |
| Circuit breaker flags | drawdown_state.json | — |
| Trade counters (today) | DB: DATE(entry_time) = today | portfolio_state.json |
| Strategy status (IDLE/POSITION) | DB: open trades query | JSON state files |
| Account equity | Broker API (live) / portfolio.yaml (dry-run) | — |
| SPX price | price_feed.py (broker or Yahoo) | — |
| VIX level | vix_provider.py | — |
| Economic events | fred_calendar.py → static JSON fallback | — |

**Key principle**: JSON state files (`portfolio_state.json`, `drawdown_state.json`) are write-ahead caches for the running bot's session. They are NEVER authoritative for dashboard display. The DB is always authoritative.

---

## Status Values for Trades

```
'active'   — Trade is open, position held
'closed'   — Trade exited normally (profit target, stop loss, 1pm, EOD)
'expired'  — Trade resolved by dashboard after bot crash (equivalent to closed)
'pending'  — Order submitted but not yet filled (transient)
'cancelled' — Order cancelled before fill
```

**All dashboard queries must include BOTH 'closed' and 'expired'.**  
This is a hard requirement established 2026-03-04 after multiple bugs where expired trades were invisible to risk bars, chart annotations, and P&L calculations.

---

## Risk Architecture

### Three-Layer Circuit Breaker System

```
Daily limit:   2% of account (resets midnight ET)
Weekly limit:  4% of account (resets Monday midnight ET)  
Monthly limit: 8% of account (resets 1st of month midnight ET)
```

**P&L source**: Always DB query for `status IN ('closed', 'expired')` filtered by period.  
**Flags source**: `drawdown_state.json` (breaker_triggered boolean + reset timestamps).  
**Reset logic**: Independent per period. Weekly reset does NOT reset monthly.

### Position Sizing

```python
# Percentage-based (scales with account)
risk_dollars = account_size * risk_per_trade_pct
contracts = floor(risk_dollars / (spread_width * 100))
contracts = max(min_contracts, min(contracts, max_contracts))
```

`max_contracts` is a hard ceiling that cannot be exceeded regardless of account size.

---

## Shadow Mode

Shadow mode is a **read-only** passive observer active only in dry-run mode. It:
- Uses a separate SchwabBroker instance (read-only, no order placement methods called)
- Runs in daemon threads that cannot crash the main bot
- Compares dry-run synthetic pricing against live Schwab quotes
- Logs to `logs/shadow.jsonl` (append-only)
- API endpoint: `GET /api/shadow?limit=50`
- Controlled via `config/strategy_params.yaml` → `broker.shadow.enabled`

Shadow mode NEVER calls any order-placement method. It only calls `get_option_chain()`, `get_current_price()`, and `get_account_balance()`.

---

## Economic Calendar Architecture

```
src/data/economic_calendar.py  ← All callers use this
    └── src/data/fred_calendar.py  ← FRED API (primary, 4h cache)
        └── config/economic_calendar.json  ← Static fallback
```

FRED API provides upcoming release dates for: CPI, NFP, GDP, PCE.  
FOMC dates come from static JSON (FRED's release endpoint returns misleading daily entries for ongoing federal funds rate publications, not actual meeting dates).

Fallback triggers when: no API key, request fails, or returns empty results.  
Source displayed in dashboard as "Live • FRED" or "Static fallback".

---

## Dashboard API Conventions

All API endpoints follow these conventions:

1. **GET endpoints** — read-only, no side effects
2. **POST endpoints** — require CSRF token (`X-API-Token` header)
3. **Trading mode check** — settings that affect live trading validate mode first
4. **DB-first** — never return stale JSON file values for financial data
5. **Status filter** — all trade queries: `WHERE status IN ('closed', 'expired')`

Key endpoints:
```
GET  /api/status          — Bot status, strategy states, equity
GET  /api/risk-status     — Drawdown bars (DB-sourced P&L + state file flags)
GET  /api/history         — MTD trade history and daily stats
GET  /api/signals         — Today's signal log (resets midnight ET)
GET  /api/calendar        — Economic events (FRED live or static fallback)
GET  /api/shadow          — Shadow mode comparisons
GET  /api/chart/bars      — OHLC bars for chart (filtered to NYSE trading dates)
POST /api/settings        — Update bot parameters (allowlisted paths only)
POST /api/mode            — Switch dry-run ↔ live (requires validation)
```

---

## Chart Architecture

The chart renders OHLC candlesticks from `/api/chart/bars` with trade overlays.

**Critical rules for chart bars:**
- Backend filters to NYSE trading dates only (no echo bars on weekends/holidays)
- Frontend checks actual bar timestamp dates before merging (Yahoo echoes Friday data with Friday timestamps on Saturday)
- Bars use `YYYY-MM-DD HH:MM` format universally for correct sorting across year boundaries

**Trade overlay queries:**
```sql
SELECT * FROM trades 
WHERE status IN ('closed', 'expired')  -- BOTH required
AND DATE(entry_time) >= chart_start_date
```

Entry price fallback chain: `spx_at_entry` → `underlying_price_at_entry` → `sortedCloses[entryIdx]`  
Exit price fallback chain: `spx_at_exit` → `sortedCloses[exitIdx]`

---

## Strategy Status Lights

All four strategy status lights derive from **DB state, not JSON files**.

```python
# Pattern (same for all four strategies):
db_has_open_trade = query_trades(strategy_type=X, status='active')
if not db_has_open_trade:
    state = 'IDLE'  # Override any stale JSON value
```

This was implemented 2026-03-03 after stale JSON files were showing phantom positions hours after trades had closed.

---

## Known Gotchas (Bugs Fixed — Don't Re-Introduce)

### 1. Unicode crash (fixed 2026-03-03)
The ✓ character (U+2713) in log messages caused `UnicodeEncodeError` on Windows (cp1252 encoding). All log handlers now use `encoding='utf-8'`. No non-ASCII characters in log messages. `BaseException` handler in main loop prevents crash from abandoning open positions.

### 2. INSERT OR REPLACE on trade close (fixed 2026-03-03)
`save_trade()` on exit was using INSERT OR REPLACE which reset `strategy_type` and all entry context to NULL. Fixed: `update_trade_close()` uses targeted UPDATE of exit fields only. Entry context is immutable after save at entry.

### 3. State file P&L overriding DB (fixed 2026-03-04)
`/api/risk-status` was reading weekly/monthly P&L from `drawdown_state.json` and only falling back to DB if value was exactly 0.0. Since state file had positive P&L from wins, expired loss trades were invisible. Fixed: weekly/monthly P&L always from DB. State file only stores boolean flags.

### 4. Chart annotation missing expired trades (fixed 2026-03-04)
Chart bar query used `WHERE status='closed'`, missing `status='expired'`. Dashboard-resolved trades (after crash) were invisible on chart. Fixed: `WHERE status IN ('closed', 'expired')` everywhere.

### 5. Trade counter stale from state file (fixed 2026-03-04)
ODTE/Swing counters read from `portfolio_state.json` which was last written the previous day. Fixed: always count from DB using `DATE(entry_time) = today_et`.

### 6. FOMC duplication in calendar (fixed 2026-03-04)
FRED's `releases/dates` endpoint returns daily entries for federal funds rate publications, not actual FOMC meeting dates. Fixed: FOMC dates always from static JSON via `_merge_fomc_from_static()`.

### 7. Yahoo echo bars on weekends (fixed previously)
Yahoo Finance returns previous trading day bars on weekends/pre-market. Two-layer fix: backend filters to NYSE trading dates, frontend checks actual bar timestamp dates before merging.

---

## Testing Principles

**The rule**: Every bug fix must have a failing test written first that proves the bug exists, then code changes until the test passes. A fix without a test is cosmetic.

**Test files and their domains:**
```
tests/test_daily_income.py         — DI strategy logic
tests/test_tag_n_turn.py           — TNT strategy logic  
tests/test_economic_calendar.py    — Calendar loading and lookup
tests/test_fred_calendar.py        — FRED API integration + fallback
tests/test_dashboard_status.py     — All dashboard API responses (DB-backed)
tests/test_risk_status.py          — Circuit breakers and drawdown bars
tests/test_trade_entry_persistence.py — Trade lifecycle DB persistence
tests/test_daily_counter_reset.py  — Counter reset at midnight ET
tests/test_enter_trade_resilience.py — Entry error handling + encoding
tests/test_slippage.py             — Slippage tracking
tests/test_finnhub_calendar.py     → renamed test_fred_calendar.py
```

**CI**: GitHub Actions runs full suite on every push (Python 3.11/3.12/3.13).

---

## Configuration Reference

`config/strategy_params.yaml` is the single configuration file. Key sections:

```yaml
broker:
  active: "dry_run"  # dry_run | schwab | etrade | ibkr
  shadow:
    enabled: false   # Toggle from dashboard in dry-run mode
    compare_interval_min: 15

portfolio:
  account_size: 50000.0
  max_contracts: 10        # Hard ceiling
  daily_contracts: 7       # 0DTE strategies
  swing_contracts: 2       # TNT
  max_daily_loss_pct: 2.0  # Circuit breaker
  
strategy:
  pulse_threshold: 10      # Close in top/bottom N% of bar
  spread_width: 5          # $5 spread
  profit_target_pct: 80    # Exit at 80% of max profit
  min_credit: 1.00         # Reject below $1.00/contract
```

Settings editable at runtime via dashboard are allowlisted in `ALLOWED_SETTINGS_PATHS` in `dashboard/app.py`. Not all YAML keys are exposed.

---

## Credentials (OS Keychain)

| Service Name | Key | Purpose |
|---|---|---|
| `SPXIncomeTrader` | `schwab_app_key` | Schwab API |
| `SPXIncomeTrader` | `schwab_app_secret` | Schwab API |
| `SPXIncomeTrader` | `etrade_consumer_key` | E*TRADE API |
| `SPXIncomeTrader` | `etrade_consumer_secret` | E*TRADE API |
| `SPXIncomeTrader` | `etrade_account_id` | E*TRADE account |
| `SPXIncomeTrader` | `fred_api_key` | FRED economic calendar |
| `SPXIncomeTrader` | `discord_webhook_url` | Trade notifications |

All credential access via `config/settings.py`. Never access keyring directly from strategy or dashboard code.

---

## Go-Live Checklist (Remaining)

- [ ] Schwab Level 3 options approval (spreads) on father's account
- [ ] Shadow mode accumulate 2+ weeks of pricing validation data
- [ ] Run pre-live 51-point audit (`build/skills/pre-live-checklist/SKILL.md`)
- [ ] Set `broker.active: schwab` in strategy_params.yaml
- [ ] Start with Daily Income only (disable TNT, ORB, B&B)
- [ ] First session: max 2-3 contracts, monitor manually

---

## Production Mode (Future)

When deploying to a non-developer user (e.g. father's laptop):

Set `app.mode: production` in config to hide:
- Shadow Mode panel
- Backtest tab
- Analytics deep-dive
- Logs tab

User-facing interface: Overview + Trade Journal + Calendar only.

Implementation: single config flag, no separate fork or branch. Same codebase, different visibility.

---

## Architectural Decisions Log

| Date | Decision | Rationale |
|---|---|---|
| 2026-02-xx | Separate dryrun/live databases | Prevents data contamination; dry-run trades never affect live analytics |
| 2026-02-xx | update_trade_close() not save_trade() on exit | INSERT OR REPLACE was resetting strategy_type and entry context to NULL |
| 2026-03-03 | DB-backed strategy status for all four strategies | JSON state files were showing phantom positions after trades closed |
| 2026-03-03 | BaseException handler in main loop | UnicodeEncodeError was silently killing bot thread, leaving open positions unmanaged |
| 2026-03-04 | DB always authoritative for P&L | State file P&L was hiding losses from expired trades in risk calculations |
| 2026-03-04 | status IN ('closed', 'expired') everywhere | Dashboard-resolved trades (after crash) must be visible in all queries |
| 2026-03-04 | FOMC dates from static JSON | FRED releases endpoint returns misleading daily entries, not actual meeting dates |
| 2026-03-04 | FRED API over Finnhub | Finnhub economic calendar is paywalled on free tier; FRED is free government data |
| 2026-03-04 | Journal API backfills economic_events from FRED cache | Trades entered before FRED integration had NULL economic_events in DB. Rather than a migration, the journal API builds a FRED cache for all trade dates and uses it as fallback when DB column is NULL. Older trades now show NFP/CPI/FOMC badges retroactively. |
