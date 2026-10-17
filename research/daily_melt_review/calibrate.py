"""How rich is 0DTE implied vol vs what SPX actually realised after the entry time?
Compares 5-minute realised variance from 10:30 to the close with (k*VIX_open)^2 * T."""
from paths import RESULTS, CHARTS
import numpy as np, pandas as pd, json, data
days = data.load()
rows = []
for d in days:
    if d['n'] < 390: continue
    c = d['c'][60:][::5]                       # 10:30 onward, 5-min samples
    rv = np.sum(np.diff(np.log(np.append(c, d['spx_close']))) ** 2)
    T = (390 - 60) / (390 * 252)
    rows.append(dict(date=d['date'], rv=rv, iv2T=(d['vix_open'] / 100) ** 2 * T,
                     v1d=d['vix1d_open'], vix=d['vix_open'],
                     absmove=abs(d['spx_close'] / d['c'][60] - 1)))
df = pd.DataFrame(rows); df['year'] = [x.year for x in df.date]
out = {}
for name, g in [('all', df), ('2019-2022', df[df.year <= 2022]), ('2023-2026', df[df.year >= 2023])]:
    kf = np.sqrt(g.rv.mean() / g.iv2T.mean())
    # straddle-style fair k: E|move| = sigma*sqrt(T)*sqrt(2/pi)
    km = (g.absmove.mean() / np.sqrt(2 / np.pi)) / np.sqrt(g.iv2T).mean()
    out[name] = dict(days=len(g), k_fair_variance=round(kf, 3), k_fair_absmove=round(km, 3))
g = df.dropna(subset=['v1d']); g = g[g.v1d > 0]
out['vix1d_open_over_vix_open_median'] = round(float((g.v1d / g.vix).median()), 3)
json.dump(out, open(str(RESULTS / 'calibration.json'), 'w'), indent=1)
print(json.dumps(out, indent=1))
