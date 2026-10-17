"""
Historical SPX Data Loader for Backtesting

Loads historical 30-minute SPX bars from CSV or downloads them from
Yahoo Finance. Also fetches daily VIX close for IV proxy in option pricing.

When 30-minute data is unavailable (yfinance only provides ~60 days of
intraday history), real 1-minute S&P 500 bars are loaded instead (see
minute_data.py). Bars synthesized from daily OHLC are produced only with
allow_synthetic=True, because their shape leaks the day's outcome.
"""

import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional, List

import pandas as pd
import pytz

logger = logging.getLogger(__name__)

ET = pytz.timezone('America/New_York')


def _get_cache_dir() -> Path:
    """Get cache directory, compatible with both source and packaged mode."""
    try:
        from src.utils.app_paths import DATA_DIR
        cache = DATA_DIR / 'database' / 'backtest_data'
    except ImportError:
        cache = Path(__file__).parent.parent.parent / 'database' / 'backtest_data'
    cache.mkdir(parents=True, exist_ok=True)
    return cache


# ---------- 30-min bar synthesis from daily data ----------

# 13 bars per day: 9:30, 10:00, ..., 15:30
_BAR_TIMES = [
    '09:30', '10:00', '10:30', '11:00', '11:30', '12:00', '12:30',
    '13:00', '13:30', '14:00', '14:30', '15:00', '15:30',
]
_N_BARS = len(_BAR_TIMES)

# U-shaped volume weights (heavier at open/close)
_VOL_WEIGHTS = [12, 10, 8, 7, 6, 6, 5, 5, 6, 7, 8, 10, 12]
_VOL_TOTAL = sum(_VOL_WEIGHTS)

# Keypoint positions for price path (indices into 14 boundary points)
_IDX_EXTREME1 = 3   # ~10:30 (morning extreme)
_IDX_EXTREME2 = 10  # ~14:00 (afternoon extreme)


def _synthesize_30m_from_daily(daily_df: pd.DataFrame) -> pd.DataFrame:
    """
    Generate synthetic 30-minute bars from daily OHLCV data.

    Creates 13 bars per trading day (9:30-15:30 ET).

    Price path per day:
      Bullish (close >= open): open -> dip near low -> rally near high -> close
      Bearish (close < open):  open -> rally near high -> drop near low -> close

    Extremes placed at ~10:30 and ~14:00 to approximate typical intraday
    patterns. Each bar gets small wicks beyond its open-close range so
    the pulse bar detector sees meaningful bar ranges.
    """
    rows = []

    for dt_idx, daily in daily_df.iterrows():
        day = dt_idx.date() if hasattr(dt_idx, 'date') else dt_idx
        d_open = float(daily['open'])
        d_high = float(daily['high'])
        d_low = float(daily['low'])
        d_close = float(daily['close'])
        d_vol = int(daily.get('volume', 0) or 100000)
        d_range = max(d_high - d_low, 0.01)

        bullish = d_close >= d_open

        # Build 4 keypoints: open, extreme1, extreme2, close
        if bullish:
            keypoints = [
                (0, d_open),
                (_IDX_EXTREME1, d_low + d_range * 0.05),
                (_IDX_EXTREME2, d_high - d_range * 0.05),
                (_N_BARS, d_close),
            ]
        else:
            keypoints = [
                (0, d_open),
                (_IDX_EXTREME1, d_high - d_range * 0.05),
                (_IDX_EXTREME2, d_low + d_range * 0.05),
                (_N_BARS, d_close),
            ]

        # Piecewise linear interpolation -> 14 boundary prices
        boundary = [0.0] * (_N_BARS + 1)
        for seg in range(len(keypoints) - 1):
            i1, p1 = keypoints[seg]
            i2, p2 = keypoints[seg + 1]
            for i in range(i1, i2 + 1):
                t = (i - i1) / (i2 - i1) if i2 != i1 else 0
                boundary[i] = p1 + t * (p2 - p1)

        # Build bars from boundary prices
        wick = d_range * 0.008  # small wicks for intra-bar range
        for b in range(_N_BARS):
            bar_o = boundary[b]
            bar_c = boundary[b + 1]
            bar_h = max(bar_o, bar_c) + wick
            bar_l = min(bar_o, bar_c) - wick
            # Clamp to daily extremes (allow tiny overshoot)
            bar_h = min(bar_h, d_high + d_range * 0.001)
            bar_l = max(bar_l, d_low - d_range * 0.001)

            bar_dt = datetime.combine(
                day, datetime.strptime(_BAR_TIMES[b], '%H:%M').time()
            )
            bar_dt = ET.localize(bar_dt)

            rows.append({
                'datetime': bar_dt,
                'open': round(bar_o, 2),
                'high': round(bar_h, 2),
                'low': round(bar_l, 2),
                'close': round(bar_c, 2),
                'volume': int(d_vol * _VOL_WEIGHTS[b] / _VOL_TOTAL),
            })

    df = pd.DataFrame(rows).set_index('datetime')
    logger.info(f"Synthesized {len(df)} 30-min bars from {len(daily_df)} daily bars")
    return df


def _download_daily_bars(
    start_date: date,
    end_date: date,
    cache_dir: Path,
) -> Path:
    """Download daily SPX bars from Yahoo Finance (full history available)."""
    import yfinance as yf

    csv_path = cache_dir / f'spx_daily_{start_date}_{end_date}.csv'
    if csv_path.exists():
        logger.info(f"Using cached daily SPX data: {csv_path}")
        return csv_path

    logger.info(f"Downloading SPX daily bars: {start_date} to {end_date}")
    data = yf.download(
        '^GSPC',
        start=str(start_date),
        end=str(end_date + timedelta(days=1)),
        interval='1d',
        progress=False,
    )

    if data.empty:
        raise ValueError(f"No SPX data available for {start_date} to {end_date}")

    if hasattr(data.columns, 'nlevels') and data.columns.nlevels > 1:
        data.columns = data.columns.get_level_values(0)

    data.to_csv(csv_path)
    logger.info(f"Saved {len(data)} daily bars to {csv_path}")
    return csv_path


# ---------- Public API ----------

def _official_daily(start_date: date, end_date: date, cache_dir: Path) -> pd.DataFrame:
    daily_csv = _download_daily_bars(start_date, end_date, cache_dir)
    daily_df = pd.read_csv(daily_csv, parse_dates=True, index_col=0)
    daily_df.columns = [c.lower().strip() for c in daily_df.columns]
    return daily_df


def download_spx_bars(
    start_date: date,
    end_date: date,
    cache_dir: Optional[Path] = None,
    allow_synthetic: bool = False,
    minute_getter=None,
) -> Path:
    """
    Download historical SPX 30-minute bars and save to CSV.

    Order of sources:
      1. Yahoo Finance 30-minute bars (only ~60 days of history).
      2. Real 1-minute S&P 500 bars (Dukascopy index CFD) aligned to the
         official SPX close -- see ``src/backtest/minute_data.py``. Missing
         days are skipped and listed in ``<csv>.quality.json``.
      3. Bars synthesized from daily OHLC -- ONLY when ``allow_synthetic=True``.
         Synthetic bars encode the day's outcome in their shape and must not
         be used to judge an intraday signal.

    Args:
        start_date: First date to download
        end_date: Last date to download
        cache_dir: Directory to save CSV (default: database/backtest_data/)
        allow_synthetic: Permit the synthetic fallback (off by default).
        minute_getter: Optional HTTP getter override (tests).

    Returns:
        Path to the saved CSV file

    Raises:
        ValueError: if no real data is available and synthesis is not allowed.
    """
    import yfinance as yf

    if start_date > date.today():
        raise ValueError(f"No SPX data available for future dates {start_date} to {end_date}")

    cache_dir = cache_dir or _get_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    # v2 cache name: earlier caches named spx_30m_<dates>.csv may hold
    # synthetic bars and are deliberately not reused.
    csv_path = cache_dir / f'spx_30m_v2_{start_date}_{end_date}.csv'
    synth_path = cache_dir / f'spx_30m_SYNTHETIC_{start_date}_{end_date}.csv'

    if csv_path.exists():
        logger.info(f"Using cached SPX data: {csv_path}")
        return csv_path
    if allow_synthetic and synth_path.exists():
        logger.warning(f"Using cached SYNTHETIC SPX data: {synth_path}")
        return synth_path

    logger.info(f"Downloading SPX 30-min bars: {start_date} to {end_date}")

    # yfinance limits intraday data to ~60 days per request
    # Download in chunks and concatenate
    all_frames = []
    chunk_start = start_date
    chunk_size = timedelta(days=55)

    while chunk_start <= end_date:
        chunk_end = min(chunk_start + chunk_size, end_date + timedelta(days=1))

        try:
            data = yf.download(
                '^GSPC',
                start=str(chunk_start),
                end=str(chunk_end),
                interval='30m',
                progress=False,
            )
            if not data.empty:
                # Flatten multi-level columns if present
                if hasattr(data.columns, 'nlevels') and data.columns.nlevels > 1:
                    data.columns = data.columns.get_level_values(0)
                all_frames.append(data)
                logger.info(f"  Downloaded {len(data)} bars for {chunk_start} to {chunk_end}")
        except Exception as e:
            logger.warning(f"  Download failed for {chunk_start} to {chunk_end}: {e}")

        chunk_start = chunk_end.date() if isinstance(chunk_end, datetime) else chunk_end

    if all_frames:
        df = pd.concat(all_frames)
        df = df[~df.index.duplicated(keep='first')]
        df = df.sort_index()

        requested_days = (end_date - start_date).days
        actual_days = len(set(df.index.date))
        expected_trading_days = max(1, requested_days * 252 / 365)
        coverage = actual_days / expected_trading_days

        if coverage >= 0.5:
            df.to_csv(csv_path)
            logger.info(f"Saved {len(df)} 30-min bars to {csv_path}")
            return csv_path
        logger.info(
            f"Yahoo 30-min data only covers {actual_days} of ~{int(expected_trading_days)} "
            f"expected trading days ({coverage:.0%}); loading real 1-minute data instead"
        )

    # Real 1-minute data aligned to the official close
    daily_df = _official_daily(start_date, end_date, cache_dir)
    try:
        from .minute_data import build_minute_bars, resample_30m, write_quality_report
        from src.utils.market_calendar import is_early_close
        official = {ts.date(): float(c) for ts, c in daily_df['close'].items()}
        days = sorted(d for d in official if start_date <= d <= end_date)
        kwargs = {'getter': minute_getter} if minute_getter else {}
        minutes, skipped = build_minute_bars(
            days, official, cache_dir / 'minute_usa500',
            early_closes=[d for d in days if is_early_close(d)], **kwargs)
        if len(minutes):
            bars = resample_30m(minutes)
            bars.to_csv(csv_path)
            write_quality_report(Path(str(csv_path) + '.quality.json'), days, skipped,
                                 'dukascopy USA500IDXUSD 1-min, aligned to official SPX close')
            if skipped:
                logger.warning(f"Real minute data: {len(skipped)} of {len(days)} days skipped "
                               f"(see {csv_path}.quality.json)")
            logger.info(f"Saved {len(bars)} real 30-min bars to {csv_path}")
            return csv_path
    except Exception as e:
        logger.warning(f"Real minute data unavailable: {e}")

    if not allow_synthetic:
        raise ValueError(
            f"No real intraday SPX data for {start_date} to {end_date}. Synthetic bars "
            "built from daily OHLC are refused because they leak the day's outcome; "
            "pass allow_synthetic=True only for UI demos."
        )

    logger.warning("Falling back to SYNTHETIC 30-min bars from daily OHLC (allow_synthetic=True)")
    synthetic = _synthesize_30m_from_daily(daily_df)
    synthetic.to_csv(synth_path)
    logger.info(f"Saved {len(synthetic)} synthetic 30-min bars to {synth_path}")
    return synth_path


def download_vix_daily(
    start_date: date,
    end_date: date,
    cache_dir: Optional[Path] = None,
) -> Path:
    """
    Download daily VIX close from Yahoo Finance.

    Returns:
        Path to saved CSV file
    """
    import yfinance as yf

    cache_dir = cache_dir or _get_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)

    csv_path = cache_dir / f'vix_daily_{start_date}_{end_date}.csv'

    if csv_path.exists():
        logger.info(f"Using cached VIX data: {csv_path}")
        return csv_path

    logger.info(f"Downloading VIX daily: {start_date} to {end_date}")

    data = yf.download(
        '^VIX',
        start=str(start_date),
        end=str(end_date + timedelta(days=1)),
        interval='1d',
        progress=False,
    )

    if data.empty:
        raise ValueError(f"No VIX data available for {start_date} to {end_date}")

    if hasattr(data.columns, 'nlevels') and data.columns.nlevels > 1:
        data.columns = data.columns.get_level_values(0)

    data.to_csv(csv_path)
    logger.info(f"Saved {len(data)} VIX days to {csv_path}")
    return csv_path


def load_bars_from_csv(
    csv_path: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> pd.DataFrame:
    """
    Load 30-minute bars from a CSV file.

    Expected columns: datetime (index or column), open, high, low, close, volume
    If input is finer granularity, resamples to 30-minute bars.

    Args:
        csv_path: Path to CSV file
        start_date: Optional start date filter
        end_date: Optional end date filter

    Returns:
        DataFrame with columns: open, high, low, close, volume
        Index: DatetimeIndex in ET timezone
    """
    df = pd.read_csv(csv_path, parse_dates=True, index_col=0)

    # Normalize column names to lowercase
    df.columns = [c.lower().strip() for c in df.columns]

    # Ensure index is DatetimeIndex (may fail to auto-parse with tz offsets)
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index, utc=True)
        df.index = df.index.tz_convert('America/New_York')

    # Ensure required columns exist
    required = {'open', 'high', 'low', 'close'}
    if not required.issubset(set(df.columns)):
        # Try alternate names
        rename_map = {}
        for col in df.columns:
            cl = col.lower()
            if 'open' in cl:
                rename_map[col] = 'open'
            elif 'high' in cl:
                rename_map[col] = 'high'
            elif 'low' in cl:
                rename_map[col] = 'low'
            elif 'close' in cl:
                rename_map[col] = 'close'
            elif 'vol' in cl:
                rename_map[col] = 'volume'
        df = df.rename(columns=rename_map)

    if not required.issubset(set(df.columns)):
        raise ValueError(f"CSV missing required columns. Found: {list(df.columns)}")

    if 'volume' not in df.columns:
        df['volume'] = 0

    # Ensure timezone-aware index
    if df.index.tz is None:
        df.index = df.index.tz_localize('America/New_York', ambiguous='NaT', nonexistent='shift_forward')
    else:
        df.index = df.index.tz_convert('America/New_York')

    # Drop NaT rows
    df = df[df.index.notna()]

    # Filter to market hours only (9:30 - 16:00 ET)
    df = df.between_time('09:30', '15:59')

    # Resample to 30-minute bars if needed
    freq = pd.infer_freq(df.index[:20])
    if freq and freq not in ('30T', '30min'):
        logger.info(f"Resampling from {freq} to 30-minute bars")
        df = df.resample('30T').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum',
        }).dropna(subset=['open'])

    # Apply date range filters
    if start_date:
        df = df[df.index.date >= start_date]
    if end_date:
        df = df[df.index.date <= end_date]

    df = df.sort_index()
    logger.info(f"Loaded {len(df)} bars from {csv_path}")
    return df[['open', 'high', 'low', 'close', 'volume']]


def load_vix_daily(
    csv_path: str,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> dict:
    """
    Load daily VIX closes into a date-keyed dict.

    Returns:
        {date: vix_close} mapping
    """
    df = pd.read_csv(csv_path, parse_dates=True, index_col=0)
    df.columns = [c.lower().strip() for c in df.columns]

    if df.index.tz is not None:
        df.index = df.index.tz_convert('America/New_York')
    else:
        df.index = df.index.tz_localize('America/New_York', ambiguous='NaT', nonexistent='shift_forward')

    df = df[df.index.notna()]

    if start_date:
        df = df[df.index.date >= start_date]
    if end_date:
        df = df[df.index.date <= end_date]

    vix_map = {}
    for dt, row in df.iterrows():
        vix_map[dt.date()] = float(row['close'])

    logger.info(f"Loaded {len(vix_map)} VIX daily values")
    return vix_map


def get_trading_days(df: pd.DataFrame) -> List[date]:
    """Extract unique trading days from a bar DataFrame."""
    return sorted(set(df.index.date))


def get_bars_for_day(df: pd.DataFrame, trading_day: date) -> pd.DataFrame:
    """Get all bars for a specific trading day."""
    return df[df.index.date == trading_day]
