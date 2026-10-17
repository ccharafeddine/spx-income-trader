"""Step 4c/5: robustness of the candidates to the pricing assumptions the result depends on
(implied-vol level, skew, bid/ask), walk-forward re-selection, and sizing."""
from paths import RESULTS, CHARTS
import json, sys, itertools, numpy as np, pandas as pd, lab, engine, metrics
from lab import evaluate
cands = json.load(open(str(RESULTS / 'candidates.json')))   # written after exp2/exp3 (chosen on IS only)
SK = {'flat': dict(b=0.0, c_put=0.0, c_call=0.0), 'base': dict(b=0.06, c_put=0.08, c_call=0.02), 'steep': dict(b=0.12, c_put=0.12, c_call=0.02)}
rows = []
for name, p in cands.items():
    for k, skn, sm, cm in [(0.62, 'base', 1.0, 1.0), (0.55, 'base', 1.0, 1.0), (0.70, 'base', 1.0, 1.0), (0.62, 'flat', 1.0, 1.0),
                           (0.62, 'steep', 1.0, 1.0), (0.62, 'base', 1.5, 1.0), (0.62, 'base', 0.5, 1.0)]:
        q = dict(p); q.update(_k=k, skew=SK[skn], spread_mult=sm)
        r, _ = evaluate(name, **q)
        rows.append(dict(candidate=name, iv_k=k, skew=skn, bidask_x=sm,
                         full_exp=r['full'].get('exp_per_trade'), full_sharpe=r['full'].get('sharpe'),
                         OOS_exp=r['OOS'].get('exp_per_trade'), OOS_pf=r['OOS'].get('profit_factor'), OOS_sharpe=r['OOS'].get('sharpe'),
                         trades=r['full'].get('trades')))
        print(rows[-1], flush=True)
pd.DataFrame(rows).to_csv(str(RESULTS / 'exp4_sensitivity.csv'), index=False)
