"""
Historical SPX Data Loader for Backtesting

Loads historical 30-minute SPX bars from CSV or downloads them from
Yahoo Finance. Also fetches daily VIX close for IV proxy in option pricing.
"""

import logging
import os
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional, List, Tuple

import pandas as pd
import pytz

logger = logging.getLogger(__name__)

ET = pytz.timezone('America/New_York')

# Default cache directory
CACHE_DIR = Path(__file__).parent.parent.parent / 'database' / 'backtest_data'


def download_spx_bars(
    start_date: date,
    end_date: date,
    cache_dir: Optional[Path] = None,
) -> Path:
    """
    Download historical SPX 30-minute bars from Yahoo Finance and save to CSV.

    Args:
        start_date: First date to download
        end_date: Last date to download
        cache_dir: Directory to save CSV (default: database/backtest_data/)

    Returns:
        Path to the saved CSV file
    """
    import yfinance as yf

    cache_dir = cache_dir or CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)

    csv_path = cache_dir / f'spx_30m_{start_date}_{end_date}.csv'

    if csv_path.exists():
        logger.info(f"Using cached SPX data: {csv_path}")
        return csv_path

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

    if not all_frames:
        raise ValueError(f"No SPX data available for {start_date} to {end_date}")

    df = pd.concat(all_frames)
    df = df[~df.index.duplicated(keep='first')]
    df = df.sort_index()

    # Save with standard column names
    df.to_csv(csv_path)
    logger.info(f"Saved {len(df)} bars to {csv_path}")
    return csv_path


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

    cache_dir = cache_dir or CACHE_DIR
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
