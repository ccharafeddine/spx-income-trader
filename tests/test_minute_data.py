"""Real minute data loader: decoding, alignment, gap handling, and the
refusal to synthesize bars by default. No network access."""
import lzma
import struct
from datetime import date, time
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from src.backtest import minute_data as md

DAY = date(2024, 1, 3)  # EST: 9:30 ET = 14:30 UTC


def _bi5(prices, start_sec=14 * 3600 + 30 * 60):
    """Build a fake Dukascopy file: one candle per minute from start_sec."""
    buf = b''
    for i, p in enumerate(prices):
        v = int(round(p * 1000))
        buf += struct.pack('>5if', start_sec + 60 * i, v, v + 100, v - 500, v + 600, 1.0)
    return lzma.compress(buf, format=lzma.FORMAT_ALONE)


def test_decode_bi5_fields_and_timezone():
    df = md.decode_bi5(_bi5([4700.0, 4701.0]), DAY)
    assert len(df) == 2
    assert df.index[0].tz is not None
    assert (df.index[0].hour, df.index[0].minute) == (9, 30)
    row = df.iloc[0]
    assert row.open == pytest.approx(4700.0)
    assert row.close == pytest.approx(4700.1)
    assert row.low == pytest.approx(4699.5)
    assert row.high == pytest.approx(4700.6)


def test_decode_empty_returns_none():
    assert md.decode_bi5(b'', DAY) is None


def test_align_session_shifts_to_official_close():
    prices = 4700 + np.arange(390) * 0.1
    m = md.decode_bi5(_bi5(prices), DAY)
    sess, why = md.align_session(m, DAY, official_close=4750.0)
    assert why == '' and len(sess) == 390
    assert sess['close'].iloc[-1] == pytest.approx(4750.0)
    # path shape preserved: same minute-to-minute changes
    assert np.allclose(np.diff(sess['close'].values), 0.1)


def test_align_session_half_day():
    m = md.decode_bi5(_bi5(4700 + np.zeros(210)), DAY)
    sess, _ = md.align_session(m, DAY, 4700.0, close_time=time(13, 0))
    assert len(sess) == 210


def test_gappy_session_is_skipped_not_filled():
    m = md.decode_bi5(_bi5(4700 + np.zeros(300)), DAY)   # 90 of 390 minutes missing
    sess, why = md.align_session(m, DAY, 4700.0)
    assert sess is None and 'gaps' in why


def test_resample_30m():
    m = md.decode_bi5(_bi5(4700 + np.arange(390) * 0.1), DAY)
    sess, _ = md.align_session(m, DAY, 4800.0)
    bars = md.resample_30m(sess)
    assert len(bars) == 13
    assert bars.index[0].strftime('%H:%M') == '09:30'


def test_build_minute_bars_reports_skipped_days(tmp_path):
    good = _bi5(4700 + np.arange(390) * 0.1)
    def getter(url):
        if '/2024/00/03/' in url:
            return good
        return b''  # empty file -> skipped
    bars, skipped = md.build_minute_bars([DAY, date(2024, 1, 4)], {DAY: 4740.0, date(2024, 1, 4): 4700.0},
                                         tmp_path, getter=getter, workers=1)
    assert len(bars) == 390
    assert skipped == {'2024-01-04': 'empty file'}


def test_fetch_day_uses_cache(tmp_path):
    calls = []
    def getter(url):
        calls.append(url)
        return _bi5([4700.0])
    md.fetch_day(DAY, tmp_path, getter)
    md.fetch_day(DAY, tmp_path, getter)
    assert len(calls) == 1
    assert '/2024/00/03/' in calls[0]  # Dukascopy months are 0-based


def test_download_refuses_synthetic_by_default(tmp_path):
    """When neither Yahoo 30m nor minute data is available, raise instead of
    silently synthesizing bars from daily OHLC."""
    from src.backtest import data_loader as dl
    daily = pd.DataFrame({'open': [4700.0], 'high': [4710.0], 'low': [4690.0], 'close': [4705.0], 'volume': [0]},
                         index=pd.DatetimeIndex([pd.Timestamp('2024-01-03')]))
    with patch('yfinance.download', return_value=pd.DataFrame()), \
         patch.object(dl, '_official_daily', return_value=daily):
        with pytest.raises(ValueError, match='Synthetic'):
            dl.download_spx_bars(DAY, DAY, cache_dir=tmp_path, minute_getter=lambda url: b'')


def test_download_allows_synthetic_only_when_asked(tmp_path):
    from src.backtest import data_loader as dl
    daily = pd.DataFrame({'open': [4700.0], 'high': [4710.0], 'low': [4690.0], 'close': [4705.0], 'volume': [0]},
                         index=pd.DatetimeIndex([pd.Timestamp('2024-01-03')]))
    with patch('yfinance.download', return_value=pd.DataFrame()), \
         patch.object(dl, '_official_daily', return_value=daily):
        p = dl.download_spx_bars(DAY, DAY, cache_dir=tmp_path, allow_synthetic=True,
                                 minute_getter=lambda url: b'')
    assert 'SYNTHETIC' in Path(p).name


def test_download_uses_real_minute_data(tmp_path):
    import json
    from src.backtest import data_loader as dl
    daily = pd.DataFrame({'open': [4700.0], 'high': [4760.0], 'low': [4690.0], 'close': [4741.0], 'volume': [0]},
                         index=pd.DatetimeIndex([pd.Timestamp('2024-01-03')]))
    good = _bi5(4700 + np.arange(390) * 0.1)
    with patch('yfinance.download', return_value=pd.DataFrame()), \
         patch.object(dl, '_official_daily', return_value=daily):
        p = dl.download_spx_bars(DAY, DAY, cache_dir=tmp_path, minute_getter=lambda url: good)
    assert 'SYNTHETIC' not in Path(p).name
    bars = dl.load_bars_from_csv(str(p))
    assert len(bars) == 13
    assert bars['close'].iloc[-1] == pytest.approx(4741.0)
    q = json.loads(Path(str(p) + '.quality.json').read_text())
    assert q['synthetic_days'] == 0 and q['loaded_days'] == 1


def test_future_dates_raise_fast(tmp_path):
    from src.backtest import data_loader as dl
    with pytest.raises(ValueError):
        dl.download_spx_bars(date(2099, 1, 1), date(2099, 1, 5), cache_dir=tmp_path)
