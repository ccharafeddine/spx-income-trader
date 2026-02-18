"""
Dry Run Broker Implementation

A broker that:
- Fetches REAL market data (via Yahoo Finance)
- Simulates order execution WITHOUT actually trading
- Logs all trading signals and decisions
- Tracks hypothetical P&L

Perfect for validating strategy logic with real market conditions
before going live.
"""

import logging
import json
import math
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional
from pathlib import Path
import pytz

from .base import BrokerInterface
from ..models.spread import CreditSpread, TradeDirection
from ..data.yahoo_finance import YahooFinanceProvider

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config.settings import BASE_DIR

logger = logging.getLogger(__name__)


class DryRunBroker(BrokerInterface):
    """
    Broker for dry-run testing with real market data.

    Features:
    - Real SPX prices from Yahoo Finance
    - Simulated options chains based on real underlying price
    - Full trade logging (what WOULD have been traded)
    - Hypothetical P&L tracking
    - No actual order execution
    """

    def __init__(
        self,
        initial_balance: float = 50000.0,
        log_file: Optional[str] = None
    ):
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.tz = pytz.timezone('US/Eastern')

        # Real market data provider
        self.market_data = YahooFinanceProvider()

        # Simulated positions (for tracking what we WOULD have)
        self.hypothetical_positions: List[Dict] = []
        self.hypothetical_trades: List[Dict] = []
        self.order_counter = 0

        # Trade signal log
        self.log_file = log_file or str(BASE_DIR / 'logs' / 'dry_run_signals.json')
        self._ensure_log_file()

        # Options chain simulation parameters
        self.spread_width = 0.20  # Bid-ask spread
        self.strike_interval = 5  # SPX strikes are $5 apart

        # Track which pricing source was used for the last chain
        self._last_chain_source = None  # 'real' or 'synthetic'

        logger.info(f"DryRunBroker initialized with ${initial_balance:,.2f}")
        logger.info(f"Signal log: {self.log_file}")

    def _ensure_log_file(self):
        """Ensure log directory and file exist."""
        log_path = Path(self.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        if not log_path.exists():
            with open(log_path, 'w') as f:
                json.dump({"signals": [], "created": datetime.now().isoformat()}, f)

    MAX_SIGNALS = 5000  # Rotate log file after this many signals

    def _log_signal(self, signal_type: str, data: dict):
        """Log a trading signal to file, rotating if too large."""
        try:
            with open(self.log_file, 'r') as f:
                log_data = json.load(f)

            signal = {
                "timestamp": datetime.now(self.tz).isoformat(),
                "type": signal_type,
                **data
            }
            log_data["signals"].append(signal)

            # Rotate: archive old signals when log exceeds limit
            if len(log_data["signals"]) > self.MAX_SIGNALS:
                archive_path = Path(self.log_file).with_suffix('.old.json')
                with open(archive_path, 'w') as f:
                    json.dump(log_data, f, indent=2, default=str)
                # Keep only the most recent signals
                log_data["signals"] = log_data["signals"][-1000:]
                logger.info(f"Signal log rotated, archived to {archive_path}")

            with open(self.log_file, 'w') as f:
                json.dump(log_data, f, indent=2, default=str)

            # Also log to console
            logger.info(f"[DRY RUN SIGNAL] {signal_type}: {data}")

        except Exception as e:
            logger.error(f"Failed to log signal: {e}")

    def get_current_price(self, symbol: str) -> float:
        """Get REAL current price from Yahoo Finance."""
        if symbol.upper() in ('SPX', '$SPX.X', '^GSPC'):
            quote = self.market_data.get_spx_quote()
            if quote:
                price = quote['price']
                logger.debug(f"SPX price from Yahoo Finance: ${price:,.2f}")

                # Check data freshness
                market_time_epoch = quote.get('market_time_epoch', 0)
                if market_time_epoch:
                    now = datetime.now(self.tz)
                    market_dt = datetime.fromtimestamp(market_time_epoch, tz=self.tz)
                    staleness = (now - market_dt).total_seconds()
                    if staleness > 7200:  # 2 hours
                        logger.warning(
                            f"Market data may be stale: last update {market_dt.strftime('%Y-%m-%d %H:%M')} ET "
                            f"({staleness / 3600:.1f} hours ago)"
                        )

                return price

        # Fallback for other symbols
        logger.warning(f"Unknown symbol {symbol}, returning 0")
        return 0.0

    def get_options_chain(self, symbol: str, expiration: str) -> Dict[float, Dict]:
        """
        Get options chain, trying real Yahoo Finance data first and
        falling back to the synthetic model if real data is unavailable.
        """
        # Try real chain first
        chain = self._fetch_real_options_chain(symbol, expiration)
        if chain:
            self._last_chain_source = 'real'
            logger.info(f"Using REAL options chain from Yahoo Finance ({len(chain)} strikes)")
            return chain

        # Fall back to synthetic
        chain = self._build_synthetic_chain(symbol, expiration)
        if chain:
            self._last_chain_source = 'synthetic'
            logger.info(f"Using SYNTHETIC options chain ({len(chain)} strikes)")
        return chain

    def _fetch_real_options_chain(self, symbol: str, expiration: str) -> Optional[Dict[float, Dict]]:
        """
        Fetch real SPX option chain from Yahoo Finance via yfinance.

        Returns the chain in the standard format or None on failure.
        Yahoo Finance may not always have 0DTE SPX data available.
        """
        try:
            import yfinance as yf
        except ImportError:
            logger.debug("yfinance not installed, skipping real chain fetch")
            return None

        try:
            ticker = yf.Ticker("^SPX")

            # Get available expiration dates
            available = ticker.options  # tuple of date strings like '2026-02-07'
            if not available:
                logger.debug("No option expirations available for ^SPX")
                return None

            # Find the requested expiration (or closest 0DTE match)
            target = expiration  # expected format: 'YYYY-MM-DD'
            if target not in available:
                # For 0DTE, try today's date in the available list
                today_str = datetime.now(self.tz).strftime('%Y-%m-%d')
                if today_str in available:
                    target = today_str
                else:
                    # Pick the nearest future expiration
                    target_date = datetime.strptime(expiration, '%Y-%m-%d').date()
                    future = [d for d in available
                              if datetime.strptime(d, '%Y-%m-%d').date() >= target_date]
                    if not future:
                        logger.debug(f"No matching expiration for {expiration} in {available[:5]}...")
                        return None
                    target = future[0]
                    logger.debug(f"Requested {expiration} not available, using nearest: {target}")

            opt = ticker.option_chain(target)
            calls_df = opt.calls
            puts_df = opt.puts

            if calls_df.empty and puts_df.empty:
                logger.debug("Real option chain returned empty DataFrames")
                return None

            def _safe_float(val, default=0.0):
                try:
                    f = float(val)
                    return default if math.isnan(f) else f
                except (TypeError, ValueError):
                    return default

            def _safe_int(val, default=0):
                try:
                    f = float(val)
                    return default if math.isnan(f) else int(f)
                except (TypeError, ValueError):
                    return default

            # Build strike-keyed chain from the two DataFrames
            chain: Dict[float, Dict] = {}

            for _, row in calls_df.iterrows():
                strike = float(row['strike'])
                chain.setdefault(strike, {})
                chain[strike].update({
                    'call_bid': _safe_float(row.get('bid')),
                    'call_ask': _safe_float(row.get('ask')),
                    'call_last': _safe_float(row.get('lastPrice')),
                    'call_volume': _safe_int(row.get('volume')),
                    'call_oi': _safe_int(row.get('openInterest')),
                })

            for _, row in puts_df.iterrows():
                strike = float(row['strike'])
                chain.setdefault(strike, {})
                chain[strike].update({
                    'put_bid': _safe_float(row.get('bid')),
                    'put_ask': _safe_float(row.get('ask')),
                    'put_last': _safe_float(row.get('lastPrice')),
                    'put_volume': _safe_int(row.get('volume')),
                    'put_oi': _safe_int(row.get('openInterest')),
                })

            # Fill any strikes that only have one side (call-only or put-only)
            for strike in chain:
                chain[strike].setdefault('call_bid', 0)
                chain[strike].setdefault('call_ask', 0)
                chain[strike].setdefault('call_last', 0)
                chain[strike].setdefault('call_volume', 0)
                chain[strike].setdefault('call_oi', 0)
                chain[strike].setdefault('put_bid', 0)
                chain[strike].setdefault('put_ask', 0)
                chain[strike].setdefault('put_last', 0)
                chain[strike].setdefault('put_volume', 0)
                chain[strike].setdefault('put_oi', 0)

            if not chain:
                return None

            logger.info(f"Fetched real option chain for ^SPX {target}: {len(chain)} strikes")
            return chain

        except Exception as e:
            logger.warning(f"Failed to fetch real options chain: {e}")
            return None

    def _build_synthetic_chain(self, symbol: str, expiration: str) -> Dict[float, Dict]:
        """
        Generate simulated options chain based on REAL underlying price.

        Uses Black-Scholes-ish approximations for realistic pricing.
        """
        # Get real SPX price
        current_price = self.get_current_price(symbol)

        if current_price == 0:
            logger.error("Cannot generate options chain without underlying price")
            return {}

        # Get VIX for volatility estimate
        vix_quote = self.market_data.get_vix_quote()
        implied_vol = (vix_quote['price'] / 100) if vix_quote else 0.15

        # Calculate time to expiration
        exp_date = datetime.strptime(expiration, '%Y-%m-%d').date()
        today = datetime.now(self.tz).date()
        dte = max(0, (exp_date - today).days)

        # For 0DTE, use fraction of day remaining
        if dte == 0:
            now = datetime.now(self.tz)
            market_close = now.replace(hour=16, minute=0, second=0)
            if now < market_close:
                hours_left = (market_close - now).total_seconds() / 3600
                dte = hours_left / 24  # Fraction of day
            else:
                dte = 0.01  # Minimal time value

        # Generate strikes around ATM
        atm_strike = round(current_price / self.strike_interval) * self.strike_interval
        strikes = [atm_strike + (i * self.strike_interval) for i in range(-20, 21)]

        chain = {}
        for strike in strikes:
            # Simple option pricing approximation
            moneyness = (current_price - strike) / current_price

            # Base value from intrinsic
            call_intrinsic = max(0, current_price - strike)
            put_intrinsic = max(0, strike - current_price)

            # Time value approximation
            time_factor = (dte ** 0.5) * implied_vol * current_price * 0.4

            # ATM options have most time value
            atm_factor = 1 - min(abs(moneyness) * 5, 0.9)
            time_value = time_factor * atm_factor

            # Calculate option prices
            call_mid = call_intrinsic + time_value
            put_mid = put_intrinsic + time_value

            # Apply bid-ask spread
            half_spread = self.spread_width / 2

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

        logger.debug(f"Generated synthetic chain with {len(chain)} strikes around ${atm_strike}")
        return chain

    def place_spread_order(
        self,
        spread: CreditSpread,
        quantity: int,
        limit_price: Optional[float] = None,
        metadata: Optional[Dict] = None
    ) -> str:
        """
        SIMULATE placing a spread order (does not actually trade).

        Logs the signal and tracks hypothetical position.
        """
        self.order_counter += 1
        order_id = f"DRY-{datetime.now().strftime('%Y%m%d%H%M%S')}-{self.order_counter}"

        # Calculate fill price (simulated)
        fill_price = limit_price if limit_price else spread.credit_received

        # Capture VIX at signal time
        vix_at_signal = None
        try:
            vix_quote = self.market_data.get_vix_quote()
            if vix_quote:
                vix_at_signal = vix_quote.get('price')
        except Exception:
            pass

        # Log the signal
        signal_data = {
            "order_id": order_id,
            "action": "OPEN",
            "direction": spread.direction.value,
            "short_strike": spread.short_leg.strike,
            "long_strike": spread.long_leg.strike,
            "quantity": quantity,
            "limit_price": limit_price,
            "simulated_fill": fill_price,
            "credit_per_contract": fill_price,
            "total_credit": fill_price * quantity * 100,
            "max_risk": spread.max_risk * quantity,
            "underlying_price": spread.underlying_price_at_entry,
            "expiration": spread.expiration.isoformat() if spread.expiration else None,
            "vix_at_signal": vix_at_signal,
            "pricing_source": self._last_chain_source or "unknown",
        }

        # Merge metadata into signal data if provided
        if metadata:
            if 'pulse_bar' in metadata:
                signal_data['pulse_bar'] = metadata['pulse_bar']
            if 'setup_bar_time' in metadata:
                signal_data['setup_bar_time'] = metadata['setup_bar_time']
            if 'breakout_time' in metadata:
                signal_data['breakout_time'] = metadata['breakout_time']
            if 'credit_quality' in metadata:
                signal_data['credit_quality'] = metadata['credit_quality']

        self._log_signal("WOULD_OPEN_SPREAD", signal_data)

        # Track hypothetical position
        position = {
            "order_id": order_id,
            "spread": spread,
            "quantity": quantity,
            "entry_price": fill_price,
            "entry_time": datetime.now(self.tz),
            "status": "open"
        }
        self.hypothetical_positions.append(position)

        # Update hypothetical balance
        credit = fill_price * quantity * 100
        self.balance += credit

        print(f"\n{'='*60}")
        print(f"  [DRY RUN] TRADE SIGNAL DETECTED")
        print(f"{'='*60}")
        print(f"  Direction:     {spread.direction.value.upper()}")
        print(f"  Short Strike:  ${spread.short_leg.strike:,.0f}")
        print(f"  Long Strike:   ${spread.long_leg.strike:,.0f}")
        print(f"  Quantity:      {quantity} contracts")
        print(f"  Credit:        ${fill_price:.2f} per spread")
        print(f"  Total Credit:  ${credit:,.2f}")
        print(f"  Max Risk:      ${spread.max_risk * quantity:,.2f}")
        print(f"  SPX Price:     ${spread.underlying_price_at_entry:,.2f}")
        print(f"{'='*60}")
        print(f"  ** NO ACTUAL ORDER PLACED - DRY RUN MODE **")
        print(f"{'='*60}\n")

        return order_id

    def get_order_status(self, order_id: str) -> Dict:
        """Return simulated fill status."""
        # Find the position
        for pos in self.hypothetical_positions:
            if pos["order_id"] == order_id:
                return {
                    "status": "filled",  # Always "fill" in dry run
                    "fill_price": pos["entry_price"],
                    "filled_quantity": pos["quantity"]
                }

        return {"status": "not_found", "fill_price": 0, "filled_quantity": 0}

    def close_spread(
        self,
        spread: CreditSpread,
        quantity: int,
        limit_price: float = 0.20
    ) -> str:
        """SIMULATE closing a spread."""
        self.order_counter += 1
        order_id = f"DRY-CLOSE-{datetime.now().strftime('%Y%m%d%H%M%S')}-{self.order_counter}"

        # Log the signal
        signal_data = {
            "order_id": order_id,
            "action": "CLOSE",
            "direction": spread.direction.value,
            "short_strike": spread.short_leg.strike,
            "long_strike": spread.long_leg.strike,
            "quantity": quantity,
            "limit_price": limit_price,
            "debit_per_contract": limit_price,
            "total_debit": limit_price * quantity * 100,
        }

        self._log_signal("WOULD_CLOSE_SPREAD", signal_data)

        # Register so get_order_status() can look it up as "filled"
        self.hypothetical_positions.append({
            "order_id": order_id,
            "spread": spread,
            "quantity": quantity,
            "entry_price": limit_price,
            "entry_time": datetime.now(self.tz),
            "status": "closed",
        })

        # Update hypothetical balance
        debit = limit_price * quantity * 100
        self.balance -= debit

        print(f"\n{'='*60}")
        print(f"  [DRY RUN] CLOSE SIGNAL")
        print(f"{'='*60}")
        print(f"  Closing {quantity} contracts at ${limit_price:.2f}")
        print(f"  Total Debit: ${debit:,.2f}")
        print(f"  ** NO ACTUAL ORDER PLACED - DRY RUN MODE **")
        print(f"{'='*60}\n")

        return order_id

    def get_position_value(self, spread: CreditSpread) -> float:
        """Get current value of spread based on real market price."""
        current_price = self.get_current_price('SPX')

        if current_price == 0:
            return 0

        # Simple spread valuation based on intrinsic value
        # Returns per-share price (not multiplied by 100) to match entry_price units
        if spread.direction == TradeDirection.BULLISH:
            # Put credit spread
            if current_price >= spread.short_leg.strike:
                return 0  # Both OTM, spread worth ~0
            elif current_price <= spread.long_leg.strike:
                return spread.spread_width  # Max loss per share
            else:
                return spread.short_leg.strike - current_price
        else:
            # Call credit spread
            if current_price <= spread.short_leg.strike:
                return 0  # Both OTM
            elif current_price >= spread.long_leg.strike:
                return spread.spread_width  # Max loss per share
            else:
                return current_price - spread.short_leg.strike

    def get_account_balance(self) -> Dict:
        """Get hypothetical account balance."""
        return {
            'net_account_value': self.balance,
            'cash_available': self.balance,
            'buying_power': self.balance * 4,
            'initial_balance': self.initial_balance,
            'hypothetical_pnl': self.balance - self.initial_balance,
            'open_positions': len([p for p in self.hypothetical_positions if p['status'] == 'open'])
        }

    def get_signal_log(self) -> List[Dict]:
        """Read the signal log."""
        try:
            with open(self.log_file, 'r') as f:
                return json.load(f).get('signals', [])
        except Exception:
            return []

    def print_summary(self):
        """Print summary of dry run session."""
        signals = self.get_signal_log()
        open_signals = [s for s in signals if s['type'] == 'WOULD_OPEN_SPREAD']
        close_signals = [s for s in signals if s['type'] == 'WOULD_CLOSE_SPREAD']

        print(f"\n{'='*60}")
        print(f"  DRY RUN SESSION SUMMARY")
        print(f"{'='*60}")
        print(f"  Total Signals:     {len(signals)}")
        print(f"  Open Signals:      {len(open_signals)}")
        print(f"  Close Signals:     {len(close_signals)}")
        print(f"  Starting Balance:  ${self.initial_balance:,.2f}")
        print(f"  Current Balance:   ${self.balance:,.2f}")
        print(f"  Hypothetical P&L:  ${self.balance - self.initial_balance:+,.2f}")
        print(f"  Signal Log:        {self.log_file}")
        print(f"{'='*60}\n")
