"""
Backtest Broker - Simulated broker for backtesting

Implements BrokerInterface with instant fills at theoretical prices.
Uses the same Black-Scholes-ish pricing from DryRunBroker for synthetic
options chains, but driven by historical data instead of live feeds.
"""

import logging
import math
from datetime import datetime, date
from typing import Dict, Optional


def _norm_cdf(x: float) -> float:
    """Standard normal CDF (Abramowitz & Stegun approximation)."""
    if x < -10:
        return 0.0
    if x > 10:
        return 1.0
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    d = 0.3989422804014327  # 1 / sqrt(2 * pi)
    p = d * math.exp(-0.5 * x * x) * (
        t * (0.319381530 + t * (-0.356563782 + t * (
            1.781477937 + t * (-1.821255978 + t * 1.330274429))))
    )
    return 1.0 - p if x >= 0 else p


def _bs_price(S: float, K: float, T: float, sigma: float,
              option_type: str = 'put') -> float:
    """Black-Scholes price for a European option (no dividends, r=0)."""
    if T <= 0 or sigma <= 0:
        if option_type == 'call':
            return max(0.0, S - K)
        return max(0.0, K - S)

    sqrt_T = math.sqrt(T)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T

    if option_type == 'call':
        return S * _norm_cdf(d1) - K * _norm_cdf(d2)
    return K * _norm_cdf(-d2) - S * _norm_cdf(-d1)

import pytz

# ---------------------------------------------------------------------------
# 0DTE pricing model (see docs/strategy_review.md for calibration)
# ---------------------------------------------------------------------------
TRADING_MINUTES_PER_YEAR = 390 * 252
# ATM 0DTE implied vol as a fraction of VIX. Median VIX1D/VIX at the open
# (2023-2026) is 0.614 and realised 10:30->close variance implies 0.617.
VIX_TO_0DTE_IV = 0.62
# Put-skew smile in standardised moneyness z = ln(K/S) / (sigma_atm * sqrt(T)):
#   sigma(K) = sigma_atm * (1 - b*z + c*z^2), c = c_put for z < 0 else c_call,
# with the wings soft-capped at ~1.7x ATM. ATM put spreads price ~$0.25
# below ATM call spreads, consistent with live fills.
SKEW_B, SKEW_C_PUT, SKEW_C_CALL = 0.06, 0.08, 0.02


def smile_vol(K: float, S: float, sigma_atm: float, T: float,
              b: float = SKEW_B, c_put: float = SKEW_C_PUT, c_call: float = SKEW_C_CALL) -> float:
    """Implied vol for strike K under the parametric put-skew smile."""
    if T <= 0 or sigma_atm <= 0:
        return sigma_atm
    z = math.log(K / S) / (sigma_atm * math.sqrt(T))
    z = max(-4.0, min(4.0, z))
    c = c_put if z < 0 else c_call
    m = 1 - b * z + c * z * z
    m = 1 + 0.7 * math.tanh((m - 1) / 0.7) if m > 1 else max(m, 0.7)
    return sigma_atm * m


def zero_dte_atm_vol(vix: float, vix1d: Optional[float] = None) -> float:
    """ATM implied vol for a same-day option: VIX1D when known, else 0.62 x VIX."""
    if vix1d and vix1d > 0:
        return vix1d / 100.0
    return VIX_TO_0DTE_IV * vix / 100.0

from src.brokers.base import BrokerInterface
from src.brokers.dry_run_broker import BidAskModel
from src.models.spread import CreditSpread, TradeDirection

logger = logging.getLogger(__name__)

ET = pytz.timezone('America/New_York')


class BacktestBroker(BrokerInterface):
    """
    Simulated broker for backtesting.

    - get_current_price() returns the current bar's close
    - get_options_chain() generates synthetic chain using Black-Scholes
      with that day's VIX as IV
    - place_spread_order() fills instantly at theoretical mid minus slippage
    - Tracks simulated account balance
    """

    def __init__(
        self,
        initial_capital: float = 50000.0,
        slippage: Optional[float] = None,
        fill_quality_factor: float = 1.0,
    ):
        self.initial_capital = initial_capital
        self.balance = initial_capital
        self._flat_slippage = slippage  # None = use BidAskModel
        self._bid_ask_model = BidAskModel()
        self._fill_quality_factor = fill_quality_factor

        # Current market state (set externally by engine each bar)
        self._current_price: float = 0.0
        self._current_bar_high: float = 0.0
        self._current_bar_low: float = 0.0
        self._current_vix: float = 15.0
        self._current_dt: Optional[datetime] = None
        self._current_dte: float = 0.01
        self._current_vix1d: Optional[float] = None
        self._close_hour, self._close_minute = 16, 0  # 13:00 on half days

        # Order tracking
        self._order_counter = 0
        self._orders: Dict[str, Dict] = {}

        # Options chain parameters
        self.strike_interval = 5
        self.bid_ask_spread = 0.20

    def set_market_state(
        self,
        price: float,
        bar_high: float,
        bar_low: float,
        vix: float,
        dt: datetime,
        dte: float = 0.01,
        vix1d: Optional[float] = None,
        close_time=None,
    ):
        """Update current market state (called by engine each bar)."""
        self._current_vix1d = vix1d
        if close_time is not None:
            self._close_hour, self._close_minute = close_time.hour, close_time.minute
        self._current_price = price
        self._current_bar_high = bar_high
        self._current_bar_low = bar_low
        self._current_vix = vix
        self._current_dt = dt
        self._current_dte = dte

    def _get_slippage(self) -> float:
        """Return total per-contract slippage (both legs combined).

        Flat override: used as-is (backward compat).
        BidAskModel: per-leg value * 2 (short + long leg), using VIX + time.
        """
        if self._flat_slippage is not None:
            return self._flat_slippage

        vix = self._current_vix
        if self._current_dt:
            hour = self._current_dt.hour
            minute = self._current_dt.minute
        else:
            hour, minute = 10, 30  # default to morning if no dt
        per_leg = self._bid_ask_model.get_spread(vix, hour, minute)
        return per_leg * 2

    def get_current_price(self, symbol: str) -> float:
        """Return the current bar's close price."""
        return self._current_price

    def get_options_chain(self, symbol: str, expiration: str) -> Dict[float, Dict]:
        """
        Generate synthetic options chain using Black-Scholes pricing.

        Uses VIX as implied volatility and proper BS math so that
        time value decays realistically for 0DTE positions.
        """
        current_price = self._current_price
        if current_price <= 0:
            return {}

        dte = self._current_dte
        zero_dte = dte < 1

        if zero_dte:
            # Same-day expiry: time in TRADING minutes to the close, ATM vol from
            # VIX1D (or 0.62 x VIX), and a put-skew smile. Earlier versions used
            # calendar hours / 24 / 365 with flat VIX vol, which understated the
            # remaining-session move ~2x and made spreads decay too fast.
            minutes_left = 0.25
            if self._current_dt:
                close = self._current_dt.replace(hour=self._close_hour, minute=self._close_minute,
                                                 second=0, microsecond=0)
                if self._current_dt < close:
                    minutes_left = max(0.25, (close - self._current_dt).total_seconds() / 60.0)
            T = minutes_left / TRADING_MINUTES_PER_YEAR
            atm_vol = zero_dte_atm_vol(self._current_vix, self._current_vix1d)
        else:
            # Multi-day (swing) options: calendar time, VIX as IV, flat (unchanged)
            T = dte / 365.0
            atm_vol = self._current_vix / 100.0

        # Generate strikes around ATM
        atm_strike = round(current_price / self.strike_interval) * self.strike_interval
        strikes = [atm_strike + (i * self.strike_interval) for i in range(-20, 21)]

        chain = {}
        half_spread = self.bid_ask_spread / 2

        for strike in strikes:
            vol = smile_vol(strike, current_price, atm_vol, T) if zero_dte else atm_vol
            call_mid = _bs_price(current_price, strike, T, vol, 'call')
            put_mid = _bs_price(current_price, strike, T, vol, 'put')

            chain[strike] = {
                'call_bid': max(0.05, call_mid - half_spread),
                'call_ask': call_mid + half_spread,
                'call_last': call_mid,
                'call_volume': 100,
                'call_oi': 500,
                'put_bid': max(0.05, put_mid - half_spread),
                'put_ask': put_mid + half_spread,
                'put_last': put_mid,
                'put_volume': 100,
                'put_oi': 500,
            }

        return chain

    def place_spread_order(
        self,
        spread: CreditSpread,
        quantity: int,
        limit_price: Optional[float] = None,
        metadata: Optional[Dict] = None,
    ) -> str:
        """Fill spread order instantly at credit minus slippage."""
        self._order_counter += 1
        order_id = f"BT-{self._order_counter}"

        fill_price = limit_price if limit_price else spread.credit_received
        # Apply slippage (total for both legs)
        fill_price = max(0.01, fill_price - self._get_slippage())
        # Apply fill quality factor (entry credit only, not exits)
        fill_price = max(0.01, fill_price * self._fill_quality_factor)

        credit = fill_price * quantity * 100
        self.balance += credit

        self._orders[order_id] = {
            'status': 'filled',
            'fill_price': fill_price,
            'filled_quantity': quantity,
            'action': 'open',
        }

        return order_id

    def get_order_status(self, order_id: str) -> Dict:
        """Return fill status for an order."""
        if order_id in self._orders:
            o = self._orders[order_id]
            return {
                'status': o['status'],
                'fill_price': o['fill_price'],
                'filled_quantity': o['filled_quantity'],
            }
        return {'status': 'not_found', 'fill_price': 0, 'filled_quantity': 0}

    def close_spread(
        self,
        spread: CreditSpread,
        quantity: int,
        limit_price: float = 0.20,
    ) -> str:
        """Close spread instantly at debit plus slippage."""
        self._order_counter += 1
        order_id = f"BT-CLOSE-{self._order_counter}"

        # Apply slippage (total for both legs)
        fill_price = limit_price + self._get_slippage()

        debit = fill_price * quantity * 100
        self.balance -= debit

        self._orders[order_id] = {
            'status': 'filled',
            'fill_price': fill_price,
            'filled_quantity': quantity,
            'action': 'close',
        }

        return order_id

    def get_position_value(self, spread: CreditSpread) -> float:
        """Get cost to close using Black-Scholes mid-prices.

        Uses mid-prices (not ask-side) because the BidAskModel already
        applies slippage separately at entry and exit execution.  Using
        ask-side here would double-count the bid-ask spread.

        Returns per-share price (not multiplied by 100).
        """
        try:
            expiration = spread.expiration.strftime('%Y-%m-%d')
            chain = self.get_options_chain('SPX', expiration)

            short_strike = spread.short_leg.strike
            long_strike = spread.long_leg.strike

            if chain and short_strike in chain and long_strike in chain:
                if spread.direction == TradeDirection.BULLISH:
                    short_mid = chain[short_strike]['put_last']
                    long_mid = chain[long_strike]['put_last']
                else:
                    short_mid = chain[short_strike]['call_last']
                    long_mid = chain[long_strike]['call_last']

                return max(0.0, short_mid - long_mid)
        except Exception:
            pass

        # Fallback: intrinsic value
        current_price = self._current_price
        if current_price <= 0:
            return 0

        if spread.direction == TradeDirection.BULLISH:
            if current_price >= spread.short_leg.strike:
                return 0
            elif current_price <= spread.long_leg.strike:
                return spread.spread_width
            else:
                return spread.short_leg.strike - current_price
        else:
            if current_price <= spread.short_leg.strike:
                return 0
            elif current_price >= spread.long_leg.strike:
                return spread.spread_width
            else:
                return current_price - spread.short_leg.strike

    def get_account_balance(self) -> Dict:
        """Return simulated account balance."""
        return {
            'net_account_value': self.balance,
            'cash_available': self.balance,
            'buying_power': self.balance * 4,
        }
