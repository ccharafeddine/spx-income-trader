"""Anchored walk-forward: each year Y (2021..2026), pick the best condor settings using only
data before Y (by Sharpe, min 100 trades), trade them in Y. Concatenate the out-of-sample years."""
from paths import RESULTS, CHARTS
import json, itertools, numpy as np, pandas as pd, lab, engine, metrics, analysis
from datetime import date
ds = lab.days()
grid = [dict(signal='fixed', entry_min=e, structure='condor', short_mode='delta', short_delta=dl, width=w,
             profit_target=pt, stop_mult=st, min_credit=0.10, bias_filter=False)
        for e, dl, w, pt, st in itertools.product([30, 90, 150, 210, 270], [0.05, 0.10, 0.15, 0.20], [5.0, 10.0], [0.5, None], [None, 2.0])]
res = analysis.walk_forward(ds, grid, range(2021, 2027), min_trades=100, runner=lambda p: engine.run(ds, _k=lab.K_BASE, **p))
for pk in res['picks']:
    pk['pick'] = {k: pk['pick'][k] for k in ('entry_min', 'short_delta', 'width', 'profit_target', 'stop_mult')}
    print(pk, flush=True)
json.dump(res, open(str(RESULTS / 'exp5_walkforward.json'), 'w'), indent=1, default=str)
print(json.dumps(res['combined_oos'], indent=1, default=str))
