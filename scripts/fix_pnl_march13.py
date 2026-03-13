# One-off script to backfill broker P&L for 2026-03-13 trade
# Safe to delete after confirmed correct
"""
Fetch Schwab transaction history for 2026-03-13 and correct the P&L
for the 6660/6665 call credit spread from price-based to broker net.

Usage:
    python scripts/fix_pnl_march13.py
"""

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

# Ensure project root is on sys.path so imports work
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import BASE_DIR
from src.brokers.schwab_auth import SchwabAuth

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TRADE_DATE = date(2026, 3, 13)
SHORT_STRIKE = 6660.0
LONG_STRIKE = 6665.0
GROSS_PNL = 420.00  # price-based: (2.5275 - 0.4175) * 100 * 2

# Expected legs (for display / matching diagnostics)
EXPECTED_LEGS = [
    ('Sell to Open',  '6660', 'C', +4477.56),
    ('Buy to Open',   '6665', 'C', -3972.44),
    ('Buy to Close',  '6660', 'C', -210.44),
    ('Sell to Close', '6665', 'C', +115.74),
]

DB_PATH = Path(r'C:\Users\nizar\AppData\Local\SPXIncomeTrader\SPXIncomeTrader\database\trades_live.db')

# Sanity-check range for the broker P&L sum
SANITY_LOW = 400.0
SANITY_HIGH = 420.0


def load_schwab_config():
    """Load Schwab config from strategy_params.yaml (same as main app)."""
    import yaml
    config_path = ROOT / 'config' / 'strategy_params.yaml'
    if config_path.exists():
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {}
    return config.get('broker', {}).get('schwab', {})


def get_schwab_client():
    """Instantiate SchwabAuth the same way SchwabBroker does."""
    cfg = load_schwab_config()
    auth = SchwabAuth(
        app_key=cfg.get('app_key', ''),
        app_secret=cfg.get('app_secret', ''),
        callback_url=cfg.get('callback_url', 'https://127.0.0.1'),
        token_path=cfg.get('token_path', 'database/schwab_token.json'),
    )
    return auth.get_client()


def fetch_transactions(client, account_hash):
    """Fetch all TRADE transactions for TRADE_DATE from Schwab."""
    from schwab.client import Client

    resp = client.get_transactions(
        account_hash,
        start_date=TRADE_DATE,
        end_date=TRADE_DATE,
        transaction_types=[Client.Transactions.TransactionType.TRADE],
    )
    resp.raise_for_status()
    return resp.json()


def get_account_hash(client):
    """Get the account hash (same pattern as SchwabBroker._get_account_hash)."""
    cfg = load_schwab_config()
    account_number = cfg.get('account_number', '')

    resp = client.get_account_numbers()
    resp.raise_for_status()
    accounts = resp.json()
    if not accounts:
        print("ERROR: No accounts found in Schwab response.")
        sys.exit(1)

    for acct in accounts:
        if str(acct.get('accountNumber')) == str(account_number):
            return acct['hashValue']

    # Fallback to first account
    h = accounts[0]['hashValue']
    num = str(accounts[0].get('accountNumber', ''))
    masked = f"****{num[-4:]}" if len(num) > 4 else '****'
    print(f"  (configured account not found, using {masked})")
    return h


def match_spread_legs(transactions):
    """Filter transactions to the 4 legs of the 6660/6665 spread.

    Matches by checking that the transaction description or instrument
    symbol contains '6660' or '6665' and is a TRADE type.
    """
    matched = []
    for txn in transactions:
        desc = (txn.get('description') or '').upper()
        # Also check transferItems instrument symbol
        symbol = ''
        for item in txn.get('transferItems', []):
            instrument = item.get('instrument', {})
            symbol = (instrument.get('symbol') or '').upper()

        net = float(txn.get('netAmount', 0) or 0)

        is_6660 = '6660' in desc or '6660' in symbol
        is_6665 = '6665' in desc or '6665' in symbol

        if is_6660 or is_6665:
            matched.append({
                'description': txn.get('description', ''),
                'net_amount': net,
                'symbol': symbol,
                'transaction_id': txn.get('transactionId'),
                'order_id': txn.get('orderId'),
            })

    return matched


def print_all_transactions(transactions):
    """Pretty-print all transactions returned by Schwab."""
    print(f"\n{'='*70}")
    print(f"  ALL TRANSACTIONS FOR {TRADE_DATE}  ({len(transactions)} total)")
    print(f"{'='*70}")
    for i, txn in enumerate(transactions, 1):
        desc = txn.get('description', '(none)')
        net = txn.get('netAmount', 0)
        order_id = txn.get('orderId', '')
        txn_id = txn.get('transactionId', '')
        fees_obj = txn.get('fees', {})
        symbol = ''
        for item in txn.get('transferItems', []):
            instrument = item.get('instrument', {})
            symbol = instrument.get('symbol', '')

        print(f"\n  [{i}] {desc}")
        print(f"      netAmount:  ${net:+,.2f}")
        print(f"      orderId:    {order_id}")
        print(f"      txnId:      {txn_id}")
        if symbol:
            print(f"      symbol:     {symbol}")
        if fees_obj:
            print(f"      fees:       {json.dumps(fees_obj)}")
    print()


def read_current_db_trade():
    """Read the current trade row from the database."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, entry_time, pnl, gross_pnl, commissions, exit_price, "
        "       short_strike, long_strike, status, pnl_percent "
        "FROM trades "
        "WHERE entry_time LIKE '2026-03-13%' AND short_strike = ? AND status = 'closed'",
        (SHORT_STRIKE,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_db(broker_pnl, commissions):
    """Write corrected P&L to the database."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Read BEFORE
    before = conn.execute(
        "SELECT id, pnl, gross_pnl, commissions, pnl_percent "
        "FROM trades "
        "WHERE entry_time LIKE '2026-03-13%' AND short_strike = ? AND status = 'closed'",
        (SHORT_STRIKE,)
    ).fetchone()

    if not before:
        print("ERROR: No matching trade found in DB.")
        conn.close()
        return False

    # Recalculate pnl_percent
    meta = conn.execute(
        "SELECT credit_received, quantity FROM trades WHERE id = ?",
        (before['id'],)
    ).fetchone()
    max_profit = (meta['credit_received'] or 0) * 100 * (meta['quantity'] or 1)
    pnl_percent = round(broker_pnl / max_profit * 100, 2) if max_profit > 0 else None

    cursor = conn.execute(
        "UPDATE trades "
        "SET pnl = ?, gross_pnl = ?, commissions = ?, pnl_percent = ? "
        "WHERE entry_time LIKE '2026-03-13%' AND short_strike = ? AND status = 'closed'",
        (round(broker_pnl, 2), GROSS_PNL, round(commissions, 2), pnl_percent,
         SHORT_STRIKE)
    )

    if cursor.rowcount != 1:
        print(f"ERROR: Expected 1 row updated, got {cursor.rowcount}. Rolling back.")
        conn.rollback()
        conn.close()
        return False

    conn.commit()

    # Read AFTER
    after = conn.execute(
        "SELECT id, pnl, gross_pnl, commissions, pnl_percent "
        "FROM trades WHERE id = ?",
        (before['id'],)
    ).fetchone()
    conn.close()

    print(f"\n  BEFORE: pnl=${before['pnl']}, gross_pnl={before['gross_pnl']}, "
          f"commissions={before['commissions']}, pnl_pct={before['pnl_percent']}")
    print(f"  AFTER:  pnl=${after['pnl']}, gross_pnl={after['gross_pnl']}, "
          f"commissions={after['commissions']}, pnl_pct={after['pnl_percent']}")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"=== Fix P&L for 2026-03-13 trade (6660/6665 call spread) ===\n")

    # 1. Check DB has the trade
    current = read_current_db_trade()
    if not current:
        print("ERROR: No matching closed trade found in trades_live.db for 2026-03-13.")
        print(f"  Looked for: entry_time LIKE '2026-03-13%', short_strike={SHORT_STRIKE}, status='closed'")
        sys.exit(1)
    print(f"  Found trade: {current['id']}")
    print(f"  Current DB pnl: ${current['pnl']}")
    print(f"  Current commissions: ${current['commissions']}")
    print(f"  Current gross_pnl: {current['gross_pnl']}")

    # 2. Connect to Schwab
    print(f"\n  Connecting to Schwab API...")
    try:
        client = get_schwab_client()
        account_hash = get_account_hash(client)
        print(f"  Authenticated successfully.")
    except Exception as e:
        print(f"ERROR: Failed to connect to Schwab: {e}")
        sys.exit(1)

    # 3. Fetch transactions
    print(f"\n  Fetching transactions for {TRADE_DATE}...")
    try:
        transactions = fetch_transactions(client, account_hash)
    except Exception as e:
        print(f"ERROR: Failed to fetch transactions: {e}")
        sys.exit(1)

    # 4. Print all transactions
    print_all_transactions(transactions)

    # 5. Match spread legs
    matched = match_spread_legs(transactions)
    print(f"  Matched {len(matched)} legs for 6660/6665 spread:")
    for leg in matched:
        print(f"    {leg['description']:30s}  ${leg['net_amount']:+,.2f}  {leg['symbol']}")

    # 6. Check for exactly 4 legs
    if len(matched) < 4:
        print(f"\n  INCOMPLETE: Only {len(matched)} of 4 expected legs found.")
        print(f"\n  Expected legs:")
        for desc, strike, cp, expected_amt in EXPECTED_LEGS:
            found = any(strike in m['description'] and desc.split()[0] in m['description']
                        for m in matched)
            status = 'FOUND' if found else 'MISSING'
            print(f"    [{status}] {desc:20s} {strike}{cp}  expected ${expected_amt:+,.2f}")
        print(f"\n  Database NOT modified.")
        sys.exit(1)

    # 7. Compute broker P&L
    broker_pnl = round(sum(m['net_amount'] for m in matched), 2)
    commissions = round(GROSS_PNL - broker_pnl, 2)

    print(f"\n{'='*50}")
    print(f"  Broker P&L from Schwab:  ${broker_pnl:+,.2f}")
    print(f"  Current DB pnl:          ${current['pnl']:+,.2f}")
    print(f"  Gross (price-based):     ${GROSS_PNL:+,.2f}")
    print(f"  Commissions (fees):      ${commissions:,.2f}")
    print(f"{'='*50}")

    # 8. Sanity check
    if not (SANITY_LOW <= broker_pnl <= SANITY_HIGH):
        print(f"\n  SANITY CHECK FAILED: ${broker_pnl:.2f} is outside "
              f"[${SANITY_LOW:.2f}, ${SANITY_HIGH:.2f}]")
        print(f"  Database NOT modified.")
        sys.exit(1)

    # 9. Prompt for confirmation
    answer = input(f"\n  Update database? (yes/no): ").strip().lower()
    if answer != 'yes':
        print("  Aborted. Database NOT modified.")
        sys.exit(0)

    # 10. Update
    print(f"\n  Updating trades_live.db...")
    ok = update_db(broker_pnl, commissions)
    if ok:
        print(f"\n  Done. P&L corrected from ${current['pnl']:.2f} → ${broker_pnl:.2f}")
    else:
        print(f"\n  Update failed.")
        sys.exit(1)


if __name__ == '__main__':
    main()
