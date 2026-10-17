"""
Record SPXW 0DTE option-chain snapshots to SQLite (dry-run, read-only quotes).

This never places, changes or cancels orders. It only calls the Schwab
market-data endpoints get_option_chain / get_quote through ReadOnlyMarketData
(see src/data/chain_recorder.py). It can run alongside the bot, whether the
bot is paused, in dry-run, or live.

Usage (from the project root, during market hours):
    python scripts/record_chains.py                    # 11:45-16:00 ET, every 60 s, today
    python scripts/record_chains.py --once             # one snapshot now (connection test)
    python scripts/record_chains.py --interval 30 --start 11:30 --end 16:00
    python scripts/record_chains.py --check            # token status only, no network calls
    python scripts/record_chains.py --token-path "C:/path/to/schwab_token.json" --db D:/quotes/chains.db

Exit codes: 0 = done (or nothing to do today), 2 = Schwab not authenticated.

Defaults can also be set in an optional `chain_recorder:` section of
config/strategy_params.yaml (see strategy_params.example.yaml). CLI flags win.

Requires:
    - Schwab app key/secret (keyring, as set up in the app's Settings page)
    - A valid Schwab OAuth token file (database/schwab_token.json), i.e. Schwab
      connected in the app within the last 7 days.

Output: database/chain_snapshots.db (table chain_snapshots, one row per
option per snapshot; failed polls go to chain_errors).
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Dry-run SPXW 0DTE chain recorder (read-only quotes, never orders)")
    p.add_argument("--interval", type=int, help="seconds between snapshots (min 15, default 60)")
    p.add_argument("--start", help="ET start time HH:MM (default 11:45)")
    p.add_argument("--end", help="ET end time HH:MM (default 16:00, or the early close)")
    p.add_argument("--strike-count", type=int, help="strikes around ATM to request (default 80)")
    p.add_argument("--db", help="SQLite path (default database/chain_snapshots.db)")
    p.add_argument("--once", action="store_true", help="take a single snapshot now and exit")
    p.add_argument("--token-path", help="Schwab token file (default: broker.schwab.token_path, "
                                        "else database/schwab_token.json). Use the desktop app's token "
                                        "file when the app is installed")
    p.add_argument("--check", action="store_true",
                   help="report token file status (age, hours left) and exit; makes no network calls")
    return p.parse_args(argv)


def resolve_config(args, params):
    from src.data.chain_recorder import RecorderConfig
    d = dict((params or {}).get("chain_recorder") or {})
    for key, val in (("interval_seconds", args.interval), ("start", args.start), ("end", args.end),
                     ("strike_count", args.strike_count), ("db_path", args.db)):
        if val is not None:
            d[key] = val
    return RecorderConfig.from_dict(d)


def token_report(status: dict, token_path) -> str:
    """One-line token status. Never includes token contents."""
    if not Path(token_path).exists():
        return f"Schwab token file not found: {token_path}"
    if status.get("valid"):
        return (f"Schwab token OK: {status['hours_remaining']:.1f} h left "
                f"(issued {status['created_at']}, expires {status['expires_at']})")
    return f"Schwab token EXPIRED (issued {status.get('created_at')}). Reconnect Schwab in the app."


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from config.settings import BASE_DIR, STRATEGY_PARAMS
    from src.data.chain_recorder import ChainRecorder, build_read_only_schwab, ET
    from src.brokers.schwab_auth import SchwabAuth

    cfg = resolve_config(args, STRATEGY_PARAMS)
    db_path = Path(cfg.db_path)
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path
    token_path = args.token_path or (STRATEGY_PARAMS.get("broker", {}).get("schwab", {}) or {}).get("token_path")

    auth = SchwabAuth(token_path=token_path)
    status = auth.get_token_status()
    logging.info(token_report(status, auth.token_path))
    if args.check:
        return 0 if status.get("valid") else 2

    if not args.once:
        from datetime import datetime
        from src.utils.market_calendar import is_trading_day
        today = datetime.now(ET).date()
        if not is_trading_day(today):
            logging.info(f"{today} is not a trading day; nothing to record")
            return 0

    try:
        md = build_read_only_schwab(token_path=auth.token_path)
    except RuntimeError as e:
        logging.error(str(e))
        return 2
    rec = ChainRecorder(md, cfg, db_path=db_path)
    logging.info(f"Chain recorder (read-only): {cfg.symbol} 0DTE, every {cfg.interval_seconds}s, "
                 f"{cfg.start:%H:%M}-{cfg.end:%H:%M} ET -> {db_path}")
    n = rec.run(once=args.once)
    logging.info(f"Done: {n} snapshot(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
