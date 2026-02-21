"""
Professional strategy tear sheet PDF generator.

Produces hedge-fund-style performance reports (monthly, weekly, or custom
date range) using reportlab for PDF layout and matplotlib for charts.
"""

import io
import logging
from datetime import date, datetime, timedelta
from collections import defaultdict

from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor, white, black
from reportlab.platypus import SimpleDocTemplate, Spacer, Table, TableStyle, Image
from reportlab.platypus import Paragraph, KeepTogether
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

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


def _pnl_color(value):
    if value is None:
        return TEXT_DIM
    return GREEN if value >= 0 else RED


def _fmt_dollar(value):
    if value is None:
        return 'N/A'
    sign = '+' if value >= 0 else '-'
    return f'{sign}${abs(value):,.2f}'


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
                         end_year=None, end_month=None):
        """Generate monthly tear sheet PDF. Returns PDF bytes.

        Supports single month (year/month only) or multi-month range
        when end_year/end_month are provided.
        """
        start = date(year, month, 1)
        if end_year is not None and end_month is not None:
            # Multi-month range
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
            # Single month
            period_label = date(year, month, 1).strftime('%B %Y')
            if month == 12:
                end = date(year + 1, 1, 1) - timedelta(days=1)
            else:
                end = date(year, month + 1, 1) - timedelta(days=1)

        period_trades = self._filter_trades(trades, start, end)
        return self._build_pdf(period_label, period_trades, analytics,
                               risk_metrics, attribution, execution,
                               data_source=data_source)

    def generate_weekly(self, start_date, end_date, trades, analytics,
                        risk_metrics, attribution, execution,
                        data_source='live'):
        """Generate weekly tear sheet PDF. Returns PDF bytes."""
        period_label = f'Week of {start_date.strftime("%b %d")}\u2013{end_date.strftime("%b %d, %Y")}'
        period_trades = self._filter_trades(trades, start_date, end_date)
        return self._build_pdf(period_label, period_trades, analytics,
                               risk_metrics, attribution, execution,
                               data_source=data_source)

    def generate_custom(self, start_date, end_date, trades, analytics,
                        risk_metrics, attribution, execution,
                        data_source='live'):
        """Generate tear sheet for custom date range. Returns PDF bytes."""
        period_label = f'{start_date.strftime("%b %d, %Y")} \u2013 {end_date.strftime("%b %d, %Y")}'
        period_trades = self._filter_trades(trades, start_date, end_date)
        return self._build_pdf(period_label, period_trades, analytics,
                               risk_metrics, attribution, execution,
                               data_source=data_source)

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

    def _build_pdf(self, period_label, trades, analytics, risk_metrics,
                   attribution, execution, data_source='live'):
        """Build the complete tear sheet PDF."""
        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=letter,
            leftMargin=0.5 * inch, rightMargin=0.5 * inch,
            topMargin=0.5 * inch, bottomMargin=0.5 * inch,
        )

        elements = []

        # Data source label
        if data_source and data_source != 'live':
            source_label = f'Data: Backtest'
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
        monthly_table = self._build_monthly_returns(analytics)
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
        elements.append(Paragraph('P&L ATTRIBUTION', self.styles['section']))
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
            ('Total P&L', _fmt_dollar(core.get('total_return_dollar')),
             _pnl_color(core.get('total_return_dollar'))),
            ('Win Rate', _fmt_pct(tm.get('win_rate')),
             GREEN if (tm.get('win_rate') or 0) >= 50 else AMBER),
            ('Profit Factor', f'{tm.get("profit_factor", 0):.2f}',
             GREEN if (tm.get('profit_factor') or 0) >= 1 else RED),
            ('Sharpe', f'{core.get("sharpe_ratio", 0):.2f}',
             GREEN if (core.get('sharpe_ratio') or 0) >= 1 else TEXT_COLOR),
            ('Sortino', f'{sortino:.2f}',
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
        """Generate equity curve chart as a reportlab Image."""
        ec = analytics.get('equity_curve', []) if analytics else []
        if len(ec) < 2:
            return None

        dates = [e['date'] for e in ec]
        equities = [e['equity'] for e in ec]

        fig, ax = plt.subplots(figsize=(7.5, 2.2), dpi=100)
        fig.patch.set_facecolor('#1a1a2e')
        ax.set_facecolor('#111827')

        color = '#10b981' if equities[-1] >= equities[0] else '#ef4444'
        ax.plot(range(len(dates)), equities, color=color, linewidth=1.5)
        ax.fill_between(range(len(dates)), equities,
                        min(equities) * 0.99, alpha=0.15, color=color)

        # X labels (show ~5 evenly spaced dates)
        n = len(dates)
        step = max(1, n // 5)
        tick_positions = list(range(0, n, step))
        ax.set_xticks(tick_positions)
        ax.set_xticklabels([dates[i] for i in tick_positions],
                           fontsize=7, color='#6b7280', rotation=0)

        ax.tick_params(axis='y', labelsize=7, labelcolor='#6b7280')
        ax.yaxis.set_major_formatter(plt.FuncFormatter(
            lambda x, _: f'${x:,.0f}'))
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.spines['bottom'].set_color('#1e2a3a')
        ax.spines['left'].set_color('#1e2a3a')
        ax.tick_params(colors='#6b7280')
        ax.grid(axis='y', color='#1e2a3a', linewidth=0.5, alpha=0.5)

        buf = io.BytesIO()
        fig.savefig(buf, format='png', bbox_inches='tight',
                    facecolor=fig.get_facecolor(), edgecolor='none')
        plt.close(fig)
        buf.seek(0)

        return Image(buf, width=7.5 * inch, height=2.2 * inch)

    def _build_monthly_returns(self, analytics):
        """Monthly returns grid table. Returns None if insufficient data."""
        monthly = analytics.get('monthly_returns') if analytics else None
        if not monthly:
            return None

        # monthly_returns is a list of {year, month, pnl, return_pct}
        if not isinstance(monthly, list) or len(monthly) < 2:
            return None

        header_style = _make_style('mr_h', 'Helvetica-Bold', 8, TEXT_DIM, TA_LEFT, 0)
        header = ['Month', 'P&L', 'Return %']
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
             'VaR 95%', _fmt_dollar(rm.get('var_95'))],
            ['VaR 99%', _fmt_dollar(rm.get('var_99')),
             'CVaR 95%', _fmt_dollar(rm.get('cvar_95'))],
            ['CVaR 99%', _fmt_dollar(rm.get('cvar_99')),
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
        header = ['Exit Reason', 'Trades', 'Win Rate', 'Avg P&L']
        data = [[Paragraph(h, header_style) for h in header]]

        for r in reasons[:5]:
            label = r.get('label', r.get('reason', ''))
            count = r.get('count', 0)
            wr = r.get('win_rate', 0)
            avg_pnl = r.get('avg_pnl', 0)
            wr_color = GREEN if wr >= 50 else RED
            data.append([
                Paragraph(str(label)[:25], _make_style(f'erl_{len(data)}', 'Helvetica', 8, TEXT_COLOR, TA_LEFT, 0)),
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

        header = ['Component', 'Total P&L', 'Avg/Trade', '% of Total']
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

        parts = [f'Avg slippage: ${avg_slip:.4f}  |  Total cost: {_fmt_dollar(total_cost)}']
        if vix_regimes:
            regime_parts = [f'{v["regime"]}: ${v["avg_slippage"]:.4f}'
                            for v in vix_regimes[:3]]
            parts.append('By VIX: ' + ', '.join(regime_parts))

        return Paragraph('  |  '.join(parts), self.styles['body'])

    def _build_trade_log(self, trades):
        """Table of all trades in the period."""
        if not trades:
            return Paragraph('No trades in this period.', self.styles['body'])

        header = ['Date', 'Strategy', 'Dir', 'Entry', 'Exit', 'Credit', 'P&L', 'Reason']
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
                Paragraph(str(t.get('strategy_type', ''))[:12],
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
                Paragraph(str(t.get('exit_reason', ''))[:15],
                          _make_style(f'r_{idx}', 'Helvetica', 7, TEXT_DIM, TA_LEFT, 0)),
            ]
            data.append(row)

        col_widths = [0.8*inch, 0.9*inch, 0.5*inch, 0.7*inch, 0.7*inch,
                      0.7*inch, 1.0*inch, 1.0*inch]
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
