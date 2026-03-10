"""
Professional strategy tear sheet PDF generator.

Produces hedge-fund-style performance reports (monthly, weekly, or custom
date range) using reportlab for PDF layout and charts (no matplotlib dependency).
"""

import io
import logging
from datetime import date, datetime, timedelta
from collections import defaultdict

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, white, black
from reportlab.platypus import SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.platypus import Paragraph, KeepTogether
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.graphics.shapes import Drawing, Rect, Line, String, PolyLine
from reportlab.graphics import renderPDF

logger = logging.getLogger(__name__)

# Colors matching dashboard theme
BG_DARK = HexColor('#1a1a2e')
BG_PANEL = HexColor('#111827')
BG_ROW_ALT = HexColor('#1e2a3a')
TEXT_COLOR = HexColor('#d1d5db')
TEXT_DIM = HexColor('#6b7280')
GREEN = HexColor('#10b981')
RED = HexColor('#ef4444')
AMBER = HexColor('#f59e0b')
BLUE = HexColor('#3b82f6')
CYAN = HexColor('#06b6d4')

VERSION = "The Daily Melt v1.0.0"

DISCLAIMER = (
    "Simulated Results \u2014 Uses synthetic pricing, instant fills at "
    "theoretical mid. Results reflect backtested performance, not live execution."
)

# Exit reason display labels (mirrors dashboard/app.py _EXIT_REASON_LABELS)
_EXIT_REASON_LABELS = {
    'profit_target': 'Profit Target (80%)',
    '1pm_close_non_trending': '1PM Close (Non-Trending)',
    '1pm_hold_trending': '1PM Hold \u2192 Expiration',
    'expiration': 'Held to Expiration',
    'expiration_1pm': 'Expiration (1PM)',
    'tnt_target_hit': 'TNT Target Hit',
    'tnt_stop_hit': 'TNT Stop Loss',
    'tnt_max_hold': 'TNT Max Hold (7 days)',
    'tnt_weekend_hold_prevention': 'TNT Weekend Hold Prevention',
    'expired_at_startup': 'Expired (Startup Recovery)',
    'direction_filter_up_day': 'Direction Filter (Up Day)',
    'unknown': 'Unknown',
}


_STRATEGY_ABBREV = {
    'daily_income': 'DI',
    'tag_n_turn': 'TNT',
    'orb': 'ORB',
    'bnb': 'B&B',
}


def _strategy_label(raw_key):
    """Convert raw strategy key to abbreviated display name."""
    return _STRATEGY_ABBREV.get(raw_key, raw_key)


def _exit_reason_label(raw_key):
    """Convert raw exit reason key to human-readable label."""
    if raw_key in _EXIT_REASON_LABELS:
        return _EXIT_REASON_LABELS[raw_key]
    # Fallback: title-case with underscores replaced by spaces
    return raw_key.replace('_', ' ').title()


def _pnl_color(value):
    if value is None:
        return TEXT_DIM
    return GREEN if value >= 0 else RED


def _fmt_dollar(value):
    if value is None:
        return 'N/A'
    sign = '+' if value >= 0 else '-'
    return f'{sign}${abs(value):,.2f}'


def _fmt_dollar_abbrev(value):
    """Abbreviated dollar format for summary cards: $30.4K, $1.2M, etc."""
    if value is None:
        return 'N/A'
    abs_val = abs(value)
    sign = '+' if value >= 0 else '-'
    if abs_val >= 1_000_000_000:
        return f'{sign}${abs_val / 1_000_000_000:.1f}B'
    elif abs_val >= 1_000_000:
        return f'{sign}${abs_val / 1_000_000:.1f}M'
    elif abs_val >= 10_000:
        return f'{sign}${abs_val / 1_000:.1f}K'
    else:
        return f'{sign}${abs_val:,.2f}'


def _fmt_pct(value):
    if value is None:
        return 'N/A'
    return f'{value:.1f}%'


def _make_style(name, font='Helvetica', size=9, color=TEXT_COLOR,
                alignment=TA_LEFT, space_after=4):
    return ParagraphStyle(
        name, fontName=font, fontSize=size, textColor=color,
        alignment=alignment, spaceAfter=space_after, leading=size + 3,
    )


class TearSheetGenerator:
    """Generate professional strategy tear sheet PDFs."""

    def __init__(self):
        self.styles = {
            'title': _make_style('title', 'Helvetica-Bold', 16, TEXT_COLOR, TA_LEFT, 2),
            'subtitle': _make_style('subtitle', 'Helvetica', 10, TEXT_DIM, TA_LEFT, 8),
            'section': _make_style('section', 'Helvetica-Bold', 11, AMBER, TA_LEFT, 6),
            'body': _make_style('body', 'Helvetica', 9, TEXT_COLOR, TA_LEFT, 3),
            'value': _make_style('value', 'Courier', 10, TEXT_COLOR, TA_LEFT, 2),
            'footer': _make_style('footer', 'Helvetica', 7, TEXT_DIM, TA_CENTER, 0),
            'card_label': _make_style('card_label', 'Helvetica', 7, TEXT_DIM, TA_CENTER, 1),
            'card_value': _make_style('card_value', 'Courier-Bold', 12, TEXT_COLOR, TA_CENTER, 0),
        }

    def generate_monthly(self, year, month, trades, analytics, risk_metrics,
                         attribution, execution, data_source='live',
                         end_year=None, end_month=None, report=None):
        """Generate monthly tear sheet PDF. Returns PDF bytes."""
        start = date(year, month, 1)
        if end_year is not None and end_month is not None:
            if end_month == 12:
                end = date(end_year + 1, 1, 1) - timedelta(days=1)
            else:
                end = date(end_year, end_month + 1, 1) - timedelta(days=1)
            start_label = start.strftime('%B %Y')
            end_label = date(end_year, end_month, 1).strftime('%B %Y')
            if start_label == end_label:
                period_label = start_label
            else:
                period_label = f'{start_label} \u2013 {end_label}'
        else:
            period_label = date(year, month, 1).strftime('%B %Y')
            if month == 12:
                end = date(year + 1, 1, 1) - timedelta(days=1)
            else:
                end = date(year, month + 1, 1) - timedelta(days=1)

        period_trades = self._filter_trades(trades, start, end)
        return self._build_pdf(period_label, period_trades, start, end,
                               data_source=data_source, report=report)

    def generate_weekly(self, start_date, end_date, trades, analytics,
                        risk_metrics, attribution, execution,
                        data_source='live', report=None):
        """Generate weekly tear sheet PDF. Returns PDF bytes."""
        period_label = f'Week of {start_date.strftime("%b %d")}\u2013{end_date.strftime("%b %d, %Y")}'
        period_trades = self._filter_trades(trades, start_date, end_date)
        return self._build_pdf(period_label, period_trades, start_date, end_date,
                               data_source=data_source, report=report)

    def generate_custom(self, start_date, end_date, trades, analytics,
                        risk_metrics, attribution, execution,
                        data_source='live', report=None):
        """Generate tear sheet for custom date range. Returns PDF bytes."""
        period_label = f'{start_date.strftime("%b %d, %Y")} \u2013 {end_date.strftime("%b %d, %Y")}'
        period_trades = self._filter_trades(trades, start_date, end_date)
        return self._build_pdf(period_label, period_trades, start_date, end_date,
                               data_source=data_source, report=report)

    def _filter_trades(self, trades, start, end):
        """Filter trades to those within the date range."""
        filtered = []
        for t in trades:
            exit_t = t.get('exit_time') or t.get('entry_time') or ''
            if isinstance(exit_t, str) and len(exit_t) >= 10:
                try:
                    trade_date = date.fromisoformat(exit_t[:10])
                    if start <= trade_date <= end:
                        filtered.append(t)
                except (ValueError, TypeError):
                    continue
        return filtered

    def _compute_analytics(self, trades, report=None, starting_capital=50000):
        """Compute analytics from filtered trades. Uses report metrics when available."""
        valid = [t for t in trades if t.get('pnl') is not None]
        if not valid:
            return {
                'core': {}, 'trade_metrics': {}, 'equity_curve': [],
                'monthly_returns': [],
            }

        sorted_trades = sorted(valid, key=lambda t: t.get('exit_time') or t.get('entry_time') or '')
        pnls = [t.get('pnl', 0) for t in sorted_trades]

        # Build daily P&L
        daily_pnl = defaultdict(float)
        for t in sorted_trades:
            exit_t = t.get('exit_time') or t.get('entry_time') or ''
            if exit_t:
                day = exit_t[:10]
                daily_pnl[day] += t.get('pnl', 0)

        # Equity curve
        equity = starting_capital
        equity_curve = []
        for day in sorted(daily_pnl.keys()):
            dpnl = daily_pnl[day]
            equity += dpnl
            equity_curve.append({'date': day, 'equity': round(equity, 2), 'daily_pnl': round(dpnl, 2)})

        total_pnl = sum(pnls)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        win_rate = (len(wins) / len(pnls) * 100) if pnls else 0
        gross_profit = sum(wins)
        gross_loss = abs(sum(losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf') if gross_profit > 0 else 0

        # Monthly returns with rolling equity
        monthly_pnl = defaultdict(float)
        for t in sorted_trades:
            exit_t = t.get('exit_time') or t.get('entry_time') or ''
            if exit_t and len(exit_t) >= 7:
                ym = exit_t[:7]
                monthly_pnl[ym] += t.get('pnl', 0)
        monthly_returns = []
        rolling_equity = starting_capital
        for ym in sorted(monthly_pnl.keys()):
            parts = ym.split('-')
            pnl = monthly_pnl[ym]
            ret_pct = (pnl / rolling_equity * 100) if rolling_equity > 0 else 0
            monthly_returns.append({
                'label': ym,
                'year': int(parts[0]),
                'month': int(parts[1]),
                'pnl': round(pnl, 2),
                'return_pct': round(ret_pct, 2),
            })
            rolling_equity += pnl

        # Use pre-computed report metrics if available (backtest data),
        # fall back to local calculation for live trades
        report_core = (report or {}).get('core', {})
        report_tm = (report or {}).get('trade_metrics', {})

        if report_core.get('sharpe_ratio') is not None:
            sharpe = report_core['sharpe_ratio']
            sortino = report_core.get('sortino_ratio', 0)
            max_dd = report_core.get('max_drawdown_pct', 0)
            calmar = report_core.get('calmar_ratio', 0)
        else:
            # Fallback: compute from equity curve
            peak = starting_capital
            max_dd = 0
            for ec in equity_curve:
                if ec['equity'] > peak:
                    peak = ec['equity']
                dd = (peak - ec['equity']) / peak * 100 if peak > 0 else 0
                if dd > max_dd:
                    max_dd = dd

            daily_returns = []
            for ec in equity_curve:
                prev = ec['equity'] - ec['daily_pnl']
                daily_returns.append(ec['daily_pnl'] / prev if prev > 0 else 0)

            sharpe = sortino = calmar = 0
            if daily_returns and len(daily_returns) > 1:
                import statistics
                mean_r = statistics.mean(daily_returns)
                std_r = statistics.stdev(daily_returns) if len(daily_returns) > 1 else 1
                sharpe = (mean_r / std_r * (252 ** 0.5)) if std_r > 0 else 0
                downside = [r for r in daily_returns if r < 0]
                down_std = statistics.stdev(downside) if len(downside) > 1 else 1
                sortino = (mean_r / down_std * (252 ** 0.5)) if down_std > 0 else 0

        if report_tm.get('win_rate') is not None:
            win_rate = report_tm['win_rate']
        if report_tm.get('profit_factor') is not None:
            profit_factor = report_tm['profit_factor']

        core = {
            'total_return_dollar': round(total_pnl, 2),
            'sharpe_ratio': round(sharpe, 3),
            'sortino_ratio': round(sortino, 3),
            'max_drawdown_pct': round(max_dd, 2),
            'calmar_ratio': round(calmar, 3) if calmar else 0,
        }
        trade_metrics = {
            'win_rate': round(win_rate, 1),
            'profit_factor': round(profit_factor, 2),
        }

        return {
            'core': core,
            'trade_metrics': trade_metrics,
            'equity_curve': equity_curve,
            'monthly_returns': monthly_returns,
        }

    def _compute_risk_metrics(self, equity_curve, report=None):
        """Compute risk metrics. Uses report equity curve for streaks when available."""
        if len(equity_curve) < 20:
            return {'sufficient_data': False}

        daily_pnls = [e['daily_pnl'] for e in equity_curve]
        sorted_pnls = sorted(daily_pnls)

        def percentile(data, pct):
            k = (len(data) - 1) * pct / 100
            f = int(k)
            c = f + 1
            if c >= len(data):
                return data[f]
            return data[f] + (k - f) * (data[c] - data[f])

        var_95 = percentile(sorted_pnls, 5)
        var_99 = percentile(sorted_pnls, 1)
        cvar_95 = sum(p for p in sorted_pnls if p <= var_95) / max(1, sum(1 for p in sorted_pnls if p <= var_95))
        cvar_99 = sum(p for p in sorted_pnls if p <= var_99) / max(1, sum(1 for p in sorted_pnls if p <= var_99))

        # Tail ratio
        upper = [p for p in sorted_pnls if p > 0]
        lower = [p for p in sorted_pnls if p < 0]
        upper_95 = percentile(upper, 95) if len(upper) > 1 else 0
        lower_5 = abs(percentile(lower, 5)) if len(lower) > 1 else 1
        tail_ratio = upper_95 / lower_5 if lower_5 > 0 else 0

        report_core = (report or {}).get('core', {})

        # Compute daily streaks from equity curve (matches dashboard algorithm).
        # For backtest reports, prefer the report's full equity curve over the
        # rebuilt one so values match the backtest tab exactly.
        report_ec = (report or {}).get('equity_curve', [])
        streak_pnls = ([e.get('daily_pnl', e.get('pnl', 0)) for e in report_ec]
                       if len(report_ec) >= 20 else daily_pnls)
        longest_win, longest_loss = self._compute_daily_streaks(streak_pnls)

        calmar = report_core.get('calmar_ratio', 0)
        if not calmar:
            peak = equity_curve[0]['equity'] - equity_curve[0]['daily_pnl']
            max_dd_dollar = 0
            for ec in equity_curve:
                if ec['equity'] > peak:
                    peak = ec['equity']
                dd = peak - ec['equity']
                if dd > max_dd_dollar:
                    max_dd_dollar = dd
            total_ret = sum(daily_pnls)
            calmar = abs(total_ret / max_dd_dollar) if max_dd_dollar > 0 else 0

        return {
            'sufficient_data': True,
            'calmar_ratio': round(calmar, 2),
            'var_95': round(var_95, 2),
            'var_99': round(var_99, 2),
            'cvar_95': round(cvar_95, 2),
            'cvar_99': round(cvar_99, 2),
            'tail_ratio': round(tail_ratio, 2),
            'longest_win_streak': longest_win,
            'longest_loss_streak': longest_loss,
        }

    @staticmethod
    def _compute_daily_streaks(daily_pnls):
        """Compute longest consecutive winning/losing day streaks.

        Matches dashboard _analytics_risk_metrics algorithm: days with $0 P&L
        do not break or extend an active streak.

        Returns (longest_win, longest_loss).
        """
        win_streaks = []
        loss_streaks = []
        current_streak = 0
        current_type = None  # 'win' or 'loss'

        for pnl in daily_pnls:
            if pnl > 0:
                if current_type == 'win':
                    current_streak += 1
                else:
                    if current_type == 'loss' and current_streak > 0:
                        loss_streaks.append(current_streak)
                    current_streak = 1
                    current_type = 'win'
            elif pnl < 0:
                if current_type == 'loss':
                    current_streak += 1
                else:
                    if current_type == 'win' and current_streak > 0:
                        win_streaks.append(current_streak)
                    current_streak = 1
                    current_type = 'loss'
            # pnl == 0 doesn't break or extend streaks

        # Flush final streak
        if current_type == 'win' and current_streak > 0:
            win_streaks.append(current_streak)
        elif current_type == 'loss' and current_streak > 0:
            loss_streaks.append(current_streak)

        longest_win = max(win_streaks) if win_streaks else 0
        longest_loss = max(loss_streaks) if loss_streaks else 0
        return longest_win, longest_loss

    def _compute_attribution(self, trades):
        """Compute P&L attribution from filtered trades."""
        from src.analytics.greeks import GreeksCalculator
        from src.analytics.pnl_attribution import PnLAttributor
        attributor = PnLAttributor(GreeksCalculator())
        return attributor.attribute_batch(trades)

    def _compute_execution(self, trades):
        """Compute execution quality from filtered trades."""
        if not trades:
            return {}

        # Slippage summary (matches dashboard/app.py _analytics_execution logic)
        slippage_trades = [t for t in trades
                          if t.get('slippage') is not None and t.get('slippage') != 0]
        if slippage_trades:
            avg_slippage = sum(t['slippage'] for t in slippage_trades) / len(slippage_trades)
            total_slippage_cost = sum(
                t['slippage'] * t.get('quantity', t.get('contracts', 1)) * 100
                for t in slippage_trades
            )
            slippage_summary = {
                'avg_slippage': round(avg_slippage, 4),
                'total_slippage_cost': round(total_slippage_cost, 2),
            }
        else:
            slippage_summary = {}

        # Exit reasons with human-readable labels
        reason_map = defaultdict(lambda: {'count': 0, 'wins': 0, 'pnls': []})
        for t in trades:
            reason = t.get('exit_reason', 'unknown')
            reason_map[reason]['count'] += 1
            pnl = t.get('pnl', 0)
            if pnl > 0:
                reason_map[reason]['wins'] += 1
            reason_map[reason]['pnls'].append(pnl)

        exit_reasons = []
        for reason, data in sorted(reason_map.items(), key=lambda x: x[1]['count'], reverse=True):
            count = data['count']
            wr = (data['wins'] / count * 100) if count > 0 else 0
            avg = sum(data['pnls']) / count if count > 0 else 0
            exit_reasons.append({
                'label': _exit_reason_label(reason),
                'count': count,
                'win_rate': round(wr, 1),
                'avg_pnl': round(avg, 2),
            })

        return {
            'slippage_summary': slippage_summary,
            'slippage_by_vix': [],
            'exit_reasons': exit_reasons[:5],
        }

    def _build_pdf(self, period_label, trades, start_date, end_date,
                   data_source='live', report=None):
        """Build the complete tear sheet PDF from filtered trades."""
        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=letter,
            leftMargin=0.5 * inch, rightMargin=0.5 * inch,
            topMargin=0.5 * inch, bottomMargin=0.5 * inch,
        )

        # Determine starting capital from report or default
        starting_capital = 50000
        if report:
            # Backtest report stores initial_capital in assumptions or top-level
            assumptions = report.get('assumptions', {})
            starting_capital = (assumptions.get('initial_capital')
                                or report.get('initial_capital')
                                or 50000)

        # Compute analytics from filtered trades, using report for pre-computed metrics
        analytics = self._compute_analytics(trades, report=report,
                                            starting_capital=starting_capital)
        risk_metrics = self._compute_risk_metrics(
            analytics.get('equity_curve', []), report=report)
        attribution = self._compute_attribution(trades)
        execution = self._compute_execution(trades)

        elements = []

        # Data source label
        if data_source and data_source != 'live':
            source_label = 'Data: Backtest'
        else:
            source_label = 'Data: Live Trades'

        # Header
        elements.append(Paragraph('The Daily Melt \u2014 Performance Report',
                                  self.styles['title']))
        elements.append(Paragraph(
            f'Period: {period_label} &nbsp;|&nbsp; '
            f'{source_label} &nbsp;|&nbsp; '
            f'Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}',
            self.styles['subtitle']))
        elements.append(Spacer(1, 8))

        # Section 1: Performance Summary
        elements.append(Paragraph('PERFORMANCE SUMMARY', self.styles['section']))
        elements.append(self._build_summary_cards(analytics, trades))
        elements.append(Spacer(1, 10))

        # Section 2: Equity Curve
        elements.append(Paragraph('EQUITY CURVE', self.styles['section']))
        try:
            chart_img = self._build_equity_chart(analytics)
        except Exception as e:
            logger.warning(f"Equity chart generation failed: {e}")
            chart_img = None
        if chart_img:
            elements.append(chart_img)
        else:
            elements.append(Paragraph('Insufficient data for equity curve.',
                                      self.styles['body']))
        elements.append(Spacer(1, 10))

        # Section 3: Monthly Returns (if multi-day data available)
        monthly_table = self._build_monthly_returns(analytics, start_date, end_date)
        if monthly_table:
            elements.append(Paragraph('MONTHLY RETURNS', self.styles['section']))
            elements.append(monthly_table)
            elements.append(Spacer(1, 10))

        # Section 4: Risk Metrics
        elements.append(Paragraph('RISK METRICS', self.styles['section']))
        elements.append(self._build_risk_table(risk_metrics))
        elements.append(Spacer(1, 10))

        # Section 5: Top Exit Reasons
        exit_reasons_table = self._build_exit_reasons(execution)
        if exit_reasons_table:
            elements.append(Paragraph('TOP EXIT REASONS', self.styles['section']))
            elements.append(exit_reasons_table)
            elements.append(Spacer(1, 10))

        # Section 6: P&L Attribution
        elements.append(Paragraph('P&amp;L ATTRIBUTION', self.styles['section']))
        elements.append(self._build_attribution_table(attribution))
        elements.append(Spacer(1, 10))

        # Section 7: Execution Quality
        elements.append(Paragraph('EXECUTION QUALITY', self.styles['section']))
        elements.append(self._build_execution_summary(execution))
        elements.append(Spacer(1, 10))

        # Section 8: Trade Log
        elements.append(Paragraph('TRADE LOG', self.styles['section']))
        elements.append(self._build_trade_log(trades))
        elements.append(Spacer(1, 12))

        # Footer
        elements.append(Paragraph(DISCLAIMER, self.styles['footer']))
        elements.append(Spacer(1, 4))
        elements.append(Paragraph(VERSION, self.styles['footer']))

        doc.build(elements, onFirstPage=self._page_bg, onLaterPages=self._page_bg)
        buf.seek(0)
        return buf.getvalue()

    def _page_bg(self, canvas, doc):
        """Dark background for each page."""
        canvas.saveState()
        canvas.setFillColor(BG_DARK)
        canvas.rect(0, 0, letter[0], letter[1], fill=1, stroke=0)
        canvas.restoreState()

    def _build_summary_cards(self, analytics, trades):
        """Performance summary as a card row."""
        core = analytics.get('core', {}) if analytics else {}
        tm = analytics.get('trade_metrics', {}) if analytics else {}

        sortino = core.get('sortino_ratio', 0) or 0

        cards = [
            ('Total P&amp;L', _fmt_dollar_abbrev(core.get('total_return_dollar')),
             _pnl_color(core.get('total_return_dollar'))),
            ('Win Rate', _fmt_pct(tm.get('win_rate')),
             GREEN if (tm.get('win_rate') or 0) >= 50 else AMBER),
            ('Profit Factor', f'{tm.get("profit_factor", 0):.2f}',
             GREEN if (tm.get('profit_factor') or 0) >= 1 else RED),
            ('Sharpe', f'{core.get("sharpe_ratio", 0):.3f}',
             GREEN if (core.get('sharpe_ratio') or 0) >= 1 else TEXT_COLOR),
            ('Sortino', f'{sortino:.3f}',
             GREEN if sortino >= 1.5 else TEXT_COLOR),
            ('Max DD', _fmt_pct(core.get('max_drawdown_pct')), RED),
            ('Trades', str(len(trades)), TEXT_COLOR),
        ]

        data = [
            [Paragraph(c[0], self.styles['card_label']) for c in cards],
            [Paragraph(c[1], ParagraphStyle(
                f'cv_{i}', fontName='Courier-Bold', fontSize=11,
                textColor=c[2], alignment=TA_CENTER, spaceAfter=0, leading=14,
            )) for i, c in enumerate(cards)],
        ]

        col_width = (7.5 * inch) / len(cards)
        t = Table(data, colWidths=[col_width] * len(cards))
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), BG_PANEL),
            ('BOX', (0, 0), (-1, -1), 0.5, HexColor('#1e2a3a')),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, HexColor('#1e2a3a')),
            ('TOPPADDING', (0, 0), (-1, 0), 6),
            ('BOTTOMPADDING', (0, -1), (-1, -1), 6),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        return t

    def _build_equity_chart(self, analytics):
        """Generate equity curve chart as a reportlab Drawing (no matplotlib)."""
        ec = analytics.get('equity_curve', []) if analytics else []
        if len(ec) < 2:
            return None

        dates = [e['date'] for e in ec]
        equities = [e['equity'] for e in ec]

        # Drawing dimensions
        chart_w = 7.5 * inch
        chart_h = 2.2 * inch
        margin_left = 55
        margin_right = 10
        margin_top = 10
        margin_bottom = 25
        plot_w = chart_w - margin_left - margin_right
        plot_h = chart_h - margin_top - margin_bottom

        d = Drawing(chart_w, chart_h)

        # Background
        d.add(Rect(0, 0, chart_w, chart_h, fillColor=HexColor('#111827'),
                    strokeColor=None))

        # Data range
        min_eq = min(equities)
        max_eq = max(equities)
        eq_range = max_eq - min_eq or 1
        # Add 2% padding
        min_eq -= eq_range * 0.02
        eq_range = max_eq - min_eq + eq_range * 0.02

        n = len(equities)
        line_color = HexColor('#10b981') if equities[-1] >= equities[0] else HexColor('#ef4444')

        # Map data to pixel coords
        def x_px(i):
            return margin_left + (i / max(n - 1, 1)) * plot_w

        def y_px(val):
            return margin_bottom + ((val - min_eq) / eq_range) * plot_h

        # Grid lines (horizontal, ~4 lines)
        grid_color = HexColor('#1e2a3a')
        label_color = HexColor('#6b7280')
        n_grid = 4
        for gi in range(n_grid + 1):
            gy = margin_bottom + (gi / n_grid) * plot_h
            d.add(Line(margin_left, gy, chart_w - margin_right, gy,
                       strokeColor=grid_color, strokeWidth=0.5))
            val = min_eq + (gi / n_grid) * eq_range
            d.add(String(margin_left - 5, gy - 3, _fmt_dollar_abbrev(val).lstrip('+'),
                         fontSize=6, fillColor=label_color, textAnchor='end',
                         fontName='Courier'))

        # Equity line
        points = []
        for i in range(n):
            points.extend([x_px(i), y_px(equities[i])])
        d.add(PolyLine(points, strokeColor=line_color, strokeWidth=1.5))

        # X-axis labels (~5 evenly spaced)
        step = max(1, n // 5)
        for i in range(0, n, step):
            lx = x_px(i)
            d.add(String(lx, 5, dates[i], fontSize=6, fillColor=label_color,
                         textAnchor='middle', fontName='Courier'))

        # Axes
        d.add(Line(margin_left, margin_bottom, chart_w - margin_right,
                   margin_bottom, strokeColor=grid_color, strokeWidth=0.5))
        d.add(Line(margin_left, margin_bottom, margin_left,
                   chart_h - margin_top, strokeColor=grid_color, strokeWidth=0.5))

        return d

    def _build_monthly_returns(self, analytics, start_date=None, end_date=None):
        """Monthly returns grid table. Returns None if insufficient data."""
        monthly = analytics.get('monthly_returns') if analytics else None
        if not monthly:
            return None

        if not isinstance(monthly, list) or len(monthly) < 2:
            return None

        # Filter to selected period if provided
        if start_date and end_date:
            start_ym = start_date.year * 100 + start_date.month
            end_ym = end_date.year * 100 + end_date.month
            monthly = [m for m in monthly if isinstance(m, dict) and
                       m.get('year', 0) * 100 + m.get('month', 0) >= start_ym and
                       m.get('year', 0) * 100 + m.get('month', 0) <= end_ym]
            if len(monthly) < 1:
                return None

        header_style = _make_style('mr_h', 'Helvetica-Bold', 8, TEXT_DIM, TA_LEFT, 0)
        header = ['Month', 'P&amp;L', 'Return %']
        data = [[Paragraph(h, header_style) for h in header]]

        for m in monthly:
            if isinstance(m, dict):
                label = m.get('label', f'{m.get("year", "")}-{m.get("month", "")}')
                pnl = m.get('pnl', 0)
                ret = m.get('return_pct', 0)
            else:
                continue
            pnl_color = _pnl_color(pnl)
            data.append([
                Paragraph(str(label), _make_style(f'ml_{len(data)}', 'Helvetica', 8, TEXT_COLOR, TA_LEFT, 0)),
                Paragraph(_fmt_dollar(pnl), _make_style(f'mp_{len(data)}', 'Courier', 8, pnl_color, TA_LEFT, 0)),
                Paragraph(_fmt_pct(ret), _make_style(f'mr_{len(data)}', 'Courier', 8, pnl_color, TA_LEFT, 0)),
            ])

        if len(data) <= 1:
            return None

        t = Table(data, colWidths=[2.5 * inch, 2.5 * inch, 2.5 * inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), HexColor('#1e2a3a')),
            ('BACKGROUND', (0, 1), (-1, -1), BG_PANEL),
            ('BOX', (0, 0), (-1, -1), 0.5, HexColor('#1e2a3a')),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, HexColor('#1e2a3a')),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ]))
        return t

    def _build_risk_table(self, risk_metrics):
        """Risk metrics as a compact table."""
        rm = risk_metrics or {}
        if not rm.get('sufficient_data'):
            return Paragraph('Insufficient data (need 20+ trading days).',
                             self.styles['body'])

        rows = [
            ['Calmar Ratio', f'{rm.get("calmar_ratio", 0):.2f}',
             'VaR 95%', _fmt_dollar_abbrev(rm.get('var_95'))],
            ['VaR 99%', _fmt_dollar_abbrev(rm.get('var_99')),
             'CVaR 95%', _fmt_dollar_abbrev(rm.get('cvar_95'))],
            ['CVaR 99%', _fmt_dollar_abbrev(rm.get('cvar_99')),
             'Tail Ratio', f'{rm.get("tail_ratio", 0):.2f}' if rm.get('tail_ratio') else 'N/A'],
            ['Best Streak', f'{rm.get("longest_win_streak", 0)} days',
             'Worst Streak', f'{rm.get("longest_loss_streak", 0)} days'],
        ]

        data = []
        for row in rows:
            data.append([
                Paragraph(row[0], self.styles['body']),
                Paragraph(row[1], self.styles['value']),
                Paragraph(row[2], self.styles['body']),
                Paragraph(row[3], self.styles['value']),
            ])

        t = Table(data, colWidths=[1.5 * inch, 1.5 * inch, 1.5 * inch, 1.5 * inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), BG_PANEL),
            ('BOX', (0, 0), (-1, -1), 0.5, HexColor('#1e2a3a')),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, HexColor('#1e2a3a')),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ]))
        return t

    def _build_exit_reasons(self, execution):
        """Top 5 exit reasons with trade counts and win rates."""
        ex = execution or {}
        reasons = ex.get('exit_reasons', [])
        if not reasons:
            return None

        header_style = _make_style('er_h', 'Helvetica-Bold', 8, TEXT_DIM, TA_LEFT, 0)
        header = ['Exit Reason', 'Trades', 'Win Rate', 'Avg P&amp;L']
        data = [[Paragraph(h, header_style) for h in header]]

        for r in reasons[:5]:
            label = r.get('label', r.get('reason', ''))
            count = r.get('count', 0)
            wr = r.get('win_rate', 0)
            avg_pnl = r.get('avg_pnl', 0)
            wr_color = GREEN if wr >= 50 else RED
            data.append([
                Paragraph(str(label)[:30], _make_style(f'erl_{len(data)}', 'Helvetica', 8, TEXT_COLOR, TA_LEFT, 0)),
                Paragraph(str(count), _make_style(f'erc_{len(data)}', 'Courier', 8, TEXT_COLOR, TA_LEFT, 0)),
                Paragraph(_fmt_pct(wr), _make_style(f'erw_{len(data)}', 'Courier', 8, wr_color, TA_LEFT, 0)),
                Paragraph(_fmt_dollar(avg_pnl), _make_style(f'erp_{len(data)}', 'Courier', 8, _pnl_color(avg_pnl), TA_LEFT, 0)),
            ])

        if len(data) <= 1:
            return None

        t = Table(data, colWidths=[2.5 * inch, 1.0 * inch, 1.5 * inch, 2.5 * inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), HexColor('#1e2a3a')),
            ('BACKGROUND', (0, 1), (-1, -1), BG_PANEL),
            ('BOX', (0, 0), (-1, -1), 0.5, HexColor('#1e2a3a')),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, HexColor('#1e2a3a')),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ]))
        return t

    def _build_attribution_table(self, attribution):
        """P&L attribution breakdown."""
        attr = attribution or {}
        if attr.get('attributed_count', 0) == 0:
            return Paragraph('No attribution data available.',
                             self.styles['body'])

        header = ['Component', 'Total P&amp;L', 'Avg/Trade', '% of Total']
        rows = [
            ['Theta', _fmt_dollar(attr.get('total_theta')),
             _fmt_dollar(attr.get('avg_theta')),
             _fmt_pct(attr.get('theta_pct'))],
            ['Delta', _fmt_dollar(attr.get('total_delta')),
             _fmt_dollar(attr.get('avg_delta')),
             _fmt_pct(attr.get('delta_pct'))],
            ['Vega', _fmt_dollar(attr.get('total_vega')),
             _fmt_dollar(attr.get('avg_vega')),
             _fmt_pct(attr.get('vega_pct'))],
            ['Residual', _fmt_dollar(attr.get('total_residual')),
             _fmt_dollar(attr.get('avg_residual')),
             _fmt_pct(attr.get('residual_pct'))],
        ]

        header_style = _make_style('th', 'Helvetica-Bold', 8, TEXT_DIM, TA_LEFT, 0)
        data = [[Paragraph(h, header_style) for h in header]]
        for row in rows:
            data.append([Paragraph(c, self.styles['value']) for c in row])

        t = Table(data, colWidths=[1.5 * inch, 2.0 * inch, 2.0 * inch, 2.0 * inch])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), HexColor('#1e2a3a')),
            ('BACKGROUND', (0, 1), (-1, -1), BG_PANEL),
            ('BOX', (0, 0), (-1, -1), 0.5, HexColor('#1e2a3a')),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, HexColor('#1e2a3a')),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ]))
        return t

    def _build_execution_summary(self, execution):
        """Execution quality one-liner."""
        ex = execution or {}
        ss = ex.get('slippage_summary', {})
        if not ss:
            return Paragraph('No execution data available.',
                             self.styles['body'])

        avg_slip = ss.get('avg_slippage', 0)
        total_cost = ss.get('total_slippage_cost', 0)
        vix_regimes = ex.get('slippage_by_vix', [])

        parts = [f'Avg slippage: ${avg_slip:.4f}  |  Total cost: {_fmt_dollar_abbrev(total_cost)}']
        if vix_regimes:
            regime_parts = [f'{v["regime"]}: ${v["avg_slippage"]:.4f}'
                            for v in vix_regimes[:3]]
            parts.append('By VIX: ' + ', '.join(regime_parts))

        return Paragraph('  |  '.join(parts), self.styles['body'])

    def _build_trade_log(self, trades):
        """Table of all trades in the period."""
        if not trades:
            return Paragraph('No trades in this period.', self.styles['body'])

        header = ['Date', 'Strategy', 'Dir', 'Entry', 'Exit', 'Credit', 'P&amp;L', 'Reason']
        header_style = _make_style('tl_h', 'Helvetica-Bold', 7, TEXT_DIM, TA_LEFT, 0)

        data = [[Paragraph(h, header_style) for h in header]]

        for idx, t in enumerate(trades[:50]):  # Cap at 50 rows to fit
            exit_t = t.get('exit_time') or t.get('entry_time') or ''
            trade_date = exit_t[:10] if len(exit_t) >= 10 else ''
            pnl = t.get('pnl')
            pnl_style = _make_style(
                f'pnl_{idx}', 'Courier', 7, _pnl_color(pnl), TA_LEFT, 0)

            row = [
                Paragraph(trade_date, _make_style(f'd_{idx}', 'Courier', 7, TEXT_COLOR, TA_LEFT, 0)),
                Paragraph(_strategy_label(t.get('strategy_type', '')),
                          _make_style(f's_{idx}', 'Helvetica', 7, TEXT_COLOR, TA_LEFT, 0)),
                Paragraph(str(t.get('direction', ''))[:4],
                          _make_style(f'dir_{idx}', 'Helvetica', 7, TEXT_COLOR, TA_LEFT, 0)),
                Paragraph(f'${t.get("entry_price", 0):.2f}',
                          _make_style(f'en_{idx}', 'Courier', 7, TEXT_COLOR, TA_LEFT, 0)),
                Paragraph(f'${t.get("exit_price", 0):.2f}',
                          _make_style(f'ex_{idx}', 'Courier', 7, TEXT_COLOR, TA_LEFT, 0)),
                Paragraph(f'${t.get("credit_received", 0):.2f}',
                          _make_style(f'cr_{idx}', 'Courier', 7, TEXT_COLOR, TA_LEFT, 0)),
                Paragraph(_fmt_dollar(pnl) if pnl is not None else 'N/A', pnl_style),
                Paragraph(_exit_reason_label(t.get('exit_reason', ''))[:25],
                          _make_style(f'r_{idx}', 'Helvetica', 7, TEXT_DIM, TA_LEFT, 0)),
            ]
            data.append(row)

        col_widths = [0.8*inch, 0.5*inch, 0.5*inch, 0.7*inch, 0.7*inch,
                      0.7*inch, 1.0*inch, 1.5*inch]
        t = Table(data, colWidths=col_widths)

        style_cmds = [
            ('BACKGROUND', (0, 0), (-1, 0), HexColor('#1e2a3a')),
            ('BOX', (0, 0), (-1, -1), 0.5, HexColor('#1e2a3a')),
            ('INNERGRID', (0, 0), (-1, -1), 0.25, HexColor('#1e2a3a')),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ]
        # Alternating row backgrounds
        for i in range(1, len(data)):
            bg = BG_ROW_ALT if i % 2 == 0 else BG_PANEL
            style_cmds.append(('BACKGROUND', (0, i), (-1, i), bg))

        t.setStyle(TableStyle(style_cmds))
        return t
