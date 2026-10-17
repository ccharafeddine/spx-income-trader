import numpy as np, pandas as pd
def stats(trades, all_days=None, label=''):
    if not trades:
        return dict(label=label, trades=0)
    df = pd.DataFrame(trades)
    pnl = df.pnl.values
    w = pnl[pnl > 0]; l = pnl[pnl <= 0]
    eq = np.cumsum(pnl); dd = (eq - np.maximum.accumulate(np.maximum(eq, 0))).min()
    # daily series incl. zero days for Sharpe
    if all_days is not None:
        g = df.groupby('date').pnl.sum()
        s = pd.Series(0.0, index=pd.Index(all_days)); s.loc[g.index] = g.values
    else:
        s = df.groupby('date').pnl.sum()
    sharpe = s.mean() / s.std() * np.sqrt(252) if s.std() > 0 else np.nan
    # t-stat of mean per-trade pnl
    t = pnl.mean() / (pnl.std(ddof=1) / np.sqrt(len(pnl))) if len(pnl) > 2 else np.nan
    return dict(label=label, trades=len(pnl), win_rate=round(100 * len(w) / len(pnl), 1),
                exp_per_trade=round(pnl.mean(), 1), avg_win=round(w.mean(), 1) if len(w) else None,
                avg_loss=round(l.mean(), 1) if len(l) else None,
                profit_factor=round(w.sum() / -l.sum(), 2) if l.sum() < 0 else np.inf,
                total=round(pnl.sum(), 0), max_dd=round(dd, 0), sharpe=round(sharpe, 2),
                t_stat=round(t, 2), avg_credit=round(df.credit.mean(), 2), avg_risk=round(df.risk.mean(), 0),
                ret_on_risk_pct=round(100 * pnl.sum() / df.risk.sum(), 2))
