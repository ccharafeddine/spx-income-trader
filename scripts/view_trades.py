#!/usr/bin/env python3
"""
View Trade History

Display recent trades with filtering options
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta
import argparse

sys.path.insert(0, str(Path(__file__).parent.parent))

from database.db_manager import DatabaseManager
from config.settings import DATABASE_PATH


def format_trade(trade):
    """Format trade for display"""
    entry_time = trade.get('entry_time', '')
    if isinstance(entry_time, str):
        try:
            entry_time = datetime.fromisoformat(entry_time).strftime('%Y-%m-%d %H:%M')
        except:
            entry_time = str(entry_time)[:16]
    
    direction = trade.get('direction', 'N/A').upper()
    strikes = f"{trade.get('short_strike', 0)}/{trade.get('long_strike', 0)}"
    credit = trade.get('credit_received', 0)
    pnl = trade.get('pnl', 0) or 0
    status = trade.get('status', 'N/A')
    
    pnl_str = f"${pnl:+.2f}"
    if pnl > 0:
        pnl_str = f"\033[92m{pnl_str}\033[0m"  # Green
    elif pnl < 0:
        pnl_str = f"\033[91m{pnl_str}\033[0m"  # Red
    
    return f"{entry_time} | {direction:8} | {strikes:11} | ${credit:5.2f} | {pnl_str:12} | {status}"


def main():
    parser = argparse.ArgumentParser(description="View trade history")
    parser.add_argument('--days', type=int, default=7, help='Number of days to show')
    parser.add_argument('--today', action='store_true', help='Show only today\'s trades')
    
    args = parser.parse_args()
    
    db = DatabaseManager(DATABASE_PATH)
    
    print("=" * 100)
    print("TRADE HISTORY")
    print("=" * 100)
    
    if args.today:
        today = datetime.now().date()
        trades = db.get_trades_by_date(today)
        print(f"Today's Trades ({today})")
    else:
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=args.days)
        
        all_trades = []
        current_date = start_date
        while current_date <= end_date:
            trades = db.get_trades_by_date(current_date)
            all_trades.extend(trades)
            current_date += timedelta(days=1)
        
        trades = all_trades
        print(f"Trades from last {args.days} days")
    
    print(f"Total trades: {len(trades)}")
    print("-" * 100)
    print(f"{'Time':<17} | {'Direction':<8} | {'Strikes':<11} | {'Credit':<6} | {'P&L':<12} | Status")
    print("-" * 100)
    
    if not trades:
        print("No trades found")
    else:
        for trade in trades:
            print(format_trade(trade))
    
    # Summary
    closed_trades = [t for t in trades if t.get('status') == 'closed' and t.get('pnl') is not None]
    
    if closed_trades:
        total_pnl = sum(t.get('pnl', 0) for t in closed_trades)
        winners = sum(1 for t in closed_trades if t.get('pnl', 0) > 0)
        losers = sum(1 for t in closed_trades if t.get('pnl', 0) < 0)
        win_rate = (winners / len(closed_trades) * 100) if closed_trades else 0
        
        print("-" * 100)
        print(f"Summary: {len(closed_trades)} closed trades | "
              f"Winners: {winners} | Losers: {losers} | "
              f"Win Rate: {win_rate:.1f}% | Total P&L: ${total_pnl:.2f}")
    
    print("=" * 100)


if __name__ == "__main__":
    main()