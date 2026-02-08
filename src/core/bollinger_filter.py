"""
Tag 'n Turn Bollinger Band Directional Bias Filter

Implements the Bollinger Band bias filter from Production Line Trading (p.30-33):
- 50-period Bollinger Bands on 30-minute bars
- Lower band tag → bullish bias
- Upper band tag → bearish bias
- Bias persists until opposite band is tagged
- Pulse bar setups should only be taken if they align with current bias
"""
from __future__ import annotations

import logging
import math
from typing import Optional, Tuple
from datetime import datetime

import yfinance as yf

from ..models.spread import TradeDirection

logger = logging.getLogger(__name__)


class BollingerFilter:
    """
    50-period Bollinger Band filter for directional bias.

    When price tags the lower band, bullish bias is established.
    When price tags the upper band, bearish bias is established.
    Bias persists until the opposite band is tagged.
    """

    def __init__(self, period: int = 50, num_std: float = 2.0, extreme_move_pct: float = 1.5):
        self.period = period
        self.num_std = num_std
        self.extreme_move_pct = extreme_move_pct
        self.closes: list[float] = []
        self.current_bias: Optional[str] = None  # None, 'bullish', 'bearish'
        self.last_tag_info: Optional[dict] = None
        self.bands: Optional[dict] = None
        self.day_open: Optional[float] = None  # Set by main.py at market open

        logger.info(f"BollingerFilter initialized: period={period}, std={num_std}, extreme_move_override={extreme_move_pct}%")

    def add_bar(self, bar) -> None:
        """
        Add a completed 30-min bar and update bands/bias.

        Uses bar.high and bar.low (not just close) for band tag detection.
        """
        self.closes.append(bar.close)

        # Keep memory bounded
        max_len = self.period + 10
        if len(self.closes) > max_len:
            self.closes = self.closes[-max_len:]

        # Recalculate bands
        self.bands = self.calculate_bands()
        if self.bands is None:
            return

        upper = self.bands['upper']
        lower = self.bands['lower']

        tagged_lower = bar.low <= lower
        tagged_upper = bar.high >= upper

        new_bias = None
        tag_band = None

        if tagged_lower and tagged_upper:
            # Both tagged — use whichever the close is nearer to
            dist_lower = abs(bar.close - lower)
            dist_upper = abs(bar.close - upper)
            if dist_lower <= dist_upper:
                new_bias = 'bullish'
                tag_band = 'lower'
            else:
                new_bias = 'bearish'
                tag_band = 'upper'
        elif tagged_lower:
            new_bias = 'bullish'
            tag_band = 'lower'
        elif tagged_upper:
            new_bias = 'bearish'
            tag_band = 'upper'

        if new_bias and new_bias != self.current_bias:
            old_bias = self.current_bias or 'none'
            self.current_bias = new_bias

            bar_time = bar.timestamp.strftime('%H:%M') if bar.timestamp else 'unknown'
            self.last_tag_info = {
                'band': tag_band,
                'time': bar_time,
                'price': round(bar.close, 2),
                'band_value': round(self.bands[tag_band], 2),
                'bar_low': round(bar.low, 2),
                'bar_high': round(bar.high, 2),
            }

            logger.info(
                f"BOLLINGER BIAS CHANGE: {old_bias} → {new_bias} "
                f"(tagged {tag_band} band at {bar_time}, "
                f"price=${bar.close:,.2f}, band=${self.bands[tag_band]:,.2f})"
            )

    def calculate_bands(self) -> Optional[dict]:
        """
        Calculate Bollinger Bands from recent closes.

        Returns dict with 'upper', 'middle', 'lower' or None if insufficient data.
        """
        if len(self.closes) < self.period:
            return None

        window = self.closes[-self.period:]
        middle = sum(window) / self.period

        # Population standard deviation
        variance = sum((x - middle) ** 2 for x in window) / self.period
        std = math.sqrt(variance)

        upper = middle + (self.num_std * std)
        lower = middle - (self.num_std * std)

        return {
            'upper': upper,
            'middle': middle,
            'lower': lower,
        }

    def check_alignment(self, direction: TradeDirection, current_price: Optional[float] = None) -> Tuple[bool, str]:
        """
        Check if a trade direction aligns with the current Bollinger bias.

        Args:
            direction: The trade direction to check (BULLISH/BEARISH)
            current_price: Current SPX price for extreme move override check

        Returns (allowed, reason).
        """
        # Check for extreme move override first
        if self.extreme_move_pct > 0 and self.day_open and current_price:
            intraday_move_pct = (current_price - self.day_open) / self.day_open * 100

            # Big down day: allow bearish setups
            if intraday_move_pct <= -self.extreme_move_pct and direction.value == 'bearish':
                logger.info(
                    f"BB FILTER OVERRIDDEN: {intraday_move_pct:.2f}% intraday move "
                    f"(threshold: -{self.extreme_move_pct}%) allows BEARISH setup"
                )
                return True, (
                    f"Extreme move override: {intraday_move_pct:.2f}% down day "
                    f"allows bearish setup (BB bias: {self.current_bias or 'none'})"
                )

            # Big up day: allow bullish setups
            if intraday_move_pct >= self.extreme_move_pct and direction.value == 'bullish':
                logger.info(
                    f"BB FILTER OVERRIDDEN: +{intraday_move_pct:.2f}% intraday move "
                    f"(threshold: +{self.extreme_move_pct}%) allows BULLISH setup"
                )
                return True, (
                    f"Extreme move override: +{intraday_move_pct:.2f}% up day "
                    f"allows bullish setup (BB bias: {self.current_bias or 'none'})"
                )

        if self.current_bias is None:
            return True, "No Bollinger bias established — trade allowed"

        if direction.value == self.current_bias:
            tag_str = ""
            if self.last_tag_info:
                tag_str = (
                    f" (tagged {self.last_tag_info['band']} band "
                    f"at {self.last_tag_info['time']}, "
                    f"${self.last_tag_info['price']:,.2f})"
                )
            return True, f"Aligned with {self.current_bias} BB bias{tag_str}"

        tag_str = ""
        if self.last_tag_info:
            tag_str = (
                f" (last tag: {self.last_tag_info['band']} band "
                f"at {self.last_tag_info['time']})"
            )
        return False, (
            f"BLOCKED: {direction.value} setup conflicts with "
            f"{self.current_bias} BB bias{tag_str}"
        )

    def seed_historical(self, symbol: str = '%5EGSPC') -> int:
        """
        Seed the filter with recent 30-min bar data from yfinance.

        Returns the number of bars loaded.
        """
        try:
            logger.info(f"Seeding Bollinger filter with historical data for {symbol}...")
            data = yf.download(symbol, period='10d', interval='30m', progress=False)

            if data.empty:
                logger.warning("No historical data returned from yfinance")
                return 0

            # Flatten multi-level columns if present (yfinance returns ('Close', '^GSPC'))
            if hasattr(data.columns, 'nlevels') and data.columns.nlevels > 1:
                data.columns = data.columns.get_level_values(0)

            count = 0
            for _, row in data.iterrows():
                self.closes.append(float(row['Close']))
                count += 1

            # Trim to max length
            max_len = self.period + 10
            if len(self.closes) > max_len:
                self.closes = self.closes[-max_len:]

            # Calculate bands
            self.bands = self.calculate_bands()

            # Check last 10 bars for recent tags to establish bias
            if self.bands and len(data) >= 10:
                recent = data.tail(10)
                for _, row in recent.iterrows():
                    low = float(row['Low'])
                    high = float(row['High'])
                    close = float(row['Close'])

                    tagged_lower = low <= self.bands['lower']
                    tagged_upper = high >= self.bands['upper']

                    if tagged_lower and tagged_upper:
                        dist_lower = abs(close - self.bands['lower'])
                        dist_upper = abs(close - self.bands['upper'])
                        if dist_lower <= dist_upper:
                            self.current_bias = 'bullish'
                        else:
                            self.current_bias = 'bearish'
                    elif tagged_lower:
                        self.current_bias = 'bullish'
                    elif tagged_upper:
                        self.current_bias = 'bearish'

                if self.current_bias:
                    logger.info(
                        f"Historical seed established {self.current_bias} bias "
                        f"from recent band tags"
                    )

            logger.info(
                f"Bollinger filter seeded with {count} bars "
                f"(bands={'calculated' if self.bands else 'insufficient data'})"
            )
            return count

        except Exception as e:
            logger.error(f"Failed to seed Bollinger filter: {e}", exc_info=True)
            return 0

    @property
    def has_sufficient_data(self) -> bool:
        """Whether we have enough bars to calculate bands."""
        return len(self.closes) >= self.period

    def set_day_open(self, price: float) -> None:
        """Set the day's opening price for extreme move override calculation."""
        self.day_open = price
        logger.info(f"Day open price set: ${price:,.2f}")

    def get_status(self) -> dict:
        """Return current filter status for dashboard/logging."""
        bands_rounded = None
        if self.bands:
            bands_rounded = {
                'upper': round(self.bands['upper'], 2),
                'middle': round(self.bands['middle'], 2),
                'lower': round(self.bands['lower'], 2),
            }

        return {
            'has_data': self.has_sufficient_data,
            'bar_count': len(self.closes),
            'current_bias': self.current_bias,
            'bands': bands_rounded,
            'last_tag_info': self.last_tag_info,
            'day_open': round(self.day_open, 2) if self.day_open else None,
            'extreme_move_pct': self.extreme_move_pct,
        }
