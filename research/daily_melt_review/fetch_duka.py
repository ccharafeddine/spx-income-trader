"""Download free Dukascopy USA500IDXUSD (S&P 500 index CFD) 1-minute BID candles, one file per UTC day.
Public datafeed, no account. Polite: small concurrency, retry with backoff."""
import os, sys, time, random, lzma, struct, urllib.request
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor
from paths import DUKA
OUT = str(DUKA)
URL = 'https://datafeed.dukascopy.com/datafeed/USA500IDXUSD/{y}/{m:02d}/{d:02d}/BID_candles_min_1.bi5'
def get(day):
    fn = f'{OUT}/{day.isoformat()}.bi5'
    if os.path.exists(fn): return 'skip'
    url = URL.format(y=day.year, m=day.month-1, d=day.day)
    for attempt in range(25):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=60) as r:
                b = r.read()
            if len(b) == 0:
                open(fn, 'wb').close(); return 'empty'
            lzma.decompress(b)  # validate
            open(fn, 'wb').write(b); return 'ok'
        except Exception as e:
            time.sleep(min(8, 1 + attempt) + random.random())
    return 'fail'
if __name__ == '__main__':
    s = date.fromisoformat(sys.argv[1]); e = date.fromisoformat(sys.argv[2])
    days = [s + timedelta(i) for i in range((e - s).days + 1) if (s + timedelta(i)).weekday() < 5]
    stats = {}
    with ThreadPoolExecutor(int(sys.argv[3]) if len(sys.argv) > 3 else 4) as ex:
        for i, r in enumerate(ex.map(get, days)):
            stats[r] = stats.get(r, 0) + 1
            if i % 50 == 0: print(i, len(days), stats, flush=True)
    print('done', stats, flush=True)
