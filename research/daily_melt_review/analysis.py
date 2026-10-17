"""Reusable evaluation tools for the Daily Melt review.

- is_oos():          split trades/days at a fixed date (default 2022-12-31)
- coinflip_benchmark(): same entry minutes, random side -> is the signal better than chance?
- cost_attribution(): how much of the gross edge bid/ask, commissions and stop slippage take
- walk_forward():    anchored walk-forward; each test year uses settings chosen only on
                     earlier years, out-of-sample years are concatenated

Everything takes the day list (see data.py) explicitly so it can be tested on synthetic days.
"""
from datetime import date
import numpy as np
import engine, metrics

IS_END = date(2022, 12, 31)


def is_oos(trades, days, is_end=IS_END):
    is_days = [d['date'] for d in days if d['date'] <= is_end]
    oos_days = [d['date'] for d in days if d['date'] > is_end]
    return dict(IS=metrics.stats([t for t in trades if t['date'] <= is_end], is_days),
                OOS=metrics.stats([t for t in trades if t['date'] > is_end], oos_days))


def coinflip_benchmark(days, trades, n_draws=100, seed=7, **params):
    """Re-run every traded day at the same entry minute with a random side."""
    if not trades:
        return dict(mean=None, p5=None, p95=None, signal_percentile=None, draws=0)
    rng = np.random.default_rng(seed)
    flips = []
    for _ in range(n_draws):
        forced = {t['date']: (t['entry_min'], str(rng.choice(['put', 'call']))) for t in trades}
        tr = engine.run(days, forced=forced, **params)
        flips.append(float(np.mean([t['pnl'] for t in tr])) if tr else 0.0)
    sig = float(np.mean([t['pnl'] for t in trades]))
    f = np.array(flips)
    return dict(mean=round(float(f.mean()), 1), p5=round(float(np.percentile(f, 5)), 1),
                p95=round(float(np.percentile(f, 95)), 1),
                signal_exp=round(sig, 1),
                signal_percentile=round(float((f < sig).mean() * 100), 1), draws=n_draws)


def cost_attribution(days, **params):
    """Total P&L at four cost levels; each step adds one cost.

    The steps run separately and an exit that depends on costs can move, so the
    pieces are differences between runs, not exact accounting identities.
    """
    def total(**o):
        p = dict(params); p.update(o)
        tr = engine.run(days, **p)
        return float(sum(t['pnl'] for t in tr)), len(tr)
    gross, n0 = total(commission_leg=0.0, _zero_spread=True, stop_slip=0.0)
    w_spread, _ = total(commission_leg=0.0, stop_slip=0.0)
    w_slip, _ = total(commission_leg=0.0)
    net, n = total()
    return dict(gross_at_mid=round(gross, 0), bid_ask=round(w_spread - gross, 0),
                stop_slippage=round(w_slip - w_spread, 0), commissions=round(net - w_slip, 0),
                net=round(net, 0), trades_gross=n0, trades_net=n)


def walk_forward(days, grid, years, min_trades=100, runner=None, score='sharpe'):
    """Anchored walk-forward over `grid` (list of param dicts).

    For each test year Y the setting with the best `score` on trades before Y
    (with at least `min_trades`) is traded in Y only.
    """
    runner = runner or (lambda p: engine.run(days, **p))
    cache = [runner(p) for p in grid]
    oos, picks = [], []
    for Y in years:
        past_days = sorted({d['date'] for d in days if d['date'].year < Y})
        best = None
        for i, tr in enumerate(cache):
            past = [t for t in tr if t['date'].year < Y]
            if len(past) < min_trades:
                continue
            s = metrics.stats(past, past_days)[score]
            if s == s and (best is None or s > best[1]):
                best = (i, s)
        if best is None:
            picks.append(dict(year=Y, pick=None, reason='no setting has enough past trades'))
            continue
        yr = [t for t in cache[best[0]] if t['date'].year == Y]
        oos += yr
        picks.append(dict(year=Y, pick_index=best[0], pick=grid[best[0]], in_sample_score=round(best[1], 2),
                          oos_year=metrics.stats(yr, [d['date'] for d in days if d['date'].year == Y])))
    oos_days = [d['date'] for d in days if d['date'].year >= min(years)]
    return dict(picks=picks, combined_oos=metrics.stats(oos, oos_days))
