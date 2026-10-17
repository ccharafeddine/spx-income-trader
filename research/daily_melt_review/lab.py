"""Shared helpers for the experiments: IS/OOS split, evaluation, tables."""
import json, numpy as np, pandas as pd, data, engine, metrics
from datetime import date
IS_END = date(2022, 12, 31)
K_BASE = 0.62
_days = None
def days():
    global _days
    import os
    if _days is None: _days = data.load(os.environ.get('DAYS_PKL', str(data.DATA / 'days.pkl')))
    return _days
def split(ds):
    return [d for d in ds if d['date'] <= IS_END], [d for d in ds if d['date'] > IS_END]
def evaluate(label, ds=None, **p):
    ds = ds or days(); p.setdefault('_k', K_BASE)
    tr = engine.run(ds, **p)
    is_d, oos_d = split(ds)
    is_t = [t for t in tr if t['date'] <= IS_END]; oos_t = [t for t in tr if t['date'] > IS_END]
    return dict(label=label,
                full=metrics.stats(tr, [d['date'] for d in ds]),
                IS=metrics.stats(is_t, [d['date'] for d in is_d]),
                OOS=metrics.stats(oos_t, [d['date'] for d in oos_d]),
                params={k: v for k, v in p.items() if k not in ('forced',)}), tr
def row(res, cols=('trades', 'win_rate', 'exp_per_trade', 'profit_factor', 'max_dd', 'sharpe', 't_stat')):
    r = {'variant': res['label']}
    for part in ('IS', 'OOS'):
        for c in cols: r[f'{part}_{c}'] = res[part].get(c)
    return r
def table(results, path=None):
    df = pd.DataFrame([row(r) for r in results])
    if path: df.to_csv(path, index=False)
    return df
