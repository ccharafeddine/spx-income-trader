"""Download the daily series the review needs (Yahoo Finance via yfinance)."""
import yfinance as yf
from paths import DATA
for t, n in [('^GSPC', 'spx_daily'), ('^VIX', 'vix'), ('^VIX1D', 'vix1d'), ('^VIX9D', 'vix9d'), ('^VIX3M', 'vix3m')]:
    d = yf.download(t, start='2018-06-01', end='2026-10-17', auto_adjust=False, progress=False)
    d.columns = [c[0] if isinstance(c, tuple) else c for c in d.columns]
    d.to_csv(DATA / f'{n}.csv'); print(n, len(d))
