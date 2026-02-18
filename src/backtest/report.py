"""
Backtest Performance Report Generator

Computes comprehensive performance analytics from backtest results.
Generates both JSON (for dashboard) and markdown (for docs) output.
"""

import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional


def generate_report(
    results: Dict,
    risk_free_rate: float = 0.045,
) -> Dict:
    """
    Generate comprehensive performance report from backtest results.

    Args:
        results: Output from BacktestEngine.run()
        risk_free_rate: Annual risk-free rate for Sharpe calculation

    Returns:
        Dict with all performance metrics, breakdown data, and chart series
    """
    trades = results['trades']
    daily_results = results['daily_results']
    equity_curve = results['equity_curve']
    initial_capital = results['initial_capital']
    final_capital = results['final_capital']

    report = {}

    # Core metrics
    report['core'] = _compute_core_metrics(
        trades, daily_results, equity_curve,
        initial_capital, final_capital, risk_free_rate,
    )

    # Trade metrics
    report['trade_metrics'] = _compute_trade_metrics(trades)

    # Breakdown data
    report['monthly_returns'] = _compute_monthly_returns(daily_results, initial_capital)
    report['by_day_of_week'] = _compute_by_day_of_week(trades)
    report['by_vix_regime'] = _compute_by_vix_regime(trades)
    report['by_strategy'] = _compute_by_strategy(trades)

    # Chart data
    report['equity_curve'] = equity_curve
    report['drawdown_series'] = _compute_drawdown_series(equity_curve, initial_capital)
    report['daily_pnl_series'] = [
        {'date': d['date'], 'pnl': d['daily_pnl']}
        for d in equity_curve
    ]

    # Summary
    report['initial_capital'] = initial_capital
    report['final_capital'] = final_capital
    report['total_trading_days'] = len(daily_results)
    report['days_with_trades'] = sum(1 for d in daily_results if d['trades'] > 0)

    return report


def _compute_core_metrics(
    trades, daily_results, equity_curve,
    initial_capital, final_capital, risk_free_rate,
) -> Dict:
    """Core performance metrics."""
    total_return_dollar = final_capital - initial_capital
    total_return_pct = (total_return_dollar / initial_capital) * 100 if initial_capital > 0 else 0

    # Annualized return
    total_days = len(daily_results)
    trading_days_per_year = 252
    years = total_days / trading_days_per_year if total_days > 0 else 1
    annualized_return = ((final_capital / initial_capital) ** (1 / years) - 1) * 100 if years > 0 else 0

    # Daily returns for Sharpe/Sortino
    daily_returns = []
    for ec in equity_curve:
        daily_pnl = ec.get('daily_pnl', 0)
        equity = ec.get('equity', initial_capital)
        prev_equity = equity - daily_pnl
        if prev_equity > 0:
            daily_returns.append(daily_pnl / prev_equity)
        else:
            daily_returns.append(0)

    # Sharpe ratio
    sharpe = _sharpe_ratio(daily_returns, risk_free_rate, trading_days_per_year)

    # Sortino ratio
    sortino = _sortino_ratio(daily_returns, risk_free_rate, trading_days_per_year)

    # Max drawdown
    dd_dollar, dd_pct, dd_duration = _max_drawdown(equity_curve, initial_capital)

    # Calmar ratio
    calmar = (annualized_return / abs(dd_pct)) if dd_pct != 0 else 0

    return {
        'total_return_dollar': round(total_return_dollar, 2),
        'total_return_pct': round(total_return_pct, 2),
        'annualized_return': round(annualized_return, 2),
        'sharpe_ratio': round(sharpe, 3),
        'sortino_ratio': round(sortino, 3),
        'calmar_ratio': round(calmar, 3),
        'max_drawdown_dollar': round(dd_dollar, 2),
        'max_drawdown_pct': round(dd_pct, 2),
        'max_drawdown_duration_days': dd_duration,
    }


def _compute_trade_metrics(trades: List[Dict]) -> Dict:
    """Trade-level statistics."""
    if not trades:
        return _empty_trade_metrics()

    total = len(trades)
    pnls = [t['pnl'] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    win_rate = (len(wins) / total * 100) if total > 0 else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    win_loss_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')

    expectancy = sum(pnls) / total if total > 0 else 0

    # Streaks
    max_consec_wins, max_consec_losses = _compute_streaks(pnls)

    # Average time in trade
    durations = [t.get('duration_minutes', 0) for t in trades if t.get('duration_minutes', 0) > 0]
    avg_duration = sum(durations) / len(durations) if durations else 0

    # Trades per month
    if trades:
        first = trades[0].get('entry_time', '')
        last = trades[-1].get('entry_time', '')
        if first and last:
            try:
                d1 = datetime.fromisoformat(first)
                d2 = datetime.fromisoformat(last)
                months = max((d2 - d1).days / 30.44, 1)
                trades_per_month = total / months
            except (ValueError, TypeError):
                trades_per_month = 0
        else:
            trades_per_month = 0
    else:
        trades_per_month = 0

    return {
        'total_trades': total,
        'win_rate': round(win_rate, 1),
        'profit_factor': round(profit_factor, 2) if profit_factor != float('inf') else 999.99,
        'expectancy': round(expectancy, 2),
        'avg_win': round(avg_win, 2),
        'avg_loss': round(avg_loss, 2),
        'win_loss_ratio': round(win_loss_ratio, 2) if win_loss_ratio != float('inf') else 999.99,
        'max_consecutive_wins': max_consec_wins,
        'max_consecutive_losses': max_consec_losses,
        'avg_duration_minutes': round(avg_duration, 0),
        'trades_per_month': round(trades_per_month, 1),
        'total_pnl': round(sum(pnls), 2),
        'gross_profit': round(gross_profit, 2),
        'gross_loss': round(gross_loss, 2),
    }


def _empty_trade_metrics() -> Dict:
    return {
        'total_trades': 0, 'win_rate': 0, 'profit_factor': 0,
        'expectancy': 0, 'avg_win': 0, 'avg_loss': 0,
        'win_loss_ratio': 0, 'max_consecutive_wins': 0,
        'max_consecutive_losses': 0, 'avg_duration_minutes': 0,
        'trades_per_month': 0, 'total_pnl': 0,
        'gross_profit': 0, 'gross_loss': 0,
    }


def _compute_streaks(pnls: List[float]):
    """Compute max consecutive wins and losses."""
    max_wins = 0
    max_losses = 0
    current_wins = 0
    current_losses = 0

    for pnl in pnls:
        if pnl > 0:
            current_wins += 1
            current_losses = 0
            max_wins = max(max_wins, current_wins)
        else:
            current_losses += 1
            current_wins = 0
            max_losses = max(max_losses, current_losses)

    return max_wins, max_losses


def _sharpe_ratio(daily_returns: List[float], risk_free_rate: float, ann_factor: int) -> float:
    """Annualized Sharpe ratio."""
    if len(daily_returns) < 2:
        return 0

    daily_rf = risk_free_rate / ann_factor
    excess = [r - daily_rf for r in daily_returns]

    mean_excess = sum(excess) / len(excess)
    variance = sum((r - mean_excess) ** 2 for r in excess) / (len(excess) - 1)
    std_dev = math.sqrt(variance) if variance > 0 else 0

    if std_dev == 0:
        return 0

    return (mean_excess / std_dev) * math.sqrt(ann_factor)


def _sortino_ratio(daily_returns: List[float], risk_free_rate: float, ann_factor: int) -> float:
    """Annualized Sortino ratio (downside deviation only)."""
    if len(daily_returns) < 2:
        return 0

    daily_rf = risk_free_rate / ann_factor
    excess = [r - daily_rf for r in daily_returns]

    mean_excess = sum(excess) / len(excess)

    # Downside deviation
    downside = [min(r, 0) ** 2 for r in excess]
    downside_var = sum(downside) / (len(downside) - 1) if len(downside) > 1 else 0
    downside_dev = math.sqrt(downside_var)

    if downside_dev == 0:
        return 0

    return (mean_excess / downside_dev) * math.sqrt(ann_factor)


def _max_drawdown(equity_curve: List[Dict], initial_capital: float):
    """Compute max drawdown in dollars, percent, and duration (trading days)."""
    if not equity_curve:
        return 0, 0, 0

    peak = initial_capital
    max_dd_dollar = 0
    max_dd_pct = 0
    peak_idx = 0
    max_dd_duration = 0
    current_dd_start = 0

    for i, ec in enumerate(equity_curve):
        equity = ec.get('equity', initial_capital)
        if equity >= peak:
            peak = equity
            peak_idx = i
            current_dd_start = i

        dd_dollar = peak - equity
        dd_pct = (dd_dollar / peak * 100) if peak > 0 else 0

        if dd_dollar > max_dd_dollar:
            max_dd_dollar = dd_dollar
            max_dd_pct = dd_pct
            max_dd_duration = i - peak_idx

    return max_dd_dollar, max_dd_pct, max_dd_duration


def _compute_drawdown_series(equity_curve: List[Dict], initial_capital: float) -> List[Dict]:
    """Compute drawdown percentage series for charting."""
    if not equity_curve:
        return []

    peak = initial_capital
    series = []

    for ec in equity_curve:
        equity = ec.get('equity', initial_capital)
        if equity > peak:
            peak = equity
        dd_pct = ((peak - equity) / peak * 100) if peak > 0 else 0
        series.append({
            'date': ec['date'],
            'drawdown_pct': round(dd_pct, 2),
        })

    return series


def _compute_monthly_returns(daily_results: List[Dict], initial_capital: float) -> List[Dict]:
    """Compute monthly returns grid (year x month)."""
    monthly = defaultdict(float)

    for dr in daily_results:
        d = date.fromisoformat(dr['date']) if isinstance(dr['date'], str) else dr['date']
        key = (d.year, d.month)
        monthly[key] += dr['pnl']

    rows = []
    for (year, month), pnl in sorted(monthly.items()):
        rows.append({
            'year': year,
            'month': month,
            'pnl': round(pnl, 2),
            'return_pct': round((pnl / initial_capital) * 100, 2),
        })

    return rows


def _compute_by_day_of_week(trades: List[Dict]) -> List[Dict]:
    """Win rate breakdown by day of week."""
    day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
    day_stats = {i: {'wins': 0, 'total': 0, 'pnl': 0} for i in range(5)}

    for t in trades:
        entry_time = t.get('entry_time', '')
        if not entry_time:
            continue
        try:
            dt = datetime.fromisoformat(entry_time)
            dow = dt.weekday()
            if dow < 5:
                day_stats[dow]['total'] += 1
                day_stats[dow]['pnl'] += t['pnl']
                if t['pnl'] > 0:
                    day_stats[dow]['wins'] += 1
        except (ValueError, TypeError):
            continue

    return [
        {
            'day': day_names[i],
            'total': day_stats[i]['total'],
            'wins': day_stats[i]['wins'],
            'win_rate': round((day_stats[i]['wins'] / day_stats[i]['total'] * 100), 1) if day_stats[i]['total'] > 0 else 0,
            'total_pnl': round(day_stats[i]['pnl'], 2),
        }
        for i in range(5)
    ]


def _compute_by_vix_regime(trades: List[Dict]) -> List[Dict]:
    """Win rate breakdown by VIX regime."""
    regimes = {
        'low': {'label': 'Low (<15)', 'wins': 0, 'total': 0, 'pnl': 0},
        'normal': {'label': 'Normal (15-20)', 'wins': 0, 'total': 0, 'pnl': 0},
        'elevated': {'label': 'Elevated (20-30)', 'wins': 0, 'total': 0, 'pnl': 0},
        'high': {'label': 'High (>30)', 'wins': 0, 'total': 0, 'pnl': 0},
    }

    for t in trades:
        vix = t.get('vix_at_entry', 0)
        if vix <= 0:
            continue

        if vix < 15:
            regime = 'low'
        elif vix < 20:
            regime = 'normal'
        elif vix < 30:
            regime = 'elevated'
        else:
            regime = 'high'

        regimes[regime]['total'] += 1
        regimes[regime]['pnl'] += t['pnl']
        if t['pnl'] > 0:
            regimes[regime]['wins'] += 1

    return [
        {
            'regime': v['label'],
            'total': v['total'],
            'wins': v['wins'],
            'win_rate': round((v['wins'] / v['total'] * 100), 1) if v['total'] > 0 else 0,
            'total_pnl': round(v['pnl'], 2),
        }
        for v in regimes.values()
    ]


def _compute_by_strategy(trades: List[Dict]) -> List[Dict]:
    """P&L breakdown by strategy type."""
    strategies = defaultdict(lambda: {'wins': 0, 'total': 0, 'pnl': 0})

    for t in trades:
        st = t.get('strategy_type', 'daily_income')
        strategies[st]['total'] += 1
        strategies[st]['pnl'] += t['pnl']
        if t['pnl'] > 0:
            strategies[st]['wins'] += 1

    return [
        {
            'strategy': k,
            'total': v['total'],
            'wins': v['wins'],
            'win_rate': round((v['wins'] / v['total'] * 100), 1) if v['total'] > 0 else 0,
            'total_pnl': round(v['pnl'], 2),
        }
        for k, v in strategies.items()
    ]


def format_markdown_report(report: Dict) -> str:
    """Generate a markdown-formatted backtest report."""
    lines = []
    lines.append('# Backtest Performance Report')
    lines.append(f'Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}')
    lines.append('')

    core = report['core']
    tm = report['trade_metrics']

    lines.append('## Summary')
    lines.append(f'- **Initial Capital:** ${report["initial_capital"]:,.2f}')
    lines.append(f'- **Final Capital:** ${report["final_capital"]:,.2f}')
    lines.append(f'- **Total Return:** ${core["total_return_dollar"]:+,.2f} ({core["total_return_pct"]:+.2f}%)')
    lines.append(f'- **Annualized Return:** {core["annualized_return"]:+.2f}%')
    lines.append(f'- **Trading Days:** {report["total_trading_days"]} ({report["days_with_trades"]} with trades)')
    lines.append('')

    lines.append('## Risk Metrics')
    lines.append(f'- **Sharpe Ratio:** {core["sharpe_ratio"]:.3f}')
    lines.append(f'- **Sortino Ratio:** {core["sortino_ratio"]:.3f}')
    lines.append(f'- **Calmar Ratio:** {core["calmar_ratio"]:.3f}')
    lines.append(f'- **Max Drawdown:** ${core["max_drawdown_dollar"]:,.2f} ({core["max_drawdown_pct"]:.2f}%)')
    lines.append(f'- **Max Drawdown Duration:** {core["max_drawdown_duration_days"]} trading days')
    lines.append('')

    lines.append('## Trade Statistics')
    lines.append(f'- **Total Trades:** {tm["total_trades"]}')
    lines.append(f'- **Win Rate:** {tm["win_rate"]}%')
    lines.append(f'- **Profit Factor:** {tm["profit_factor"]}')
    lines.append(f'- **Expectancy:** ${tm["expectancy"]:+.2f} per trade')
    lines.append(f'- **Avg Win:** ${tm["avg_win"]:+.2f}')
    lines.append(f'- **Avg Loss:** ${tm["avg_loss"]:+.2f}')
    lines.append(f'- **Win/Loss Ratio:** {tm["win_loss_ratio"]:.2f}')
    lines.append(f'- **Max Consecutive Wins:** {tm["max_consecutive_wins"]}')
    lines.append(f'- **Max Consecutive Losses:** {tm["max_consecutive_losses"]}')
    lines.append(f'- **Avg Duration:** {tm["avg_duration_minutes"]:.0f} minutes')
    lines.append(f'- **Trades/Month:** {tm["trades_per_month"]:.1f}')
    lines.append('')

    # Monthly returns table
    monthly = report.get('monthly_returns', [])
    if monthly:
        lines.append('## Monthly Returns')
        lines.append('')
        lines.append('| Year | Month | P&L | Return % |')
        lines.append('|------|-------|-----|----------|')
        month_names = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                       'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
        for m in monthly:
            mn = month_names[m['month']] if m['month'] <= 12 else str(m['month'])
            lines.append(f'| {m["year"]} | {mn} | ${m["pnl"]:+,.2f} | {m["return_pct"]:+.2f}% |')
        lines.append('')

    # Day of week
    dow = report.get('by_day_of_week', [])
    if dow:
        lines.append('## Win Rate by Day of Week')
        lines.append('')
        lines.append('| Day | Trades | Win Rate | P&L |')
        lines.append('|-----|--------|----------|-----|')
        for d in dow:
            if d['total'] > 0:
                lines.append(f'| {d["day"]} | {d["total"]} | {d["win_rate"]}% | ${d["total_pnl"]:+,.2f} |')
        lines.append('')

    # VIX regime
    vix = report.get('by_vix_regime', [])
    if vix:
        lines.append('## Win Rate by VIX Regime')
        lines.append('')
        lines.append('| Regime | Trades | Win Rate | P&L |')
        lines.append('|--------|--------|----------|-----|')
        for v in vix:
            if v['total'] > 0:
                lines.append(f'| {v["regime"]} | {v["total"]} | {v["win_rate"]}% | ${v["total_pnl"]:+,.2f} |')
        lines.append('')

    return '\n'.join(lines)
