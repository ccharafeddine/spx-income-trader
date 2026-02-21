"""
P&L decomposition into Greek component drivers.

Approximates how much of each trade's P&L came from theta decay,
delta movement, vega (IV change), and residual (gamma, skew, etc.)
using first-order Greek estimates at entry.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

TRADING_HOURS_PER_DAY = 6.5


class PnLAttributor:
    """Decompose trade P&L into theta, delta, vega, and residual components."""

    def __init__(self, greeks_calculator):
        self.calc = greeks_calculator

    def attribute_trade(self, trade_record):
        """Decompose a single trade's P&L into component drivers.

        Args:
            trade_record: dict with keys:
                entry_time, exit_time, short_strike, long_strike,
                direction, quantity, pnl, vix_at_entry, vix_at_exit,
                spx_at_entry, spx_at_exit, expiration

        Returns:
            dict with theta_pnl, delta_pnl, vega_pnl, residual_pnl,
            theta_pct, delta_pct, vega_pct, hours_held, or None values
            if data is insufficient.
        """
        actual_pnl = trade_record.get('pnl')
        if actual_pnl is None:
            return self._empty_result()

        # Required fields
        short_strike = trade_record.get('short_strike')
        long_strike = trade_record.get('long_strike')
        spx_entry = trade_record.get('spx_at_entry')
        spx_exit = trade_record.get('spx_at_exit')
        vix_entry = trade_record.get('vix_at_entry')
        vix_exit = trade_record.get('vix_at_exit')
        quantity = trade_record.get('quantity', 1)

        if not all([short_strike, long_strike, spx_entry]):
            return self._empty_result()

        # Hours held
        hours_held = self._compute_hours_held(trade_record)
        if hours_held is None or hours_held <= 0:
            return self._empty_result()

        # Determine spread type from direction
        direction = trade_record.get('direction', 'bullish')
        spread_type = 'put_credit' if direction == 'bullish' else 'call_credit'

        # IV proxy: VIX/100
        iv_entry = (vix_entry / 100.0) if vix_entry else 0.20

        # Compute DTE at entry
        dte_years = self._compute_dte_years(trade_record)
        if dte_years is None or dte_years <= 0:
            dte_years = 1.0 / 365.0  # default to 1 day for 0DTE

        # Get spread Greeks at entry
        greeks = self.calc.calculate_spread_greeks(
            spx_entry, short_strike, long_strike, dte_years, iv_entry,
            spread_type=spread_type, quantity=quantity
        )

        net_theta = greeks['theta']  # per calendar day (already * qty * 100)
        net_delta = greeks['delta']
        net_vega = greeks['vega']   # per 1% IV change

        # Theta P&L: net_theta * fraction of trading day
        trading_day_fraction = hours_held / TRADING_HOURS_PER_DAY
        theta_pnl = net_theta * trading_day_fraction

        # Delta P&L: net_delta * SPX move
        spx_move = (spx_exit - spx_entry) if spx_exit else 0
        delta_pnl = net_delta * spx_move

        # Vega P&L: net_vega * IV change (in percentage points)
        if vix_entry and vix_exit:
            iv_change_pct = (vix_exit - vix_entry)  # VIX points = IV pct points
            vega_pnl = net_vega * iv_change_pct
        else:
            vega_pnl = 0

        # Residual: everything not explained (gamma, higher-order, skew)
        residual_pnl = actual_pnl - theta_pnl - delta_pnl - vega_pnl

        # Percentage of total P&L
        abs_total = abs(actual_pnl) if actual_pnl != 0 else 1
        theta_pct = (theta_pnl / abs_total) * 100
        delta_pct = (delta_pnl / abs_total) * 100
        vega_pct = (vega_pnl / abs_total) * 100

        return {
            'theta_pnl': round(theta_pnl, 2),
            'delta_pnl': round(delta_pnl, 2),
            'vega_pnl': round(vega_pnl, 2),
            'residual_pnl': round(residual_pnl, 2),
            'theta_pct': round(theta_pct, 1),
            'delta_pct': round(delta_pct, 1),
            'vega_pct': round(vega_pct, 1),
            'hours_held': round(hours_held, 2),
            'actual_pnl': round(actual_pnl, 2),
            'attributed': True,
        }

    def attribute_batch(self, trades):
        """Aggregate attribution across multiple trades.

        Returns totals and averages for each component, plus
        per-trade detail list.
        """
        results = []
        total_theta = 0
        total_delta = 0
        total_vega = 0
        total_residual = 0
        total_actual = 0
        attributed_count = 0

        for t in trades:
            r = self.attribute_trade(t)
            results.append(r)
            if r.get('attributed'):
                total_theta += r['theta_pnl']
                total_delta += r['delta_pnl']
                total_vega += r['vega_pnl']
                total_residual += r['residual_pnl']
                total_actual += r['actual_pnl']
                attributed_count += 1

        if attributed_count == 0:
            return {
                'total_theta': 0, 'total_delta': 0,
                'total_vega': 0, 'total_residual': 0,
                'total_actual': 0,
                'avg_theta': 0, 'avg_delta': 0,
                'avg_vega': 0, 'avg_residual': 0,
                'theta_pct': 0, 'delta_pct': 0,
                'vega_pct': 0, 'residual_pct': 0,
                'attributed_count': 0,
                'total_count': len(trades),
                'interpretation': 'Insufficient data for attribution.',
            }

        abs_total = abs(total_actual) if total_actual != 0 else 1
        theta_pct = (total_theta / abs_total) * 100
        delta_pct = (total_delta / abs_total) * 100
        vega_pct = (total_vega / abs_total) * 100
        residual_pct = (total_residual / abs_total) * 100

        interpretation = self._build_interpretation(
            theta_pct, delta_pct, vega_pct, total_actual
        )

        return {
            'total_theta': round(total_theta, 2),
            'total_delta': round(total_delta, 2),
            'total_vega': round(total_vega, 2),
            'total_residual': round(total_residual, 2),
            'total_actual': round(total_actual, 2),
            'avg_theta': round(total_theta / attributed_count, 2),
            'avg_delta': round(total_delta / attributed_count, 2),
            'avg_vega': round(total_vega / attributed_count, 2),
            'avg_residual': round(total_residual / attributed_count, 2),
            'theta_pct': round(theta_pct, 1),
            'delta_pct': round(delta_pct, 1),
            'vega_pct': round(vega_pct, 1),
            'residual_pct': round(residual_pct, 1),
            'attributed_count': attributed_count,
            'total_count': len(trades),
            'interpretation': interpretation,
        }

    def _compute_hours_held(self, trade):
        """Compute hours held from entry/exit times."""
        entry_t = trade.get('entry_time')
        exit_t = trade.get('exit_time')
        if not entry_t or not exit_t:
            # Fall back to duration_minutes if available
            dm = trade.get('duration_minutes') or trade.get('time_in_trade_minutes')
            if dm:
                return dm / 60.0
            return None

        try:
            if isinstance(entry_t, str):
                entry_t = datetime.fromisoformat(entry_t)
            if isinstance(exit_t, str):
                exit_t = datetime.fromisoformat(exit_t)
            delta = (exit_t - entry_t).total_seconds()
            return delta / 3600.0 if delta > 0 else None
        except (ValueError, TypeError):
            return None

    def _compute_dte_years(self, trade):
        """Compute DTE in years from entry_time to expiration."""
        entry_t = trade.get('entry_time')
        exp = trade.get('expiration')
        if not entry_t or not exp:
            return None

        try:
            if isinstance(entry_t, str):
                entry_t = datetime.fromisoformat(entry_t)
            if isinstance(exp, str):
                exp = datetime.fromisoformat(exp)
            delta = (exp - entry_t).total_seconds()
            if delta <= 0:
                return 1.0 / 365.0
            return delta / (365.25 * 24 * 3600)
        except (ValueError, TypeError):
            return None

    def _build_interpretation(self, theta_pct, delta_pct, vega_pct, total_pnl):
        """Generate a one-line text summary of P&L drivers."""
        parts = []
        abs_theta = abs(theta_pct)
        abs_delta = abs(delta_pct)
        abs_vega = abs(vega_pct)

        # Sort by magnitude
        drivers = sorted([
            ('theta decay', abs_theta, theta_pct),
            ('delta movement', abs_delta, delta_pct),
            ('vega (IV changes)', abs_vega, vega_pct),
        ], key=lambda x: x[1], reverse=True)

        top = drivers[0]
        second = drivers[1]

        if top[1] >= 50:
            direction = 'favorable' if top[2] > 0 else 'adverse'
            parts.append(f"{top[1]:.0f}% of P&L driven by {direction} {top[0]}")
        else:
            parts.append(f"P&L spread across multiple drivers (largest: {top[0]} at {top[1]:.0f}%)")

        if second[1] >= 15:
            direction = 'favorable' if second[2] > 0 else 'adverse'
            parts.append(f"{second[1]:.0f}% by {direction} {second[0]}")

        if abs_theta > abs_delta and abs_theta > abs_vega:
            parts.append("Strategy is primarily a time-decay collector.")
        elif abs_delta > abs_theta and abs_delta > abs_vega:
            parts.append("Strategy is primarily directional.")

        return " ".join(parts)

    def _empty_result(self):
        return {
            'theta_pnl': None, 'delta_pnl': None,
            'vega_pnl': None, 'residual_pnl': None,
            'theta_pct': None, 'delta_pct': None,
            'vega_pct': None, 'hours_held': None,
            'actual_pnl': None, 'attributed': False,
        }
