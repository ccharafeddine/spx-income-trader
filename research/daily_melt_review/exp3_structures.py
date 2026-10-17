"""Step 4b: drop the directional signal; non-directional 0DTE iron condors at a fixed time.
Grid is chosen on IS (2019-2022) only; OOS (2023-2026) is reported untouched."""
from paths import RESULTS, CHARTS
import json, itertools, pandas as pd, lab
from lab import evaluate
grid = []
for entry_min, dlt, width, pt, stop in itertools.product([30, 90, 150, 210, 270], [0.05, 0.10, 0.15, 0.20], [5.0, 10.0], [0.5, None], [None, 2.0]):
    grid.append((f'IC t+{entry_min}m d{dlt} w{width:g} pt{pt} stop{stop}',
                 dict(signal='fixed', entry_min=entry_min, structure='condor', short_mode='delta', short_delta=dlt,
                      width=width, profit_target=pt, stop_mult=stop, min_credit=0.10, bias_filter=False)))
res = []
for name, p in grid:
    r, _ = evaluate(name, **p); res.append(r)
    print(name, r['IS'].get('exp_per_trade'), r['IS'].get('sharpe'), '| OOS', r['OOS'].get('exp_per_trade'), r['OOS'].get('sharpe'), flush=True)
df = lab.table(res, str(RESULTS / 'exp3_condor_grid.csv'))
json.dump(res, open(str(RESULTS / 'exp3_condor_grid.json'), 'w'), indent=1, default=str)
