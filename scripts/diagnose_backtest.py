"""
Backtest Diagnostic Script

Runs a backtest and prints trade-level details to verify:
1. Position sizing math at different equity levels
2. Equity endpoint vs daily P&L sum consistency
3. Sample trades from early and late in the backtest

Usage:
    python scripts/diagnose_backtest.py --csv path/to/spx.csv [--vix-csv path/to/vix.csv]
    python scripts/diagnose_backtest.py --start 2024-01-01 --end 2024-12-31
"""

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.backtest.data_loader import (
    download_spx_bars, download_vix_daily,
    load_bars_from_csv, load_vix_daily,
)
from src.backtest.engine import BacktestEngine


def main():
    parser = argparse.ArgumentParser(description='Backtest diagnostic tool')
    parser.add_argument('--start', type=str, default='2024-01-01')
    parser.add_argument('--end', type=str, default='2024-12-31')
    parser.add_argument('--capital', type=float, default=15000.0)
    parser.add_argument('--csv', type=str, default=None)
    parser.add_argument('--vix-csv', type=str, default=None)
    parser.add_argument('--max-contracts', type=int, default=5)
    parser.add_argument('--spread-width', type=float, default=5.0)
    parser.add_argument('--min-credit', type=float, default=1.00)
    parser.add_argument('--slippage', type=float, default=0.02)
    parser.add_argument('--max-daily-loss', type=float, default=2.0)
    args = parser.parse_args()

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)

    # Load data
    if args.csv:
        spx_csv = args.csv
    else:
        print("Downloading SPX data...")
        spx_csv = str(download_spx_bars(start_date, end_date))

    if args.vix_csv:
        vix_csv = args.vix_csv
    else:
        try:
            print("Downloading VIX data...")
            vix_csv = str(download_vix_daily(start_date, end_date))
        except Exception:
            vix_csv = None

    bars_df = load_bars_from_csv(spx_csv, start_date, end_date)
    vix_daily = {}
    if vix_csv:
        try:
            vix_daily = load_vix_daily(vix_csv, start_date, end_date)
        except Exception:
            pass

    print(f"\nLoaded {len(bars_df)} bars, {len(vix_daily)} VIX days")

    # Run backtest
    engine = BacktestEngine(
        bars_df=bars_df,
        vix_daily=vix_daily,
        initial_capital=args.capital,
        spread_width=args.spread_width,
        min_credit=args.min_credit,
        max_contracts=args.max_contracts,
        daily_contracts=args.max_contracts,
        swing_contracts=args.max_contracts,
        slippage=args.slippage,
        max_daily_loss_pct=args.max_daily_loss,
    )

    results = engine.run()

    trades = results['trades']
    daily_results = results['daily_results']
    initial_capital = results['initial_capital']
    final_capital = results['final_capital']

    print(f"\n{'='*70}")
    print(f"  BACKTEST DIAGNOSTIC REPORT")
    print(f"{'='*70}")
    print(f"  Period:          {args.start} to {args.end}")
    print(f"  Initial Capital: ${initial_capital:,.2f}")
    print(f"  Final Capital:   ${final_capital:,.2f}")
    print(f"  Total Trades:    {len(trades)}")
    print(f"  Trading Days:    {len(daily_results)}")

    # Compute daily P&L sum
    daily_pnl_sum = sum(dr['pnl'] for dr in daily_results)
    expected_final = initial_capital + daily_pnl_sum
    discrepancy = final_capital - expected_final

    print(f"\n--- Equity Consistency Check ---")
    print(f"  Sum of daily P&L:  ${daily_pnl_sum:,.2f}")
    print(f"  Expected final:    ${expected_final:,.2f}")
    print(f"  Actual final:      ${final_capital:,.2f}")
    print(f"  Discrepancy:       ${discrepancy:+,.2f}")

    if abs(discrepancy) < 1.0:
        print(f"  STATUS: PASS (within $1 tolerance)")
    else:
        print(f"  STATUS: FAIL (discrepancy exceeds $1)")

    if not trades:
        print("\nNo trades to display.")
        return

    # Show sample trades
    n_sample = min(5, len(trades))

    print(f"\n--- First {n_sample} Trades (early/small account) ---")
    _print_trades(trades[:n_sample], initial_capital, daily_results)

    if len(trades) > 10:
        print(f"\n--- Last {n_sample} Trades (late/large account) ---")
        _print_trades(trades[-n_sample:], initial_capital, daily_results)

    # Monthly summary
    from collections import defaultdict
    monthly_pnl = defaultdict(float)
    for dr in daily_results:
        d = date.fromisoformat(dr['date']) if isinstance(dr['date'], str) else dr['date']
        monthly_pnl[(d.year, d.month)] += dr['pnl']

    print(f"\n--- Monthly P&L Summary ---")
    running_equity = initial_capital
    for (year, month), pnl in sorted(monthly_pnl.items()):
        pct = (pnl / running_equity) * 100 if running_equity > 0 else 0
        print(f"  {year}-{month:02d}: P&L=${pnl:+,.2f}  "
              f"Start=${running_equity:,.2f}  Return={pct:+.2f}%")
        running_equity += pnl


def _print_trades(trades, initial_capital, daily_results):
    """Print trade details with position sizing context."""
    # Build running equity by date for context
    equity_by_date = {}
    running = initial_capital
    for dr in daily_results:
        d = dr['date'] if isinstance(dr['date'], str) else dr['date'].isoformat()
        equity_by_date[d] = running
        running += dr['pnl']

    for t in trades:
        entry_date = t['entry_time'][:10] if isinstance(t['entry_time'], str) else t['entry_time'].strftime('%Y-%m-%d')
        equity_at_entry = equity_by_date.get(entry_date, initial_capital)
        qty = t.get('quantity', 1)
        credit = t.get('credit_received', 0)
        pnl = t.get('pnl', 0)
        direction = t.get('direction', '?')
        reason = t.get('exit_reason', '?')
        strategy = t.get('strategy_type', '?')

        print(f"  [{entry_date}] {strategy}/{direction} qty={qty} "
              f"credit=${credit:.2f} pnl=${pnl:+,.2f} "
              f"exit={reason} equity~${equity_at_entry:,.0f}")


if __name__ == '__main__':
    main()
