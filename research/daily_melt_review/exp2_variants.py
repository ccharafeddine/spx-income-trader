"""Step 4: one change at a time vs the live rules, then structure changes. IS = 2019-2022, OOS = 2023-2026."""
from paths import RESULTS, CHARTS
import json, pandas as pd, lab
from lab import evaluate
V = [
 ('Baseline (live rules)', {}),
 # signal / timing filters
 ('No morning-bias filter', dict(bias_filter=False)),
 ('Skip days with |gap| > 0.5%', dict(gap_max=0.5)),
 ('Ignore 9:30 bar when |gap| > 0.3%', dict(skip_first_bar_if_gap=0.3)),
 ('Setups only from 10:00/10:30 bars', dict(setup_bar_starts=(30, 60))),
 ('VIX open <= 25', dict(vix_max=25)),
 ('VIX open >= 15', dict(vix_min=15)),
 ('Contango only (VIX/VIX3M <= 1)', dict(ts_max=1.0)),
 ('Skip FOMC days', dict(skip_fomc=True)),
 # exits
 ('Profit target 50%', dict(profit_target=0.50)),
 ('No profit target (always settle)', dict(profit_target=None)),
 ('Stop at 2x credit', dict(stop_mult=2.0)),
 ('Stop at 1.5x credit', dict(stop_mult=1.5)),
 ('Stop when SPX 5 pts through short strike', dict(stop_underlying=5.0)),
 ('Flatten at 15:45', dict(time_exit=375)),
 # strikes / width (directional, same signal)
 ('Short 30-delta, $5 wide', dict(short_mode='delta', short_delta=0.30, min_credit=0.50)),
 ('Short 20-delta, $5 wide', dict(short_mode='delta', short_delta=0.20, min_credit=0.30)),
 ('Short 10-delta, $5 wide', dict(short_mode='delta', short_delta=0.10, min_credit=0.10)),
 ('ATM, $10 wide', dict(width=10.0, min_credit=2.0)),
 ('Short 20-delta, $10 wide', dict(short_mode='delta', short_delta=0.20, width=10.0, min_credit=0.60)),
]
res = []; trades = {}
for name, p in V:
    r, tr = evaluate(name, **p); res.append(r); trades[name] = tr
    print(name, r['IS'].get('exp_per_trade'), r['OOS'].get('exp_per_trade'), flush=True)
lab.table(res, str(RESULTS / 'exp2_single_changes.csv'))
json.dump(res, open(str(RESULTS / 'exp2_single_changes.json'), 'w'), indent=1, default=str)
