"""
Market regime analysis for strategy performance evaluation.

Classifies trades by VIX regime and SPX daily return regime, computes
per-regime performance metrics, and measures market neutrality via
correlation and beta to SPX.
"""

import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

MIN_TRADES = 5

# VIX regime thresholds
VIX_REGIMES = [
    ('Calm', 0, 15),
    ('Normal', 15, 20),
    ('Elevated', 20, 30),
    ('Crisis', 30, float('inf')),
]

# SPX daily return regimes
DIRECTION_REGIMES = [
    ('Strong Down', -float('inf'), -1.0),
    ('Down', -1.0, -0.25),
    ('Flat', -0.25, 0.25),
    ('Up', 0.25, 1.0),
    ('Strong Up', 1.0, float('inf')),
]


def _classify_vix_regime(vix):
    """Classify a VIX value into a regime name."""
    if vix is None:
        return None
    for name, low, high in VIX_REGIMES:
        if low <= vix < high:
            return name
    return None


def _classify_direction_regime(daily_return_pct):
    """Classify a daily SPX return into a direction regime."""
    if daily_return_pct is None:
        return None
    for name, low, high in DIRECTION_REGIMES:
        if low <= daily_return_pct < high:
            return name
    return None


def _compute_regime_stats(trades_in_regime):
    """Compute performance stats for a group of trades."""
    if not trades_in_regime:
        return {
            'count': 0, 'win_rate': 0, 'avg_pnl': 0, 'total_pnl': 0,
            'profit_factor': 0, 'best_trade': 0, 'worst_trade': 0,
        }

    pnls = [t.get('pnl', 0) for t in trades_in_regime]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    total_wins = sum(wins) if wins else 0
    total_losses = abs(sum(losses)) if losses else 0

    return {
        'count': len(pnls),
        'win_rate': round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
        'avg_pnl': round(sum(pnls) / len(pnls), 2) if pnls else 0,
        'total_pnl': round(sum(pnls), 2),
        'profit_factor': round(total_wins / total_losses, 2) if total_losses > 0 else (
            float('inf') if total_wins > 0 else 0),
        'best_trade': round(max(pnls), 2) if pnls else 0,
        'worst_trade': round(min(pnls), 2) if pnls else 0,
    }


def _get_daily_return_pct(trade):
    """Get SPX daily return percentage for a trade.

    Uses daily_move_pct if available, otherwise computes from
    spx_at_entry and spx_at_exit.
    """
    dmp = trade.get('daily_move_pct')
    if dmp is not None:
        return float(dmp)

    entry = trade.get('spx_at_entry')
    exit_p = trade.get('spx_at_exit')
    if entry and exit_p and entry > 0:
        return ((exit_p - entry) / entry) * 100
    return None


class RegimeAnalyzer:
    """Analyze strategy performance across market regimes."""

    def analyze_by_vix_regime(self, trades):
        """Performance breakdown by VIX regime at entry.

        Returns a list of regime dicts ordered Calm -> Crisis.
        """
        buckets = defaultdict(list)
        for t in trades:
            vix = t.get('vix_at_entry')
            regime = _classify_vix_regime(vix)
            if regime:
                buckets[regime].append(t)

        results = []
        for name, _, _ in VIX_REGIMES:
            stats = _compute_regime_stats(buckets.get(name, []))
            stats['regime'] = name
            results.append(stats)
        return results

    def analyze_by_direction_regime(self, trades):
        """Performance breakdown by SPX daily return regime.

        Returns a list of regime dicts ordered Strong Down -> Strong Up.
        """
        buckets = defaultdict(list)
        for t in trades:
            daily_ret = _get_daily_return_pct(t)
            regime = _classify_direction_regime(daily_ret)
            if regime:
                buckets[regime].append(t)

        results = []
        for name, _, _ in DIRECTION_REGIMES:
            stats = _compute_regime_stats(buckets.get(name, []))
            stats['regime'] = name
            results.append(stats)
        return results

    def analyze_regime_transitions(self, trades):
        """How the strategy performs during VIX transitions.

        Groups trades into rising VIX, falling VIX, and stable VIX
        based on entry vs exit VIX.
        """
        rising = []
        falling = []
        stable = []

        for t in trades:
            vix_entry = t.get('vix_at_entry')
            vix_exit = t.get('vix_at_exit')
            if vix_entry is None or vix_exit is None:
                continue
            diff = vix_exit - vix_entry
            if diff > 0.5:
                rising.append(t)
            elif diff < -0.5:
                falling.append(t)
            else:
                stable.append(t)

        return {
            'rising_vix': _compute_regime_stats(rising),
            'falling_vix': _compute_regime_stats(falling),
            'stable_vix': _compute_regime_stats(stable),
        }

    def market_neutrality_score(self, trades):
        """Measure correlation of trade P&L to SPX daily return.

        Returns correlation, R-squared, beta, and interpretation.
        Requires at least MIN_TRADES trades with both P&L and daily return.
        """
        pairs = []
        for t in trades:
            pnl = t.get('pnl')
            daily_ret = _get_daily_return_pct(t)
            if pnl is not None and daily_ret is not None:
                pairs.append((daily_ret, pnl))

        if len(pairs) < MIN_TRADES:
            return {
                'correlation': None,
                'r_squared': None,
                'beta': None,
                'sufficient_data': False,
                'data_points': len(pairs),
                'interpretation': 'Insufficient data for neutrality analysis.',
            }

        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        n = len(pairs)

        mean_x = sum(xs) / n
        mean_y = sum(ys) / n

        cov_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / n
        var_x = sum((x - mean_x) ** 2 for x in xs) / n
        var_y = sum((y - mean_y) ** 2 for y in ys) / n

        std_x = var_x ** 0.5
        std_y = var_y ** 0.5

        if std_x == 0 or std_y == 0:
            correlation = 0.0
        else:
            correlation = cov_xy / (std_x * std_y)

        r_squared = correlation ** 2
        beta = cov_xy / var_x if var_x > 0 else 0.0

        abs_corr = abs(correlation)
        if abs_corr < 0.2:
            level = 'low'
        elif abs_corr < 0.5:
            level = 'moderate'
        else:
            level = 'high'

        interpretation = f'Strategy shows {level} correlation to market direction.'

        return {
            'correlation': round(correlation, 3),
            'r_squared': round(r_squared, 3),
            'beta': round(beta, 3),
            'sufficient_data': True,
            'data_points': n,
            'interpretation': interpretation,
        }

    def analyze_direction_drilldown(self, trades):
        """Cross-tabulate market direction regime x trade direction.

        Returns a list of rows (one per direction regime) with bullish/bearish
        sub-stats, plus up-day detail cards and interpretation text.
        """
        # Group trades by (market_direction, trade_direction)
        cross = {}  # {regime_name: {'bullish': [...], 'bearish': [...]}}
        for name, _, _ in DIRECTION_REGIMES:
            cross[name] = {'bullish': [], 'bearish': []}

        for t in trades:
            daily_ret = _get_daily_return_pct(t)
            regime = _classify_direction_regime(daily_ret)
            if not regime:
                continue
            trade_dir = (t.get('direction') or '').lower()
            if trade_dir in ('bullish', 'bearish'):
                cross[regime][trade_dir].append(t)

        # Build table rows
        rows = []
        for name, _, _ in DIRECTION_REGIMES:
            bull = cross[name]['bullish']
            bear = cross[name]['bearish']
            bull_pnls = [t.get('pnl', 0) for t in bull]
            bear_pnls = [t.get('pnl', 0) for t in bear]
            bull_wins = sum(1 for p in bull_pnls if p > 0)
            bear_wins = sum(1 for p in bear_pnls if p > 0)
            rows.append({
                'regime': name,
                'bull_count': len(bull),
                'bull_win_rate': round(bull_wins / len(bull) * 100, 1) if bull else 0,
                'bull_avg_pnl': round(sum(bull_pnls) / len(bull_pnls), 2) if bull_pnls else 0,
                'bear_count': len(bear),
                'bear_win_rate': round(bear_wins / len(bear) * 100, 1) if bear else 0,
                'bear_avg_pnl': round(sum(bear_pnls) / len(bear_pnls), 2) if bear_pnls else 0,
            })

        # Up-day detail
        up_row = next((r for r in rows if r['regime'] == 'Up'), None)
        up_detail = None
        if up_row and (up_row['bull_count'] + up_row['bear_count']) > 0:
            up_detail = {
                'bull_count': up_row['bull_count'],
                'bull_win_rate': up_row['bull_win_rate'],
                'bull_avg_pnl': up_row['bull_avg_pnl'],
                'bear_count': up_row['bear_count'],
                'bear_win_rate': up_row['bear_win_rate'],
                'bear_avg_pnl': up_row['bear_avg_pnl'],
            }

        # Interpretation
        interpretation = self._direction_drilldown_interpretation(up_row)

        return {
            'rows': rows,
            'up_detail': up_detail,
            'interpretation': interpretation,
        }

    @staticmethod
    def _direction_drilldown_interpretation(up_row):
        """Generate interpretation text from the Up-day row."""
        if not up_row or (up_row['bull_count'] + up_row['bear_count']) == 0:
            return 'No trades on moderate up days to analyze.'

        LOW_WR = 50
        bull_low = up_row['bull_count'] >= 2 and up_row['bull_win_rate'] < LOW_WR
        bear_low = up_row['bear_count'] >= 2 and up_row['bear_win_rate'] < LOW_WR

        if bull_low and bear_low:
            return ('Both directions struggle on moderate up days. '
                    'This regime may have characteristics that undermine the pulse bar signal.')
        if bear_low:
            return ('Bearish setups (call spreads) on moderate up days are the primary source of losses. '
                    'Consider filtering bearish signals when morning bias is moderately bullish.')
        if bull_low:
            return ('Bullish setups (put spreads) on moderate up days are underperforming. '
                    'Investigate if these are late-morning reversals.')
        return 'No significant underperformance detected on moderate up days.'

    def analyze(self, trades):
        """Run full regime analysis. Returns combined result dict."""
        return {
            'vix_regimes': self.analyze_by_vix_regime(trades),
            'direction_regimes': self.analyze_by_direction_regime(trades),
            'transitions': self.analyze_regime_transitions(trades),
            'neutrality': self.market_neutrality_score(trades),
            'direction_drilldown': self.analyze_direction_drilldown(trades),
            'trade_count': len(trades),
        }
