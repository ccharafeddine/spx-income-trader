# Dry-run 0DTE chain recorder

`scripts/record_chains.py` saves snapshots of the SPXW 0DTE option chain to SQLite. These real quotes are used to
check the backtester's option model and the iron-condor candidate (see
[strategy_review/README.md](strategy_review/README.md)).

**It never trades.** The recorder only gets a `ReadOnlyMarketData` wrapper, which exposes `get_option_chain`,
`get_quote` and `get_quotes`. Any other call, such as `place_order`, `cancel_order` or `get_account`, raises
`PermissionError`. It does not create a broker, does not read the account number, and writes to its own database. It
is not started by the bot.

## Requirements

* Schwab connected in the app: the app key and secret are in the keyring (Settings page), and a valid OAuth token is
  in `database/schwab_token.json`. Schwab refresh tokens last 7 days. Reconnect in the app (or run
  `python -m src.brokers.schwab_auth`) when the bot warns about token expiry.
* The Schwab app needs the *Market Data* API product, which the bot already uses for quotes and chains.

## Run

From the project root, on a trading day:

```bash
python scripts/record_chains.py --once     # one snapshot now: tests auth and the database
python scripts/record_chains.py            # every 60 s from 11:45 to 16:00 ET (stops at an early close)
python scripts/record_chains.py --interval 30 --start 11:30 --end 16:00 --strike-count 100
```

Start it before 11:45 ET and it waits for the window, then exits when the window closes. To record every day, schedule
it with Windows Task Scheduler at about 11:40 ET on weekdays. It does nothing on market holidays. It can run while the
bot is running (paused, dry-run or live).

You can set defaults in an optional `chain_recorder:` section of `config/strategy_params.yaml` (see the example file).
Command-line flags override them.

## Desktop-app token, health check and exit codes

When Schwab is connected through the installed desktop app, the token lives in the app's data folder
(`%LOCALAPPDATA%\SPXIncomeTrader\SPXIncomeTrader\database\schwab_token.json` on Windows). Point the recorder at it:

```bash
python scripts/record_chains.py --check --token-path "%LOCALAPPDATA%/SPXIncomeTrader/SPXIncomeTrader/database/schwab_token.json"
```

`--check` prints only the token's age and hours left (never its contents) and makes no network calls. On a
non-trading day the recorder exits before touching Schwab. Exit codes: `0` done or nothing to do, `2` Schwab not
authenticated (reconnect in the app).

## Output

`database/chain_snapshots.db`:

* `chain_snapshots`: one row per option per snapshot. Columns: `ts_et`, `expiration`, `underlying`, `strike`,
  `put_call`, `bid`, `ask`, `last`, `mark`, sizes, greeks, `iv`, `volume`, `open_interest`.
* `chain_errors`: polls that failed (the recorder keeps going).

At 80 strikes × 2 sides × about 255 snapshots, a day is about 40k rows (roughly 5 MB).
