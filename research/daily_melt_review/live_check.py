"""Sanity check: replay the live rules over the live-trading window (Mar 10 - May 1, 2026)
and compare trade days and outcomes with the 21-trade live record (per-day P&L from the
app's results calendar screenshots; contract counts varied, so compare signs, not dollars)."""
from paths import RESULTS, CHARTS
import json, pandas as pd, lab, engine
from datetime import date
# Live per-day P&L is private: keep it in data/live_pnl.json (gitignored), {"YYYY-MM-DD": pnl, ...}.
from paths import DATA
live = json.load(open(DATA / 'live_pnl.json'))
offline = {'2026-03-10', '2026-04-17', '2026-04-20', '2026-04-21'}
ds = [d for d in lab.days() if date(2026, 3, 11) <= d['date'] <= date(2026, 5, 1) and d['date'].isoformat() not in offline]
tr = {t['date'].isoformat(): t for t in engine.run(ds, _k=lab.K_BASE)}
rows = []
for d in ds:
    k = d['date'].isoformat(); m = tr.get(k)
    rows.append(dict(date=k, live_pnl=live.get(k), model_side=m['side'] if m else None,
                     model_entry=f"{9 + (30 + m['entry_min']) // 60}:{(30 + m['entry_min']) % 60:02d}" if m else None,
                     model_pnl_1lot=round(m['pnl'], 0) if m else None))
df = pd.DataFrame(rows)
both = df.dropna(subset=['live_pnl', 'model_pnl_1lot'])
summary = dict(session_days=len(df), live_trades=int(df.live_pnl.notna().sum()), model_trades=int(df.model_pnl_1lot.notna().sum()),
               days_both_traded=len(both), traded_day_agreement=int(((df.live_pnl.notna()) == (df.model_pnl_1lot.notna())).sum()),
               win_loss_sign_agreement_on_common_days=int(((both.live_pnl > 0) == (both.model_pnl_1lot > 0)).sum()),
               live_win_rate=round(100 * (df.live_pnl > 0).sum() / df.live_pnl.notna().sum(), 1),
               model_win_rate=round(100 * (df.model_pnl_1lot > 0).sum() / max(1, df.model_pnl_1lot.notna().sum()), 1),
               model_total_1lot=round(df.model_pnl_1lot.sum(), 0), live_total=round(df.live_pnl.sum(), 2))
summary.pop('live_total')  # keep dollar amounts of the live account out of the repo
out = df.assign(live_result=df.live_pnl.map(lambda v: None if pd.isna(v) else ('W' if v > 0 else 'L'))).drop(columns='live_pnl')
out.to_csv(str(RESULTS / 'live_check.csv'), index=False)
json.dump(summary, open(str(RESULTS / 'live_check.json'), 'w'), indent=1)
print(df.to_string()); print(json.dumps(summary, indent=1))
