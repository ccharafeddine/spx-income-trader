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

import pytz

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
    ):
        """Update current market state (called by engine each bar)."""
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
        Generate synthetic options chain using Black-Scholes approximation.

        Uses the same logic as DryRunBroker._build_synthetic_chain() but
        with VIX from historical data instead of live feed.
        """
        current_price = self._current_price
        if current_price <= 0:
            return {}

        implied_vol = self._current_vix / 100.0
        dte = self._current_dte

        # For 0DTE, use fraction of day remaining
        if dte < 1:
            if self._current_dt:
                market_close = self._current_dt.replace(hour=16, minute=0, second=0)
                if self._current_dt < market_close:
                    hours_left = (market_close - self._current_dt).total_seconds() / 3600
                    dte = max(hours_left / 24, 0.01)
                else:
                    dte = 0.01

        # Generate strikes around ATM
        atm_strike = round(current_price / self.strike_interval) * self.strike_interval
        strikes = [atm_strike + (i * self.strike_interval) for i in range(-20, 21)]

        chain = {}
        half_spread = self.bid_ask_spread / 2

        for strike in strikes:
            moneyness = (current_price - strike) / current_price

            call_intrinsic = max(0, current_price - strike)
            put_intrinsic = max(0, strike - current_price)

            time_factor = (max(dte, 0) ** 0.5) * implied_vol * current_price * 0.4
            atm_factor = 1 - min(abs(moneyness) * 5, 0.9)
            time_value = time_factor * atm_factor

            call_mid = call_intrinsic + time_value
            put_mid = put_intrinsic + time_value

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
        """Get current intrinsic value of spread based on bar price."""
        current_price = self._current_price
        if current_price <= 0:
            return 0

        # Same logic as DryRunBroker - returns per-share (not *100)
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
