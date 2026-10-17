"""Step 1-3: honest baseline + diagnosis."""
from paths import RESULTS, CHARTS
import json, sys, numpy as np, pandas as pd, lab, engine, metrics, analysis
from lab import evaluate
R = {}
ds = lab.days(); all_dates = [d['date'] for d in ds]
base, bt = evaluate('Live rules (baseline)')
R['baseline'] = base
df = pd.DataFrame(bt)
df.to_csv(str(RESULTS / 'baseline_trades.csv'), index=False)
# --- diagnosis: direction accuracy of the signal ---
df['right_dir'] = np.where(df.side == 'put', df.move_after > 0, df.move_after < 0)
R['signal_hit_rate'] = round(100 * df.right_dir.mean(), 1)
R['win_rate_by_side'] = df.assign(win=df.pnl > 0).groupby('side').agg(n=('pnl', 'size'), win=('win', 'mean'), exp=('pnl', 'mean'), credit=('credit', 'mean')).round(3).reset_index().to_dict('records')
# coin flip: same entry minutes, random side
cf = analysis.coinflip_benchmark(ds, bt, n_draws=100, seed=7, _k=lab.K_BASE)
R['coinflip_exp_per_trade'] = dict(mean=cf['mean'], p5=cf['p5'], p95=cf['p95'])
R['baseline_percentile_vs_coinflip'] = cf['signal_percentile']
R['cost_attribution'] = analysis.cost_attribution(ds, _k=lab.K_BASE)
# reversed signal
rev, _ = evaluate('Pulse reversed (fade the breakout)', signal='pulse_reverse'); R['reversed'] = rev
# zero-cost version (how much do costs take?)
zc, _ = evaluate('Live rules, zero commissions and fills at mid', commission_leg=0.0, _zero_spread=True, stop_slip=0.0); R['zero_cost'] = zc
# by gap bucket, by setup bar, by VIX
df['gap_b'] = pd.cut(df.gap.abs(), [0, 0.25, 0.5, 1.0, 10], labels=['<0.25%', '0.25-0.5%', '0.5-1%', '>1%'])
df['bar'] = pd.cut(df.entry_min, [0, 60, 90, 121], labels=['after 9:30 bar', 'after 10:00 bar', 'after 10:30 bar'])
df['vix_b'] = pd.cut(df.vix, [0, 15, 20, 25, 100], labels=['<15', '15-20', '20-25', '>25'])
for col in ['gap_b', 'bar', 'vix_b', 'reason']:
    R[f'by_{col}'] = df.assign(win=df.pnl > 0).groupby(col, observed=True).agg(n=('pnl', 'size'), win_rate=('win', 'mean'), exp=('pnl', 'mean')).round(3).reset_index().astype({col: str}).to_dict('records')
# break-even win rate
R['breakeven_win_rate'] = round(100 * -df[df.pnl <= 0].pnl.mean() / (df[df.pnl > 0].pnl.mean() - df[df.pnl <= 0].pnl.mean()), 1)
json.dump(R, open(str(RESULTS / 'exp1_baseline.json'), 'w'), indent=1, default=str)
print(json.dumps({k: v for k, v in R.items()}, indent=1, default=str)[:6000])
