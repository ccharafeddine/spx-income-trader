"""Honest, minute-level backtester for Daily Melt-style 0DTE SPX credit spreads.

No look-ahead: every decision at minute i uses only bars that have closed by minute i.
Option prices are modelled (see options.py); P&L is per 1 contract (x100), net of
bid/ask (natural on entry, ask-side on early exit), commissions, and stop slippage.
"""
import numpy as np
from options import leg_price, half_spread, delta

BASE = dict(
    # --- signal ---
    signal='pulse',            # 'pulse' (live rules), 'fixed' (non-directional entry at entry_min), 'pulse_reverse'
    pulse_thr=10.0, min_range=2.0, bar_min=30,
    setup_bar_starts=(0, 30, 60),  # minutes after 9:30 whose completed bar may arm a setup (9:30,10:00,10:30)
    window_end=120,            # pending setups expire at 11:30 (minute 120)
    bias_filter=True, bias_thr=0.25,
    gap_max=None,              # skip day if |overnight gap| % > gap_max
    skip_first_bar_if_gap=None,  # ignore the 9:30 bar as a setup if |gap|% > value
    entry_min=60,              # for signal='fixed': entry minute after 9:30
    # --- regime filters (only data known before entry) ---
    vix_min=None, vix_max=None, ts_max=None,  # ts = VIX / VIX3M (prior close); >1 = backwardation
    skip_fomc=False,
    # --- structure ---
    structure='vertical',      # 'vertical' (directional) or 'condor' (both sides)
    short_mode='atm',          # 'atm' (live: round(S/5)*5) or 'delta'
    short_delta=0.50, width=5.0, min_credit=1.00,
    # --- exits ---
    profit_target=0.80,        # fraction of credit captured (None = off)
    stop_mult=None,            # close when cost-to-close >= stop_mult x credit (None = off)
    stop_underlying=None,      # close when SPX crosses short strike by this many points against
    time_exit=None,            # minute after 9:30 to flatten (None = settle)
    # --- costs / model ---
    commission_leg=1.30, stop_slip=0.05, iv_scale=1.0,
    skew=dict(b=0.06, c_put=0.08, c_call=0.02),
)

def _bars(d, bar_min):
    n = d['n']; out = []
    for s in range(0, n - bar_min + 1, bar_min):
        e = s + bar_min
        out.append((s, e, d['o'][s], d['h'][s:e].max(), d['l'][s:e].min(), d['c'][e - 1]))
    return out

def sig_atm(d, p):
    # ATM 0DTE vol: VIX open scaled to VIX1D (ratio calibrated in calibrate.py), x iv_scale
    return d['vix_open'] / 100 * p['_k'] * p['iv_scale']

def find_entry(d, p):
    """Return list of (entry_minute, side) where side in {'put','call'} for the short leg."""
    n = d['n']; c = d['c']; h = d['h']; l = d['l']
    gap = (d['o'][0] / d['spx_prev_close'] - 1) * 100
    if p['gap_max'] is not None and abs(gap) > p['gap_max']: return []
    if p.get('forced') is not None:
        return [p['forced'][d['date']]] if d['date'] in p['forced'] else []
    if p['signal'] == 'fixed':
        if p['entry_min'] >= n - 5: return []
        return [(p['entry_min'], None)]
    pending = None
    bars = {b[0]: b for b in _bars(d, p['bar_min'])}
    day_open = d['o'][0]
    for i in range(n):
        # 1) trigger check for minutes after the pulse bar closed (live checks trigger before new setups)
        if pending is not None and i >= pending['armed'] and i <= p['window_end']:
            if pending['dir'] > 0 and h[i] > pending['trig']:
                return [(i, 'put' if p['signal'] == 'pulse' else 'call')]
            if pending['dir'] < 0 and l[i] < pending['trig']:
                return [(i, 'call' if p['signal'] == 'pulse' else 'put')]
        if i > p['window_end']: return []
        # 2) a bar completes at minute i (bar covering [s, i)) -> evaluate pulse at its close
        s = i - p['bar_min']
        if s in bars and s in p['setup_bar_starts']:
            _, e, o, hh, ll, cc = bars[s]
            rng = hh - ll
            if rng > 0 and rng >= p['min_range']:
                pct = (cc - ll) / rng * 100
                dirn = 1 if (cc > o and pct >= 100 - p['pulse_thr']) else (-1 if (cc < o and pct <= p['pulse_thr']) else 0)
                if dirn and s == 0 and p['skip_first_bar_if_gap'] is not None and abs(gap) > p['skip_first_bar_if_gap']:
                    dirn = 0
                if dirn and p['bias_filter']:
                    mv = (cc / day_open - 1) * 100   # move from open known at bar close (no look-ahead)
                    if (dirn < 0 and mv >= p['bias_thr']) or (dirn > 0 and mv <= -p['bias_thr']): dirn = 0
                if dirn:
                    pending = dict(dir=dirn, trig=hh if dirn > 0 else ll, armed=i)  # newer pulse replaces older
    return []

def strikes_for(S, side, mins_left, sa, p):
    w = p['width']
    if p['short_mode'] == 'atm':
        k = round(S / 5) * 5
    else:  # walk out from ATM; first $5 strike whose |delta| <= target (monotone region of the smile)
        atm = round(S / 5) * 5
        step = -5.0 if side == 'put' else 5.0
        grid = atm + step * np.arange(0, 120)
        dl = np.abs(delta(S, grid, mins_left, sa, side == 'call', p['skew']))
        ok = np.nonzero(dl <= p['short_delta'])[0]
        k = float(grid[ok[0]]) if len(ok) else float(grid[-1])
    return (k, k - w) if side == 'put' else (k, k + w)

def simulate_day(d, p):
    if p['skip_fomc'] and d['fomc']: return None
    ts = d['vix_prev'] / d['vix3m_prev'] if d['vix3m_prev'] == d['vix3m_prev'] else np.nan
    if p['vix_min'] is not None and d['vix_open'] < p['vix_min']: return None
    if p['vix_max'] is not None and d['vix_open'] > p['vix_max']: return None
    if p['ts_max'] is not None and not (ts <= p['ts_max']): return None
    ent = find_entry(d, p)
    if not ent: return None
    i0, side = ent[0]
    n = d['n']; S0 = d['c'][i0]
    if i0 >= n - 2: return None
    sa = sig_atm(d, p); vix = d['vix_open']
    sides = [side] if p['structure'] == 'vertical' else ['put', 'call']
    if p['structure'] == 'vertical' and side is None: return None
    legs = []  # (strike, is_call, sign) sign=-1 short, +1 long
    for sd in sides:
        ks, kl = strikes_for(S0, sd, n - i0, sa, p)
        legs += [(ks, sd == 'call', -1), (kl, sd == 'call', +1)]
    K = np.array([x[0] for x in legs]); C = np.array([x[1] for x in legs]); SG = np.array([x[2] for x in legs])
    # price path from entry minute to close (marks at minute closes)
    idx = np.arange(i0, n)
    S = d['c'][idx]; ml = (n - 1 - idx) + 1.0   # minutes left at each mark (close of minute i -> n-1-i +1)
    P = np.vstack([leg_price(S, K[j], ml, sa, C[j], p['skew']) for j in range(len(K))])  # legs x time
    HS = half_spread(P, vix) * (0.0 if p.get('_zero_spread') else p.get('spread_mult', 1.0))
    mid_val = (-(SG[:, None]) * P).sum(0)        # short legs +P, long legs -P  -> spread value
    natural_credit = ((-(SG[:, None]) * P) - HS).sum(0)   # sell at bid / buy at ask
    close_cost = ((-(SG[:, None]) * P) + HS).sum(0)       # buy back short at ask, sell long at bid
    credit = round(natural_credit[0] / 0.05) * 0.05
    if credit <= 0 or credit < p['min_credit']: return None
    nleg = len(K)
    exit_i = None; reason = 'settle'
    for t in range(1, len(idx)):
        if p['time_exit'] is not None and idx[t] >= p['time_exit']:
            exit_i = t; reason = 'time'; break
        cc = close_cost[t]
        if p['profit_target'] is not None and min(credit - cc, credit) >= p['profit_target'] * credit:
            exit_i = t; reason = 'target'; break
        if p['stop_mult'] is not None and cc >= p['stop_mult'] * credit:
            exit_i = t; reason = 'stop'; break
        if p['stop_underlying'] is not None:
            St = S[t]; hit = False
            for (k, is_call, sg) in legs:
                if sg == -1 and ((is_call and St > k + p['stop_underlying']) or ((not is_call) and St < k - p['stop_underlying'])):
                    hit = True
            if hit: exit_i = t; reason = 'stop'; break
    fees = nleg * p['commission_leg']
    if exit_i is None:
        Sx = d['spx_close']
        val = 0.0
        for (k, is_call, sg) in legs:
            intr = max(Sx - k, 0) if is_call else max(k - Sx, 0)
            val += -sg * intr
        pnl = (credit - val) * 100 - fees
        exit_min = n
    else:
        cost = close_cost[exit_i] + (p['stop_slip'] if reason == 'stop' else 0)
        cost = min(cost, p['width'] * (1 if p['structure'] == 'vertical' else 1))  # can't pay more than width per side
        pnl = (credit - cost) * 100 - 2 * fees
        exit_min = idx[exit_i]
    max_loss = (p['width'] - credit) * 100
    return dict(date=d['date'], side=side or 'both', entry_min=int(i0), S0=float(S0), short=float(K[0]),
                credit=float(credit), pnl=float(pnl), reason=reason, exit_min=int(exit_min),
                risk=float(max(max_loss, 1)), vix=float(vix), fomc=d['fomc'],
                gap=float((d['o'][0] / d['spx_prev_close'] - 1) * 100),
                move_after=float(d['spx_close'] - S0))

def run(days, **over):
    p = dict(BASE); p.update(over)
    p.setdefault('_k', 1.0)
    out = []
    for d in days:
        r = simulate_day(d, p)
        if r: out.append(r)
    return out
