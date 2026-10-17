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
    return p.parse_args(argv)


def resolve_config(args, params):
    from src.data.chain_recorder import RecorderConfig
    d = dict((params or {}).get("chain_recorder") or {})
    for key, val in (("interval_seconds", args.interval), ("start", args.start), ("end", args.end),
                     ("strike_count", args.strike_count), ("db_path", args.db)):
        if val is not None:
            d[key] = val
    return RecorderConfig.from_dict(d)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from config.settings import BASE_DIR, STRATEGY_PARAMS
    from src.data.chain_recorder import ChainRecorder, build_read_only_schwab

    cfg = resolve_config(args, STRATEGY_PARAMS)
    db_path = Path(cfg.db_path)
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path
    token_path = (STRATEGY_PARAMS.get("broker", {}).get("schwab", {}) or {}).get("token_path")

    md = build_read_only_schwab(token_path=token_path)
    rec = ChainRecorder(md, cfg, db_path=db_path)
    logging.info(f"Chain recorder (read-only): {cfg.symbol} 0DTE, every {cfg.interval_seconds}s, "
                 f"{cfg.start:%H:%M}-{cfg.end:%H:%M} ET -> {db_path}")
    n = rec.run(once=args.once)
    logging.info(f"Done: {n} snapshot(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
