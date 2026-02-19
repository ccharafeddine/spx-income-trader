"""
Backtest CLI Runner

Usage:
    python -m src.backtest.runner --start 2024-01-01 --end 2025-12-31

Options:
    --start         Start date (YYYY-MM-DD)
    --end           End date (YYYY-MM-DD)
    --capital       Initial capital (default: 50000)
    --pulse-threshold  Pulse bar threshold % (default: 10)
    --spread-width  Credit spread width $ (default: 5)
    --profit-target Profit target % (default: 80)
    --min-credit    Minimum credit $ (default: 1.00)
    --max-contracts Max contracts per trade (default: 5)
    --slippage      Slippage per contract $ (default: 0.02)
    --max-daily-loss Max daily loss % (default: 2.0)
    --output        Output markdown file path
    --csv           Path to existing CSV (skips download)
"""

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.backtest.data_loader import (
    download_spx_bars, download_vix_daily,
    load_bars_from_csv, load_vix_daily,
)
from src.backtest.engine import BacktestEngine
from src.backtest.report import generate_report, format_markdown_report

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description='SPX Income Strategy Backtester',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument('--start', type=str, required=True,
                        help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end', type=str, required=True,
                        help='End date (YYYY-MM-DD)')
    parser.add_argument('--capital', type=float, default=50000.0,
                        help='Initial capital (default: 50000)')
    parser.add_argument('--pulse-threshold', type=float, default=10.0,
                        help='Pulse bar threshold %% (default: 10)')
    parser.add_argument('--spread-width', type=float, default=5.0,
                        help='Credit spread width $ (default: 5)')
    parser.add_argument('--profit-target', type=float, default=80.0,
                        help='Profit target %% (default: 80)')
    parser.add_argument('--min-credit', type=float, default=1.00,
                        help='Minimum credit $ (default: 1.00)')
    parser.add_argument('--max-contracts', type=int, default=5,
                        help='Max contracts per trade (default: 5)')
    parser.add_argument('--slippage', type=float, default=0.02,
                        help='Slippage per contract $ (default: 0.02)')
    parser.add_argument('--max-daily-loss', type=float, default=2.0,
                        help='Max daily loss %% (default: 2.0)')
    parser.add_argument('--output', type=str, default=None,
                        help='Output markdown file path')
    parser.add_argument('--csv', type=str, default=None,
                        help='Path to existing SPX CSV (skips download)')
    parser.add_argument('--vix-csv', type=str, default=None,
                        help='Path to existing VIX CSV (skips download)')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Verbose logging')

    args = parser.parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S',
    )

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)

    print(f"\n{'='*60}")
    print(f"  SPX Income Strategy Backtester")
    print(f"{'='*60}")
    print(f"  Period:     {start_date} to {end_date}")
    print(f"  Capital:    ${args.capital:,.0f}")
    print(f"  Pulse:      {args.pulse_threshold}%")
    print(f"  Spread:     ${args.spread_width}")
    print(f"  Target:     {args.profit_target}%")
    print(f"  Min Credit: ${args.min_credit}")
    print(f"  Slippage:   ${args.slippage}")
    print(f"{'='*60}\n")

    # Load or download SPX data
    if args.csv:
        spx_csv = args.csv
    else:
        print("Downloading SPX 30-minute data from Yahoo Finance...")
        try:
            spx_csv = str(download_spx_bars(start_date, end_date))
        except Exception as e:
            print(f"ERROR: Failed to download SPX data: {e}")
            sys.exit(1)

    # Load or download VIX data
    if args.vix_csv:
        vix_csv = args.vix_csv
    else:
        print("Downloading VIX daily data...")
        try:
            vix_csv = str(download_vix_daily(start_date, end_date))
        except Exception as e:
            print(f"WARNING: VIX download failed ({e}), using default VIX=15")
            vix_csv = None

    # Load data
    print("Loading bar data...")
    bars_df = load_bars_from_csv(spx_csv, start_date, end_date)
    print(f"  Loaded {len(bars_df)} bars")

    vix_daily = {}
    if vix_csv:
        try:
            vix_daily = load_vix_daily(vix_csv, start_date, end_date)
            print(f"  Loaded {len(vix_daily)} VIX daily values")
        except Exception as e:
            print(f"  VIX loading failed ({e}), using default VIX=15")

    # Progress bar
    try:
        from tqdm import tqdm
        pbar = tqdm(total=0, desc="Backtesting", unit="day")

        def progress_callback(current, total, trading_day):
            if pbar.total != total:
                pbar.total = total
                pbar.refresh()
            pbar.update(1)
            pbar.set_postfix(date=str(trading_day))

    except ImportError:
        pbar = None

        def progress_callback(current, total, trading_day):
            if current % 50 == 0 or current == total:
                pct = current / total * 100
                print(f"  Progress: {pct:.0f}% ({current}/{total} days) - {trading_day}")

    # Run backtest
    print("\nRunning backtest...")
    engine = BacktestEngine(
        bars_df=bars_df,
        vix_daily=vix_daily,
        initial_capital=args.capital,
        pulse_threshold=args.pulse_threshold,
        spread_width=args.spread_width,
        profit_target_pct=args.profit_target,
        min_credit=args.min_credit,
        max_contracts=args.max_contracts,
        slippage=args.slippage,
        max_daily_loss_pct=args.max_daily_loss,
        progress_callback=progress_callback,
    )

    results = engine.run()

    if pbar:
        pbar.close()

    # Generate report
    report = generate_report(results)

    # Print summary
    core = report['core']
    tm = report['trade_metrics']

    print(f"\n{'='*60}")
    print(f"  BACKTEST RESULTS")
    print(f"{'='*60}")
    print(f"  Total Return:     ${core['total_return_dollar']:+,.2f} ({core['total_return_pct']:+.2f}%)")
    print(f"  Annualized:       {core['annualized_return']:+.2f}%")
    print(f"  Sharpe Ratio:     {core['sharpe_ratio']:.3f}")
    print(f"  Sortino Ratio:    {core['sortino_ratio']:.3f}")
    print(f"  Max Drawdown:     ${core['max_drawdown_dollar']:,.2f} ({core['max_drawdown_pct']:.2f}%)")
    print(f"  Calmar Ratio:     {core['calmar_ratio']:.3f}")
    print(f"{'='*60}")
    print(f"  Total Trades:     {tm['total_trades']}")
    print(f"  Win Rate:         {tm['win_rate']}%")
    print(f"  Profit Factor:    {tm['profit_factor']}")
    print(f"  Expectancy:       ${tm['expectancy']:+.2f}/trade")
    print(f"  Avg Win:          ${tm['avg_win']:+.2f}")
    print(f"  Avg Loss:         ${tm['avg_loss']:+.2f}")
    print(f"  Max Consec Wins:  {tm['max_consecutive_wins']}")
    print(f"  Max Consec Losses:{tm['max_consecutive_losses']}")
    print(f"{'='*60}\n")

    # Save markdown report
    output_path = args.output
    if not output_path:
        output_path = f"docs/backtest_report_{datetime.now().strftime('%Y%m%d')}.md"

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    md = format_markdown_report(report)
    with open(output_file, 'w') as f:
        f.write(md)

    print(f"Report saved to: {output_file}")


if __name__ == '__main__':
    main()
