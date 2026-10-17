"""Build per-day intraday SPX minute paths for the backtest.

Source: Dukascopy USA500IDXUSD 1-minute BID candles (a free S&P 500 index CFD feed).
Each session is shifted by a constant so its 15:59 bar close equals the official
SPX close from Yahoo (^GSPC). The intraday *shape* is the CFD's, the level is SPX.
"""
import os, lzma, struct, json, pickle
from datetime import date, datetime, timedelta, time
import numpy as np, pandas as pd
from zoneinfo import ZoneInfo
from paths import DATA, DUKA, HERE

ROOT = str(DATA.parent)
ET = ZoneInfo('America/New_York'); UTC = ZoneInfo('UTC')

def read_bi5(path, day):
    b = open(path, 'rb').read()
    if not b: return None
    raw = lzma.decompress(b)
    n = len(raw) // 24
    arr = np.frombuffer(raw[:n*24], dtype='>i4').reshape(n, 6)[:, :5].astype(np.int64)
    t0 = datetime(day.year, day.month, day.day, tzinfo=UTC)
    idx = pd.DatetimeIndex([t0 + timedelta(seconds=int(s)) for s in arr[:, 0]]).tz_convert(ET)
    df = pd.DataFrame({'o': arr[:, 1] / 1000, 'c': arr[:, 2] / 1000, 'l': arr[:, 3] / 1000, 'h': arr[:, 4] / 1000}, index=idx)
    return df

def _schedule(spx, start='2019-01-01', end='2026-10-16'):
    """NYSE sessions with their close time (pandas_market_calendars if
    installed, else Yahoo's trading days + the repo's early-close list)."""
    try:
        import pandas_market_calendars as mcal
        return mcal.get_calendar('NYSE').schedule(start, end)
    except ImportError:
        import sys; sys.path.insert(0, str(HERE.parent.parent))
        from src.utils.market_calendar import is_early_close
        idx = spx.loc[start:end].index
        closes = [pd.Timestamp(datetime.combine(d.date(), time(13 if is_early_close(d.date()) else 16)), tz=ET) for d in idx]
        return pd.DataFrame({'market_close': closes}, index=idx)


def build(cache=str(DATA / 'days.pkl')):
    spx = pd.read_csv(f'{ROOT}/data/spx_daily.csv', index_col=0, parse_dates=True)
    sched = _schedule(spx)
    vix = pd.read_csv(f'{ROOT}/data/vix.csv', index_col=0, parse_dates=True)
    v1d = pd.read_csv(f'{ROOT}/data/vix1d.csv', index_col=0, parse_dates=True)
    v9d = pd.read_csv(f'{ROOT}/data/vix9d.csv', index_col=0, parse_dates=True)
    v3m = pd.read_csv(f'{ROOT}/data/vix3m.csv', index_col=0, parse_dates=True)
    fomc = set(json.load(open(HERE / 'fomc_dates.json')))
    days = []; skipped = []
    for d, row in sched.iterrows():
        dd = d.date()
        close_et = row.market_close.tz_convert(ET)
        nmin = int((close_et.hour * 60 + close_et.minute) - (9 * 60 + 30))
        f = f'{DUKA}/{dd.isoformat()}.bi5'
        if not os.path.exists(f) or pd.Timestamp(dd) not in spx.index:
            skipped.append((dd, 'missing')); continue
        df = read_bi5(f, dd)
        if df is None: skipped.append((dd, 'empty')); continue
        start = pd.Timestamp(datetime.combine(dd, time(9, 30)), tz=ET)
        idx = pd.date_range(start, periods=nmin, freq='1min')
        s = df.reindex(idx)
        if s['c'].isna().mean() > 0.05:
            skipped.append((dd, f"gaps {s['c'].isna().mean():.2f}")); continue
        s = s.ffill().bfill()
        # identical-price flat runs (feed freeze) check
        frozen = (s['h'] == s['l']).mean()
        if frozen > 0.3: skipped.append((dd, f'frozen {frozen:.2f}')); continue
        off = float(spx.loc[pd.Timestamp(dd), 'Close']) - float(s['c'].iloc[-1])
        prev = spx.index[spx.index.get_loc(pd.Timestamp(dd)) - 1]
        def g(tab, col, dt=pd.Timestamp(dd)):
            try: return float(tab.loc[dt, col])
            except KeyError: return np.nan
        days.append(dict(
            date=dd, n=nmin,
            o=s['o'].values + off, h=s['h'].values + off, l=s['l'].values + off, c=s['c'].values + off,
            spx_open=g(spx, 'Open'), spx_close=g(spx, 'Close'), spx_prev_close=float(spx.loc[prev, 'Close']),
            vix_open=g(vix, 'Open'), vix_prev=g(vix, 'Close', prev), vix1d_open=g(v1d, 'Open'), vix1d_prev=g(v1d, 'Close', prev),
            vix9d_prev=g(v9d, 'Close', prev), vix3m_prev=g(v3m, 'Close', prev),
            fomc=dd.isoformat() in fomc, open_resid=(s['o'].values[0] + off) - g(spx, 'Open'), offset=off))
    pickle.dump(days, open(cache, 'wb'))
    return days, skipped

def load(cache=str(DATA / 'days.pkl')):
    return pickle.load(open(cache, 'rb'))

if __name__ == '__main__':
    days, sk = build()
    print(len(days), 'days;', len(sk), 'skipped')
    print(pd.Series([s[1].split()[0] for s in sk]).value_counts())
    r = np.array([d['open_resid'] for d in days])
    print('open residual (aligned CFD 9:30 open - official SPX open) pts: median %.2f, MAD %.2f, p95 abs %.2f' % (np.median(r), np.median(abs(r - np.median(r))), np.percentile(abs(r), 95)))
