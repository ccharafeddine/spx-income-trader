"""
Charles Schwab Broker Implementation

Implements the BrokerInterface for Schwab API integration via schwab-py.

SAFETY FEATURES:
- All orders are LIMIT orders (never market) via schwab-py order templates
- Order confirmation polling with timeout
- Unfilled orders cancelled after timeout
- Comprehensive logging of all order details
- Exponential backoff retry for transient failures
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, date
from enum import Enum
from typing import Dict, List, Optional, Any

from .base import BrokerInterface
from .schwab_auth import SchwabAuth
from ..models.spread import CreditSpread, OptionLeg, TradeDirection

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

logger = logging.getLogger(__name__)


class OrderStatus(Enum):
    """Normalized order status values"""
    OPEN = "OPEN"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    NOT_FOUND = "NOT_FOUND"


@dataclass
class OrderResult:
    """Result of an order operation"""
    success: bool
    order_id: Optional[str]
    status: OrderStatus
    fill_price: float
    filled_quantity: int
    message: str


class SchwabAPIError(Exception):
    """Schwab API error"""
    MAX_RESPONSE_LENGTH = 200

    def __init__(self, message: str, status_code: int = None, response: str = None):
        self.message = message
        self.status_code = status_code
        if response and len(response) > self.MAX_RESPONSE_LENGTH:
            self.response = response[:self.MAX_RESPONSE_LENGTH] + '...[truncated]'
        else:
            self.response = response
        super().__init__(self.message)


# Map Schwab API status strings to our normalized status
_SCHWAB_STATUS_MAP = {
    # Pending states
    'AWAITING_PARENT_ORDER': 'pending',
    'AWAITING_CONDITION': 'pending',
    'AWAITING_STOP_CONDITION': 'pending',
    'NEW': 'pending',
    'AWAITING_RELEASE_TIME': 'pending',
    'PENDING_ACKNOWLEDGEMENT': 'pending',
    'PENDING_RECALL': 'pending',
    # Open/working states
    'AWAITING_MANUAL_REVIEW': 'open',
    'ACCEPTED': 'open',
    'AWAITING_UR_OUT': 'open',
    'PENDING_ACTIVATION': 'open',
    'QUEUED': 'open',
    'WORKING': 'open',
    # Terminal states
    'FILLED': 'filled',
    'EXPIRED': 'expired',
    'CANCELED': 'cancelled',
    'PENDING_CANCEL': 'cancelled',
    'REJECTED': 'rejected',
    'PENDING_REPLACE': 'rejected',
    'REPLACED': 'cancelled',
    'UNKNOWN': 'not_found',
}

# Reverse map: normalized lowercase -> OrderStatus enum
_NORMALIZED_TO_ENUM = {
    'filled': OrderStatus.EXECUTED,
    'open': OrderStatus.OPEN,
    'pending': OrderStatus.PENDING,
    'cancelled': OrderStatus.CANCELLED,
    'expired': OrderStatus.EXPIRED,
    'rejected': OrderStatus.REJECTED,
    'not_found': OrderStatus.NOT_FOUND,
}


class SchwabBroker(BrokerInterface):
    """
    Schwab implementation of BrokerInterface.

    CRITICAL SAFETY REQUIREMENTS:
    1. All orders are LIMIT orders (schwab-py templates enforce this)
    2. Order fills are confirmed via polling
    3. Unfilled orders are cancelled after timeout
    4. All order details are logged before submission
    """

    # Safety constants
    MAX_ORDER_RETRIES = 3
    RETRY_BASE_DELAY = 1.0  # seconds
    ORDER_POLL_INTERVAL = 2.0  # seconds
    ORDER_TIMEOUT = 30  # seconds

    def __init__(self, config: dict, auth: SchwabAuth = None):
        self.config = config
        self.auth = auth or SchwabAuth(
            app_key=config.get('app_key', ''),
            app_secret=config.get('app_secret', ''),
            callback_url=config.get('callback_url', 'https://127.0.0.1'),
            token_path=config.get('token_path', 'database/schwab_token.json'),
        )

        self._account_number = config.get('account_number', '')
        self._account_hash = None
        self._utils = None

        logger.info("SchwabBroker initialized")

    def _get_client(self):
        """Get the authenticated schwab client."""
        return self.auth.get_client()

    def _get_account_hash(self) -> str:
        """Fetch and cache the hashed account ID from Schwab."""
        if self._account_hash:
            return self._account_hash

        client = self._get_client()
        resp = self._call_api(client.get_account_numbers, context="get_account_numbers")

        accounts = resp.json()
        if not accounts:
            raise SchwabAPIError("No accounts found")

        # Find matching account or use first
        for acct in accounts:
            if str(acct.get('accountNumber')) == str(self._account_number):
                self._account_hash = acct['hashValue']
                break

        if not self._account_hash:
            self._account_hash = accounts[0]['hashValue']
            acct_num = str(accounts[0].get('accountNumber', ''))
            masked = f"****{acct_num[-4:]}" if len(acct_num) > 4 else '****'
            logger.warning(f"Configured account not found, using: {masked}")

        # Create Utils for order ID extraction
        from schwab.utils import Utils
        self._utils = Utils(self._get_client(), self._account_hash)

        return self._account_hash

    def _check_response(self, resp, context: str = ""):
        """Check an httpx response for errors."""
        if resp.status_code >= 400:
            try:
                body = resp.text[:200]
            except Exception:
                body = "(unable to read response)"
            raise SchwabAPIError(
                f"Schwab API error ({context}): HTTP {resp.status_code}",
                status_code=resp.status_code,
                response=body,
            )

    def _call_api(self, func, *args, context: str = "", retry_count: int = 0, **kwargs):
        """Execute a schwab-py client call with retry logic.

        Handles:
        - 401 Unauthorized: re-creates client and retries once
        - 429 Rate Limited: exponential backoff
        - 500/502/503/504: exponential backoff
        - Network errors: exponential backoff
        """
        try:
            resp = func(*args, **kwargs)

            # 401: token may have expired, schwab-py auto-refresh may have failed
            if resp.status_code == 401 and retry_count == 0:
                logger.warning(f"HTTP 401 ({context}) - re-creating client and retrying")
                self.auth._client = None  # Force client re-creation
                new_client = self._get_client()
                # Re-bind the function to the new client
                method_name = func.__name__
                new_func = getattr(new_client, method_name, None)
                if new_func:
                    return self._call_api(new_func, *args, context=context,
                                          retry_count=retry_count + 1, **kwargs)
                # If we can't re-bind, fall through to error
                self._check_response(resp, context)

            # 429: rate limited
            if resp.status_code == 429 and retry_count < self.MAX_ORDER_RETRIES:
                delay = self.RETRY_BASE_DELAY * (2 ** (retry_count + 1))
                logger.warning(f"Rate limited 429 ({context}), backing off {delay:.1f}s "
                               f"(attempt {retry_count + 1})")
                time.sleep(delay)
                return self._call_api(func, *args, context=context,
                                      retry_count=retry_count + 1, **kwargs)

            # 5xx: transient server errors
            if resp.status_code in (500, 502, 503, 504) and retry_count < self.MAX_ORDER_RETRIES:
                delay = self.RETRY_BASE_DELAY * (2 ** retry_count)
                logger.warning(f"Transient error {resp.status_code} ({context}), "
                               f"retrying in {delay:.1f}s (attempt {retry_count + 1})")
                time.sleep(delay)
                return self._call_api(func, *args, context=context,
                                      retry_count=retry_count + 1, **kwargs)

            self._check_response(resp, context)
            return resp

        except SchwabAPIError:
            raise
        except Exception as e:
            if retry_count < self.MAX_ORDER_RETRIES:
                delay = self.RETRY_BASE_DELAY * (2 ** retry_count)
                logger.warning(f"Request error ({context}): {e}, "
                               f"retrying in {delay:.1f}s (attempt {retry_count + 1})")
                time.sleep(delay)
                return self._call_api(func, *args, context=context,
                                      retry_count=retry_count + 1, **kwargs)
            raise SchwabAPIError(
                f"Request failed after {self.MAX_ORDER_RETRIES} retries ({context}): {e}"
            )

    # =========================================================================
    # MARKET DATA METHODS
    # =========================================================================

    def get_current_price(self, symbol: str) -> float:
        """Get current market price for symbol."""
        client = self._get_client()

        # Schwab uses $SPX for SPX index
        schwab_symbol = '$SPX' if symbol.upper() in ('SPX', '$SPX.X', '^GSPC') else symbol

        try:
            resp = self._call_api(client.get_quote, schwab_symbol,
                                  context=f"get_quote({schwab_symbol})")

            data = resp.json()
            quote_data = data.get(schwab_symbol, {})

            # Try reference data for last price, fall back to quote
            ref = quote_data.get('reference', {})
            quote = quote_data.get('quote', {})

            price = quote.get('lastPrice', 0) or quote.get('closePrice', 0)
            if price and price > 0:
                return float(price)

            # Market may be closed, try close price
            close_price = quote.get('closePrice', 0) or ref.get('lastPrice', 0)
            if close_price and close_price > 0:
                logger.debug(f"Using close price for {schwab_symbol}: ${close_price}")
                return float(close_price)

            logger.warning(f"No valid price for {schwab_symbol}")
            return 0.0

        except SchwabAPIError:
            raise
        except Exception as e:
            logger.error(f"Error getting price for {symbol}: {e}")
            return 0.0

    def get_options_chain(self, symbol: str, expiration: str) -> Dict[float, Dict]:
        """Get options chain in our standard format.

        Args:
            symbol: Underlying symbol (e.g. 'SPX')
            expiration: Expiration date as 'YYYY-MM-DD'

        Returns:
            {strike: {'call_bid': x, 'call_ask': x, 'put_bid': x, 'put_ask': x, ...}}
        """
        client = self._get_client()
        schwab_symbol = '$SPX' if symbol.upper() in ('SPX', '$SPX.X') else symbol

        try:
            from datetime import datetime as dt
            exp_date = dt.strptime(expiration, '%Y-%m-%d').date()

            from schwab.client import Client
            resp = self._call_api(
                client.get_option_chain,
                schwab_symbol,
                contract_type=Client.Options.ContractType.ALL,
                from_date=exp_date,
                to_date=exp_date,
                strike_count=50,
                context=f"get_option_chain({schwab_symbol}, {expiration})",
            )

            data = resp.json()
            return self._transform_chain(data, expiration)

        except SchwabAPIError:
            raise
        except Exception as e:
            logger.error(f"Error getting options chain: {e}")
            return {}

    def _transform_chain(self, raw_data: dict, target_expiration: str) -> Dict[float, Dict]:
        """Transform Schwab option chain response to our standard format."""
        chain = {}

        call_map = raw_data.get('callExpDateMap', {})
        put_map = raw_data.get('putExpDateMap', {})

        def _sf(val, default=0.0):
            try:
                return float(val) if val is not None else default
            except (ValueError, TypeError):
                return default

        # Process calls
        for exp_key, strikes in call_map.items():
            # exp_key format: "2026-02-13:0" (date:DTE)
            for strike_str, options in strikes.items():
                strike = _sf(strike_str)
                if strike <= 0:
                    continue
                # options is a list, take first
                opt = options[0] if isinstance(options, list) else options
                chain.setdefault(strike, {})
                chain[strike].update({
                    'call_bid': _sf(opt.get('bid')),
                    'call_ask': _sf(opt.get('ask')),
                    'call_last': _sf(opt.get('last')),
                    'call_delta': _sf(opt.get('delta')),
                    'call_volume': int(_sf(opt.get('totalVolume'))),
                    'call_oi': int(_sf(opt.get('openInterest'))),
                    'call_symbol': opt.get('symbol', ''),
                })

        # Process puts
        for exp_key, strikes in put_map.items():
            for strike_str, options in strikes.items():
                strike = _sf(strike_str)
                if strike <= 0:
                    continue
                opt = options[0] if isinstance(options, list) else options
                chain.setdefault(strike, {})
                chain[strike].update({
                    'put_bid': _sf(opt.get('bid')),
                    'put_ask': _sf(opt.get('ask')),
                    'put_last': _sf(opt.get('last')),
                    'put_delta': _sf(opt.get('delta')),
                    'put_volume': int(_sf(opt.get('totalVolume'))),
                    'put_oi': int(_sf(opt.get('openInterest'))),
                    'put_symbol': opt.get('symbol', ''),
                })

        # Fill missing sides
        for strike in chain:
            for key in ('call_bid', 'call_ask', 'call_last', 'call_delta',
                        'call_volume', 'call_oi',
                        'put_bid', 'put_ask', 'put_last', 'put_delta',
                        'put_volume', 'put_oi'):
                chain[strike].setdefault(key, 0)

        if not chain:
            logger.warning(f"Empty options chain for {target_expiration}")

        return dict(sorted(chain.items()))

    # =========================================================================
    # ORDER METHODS - WITH SAFETY FEATURES
    # =========================================================================

    def _build_option_symbol(self, strike: float, contract_type: str, expiration) -> str:
        """Build Schwab OCC option symbol.

        Args:
            strike: Strike price
            contract_type: 'call' or 'put'
            expiration: date or datetime object
        """
        from schwab.orders.options import OptionSymbol

        if hasattr(expiration, 'date'):
            exp_date = expiration.date()
        elif isinstance(expiration, str):
            exp_date = datetime.strptime(expiration, '%Y-%m-%d').date()
        else:
            exp_date = expiration

        ct = 'C' if contract_type.lower() == 'call' else 'P'

        # Format strike as string (Schwab expects whole/decimal number)
        strike_str = str(int(strike)) if strike == int(strike) else str(strike)

        sym = OptionSymbol('SPXW', exp_date, ct, strike_str)
        return sym.build()

    def _log_order_details(
        self,
        action: str,
        spread: CreditSpread,
        quantity: int,
        limit_price: float,
    ) -> None:
        """Log full order details BEFORE submission."""
        logger.info("=" * 60)
        logger.info(f"ORDER SUBMISSION: {action}")
        logger.info("=" * 60)
        logger.info(f"  Direction:      {spread.direction.value.upper()}")
        logger.info(f"  Short Strike:   ${spread.short_leg.strike:,.0f} ({spread.short_leg.option_type})")
        logger.info(f"  Long Strike:    ${spread.long_leg.strike:,.0f} ({spread.long_leg.option_type})")
        logger.info(f"  Spread Width:   ${spread.spread_width:,.2f}")
        logger.info(f"  Quantity:       {quantity} contracts")
        logger.info(f"  Limit Price:    ${limit_price:.2f}")
        logger.info(f"  Total Credit:   ${limit_price * quantity * 100:,.2f}")
        logger.info(f"  Max Risk:       ${spread.max_risk * quantity:,.2f}")
        logger.info(f"  Expiration:     {spread.expiration}")
        logger.info(f"  Underlying:     ${spread.underlying_price_at_entry:,.2f}")
        logger.info("=" * 60)

    def place_spread_order(
        self,
        spread: CreditSpread,
        quantity: int,
        limit_price: Optional[float] = None,
        metadata: Optional[Dict] = None,
    ) -> str:
        """Place vertical spread order.

        Returns order_id if successful, empty string if failed.

        SAFETY: Always uses LIMIT orders via schwab-py templates.
        """
        import schwab.orders.options as opts

        if quantity <= 0:
            logger.error(f"Invalid quantity: {quantity}")
            return ''

        limit_price = limit_price if limit_price is not None else spread.credit_received
        if limit_price <= 0:
            logger.error(f"Invalid limit price: {limit_price}")
            return ''

        account_hash = self._get_account_hash()

        # SAFETY: Log full order details
        self._log_order_details('OPEN', spread, quantity, limit_price)

        # Build option symbols
        short_symbol = self._build_option_symbol(
            spread.short_leg.strike,
            spread.short_leg.option_type,
            spread.expiration,
        )
        long_symbol = self._build_option_symbol(
            spread.long_leg.strike,
            spread.long_leg.option_type,
            spread.expiration,
        )

        logger.info(f"  Short symbol: {short_symbol}")
        logger.info(f"  Long symbol:  {long_symbol}")

        # Build order using schwab-py templates
        if spread.direction == TradeDirection.BEARISH:
            # Bear call vertical (sell call spread for credit)
            order = opts.bear_call_vertical_open(
                short_symbol, long_symbol, quantity, limit_price,
            )
        else:
            # Bull put vertical (sell put spread for credit)
            order = opts.bull_put_vertical_open(
                long_symbol, short_symbol, quantity, limit_price,
            )

        # Place order
        try:
            client = self._get_client()
            resp = self._call_api(client.place_order, account_hash, order,
                                  context="place_order")

            order_id = self._extract_order_id(resp)
            if not order_id:
                logger.error("No order ID returned from place_order")
                return ''

            logger.info(f"Order placed, order_id={order_id}")

            # Poll for fill
            result = self._poll_order_fill(str(order_id), self.ORDER_TIMEOUT)
            if not result.success:
                logger.warning(f"Order {order_id} did not fill: {result.message}")
                return ''

            return str(order_id)

        except SchwabAPIError as e:
            logger.error(f"Order placement failed: {e.message}")
            return ''
        except Exception as e:
            logger.error(f"Order placement error: {e}")
            return ''

    def close_spread(
        self,
        spread: CreditSpread,
        quantity: int,
        limit_price: float = 0.20,
    ) -> str:
        """Close existing spread.

        Returns order_id if successful, empty string if failed.
        """
        import schwab.orders.options as opts

        if quantity <= 0:
            logger.error(f"Invalid close quantity: {quantity}")
            return ''
        if limit_price < 0:
            logger.error(f"Invalid close limit price: {limit_price}")
            return ''

        account_hash = self._get_account_hash()

        # SAFETY: Log full order details
        self._log_order_details('CLOSE', spread, quantity, limit_price)

        # Build option symbols
        short_symbol = self._build_option_symbol(
            spread.short_leg.strike,
            spread.short_leg.option_type,
            spread.expiration,
        )
        long_symbol = self._build_option_symbol(
            spread.long_leg.strike,
            spread.long_leg.option_type,
            spread.expiration,
        )

        # Build close order
        if spread.direction == TradeDirection.BEARISH:
            order = opts.bear_call_vertical_close(
                short_symbol, long_symbol, quantity, limit_price,
            )
        else:
            order = opts.bull_put_vertical_close(
                long_symbol, short_symbol, quantity, limit_price,
            )

        try:
            client = self._get_client()
            resp = self._call_api(client.place_order, account_hash, order,
                                  context="close_spread")

            order_id = self._extract_order_id(resp)
            if not order_id:
                logger.error("No order ID returned from close order")
                return ''

            logger.info(f"Close order placed, order_id={order_id}")

            result = self._poll_order_fill(str(order_id), self.ORDER_TIMEOUT)
            if not result.success:
                logger.warning(f"Close order {order_id} did not fill: {result.message}")
                return ''

            return str(order_id)

        except SchwabAPIError as e:
            logger.error(f"Close order failed: {e.message}")
            return ''
        except Exception as e:
            logger.error(f"Close order error: {e}")
            return ''

    def get_order_status(self, order_id: str) -> Dict:
        """Get order status and fill information.

        Returns normalized status strings that match what position_manager expects.
        """
        try:
            client = self._get_client()
            account_hash = self._get_account_hash()

            resp = self._call_api(client.get_order, int(order_id), account_hash,
                                  context=f"get_order({order_id})")

            order_data = resp.json()
            return self._parse_order_response(order_data)

        except SchwabAPIError as e:
            logger.error(f"Error getting order status: {e.message}")
        except Exception as e:
            logger.error(f"Error getting order status: {e}")

        return {'status': 'not_found', 'fill_price': 0, 'filled_quantity': 0}

    def _parse_order_response(self, order_data: dict) -> Dict:
        """Parse a Schwab order response into our standard format."""
        raw_status = order_data.get('status', 'UNKNOWN')
        normalized = _SCHWAB_STATUS_MAP.get(raw_status, 'not_found')

        fill_price = 0.0
        filled_quantity = int(order_data.get('filledQuantity', 0) or 0)

        # Extract fill price from orderActivityCollection -> executionLegs
        activities = order_data.get('orderActivityCollection', [])
        if activities:
            for activity in activities:
                legs = activity.get('executionLegs', [])
                if legs:
                    # Average the execution prices
                    prices = [leg.get('price', 0) for leg in legs if leg.get('price')]
                    if prices:
                        fill_price = sum(prices) / len(prices)
                        break

        # Fallback: use the order price if no execution data
        if fill_price == 0 and filled_quantity > 0:
            fill_price = float(order_data.get('price', 0) or 0)

        return {
            'status': normalized,
            'fill_price': float(fill_price),
            'filled_quantity': filled_quantity,
        }

    def _extract_order_id(self, response) -> Optional[int]:
        """Extract order ID from place_order response.

        Uses schwab.utils.Utils if available, falls back to header parsing.
        """
        if self._utils:
            try:
                return self._utils.extract_order_id(response)
            except Exception as e:
                logger.warning(f"Utils.extract_order_id failed: {e}")

        # Fallback: parse Location header manually
        try:
            import re
            location = response.headers.get('Location', '')
            m = re.search(r'/orders/(\d+)', location)
            if m:
                return int(m.group(1))
        except Exception:
            pass

        return None

    def _poll_order_fill(self, order_id: str, timeout: float = 30) -> OrderResult:
        """Poll order status until filled, cancelled, or timeout."""
        start = time.time()
        polls = 0

        while (time.time() - start) < timeout:
            polls += 1
            time.sleep(self.ORDER_POLL_INTERVAL)

            status = self.get_order_status(order_id)
            raw_status = status.get('status', 'not_found')
            order_status = _NORMALIZED_TO_ENUM.get(raw_status, OrderStatus.NOT_FOUND)

            logger.debug(f"Order {order_id} poll {polls}: {order_status.value}")

            if order_status == OrderStatus.EXECUTED:
                fill_price = status.get('fill_price', 0)
                filled_qty = status.get('filled_quantity', 0)
                logger.info(
                    f"Order {order_id} FILLED: {filled_qty} contracts @ ${fill_price:.2f}"
                )
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    status=order_status,
                    fill_price=fill_price,
                    filled_quantity=filled_qty,
                    message=f"Filled at ${fill_price:.2f}",
                )

            if order_status in (OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
                logger.warning(f"Order {order_id} ended with status: {order_status.value}")
                return OrderResult(
                    success=False,
                    order_id=order_id,
                    status=order_status,
                    fill_price=0,
                    filled_quantity=0,
                    message=f"Order {order_status.value}",
                )

        # Timeout: cancel the order
        logger.warning(f"Order {order_id} did not fill within {timeout}s, cancelling...")
        self._cancel_order(order_id)

        return OrderResult(
            success=False,
            order_id=order_id,
            status=OrderStatus.CANCELLED,
            fill_price=0,
            filled_quantity=0,
            message=f"Timeout after {timeout}s - order cancelled",
        )

    def _cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        try:
            client = self._get_client()
            account_hash = self._get_account_hash()
            resp = client.cancel_order(int(order_id), account_hash)
            if resp.status_code < 400:
                logger.info(f"Order {order_id} cancelled")
                return True
            else:
                logger.error(f"Failed to cancel order {order_id}: HTTP {resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    def get_position_value(self, spread: CreditSpread) -> float:
        """Get current market value of spread (cost to close).

        Returns per-share price (not multiplied by 100) to match entry_price units.
        """
        expiration = spread.expiration.strftime('%Y-%m-%d')
        chain = self.get_options_chain('SPX', expiration)

        short_strike = spread.short_leg.strike
        long_strike = spread.long_leg.strike

        if short_strike not in chain or long_strike not in chain:
            logger.warning(f"Strikes {short_strike}/{long_strike} not in chain")
            return 0

        if spread.direction == TradeDirection.BULLISH:
            short_mid = (chain[short_strike]['put_bid'] + chain[short_strike]['put_ask']) / 2
            long_mid = (chain[long_strike]['put_bid'] + chain[long_strike]['put_ask']) / 2
        else:
            short_mid = (chain[short_strike]['call_bid'] + chain[short_strike]['call_ask']) / 2
            long_mid = (chain[long_strike]['call_bid'] + chain[long_strike]['call_ask']) / 2

        # Spread value = cost to close (buy back short, sell long)
        spread_value = short_mid - long_mid
        return spread_value

    def get_account_balance(self) -> Dict:
        """Get account balance information."""
        client = self._get_client()
        account_hash = self._get_account_hash()

        from schwab.client import Client
        resp = self._call_api(client.get_account, account_hash,
                              fields=[Client.Account.Fields.POSITIONS],
                              context="get_account")

        data = resp.json()
        acct = data.get('securitiesAccount', {})
        balances = acct.get('currentBalances', {})

        return {
            'net_account_value': float(balances.get('liquidationValue', 0) or 0),
            'cash_available': float(balances.get('availableFunds', 0) or 0),
            'buying_power': float(balances.get('buyingPower', 0) or 0),
        }
