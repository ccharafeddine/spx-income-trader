"""
Yahoo Finance Market Data Provider

Provides real-time (15-min delayed) SPX quotes from Yahoo Finance.
Used for strategy simulation with real market data.

Note: Yahoo Finance has rate limits. This provider includes:
- Request caching (30 second minimum between API calls)
- Fallback to direct HTTP when yfinance fails
- Graceful degradation when rate limited
"""

import logging
import time
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple
import pytz
import requests

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False
    yf = None

logger = logging.getLogger(__name__)


class YahooFinanceProvider:
    """
    Fetches real market data from Yahoo Finance.

    SPX symbols:
    - ^GSPC: S&P 500 Index (equivalent to SPX)
    - ^SPX: Alternative SPX symbol
    - ^VIX: Volatility Index (useful for context)
    """

    # Yahoo Finance symbols
    SPX_SYMBOL = "^GSPC"  # S&P 500 Index
    VIX_SYMBOL = "^VIX"   # Volatility Index

    def __init__(self):
        self.tz = pytz.timezone('US/Eastern')
        self._cache: Dict[str, Tuple[datetime, dict]] = {}
        self._cache_duration = timedelta(seconds=60)  # Cache for 60 seconds to avoid rate limits
        self._last_request_time = 0
        self._min_request_interval = 5  # Minimum 5 seconds between requests

        # HTTP session for direct API calls
        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })

    def get_spx_quote(self) -> Optional[Dict]:
        """
        Get current SPX quote data.

        Returns dict with:
        - price: Current/last price
        - open: Day's open
        - high: Day's high
        - low: Day's low
        - previous_close: Previous day's close
        - change: Dollar change from previous close
        - change_pct: Percent change from previous close
        - volume: Trading volume
        - timestamp: Quote timestamp
        """
        return self._get_quote(self.SPX_SYMBOL)

    def get_vix_quote(self) -> Optional[Dict]:
        """Get current VIX quote for volatility context."""
        return self._get_quote(self.VIX_SYMBOL)

    def _get_quote(self, symbol: str) -> Optional[Dict]:
        """Fetch quote for symbol with caching."""
        now = datetime.now(self.tz)

        # Check cache first
        if symbol in self._cache:
            cache_time, cache_data = self._cache[symbol]
            if now - cache_time < self._cache_duration:
                logger.debug(f"Using cached data for {symbol}")
                return cache_data

        # Rate limiting
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_request_interval:
            wait_time = self._min_request_interval - elapsed
            logger.debug(f"Rate limiting: waiting {wait_time:.1f}s")
            time.sleep(wait_time)

        self._last_request_time = time.time()

        # Try direct HTTP API first (more reliable)
        quote_data = self._fetch_direct_http(symbol)

        # Fallback to yfinance if direct HTTP fails
        if quote_data is None and YFINANCE_AVAILABLE:
            quote_data = self._fetch_via_yfinance(symbol)

        if quote_data:
            # Cache the result
            self._cache[symbol] = (now, quote_data)

        return quote_data

    def _fetch_direct_http(self, symbol: str) -> Optional[Dict]:
        """Fetch quote using direct HTTP request to Yahoo Finance API."""
        try:
            # Yahoo Finance chart API
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            params = {
                'interval': '1d',
                'range': '5d'
            }

            response = self._session.get(url, params=params, timeout=10)

            if response.status_code == 429:
                logger.warning("Yahoo Finance rate limited (429). Using cached data if available.")
                return None

            if response.status_code != 200:
                logger.debug(f"HTTP {response.status_code} for {symbol}")
                return None

            data = response.json()

            if 'chart' not in data or 'result' not in data['chart']:
                return None

            result = data['chart']['result']
            if not result:
                return None

            chart = result[0]
            meta = chart.get('meta', {})
            quotes = chart.get('indicators', {}).get('quote', [{}])[0]

            # Extract data
            closes = quotes.get('close', [])
            opens = quotes.get('open', [])
            highs = quotes.get('high', [])
            lows = quotes.get('low', [])
            volumes = quotes.get('volume', [])

            if not closes or all(c is None for c in closes):
                return None

            # Get last valid values
            price = None
            for c in reversed(closes):
                if c is not None:
                    price = float(c)
                    break

            if price is None:
                return None

            # Find last valid index
            last_idx = len(closes) - 1
            while last_idx >= 0 and closes[last_idx] is None:
                last_idx -= 1

            open_price = float(opens[last_idx]) if last_idx >= 0 and opens[last_idx] else 0
            high_price = float(highs[last_idx]) if last_idx >= 0 and highs[last_idx] else 0
            low_price = float(lows[last_idx]) if last_idx >= 0 and lows[last_idx] else 0
            volume = int(volumes[last_idx]) if last_idx >= 0 and volumes[last_idx] else 0

            # Previous close
            prev_close = float(meta.get('chartPreviousClose', 0) or meta.get('previousClose', 0))
            if prev_close == 0 and last_idx > 0:
                for i in range(last_idx - 1, -1, -1):
                    if closes[i] is not None:
                        prev_close = float(closes[i])
                        break

            now = datetime.now(self.tz)

            # Extract market time from Yahoo's response for freshness checks
            market_time_epoch = meta.get('regularMarketTime', 0)
            market_time_dt = None
            if market_time_epoch:
                market_time_dt = datetime.fromtimestamp(market_time_epoch, tz=self.tz)

            quote_data = {
                'symbol': symbol,
                'price': price,
                'open': open_price,
                'high': high_price,
                'low': low_price,
                'previous_close': prev_close,
                'volume': volume,
                'timestamp': now.isoformat(),
                'market_time': market_time_dt.isoformat() if market_time_dt else None,
                'market_time_epoch': market_time_epoch
            }

            # Calculate change
            if prev_close > 0:
                quote_data['change'] = price - prev_close
                quote_data['change_pct'] = (quote_data['change'] / prev_close) * 100
            else:
                quote_data['change'] = 0
                quote_data['change_pct'] = 0

            return quote_data

        except requests.exceptions.RequestException as e:
            logger.debug(f"HTTP request failed for {symbol}: {e}")
            return None
        except Exception as e:
            logger.debug(f"Error parsing response for {symbol}: {e}")
            return None

    def _fetch_via_yfinance(self, symbol: str) -> Optional[Dict]:
        """Fallback: fetch quote using yfinance library."""
        try:
            ticker = yf.Ticker(symbol)

            price = None
            open_price = 0
            high_price = 0
            low_price = 0
            prev_close = 0
            volume = 0

            try:
                hist = ticker.history(period="5d", interval="1d")
                if not hist.empty:
                    last_row = hist.iloc[-1]
                    price = float(last_row['Close'])
                    open_price = float(last_row['Open'])
                    high_price = float(last_row['High'])
                    low_price = float(last_row['Low'])
                    volume = int(last_row['Volume'])

                    if len(hist) > 1:
                        prev_close = float(hist.iloc[-2]['Close'])
                    else:
                        prev_close = open_price
            except Exception as e:
                logger.debug(f"yfinance history failed for {symbol}: {e}")

            if price is None:
                return None

            now = datetime.now(self.tz)
            quote_data = {
                'symbol': symbol,
                'price': price,
                'open': open_price,
                'high': high_price,
                'low': low_price,
                'previous_close': prev_close,
                'volume': volume,
                'timestamp': now.isoformat(),
                'market_time': None,
                'market_time_epoch': 0
            }

            if prev_close > 0:
                quote_data['change'] = price - prev_close
                quote_data['change_pct'] = (quote_data['change'] / prev_close) * 100
            else:
                quote_data['change'] = 0
                quote_data['change_pct'] = 0

            return quote_data

        except Exception as e:
            logger.debug(f"yfinance failed for {symbol}: {e}")
            return None

    def get_spx_price(self) -> Optional[float]:
        """Get just the current SPX price."""
        quote = self.get_spx_quote()
        return quote['price'] if quote else None

    def get_intraday_bars(self, interval: str = "5m", period: str = "1d") -> Optional[list]:
        """
        Get intraday bar data for SPX.

        Args:
            interval: Bar interval (1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h)
            period: Time period (1d, 5d, 1mo)

        Returns:
            List of bar dicts with: timestamp, open, high, low, close, volume
        """
        if not YFINANCE_AVAILABLE:
            logger.warning("yfinance not installed - intraday bars unavailable")
            return None

        # Check cache (same pattern as _get_quote)
        cache_key = f"intraday_{interval}_{period}"
        now = datetime.now(self.tz)
        if cache_key in self._cache:
            cache_time, cache_data = self._cache[cache_key]
            if now - cache_time < self._cache_duration:
                logger.debug(f"Using cached intraday bars ({interval}/{period})")
                return cache_data

        try:
            ticker = yf.Ticker(self.SPX_SYMBOL)
            hist = ticker.history(period=period, interval=interval, timeout=5)

            if hist is None or hist.empty:
                # Cache empty result briefly (30s) to avoid hammering Yahoo
                self._cache[cache_key] = (now, None)
                return None

            bars = []
            for idx, row in hist.iterrows():
                try:
                    bars.append({
                        'timestamp': idx.to_pydatetime(),
                        'open': float(row['Open']),
                        'high': float(row['High']),
                        'low': float(row['Low']),
                        'close': float(row['Close']),
                        'volume': int(row.get('Volume', 0))
                    })
                except (KeyError, TypeError, ValueError):
                    continue  # Skip malformed rows

            self._cache[cache_key] = (now, bars)
            return bars

        except Exception as e:
            logger.error(f"Error fetching intraday bars: {e}")
            # Cache failure briefly to avoid repeated slow calls
            self._cache[cache_key] = (now, None)
            return None

    def is_market_open(self) -> bool:
        """Check if US market is currently open."""
        now = datetime.now(self.tz)

        # Weekend check
        if now.weekday() >= 5:
            return False

        # Market hours: 9:30 AM - 4:00 PM ET
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)

        return market_open <= now <= market_close


# Convenience function
def get_spx_price() -> Optional[float]:
    """Quick helper to get current SPX price."""
    try:
        provider = YahooFinanceProvider()
        return provider.get_spx_price()
    except Exception as e:
        logger.error(f"Failed to get SPX price: {e}")
        return None


if __name__ == "__main__":
    # Quick test
    logging.basicConfig(level=logging.INFO)

    print("Testing Yahoo Finance Provider...")
    print()

    try:
        provider = YahooFinanceProvider()

        # SPX quote
        quote = provider.get_spx_quote()
        if quote:
            print(f"SPX Quote:")
            print(f"  Price:     ${quote['price']:,.2f}")
            print(f"  Open:      ${quote['open']:,.2f}")
            print(f"  High:      ${quote['high']:,.2f}")
            print(f"  Low:       ${quote['low']:,.2f}")
            print(f"  Change:    ${quote['change']:+,.2f} ({quote['change_pct']:+.2f}%)")
            print(f"  Timestamp: {quote['timestamp']}")
        else:
            print("Could not fetch SPX quote")

        print()

        # VIX quote
        vix = provider.get_vix_quote()
        if vix:
            print(f"VIX: {vix['price']:.2f} ({vix['change_pct']:+.2f}%)")

        print()
        print(f"Market Open: {provider.is_market_open()}")

    except ImportError as e:
        print(f"Error: {e}")
        print("Install yfinance: pip install yfinance")
