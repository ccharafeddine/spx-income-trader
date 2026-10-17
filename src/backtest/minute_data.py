"""Real intraday S&P 500 minute data for the backtester.

Yahoo Finance keeps only ~60 days of 30-minute history, so long backtests
used to fall back to bars *synthesized* from each day's open/high/low/close.
Those synthetic bars encode the day's outcome in their shape, which makes
any intraday signal look better than it is.

This module loads free, public 1-minute candles for the S&P 500 index CFD
(Dukascopy ``USA500IDXUSD``, no account needed), one file per UTC day, and
aligns each session to the official SPX close:

    aligned_minute = cfd_minute + (official_spx_close - cfd_close_at_15:59)

The intraday *path* comes from the CFD (hourly changes correlate ~0.98 with
^GSPC), the *level* is SPX, and settlement matches the official close.
Days that are missing, empty or full of gaps are skipped and reported --
never synthesized.
"""
from __future__ import annotations

import json
import logging
import lzma
import random
import time as _time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

DUKASCOPY_URL = ('https://datafeed.dukascopy.com/datafeed/USA500IDXUSD/'
                 '{y}/{m:02d}/{d:02d}/BID_candles_min_1.bi5')
PRICE_SCALE = 1000.0
MAX_GAP_FRACTION = 0.05      # skip a session if >5% of its minutes are missing
MAX_FROZEN_FRACTION = 0.30   # skip a session if >30% of minutes have high == low


def decode_bi5(raw: bytes, day: date) -> Optional[pd.DataFrame]:
    """Decode one Dukascopy daily candle file into ET-indexed minute bars.

    Record layout (big-endian, 24 bytes): seconds-from-UTC-midnight, open,
    close, low, high (ints, price x 1000), volume (float).
    """
    if not raw:
        return None
    data = lzma.decompress(raw)
    n = len(data) // 24
    if n == 0:
        return None
    arr = np.frombuffer(data[:n * 24], dtype='>i4').reshape(n, 6)[:, :5].astype(np.int64)
    t0 = pd.Timestamp(datetime(day.year, day.month, day.day, tzinfo=timezone.utc))
    idx = (t0 + pd.to_timedelta(arr[:, 0], unit='s')).tz_convert('America/New_York')
    return pd.DataFrame({
        'open': arr[:, 1] / PRICE_SCALE,
        'high': arr[:, 4] / PRICE_SCALE,
        'low': arr[:, 3] / PRICE_SCALE,
        'close': arr[:, 2] / PRICE_SCALE,
    }, index=idx)


def _http_get(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_day(day: date, cache_dir: Path, getter: Callable[[str], bytes] = _http_get,
              retries: int = 20) -> Optional[bytes]:
    """Return the raw .bi5 bytes for ``day`` (UTC), using an on-disk cache."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f'{day.isoformat()}.bi5'
    if path.exists():
        return path.read_bytes()
    url = DUKASCOPY_URL.format(y=day.year, m=day.month - 1, d=day.day)  # month is 0-based
    for attempt in range(retries):
        try:
            raw = getter(url)
            if raw:
                lzma.decompress(raw)  # validate before caching
            path.write_bytes(raw or b'')
            return raw
        except Exception as exc:  # 503s/timeouts are common; back off and retry
            logger.debug(f"minute data {day}: attempt {attempt + 1} failed ({exc})")
            _time.sleep(min(8, 1 + attempt) + random.random())
    logger.warning(f"minute data {day}: giving up after {retries} attempts")
    return None


def align_session(minutes: pd.DataFrame, day: date, official_close: float,
                  close_time: time = time(16, 0)) -> Tuple[Optional[pd.DataFrame], str]:
    """Cut one session (9:30 to close) out of a day's minute bars and shift
    it so the last minute closes at the official SPX close.

    Returns (frame, '') or (None, reason) when the session is unusable.
    """
    start = pd.Timestamp(datetime.combine(day, time(9, 30))).tz_localize('America/New_York')
    nmin = (close_time.hour * 60 + close_time.minute) - (9 * 60 + 30)
    idx = pd.date_range(start, periods=nmin, freq='1min')
    s = minutes.reindex(idx)
    gap = float(s['close'].isna().mean())
    if gap > MAX_GAP_FRACTION:
        return None, f'gaps {gap:.0%}'
    s = s.ffill().bfill()
    frozen = float((s['high'] == s['low']).mean())
    if frozen > MAX_FROZEN_FRACTION:
        return None, f'frozen feed {frozen:.0%}'
    offset = float(official_close) - float(s['close'].iloc[-1])
    s = s + offset
    s['volume'] = 0
    return s, ''


def resample_30m(minutes: pd.DataFrame) -> pd.DataFrame:
    return minutes.resample('30min', label='left', closed='left').agg(
        {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}
    ).dropna(subset=['open'])


def build_minute_bars(
    trading_days: Iterable[date],
    official_closes: Dict[date, float],
    cache_dir: Path,
    early_closes: Iterable[date] = (),
    getter: Callable[[str], bytes] = _http_get,
    workers: int = 8,
) -> Tuple[pd.DataFrame, Dict[str, str]]:
    """Aligned 1-minute session bars for ``trading_days``.

    Returns (bars, skipped) where ``skipped`` maps ISO date -> reason.
    """
    days = [d for d in trading_days]
    early = set(early_closes)
    with ThreadPoolExecutor(max(1, workers)) as ex:
        raws = list(ex.map(lambda d: fetch_day(d, cache_dir, getter), days))
    frames: List[pd.DataFrame] = []
    skipped: Dict[str, str] = {}
    for d, raw in zip(days, raws):
        if d not in official_closes:
            skipped[d.isoformat()] = 'no official close'
            continue
        if raw is None:
            skipped[d.isoformat()] = 'download failed'
            continue
        m = decode_bi5(raw, d)
        if m is None:
            skipped[d.isoformat()] = 'empty file'
            continue
        sess, why = align_session(m, d, official_closes[d], time(13, 0) if d in early else time(16, 0))
        if sess is None:
            skipped[d.isoformat()] = why
            continue
        frames.append(sess)
    bars = pd.concat(frames).sort_index() if frames else pd.DataFrame(
        columns=['open', 'high', 'low', 'close', 'volume'])
    return bars, skipped


def write_quality_report(path: Path, requested: List[date], skipped: Dict[str, str], source: str) -> dict:
    report = {
        'source': source,
        'requested_days': len(requested),
        'loaded_days': len(requested) - len(skipped),
        'skipped_days': skipped,
        'synthetic_days': 0,
    }
    path.write_text(json.dumps(report, indent=1))
    return report
