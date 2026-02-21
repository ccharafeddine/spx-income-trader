"""
Black-Scholes Greeks calculator for SPX credit spread portfolios.

Provides single-leg, spread-level, and portfolio-level Greeks computation
for real-time risk analytics display on the dashboard.
"""

import math
import logging
from datetime import datetime

import pytz

logger = logging.getLogger(__name__)

ET = pytz.timezone('America/New_York')

# Floor for DTE to prevent division by zero in Black-Scholes
MIN_DTE_YEARS = 1e-6
# Floor for IV to prevent division by zero (VIX=0 would cause ZeroDivisionError)
MIN_IV = 0.01

_SQRT_2 = math.sqrt(2)
_SQRT_2PI = math.sqrt(2 * math.pi)


def _norm_cdf(x):
    """Standard normal CDF using math.erf (identical precision to scipy.stats._norm_cdf)."""
    return 0.5 * (1.0 + math.erf(x / _SQRT_2))


def _norm_pdf(x):
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / _SQRT_2PI


class GreeksCalculator:
    """Compute Black-Scholes Greeks for options and credit spread portfolios."""

    def __init__(self, risk_free_rate=0.05):
        self.risk_free_rate = risk_free_rate

    def _d1_d2(self, spot, strike, dte_years, iv):
        """Compute d1 and d2 for Black-Scholes formula."""
        t = max(dte_years, MIN_DTE_YEARS)
        iv = max(iv, MIN_IV)
        sqrt_t = math.sqrt(t)
        d1 = (math.log(spot / strike) + (self.risk_free_rate + 0.5 * iv ** 2) * t) / (iv * sqrt_t)
        d2 = d1 - iv * sqrt_t
        return d1, d2

    def calculate_greeks(self, spot, strike, dte_years, iv, option_type='put'):
        """
        Calculate Black-Scholes Greeks for a single option leg.

        Args:
            spot: Current underlying price (SPX)
            strike: Option strike price
            dte_years: Time to expiration in years
            iv: Implied volatility (decimal, e.g. 0.20 for 20%)
            option_type: 'call' or 'put'

        Returns:
            dict with delta, gamma, theta, vega, rho
        """
        t = max(dte_years, MIN_DTE_YEARS)
        iv = max(iv, MIN_IV)
        d1, d2 = self._d1_d2(spot, strike, t, iv)
        sqrt_t = math.sqrt(t)
        discount = math.exp(-self.risk_free_rate * t)

        # Gamma and Vega are the same for calls and puts
        gamma = _norm_pdf(d1) / (spot * iv * sqrt_t)
        # Vega per 1% IV change (divide standard vega by 100)
        vega = spot * _norm_pdf(d1) * sqrt_t / 100.0

        if option_type == 'call':
            delta = _norm_cdf(d1)
            theta = (
                (-spot * _norm_pdf(d1) * iv / (2 * sqrt_t))
                - self.risk_free_rate * strike * discount * _norm_cdf(d2)
            ) / 365.0  # per calendar day
            rho = strike * t * discount * _norm_cdf(d2) / 100.0
        else:  # put
            delta = _norm_cdf(d1) - 1.0
            theta = (
                (-spot * _norm_pdf(d1) * iv / (2 * sqrt_t))
                + self.risk_free_rate * strike * discount * _norm_cdf(-d2)
            ) / 365.0  # per calendar day
            rho = -strike * t * discount * _norm_cdf(-d2) / 100.0

        return {
            'delta': delta,
            'gamma': gamma,
            'theta': theta,
            'vega': vega,
            'rho': rho,
        }

    def calculate_spread_greeks(self, spot, short_strike, long_strike, dte_years, iv,
                                spread_type='put_credit', quantity=1):
        """
        Calculate net Greeks for a credit spread.

        For a put credit spread (bullish): short higher-strike put, long lower-strike put.
        For a call credit spread (bearish): short lower-strike call, long higher-strike call.

        Net = (short_greeks - long_greeks) * quantity * 100 (contract multiplier)

        Args:
            spot: Current SPX price
            short_strike: Strike of the short leg
            long_strike: Strike of the long leg
            dte_years: Time to expiration in years
            iv: Implied volatility (decimal)
            spread_type: 'put_credit' or 'call_credit'
            quantity: Number of spread contracts

        Returns:
            dict with net delta, gamma, theta, vega
        """
        option_type = 'put' if spread_type == 'put_credit' else 'call'

        short_greeks = self.calculate_greeks(spot, short_strike, dte_years, iv, option_type)
        long_greeks = self.calculate_greeks(spot, long_strike, dte_years, iv, option_type)

        multiplier = quantity * 100

        # Net = (long_greeks - short_greeks) * multiplier
        # We are SHORT the short leg and LONG the long leg
        return {
            'delta': (long_greeks['delta'] - short_greeks['delta']) * multiplier,
            'gamma': (long_greeks['gamma'] - short_greeks['gamma']) * multiplier,
            'theta': (long_greeks['theta'] - short_greeks['theta']) * multiplier,
            'vega': (long_greeks['vega'] - short_greeks['vega']) * multiplier,
        }

    def calculate_portfolio_greeks(self, positions, spot_price, vix):
        """
        Aggregate Greeks across all open positions.

        Args:
            positions: List of position dicts from classify_trades()
                       Each has: short_strike, long_strike, quantity, direction, expiration
            spot_price: Current SPX price
            vix: Current VIX level (e.g. 19.06)

        Returns:
            dict with net_delta, net_gamma, net_theta, net_vega, theta_per_hour,
                 position_count, interpretation, iv_used
        """
        if not positions or not spot_price or not vix:
            return self._empty_result()

        iv = vix / 100.0
        net_delta = 0.0
        net_gamma = 0.0
        net_theta = 0.0
        net_vega = 0.0
        counted = 0

        for pos in positions:
            dte_years = self._compute_dte_years(pos)
            if dte_years is None:
                continue

            short_strike = pos.get('short_strike')
            long_strike = pos.get('long_strike')
            quantity = pos.get('quantity', 1)
            direction = pos.get('direction', 'bullish')

            if not short_strike or not long_strike:
                continue

            spread_type = 'put_credit' if direction == 'bullish' else 'call_credit'

            spread_greeks = self.calculate_spread_greeks(
                spot_price, short_strike, long_strike, dte_years, iv,
                spread_type=spread_type, quantity=quantity
            )

            net_delta += spread_greeks['delta']
            net_gamma += spread_greeks['gamma']
            net_theta += spread_greeks['theta']
            net_vega += spread_greeks['vega']
            counted += 1

        if counted == 0:
            return self._empty_result()

        # Trading hours per day for theta_per_hour
        trading_hours = 6.5
        theta_per_hour = net_theta / trading_hours

        interpretation = self._build_interpretation(net_delta, theta_per_hour, True)

        return {
            'has_positions': True,
            'net_delta': round(net_delta, 4),
            'net_gamma': round(net_gamma, 4),
            'net_theta': round(net_theta, 4),
            'net_vega': round(net_vega, 4),
            'theta_per_hour': round(theta_per_hour, 4),
            'position_count': counted,
            'interpretation': interpretation,
            'iv_used': round(iv, 4),
        }

    def _compute_dte_years(self, position):
        """Parse expiration from a position dict and compute remaining time in years."""
        exp_str = position.get('expiration')
        if not exp_str:
            return None

        try:
            exp_str = str(exp_str).strip()
            dt = None

            # Try fromisoformat first
            try:
                dt = datetime.fromisoformat(exp_str)
            except ValueError:
                pass

            # Try date-only
            if dt is None and len(exp_str) >= 10:
                try:
                    dt = datetime.strptime(exp_str[:10], '%Y-%m-%d')
                except ValueError:
                    pass

            if dt is None:
                return None

            # Ensure timezone-aware
            if dt.tzinfo is None:
                dt = ET.localize(dt)

            # If midnight (date-only), set to 4 PM ET (market close)
            if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
                dt = dt.replace(hour=16, minute=0, second=0)

            now = datetime.now(ET)
            remaining_seconds = (dt - now).total_seconds()

            if remaining_seconds <= 0:
                # Already expired, use epsilon
                return MIN_DTE_YEARS

            seconds_per_year = 365.25 * 24 * 3600
            return max(remaining_seconds / seconds_per_year, MIN_DTE_YEARS)

        except (ValueError, TypeError):
            return None

    def _build_interpretation(self, net_delta, theta_per_hour, has_positions):
        """Generate a one-line text summary of portfolio Greek exposure."""
        if not has_positions:
            return "No open positions."

        parts = []

        if net_delta < -1:
            parts.append("Portfolio is net short delta (bearish lean)")
        elif net_delta > 1:
            parts.append("Portfolio is net long delta (bullish lean)")
        else:
            parts.append("Portfolio is delta-neutral")

        if theta_per_hour > 0:
            parts.append(f"collecting ${abs(theta_per_hour):.2f}/hr theta decay.")
        elif theta_per_hour < 0:
            parts.append(f"paying ${abs(theta_per_hour):.2f}/hr theta decay.")
        else:
            parts.append("no theta exposure.")

        return ", ".join(parts)

    def _empty_result(self):
        """Return a zeroed-out result when there are no positions."""
        return {
            'has_positions': False,
            'net_delta': 0.0,
            'net_gamma': 0.0,
            'net_theta': 0.0,
            'net_vega': 0.0,
            'theta_per_hour': 0.0,
            'position_count': 0,
            'interpretation': 'No open positions.',
            'iv_used': 0.0,
        }
