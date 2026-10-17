"""Charts for review.md (Graphite palette: dark grey, coral accent, green/red only for P&L)."""
from paths import RESULTS, CHARTS
import json, numpy as np, pandas as pd, matplotlib
matplotlib.use('Agg'); import matplotlib.pyplot as plt
import lab, engine
BG = '#16181D'; PANEL = '#1E2127'; FG = '#D6D9DF'; MUTED = '#8A909C'; ACC = '#F0605D'; GRN = '#3FB27F'; RED = '#E5534B'; GRID = '#2C3038'
plt.rcParams.update({'figure.facecolor': BG, 'axes.facecolor': PANEL, 'axes.edgecolor': GRID, 'axes.labelcolor': FG,
                     'xtick.color': MUTED, 'ytick.color': MUTED, 'text.color': FG, 'grid.color': GRID, 'font.size': 10,
                     'axes.titleweight': 'bold', 'axes.titlesize': 12, 'font.family': 'DejaVu Sans'})
OUT = str(CHARTS)
def eq_chart(series, fname, title, ylabel='Cumulative P&L, 1 contract ($)'):
    fig, ax = plt.subplots(figsize=(10, 4.6))
    cols = [ACC, '#9AA4B2', '#5B8DEF', '#E0B252', '#B784F0']
    for (lab_, tr), c in zip(series, cols):
        if not tr: continue
        df = pd.DataFrame(tr).sort_values('date')
        ax.plot(pd.to_datetime(df.date), df.pnl.cumsum(), color=c, lw=1.6, label=lab_)
    ax.axvline(pd.Timestamp('2023-01-01'), color=MUTED, ls='--', lw=1); ax.text(pd.Timestamp('2023-01-15'), ax.get_ylim()[1] * 0.92, 'out-of-sample →', color=MUTED, fontsize=9)
    ax.axhline(0, color=MUTED, lw=0.8); ax.grid(True, lw=0.5); ax.set_title(title, loc='left'); ax.set_ylabel(ylabel)
    ax.legend(frameon=False, fontsize=9, loc='upper left'); fig.tight_layout(); fig.savefig(f'{OUT}/{fname}', dpi=150); plt.close(fig)
if __name__ == '__main__':
    import sys
    cands = json.load(open(str(RESULTS / 'candidates.json')))
    base = engine.run(lab.days(), _k=lab.K_BASE)
    eq_chart([('Live rules (honest backtest)', base)], '01_baseline_equity.png', 'Daily Melt live rules: honest backtest, 2019–2026')
    ser = [('Live rules', base)] + [(n, engine.run(lab.days(), _k=lab.K_BASE, **p)) for n, p in cands.items()]
    eq_chart(ser, '02_candidates_equity.png', 'Candidates vs live rules (parameters chosen on 2019–2022 only)')
    # win rate needed vs achieved
    df = pd.DataFrame(base); w = df[df.pnl > 0].pnl.mean(); l = -df[df.pnl <= 0].pnl.mean()
    fig, ax = plt.subplots(figsize=(7, 3.6))
    vals = [100 * (df.pnl > 0).mean(), 100 * l / (w + l)]
    ax.barh(['Win rate achieved', 'Win rate needed\nto break even'], vals, color=[MUTED, ACC])
    for i, v in enumerate(vals): ax.text(v + 0.5, i, f'{v:.1f}%', va='center')
    ax.set_xlim(0, 100); ax.set_title('Why it loses: avg loss ${:.0f} vs avg win ${:.0f}'.format(l, w), loc='left'); fig.tight_layout(); fig.savefig(f'{OUT}/03_breakeven.png', dpi=150); plt.close(fig)
    # P&L by gap bucket and by side
    df['gap_b'] = pd.cut(df.gap.abs(), [0, 0.25, 0.5, 1.0, 10], labels=['<0.25%', '0.25–0.5%', '0.5–1%', '>1%'])
    fig, axs = plt.subplots(1, 2, figsize=(10, 3.6))
    g = df.groupby('gap_b', observed=True).pnl.mean(); axs[0].bar(g.index.astype(str), g.values, color=[GRN if v > 0 else RED for v in g.values]); axs[0].set_title('Avg P&L per trade by opening gap', loc='left'); axs[0].axhline(0, color=MUTED, lw=.8)
    g = df.groupby('side').pnl.mean(); axs[1].bar(['Bull put (bullish signal)' if s == 'put' else 'Bear call (bearish signal)' for s in g.index], g.values, color=[GRN if v > 0 else RED for v in g.values]); axs[1].set_title('Avg P&L per trade by side', loc='left'); axs[1].axhline(0, color=MUTED, lw=.8)
    fig.tight_layout(); fig.savefig(f'{OUT}/04_breakdown.png', dpi=150); plt.close(fig)
    print('charts done')
