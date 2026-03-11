"""
E*TRADE Broker Implementation

Implements the BrokerInterface for E*TRADE API integration.
Supports both sandbox and production environments.

SAFETY FEATURES:
- All orders are LIMIT orders (never market)
- Preview required before placement
- Order confirmation polling with timeout
- Exponential backoff retry for transient failures
- Comprehensive logging of all order details
- dry_run parameter to preview without executing
"""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, date
from typing import Dict, List, Optional, Any, Tuple
from xml.etree import ElementTree
from enum import Enum

from .base import BrokerInterface
from .etrade_auth import ETradeAuth
from ..models.spread import CreditSpread, OptionLeg, TradeDirection

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config.settings import ETRADE_CONFIG

logger = logging.getLogger(__name__)


class OrderStatus(Enum):
    """E*TRADE order status values"""
    OPEN = "OPEN"
    EXECUTED = "EXECUTED"
    CANCELLED = "CANCELLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
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
    preview_only: bool = False


class ETradeAPIError(Exception):
    """E*TRADE API error"""
    MAX_RESPONSE_LENGTH = 200

    def __init__(self, message: str, status_code: int = None, response: str = None):
        self.message = message
        self.status_code = status_code
        # Truncate response to avoid leaking sensitive data in logs/tracebacks
        if response and len(response) > self.MAX_RESPONSE_LENGTH:
            self.response = response[:self.MAX_RESPONSE_LENGTH] + '...[truncated]'
        else:
            self.response = response
        super().__init__(self.message)


class ETradeBroker(BrokerInterface):
    """
    E*TRADE implementation of BrokerInterface.

    CRITICAL SAFETY REQUIREMENTS:
    1. All orders are LIMIT orders - never market
    2. Orders are previewed before placement
    3. Order fills are confirmed via polling
    4. Unfilled orders are cancelled after timeout
    5. All order details are logged before submission
    """

    # API endpoints
    ACCOUNTS_LIST = "/v1/accounts/list"
    ACCOUNT_BALANCE = "/v1/accounts/{account_id_key}/balance"
    ACCOUNT_PORTFOLIO = "/v1/accounts/{account_id_key}/portfolio"
    MARKET_QUOTE = "/v1/market/quote/{symbols}"
    OPTION_CHAINS = "/v1/market/optionchains"
    OPTION_EXPIRE_DATE = "/v1/market/optionexpiredate"
    ORDERS_LIST = "/v1/accounts/{account_id_key}/orders"
    ORDER_PREVIEW = "/v1/accounts/{account_id_key}/orders/preview"
    ORDER_PLACE = "/v1/accounts/{account_id_key}/orders/place"
    ORDER_CANCEL = "/v1/accounts/{account_id_key}/orders/cancel"

    # Safety constants
    MAX_ORDER_RETRIES = 3
    RETRY_BASE_DELAY = 1.0  # seconds
    ORDER_POLL_INTERVAL = 1.0  # seconds
    MAX_ORDER_POLLS = 10
    DEFAULT_ORDER_TIMEOUT = 30  # seconds
    REQUEST_TIMEOUT = 30  # seconds per HTTP request

    def __init__(
        self,
        auth: Optional[ETradeAuth] = None,
        account_id: Optional[str] = None,
        order_timeout: int = DEFAULT_ORDER_TIMEOUT
    ):
        self.auth = auth or ETradeAuth()
        self.configured_account_id = account_id or ETRADE_CONFIG['account_id']
        self.base_url = ETRADE_CONFIG['base_url']
        self.order_timeout = order_timeout

        # Will be populated after authentication
        self.account_id_key: Optional[str] = None
        self.accounts: List[Dict] = []

        logger.info(f"ETradeBroker initialized: sandbox={ETRADE_CONFIG['sandbox']}, timeout={order_timeout}s")

    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        headers: Optional[Dict] = None,
        retry_count: int = 0
    ) -> Dict:
        """
        Make authenticated API request with retry logic.

        Implements exponential backoff for transient failures.
        """
        if not self.auth.is_authenticated():
            raise ETradeAPIError("Not authenticated")

        # Proactive token renewal if approaching expiry (2-hour limit).
        # 110 minutes gives a 10-minute buffer before the hard 120-minute
        # timeout, reducing reliance on reactive 401 recovery.
        if retry_count == 0 and getattr(self.auth, 'token_timestamp', None):
            token_age = time.time() - self.auth.token_timestamp
            if token_age > 110 * 60:
                logger.info(f"Token age {token_age / 60:.0f}min, proactively renewing")
                try:
                    self.auth._renew_token()
                except Exception as e:
                    logger.warning(f"Proactive token renewal failed: {e}")

        url = f"{self.base_url}{endpoint}"

        default_headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        if headers:
            default_headers.update(headers)

        session = self.auth.get_session()

        try:
            if method.upper() == 'GET':
                response = session.get(url, params=params, headers=default_headers, timeout=self.REQUEST_TIMEOUT)
            elif method.upper() == 'POST':
                response = session.post(url, params=params, json=json_data, headers=default_headers, timeout=self.REQUEST_TIMEOUT)
            elif method.upper() == 'PUT':
                response = session.put(url, params=params, json=json_data, headers=default_headers, timeout=self.REQUEST_TIMEOUT)
            else:
                raise ValueError(f"Unsupported method: {method}")

            # Handle 401 Unauthorized - token may have expired
            if response.status_code == 401 and retry_count == 0:
                logger.warning("HTTP 401 - attempting token renewal before retry")
                try:
                    if self.auth._renew_token():
                        logger.info("Token renewed successfully, retrying request")
                        return self._request(method, endpoint, params, json_data, headers, retry_count + 1)
                    else:
                        logger.error("Token renewal failed - re-authentication required")
                except Exception as e:
                    logger.error(f"Token renewal error during 401 recovery: {e}")

            # Handle 429 Too Many Requests - back off and retry
            if response.status_code == 429 and retry_count < self.MAX_ORDER_RETRIES:
                delay = self.RETRY_BASE_DELAY * (2 ** (retry_count + 1))  # Start with longer delay
                logger.warning(f"Rate limited (429), backing off {delay:.1f}s (attempt {retry_count + 1})")
                time.sleep(delay)
                return self._request(method, endpoint, params, json_data, headers, retry_count + 1)

            # Check for transient errors that can be retried
            if response.status_code in (500, 502, 503, 504) and retry_count < self.MAX_ORDER_RETRIES:
                delay = self.RETRY_BASE_DELAY * (2 ** retry_count)
                logger.warning(
                    f"Transient API error ({response.status_code}), "
                    f"retrying in {delay:.1f}s (attempt {retry_count + 1}/{self.MAX_ORDER_RETRIES})"
                )
                time.sleep(delay)
                return self._request(method, endpoint, params, json_data, headers, retry_count + 1)

            if response.status_code >= 400:
                raise ETradeAPIError(
                    f"API error: {response.status_code}",
                    status_code=response.status_code,
                    response=response.text
                )

            # E*TRADE can return JSON or XML
            content_type = response.headers.get('Content-Type', '')

            if 'application/json' in content_type:
                return response.json()
            elif 'application/xml' in content_type or 'text/xml' in content_type:
                return self._xml_to_dict(response.text)
            else:
                try:
                    return response.json()
                except (ValueError, KeyError, TypeError):
                    return {'raw': response.text}

        except ETradeAPIError:
            raise
        except Exception as e:
            if retry_count < self.MAX_ORDER_RETRIES:
                delay = self.RETRY_BASE_DELAY * (2 ** retry_count)
                logger.warning(
                    f"Request error: {e}, retrying in {delay:.1f}s "
                    f"(attempt {retry_count + 1}/{self.MAX_ORDER_RETRIES})"
                )
                time.sleep(delay)
                return self._request(method, endpoint, params, json_data, headers, retry_count + 1)
            raise ETradeAPIError(f"Request failed after {self.MAX_ORDER_RETRIES} retries: {e}")

    def _xml_to_dict(self, xml_string: str) -> Dict:
        """Convert XML response to dictionary"""
        try:
            root = ElementTree.fromstring(xml_string)
            return self._element_to_dict(root)
        except Exception as e:
            return {'raw': xml_string, 'parse_error': str(e)}

    def _element_to_dict(self, element) -> Dict:
        """Recursively convert XML element to dict"""
        result = {}

        for child in element:
            child_data = self._element_to_dict(child) if len(child) > 0 else child.text

            if child.tag in result:
                if not isinstance(result[child.tag], list):
                    result[child.tag] = [result[child.tag]]
                result[child.tag].append(child_data)
            else:
                result[child.tag] = child_data

        return result

    # =========================================================================
    # CONNECTION & ACCOUNT METHODS
    # =========================================================================

    def connect(self) -> bool:
        """
        Authenticate and connect to E*TRADE.
        Returns True if successful.
        """
        if not self.auth.is_authenticated():
            if not self.auth.authenticate():
                return False

        try:
            self._load_accounts()
            return True
        except Exception as e:
            logger.error(f"Error loading accounts: {e}")
            return False

    def _load_accounts(self) -> None:
        """Load account list and find account_id_key"""
        response = self._request('GET', self.ACCOUNTS_LIST)

        accounts_response = response.get('AccountListResponse', response)
        accounts = accounts_response.get('Accounts', {}).get('Account', [])

        if not isinstance(accounts, list):
            accounts = [accounts]

        self.accounts = accounts

        for account in accounts:
            account_id = account.get('accountId')
            if account_id == self.configured_account_id:
                self.account_id_key = account.get('accountIdKey')
                break

        if not self.account_id_key and accounts:
            self.account_id_key = accounts[0].get('accountIdKey')
            fallback_id = accounts[0].get('accountId', '')
            masked = f"****{fallback_id[-4:]}" if len(fallback_id) > 4 else '****'
            logger.warning(f"Configured account not found, using: {masked}")

    def get_accounts(self) -> List[Dict]:
        """Get list of all accounts"""
        if not self.accounts:
            self._load_accounts()
        return self.accounts

    def get_account_balance(self) -> Dict:
        """Get account balance information"""
        if not self.account_id_key:
            self._load_accounts()

        endpoint = self.ACCOUNT_BALANCE.format(account_id_key=self.account_id_key)
        params = {
            'instType': 'BROKERAGE',
            'realTimeNAV': 'true'
        }

        response = self._request('GET', endpoint, params=params)
        balance_response = response.get('BalanceResponse', response)
        computed = balance_response.get('Computed', {})

        return {
            'net_account_value': float(computed.get('RealTimeValues', {}).get('totalAccountValue', 0)),
            'cash_available': float(computed.get('cashAvailableForInvestment', 0)),
            'buying_power': float(computed.get('RealTimeValues', {}).get('totalAccountValue', 0)),
            'settled_cash': float(computed.get('settledCashForInvestment', 0)),
            'margin_buying_power': float(computed.get('marginBuyingPower', 0))
        }

    def get_open_positions(self) -> List[Dict]:
        """Fetch open positions from E*TRADE account for reconciliation."""
        if not self.account_id_key:
            self._load_accounts()

        endpoint = self.ACCOUNT_PORTFOLIO.format(account_id_key=self.account_id_key)
        try:
            response = self._request('GET', endpoint)
            portfolio_response = response.get('PortfolioResponse', response)
            account_portfolios = portfolio_response.get('AccountPortfolio', [])

            if not isinstance(account_portfolios, list):
                account_portfolios = [account_portfolios]

            positions = []
            for portfolio in account_portfolios:
                for pos in portfolio.get('Position', []):
                    product = pos.get('Product', {})
                    positions.append({
                        'symbol': product.get('symbol', ''),
                        'security_type': product.get('securityType', ''),
                        'quantity': int(pos.get('quantity', 0)),
                        'market_value': float(pos.get('marketValue', 0)),
                    })
            return positions
        except ETradeAPIError as e:
            if e.status_code == 204:
                return []  # No positions
            logger.error(f"Failed to fetch positions: {e.message}")
            return []

    # =========================================================================
    # MARKET DATA METHODS
    # =========================================================================

    def get_current_price(self, symbol: str) -> float:
        """Get current market price for symbol"""
        quote = self.get_quote(symbol)
        return quote.get('lastTrade', quote.get('last', 0))

    def get_quote(self, symbol: str) -> Dict:
        """Get detailed quote for symbol"""
        # E*TRADE uses $SPX.X for SPX index
        if symbol.upper() == 'SPX':
            symbol = '$SPX.X'

        endpoint = self.MARKET_QUOTE.format(symbols=symbol)
        params = {'detailFlag': 'ALL'}

        response = self._request('GET', endpoint, params=params)

        quote_response = response.get('QuoteResponse', response)
        quote_data = quote_response.get('QuoteData', [])

        if isinstance(quote_data, list):
            if not quote_data:
                logger.warning(f"Empty QuoteData for {symbol}")
                return {'symbol': symbol, 'lastTrade': 0, 'last': 0, 'bid': 0, 'ask': 0}
            quote_data = quote_data[0]

        all_data = quote_data.get('All', {}) if isinstance(quote_data, dict) else {}
        product = quote_data.get('Product', {}) if isinstance(quote_data, dict) else {}

        def _safe_float(val, default=0):
            try:
                return float(val) if val is not None else default
            except (ValueError, TypeError):
                return default

        return {
            'symbol': product.get('symbol', symbol),
            'lastTrade': _safe_float(all_data.get('lastTrade', 0)),
            'last': _safe_float(all_data.get('lastTrade', 0)),
            'bid': _safe_float(all_data.get('bid', 0)),
            'ask': _safe_float(all_data.get('ask', 0)),
            'high': _safe_float(all_data.get('high', 0)),
            'low': _safe_float(all_data.get('low', 0)),
            'open': _safe_float(all_data.get('open', 0)),
            'close': _safe_float(all_data.get('previousClose', 0)),
            'volume': int(_safe_float(all_data.get('totalVolume', 0))),
            'changeClose': _safe_float(all_data.get('changeClose', 0)),
            'changeClosePercentage': _safe_float(all_data.get('changeClosePercentage', 0)),
            'timestamp': all_data.get('dateTimeUTC', '')
        }

    def get_option_expirations(self, symbol: str, debug: bool = False) -> List[str]:
        """Get available option expiration dates"""
        if symbol.upper() == 'SPX':
            symbol = 'SPX'

        endpoint = self.OPTION_EXPIRE_DATE
        params = {'symbol': symbol}

        response = self._request('GET', endpoint, params=params)

        if debug:
            logger.debug(f"Raw expiration response keys: {list(response.keys()) if isinstance(response, dict) else type(response).__name__}")

        expire_response = response.get('OptionExpireDateResponse', response)
        expire_dates = expire_response.get('ExpirationDate', [])

        if not isinstance(expire_dates, list):
            expire_dates = [expire_dates]

        dates = []
        for exp in expire_dates:
            if isinstance(exp, dict):
                year = exp.get('year')
                month = exp.get('month')
                day = exp.get('day')
                if year and month and day:
                    try:
                        dates.append(f"{int(year)}-{int(month):02d}-{int(day):02d}")
                    except (ValueError, TypeError) as e:
                        if debug:
                            logger.debug(f"Failed to parse date {exp}: {e}")
            elif isinstance(exp, str):
                dates.append(exp)

        return sorted(dates)

    def get_options_chain(self, symbol: str, expiration: str) -> Dict[float, Dict]:
        """
        Get options chain for symbol.
        Returns: {strike: {'call_bid': x, 'call_ask': x, 'put_bid': x, 'put_ask': x, ...}}
        """
        if symbol.upper() == 'SPX':
            symbol = 'SPX'

        exp_parts = expiration.split('-')
        if len(exp_parts) != 3:
            logger.error(f"Invalid expiration format: {expiration} (expected YYYY-MM-DD)")
            return {}
        try:
            exp_year = exp_parts[0]
            exp_month = int(exp_parts[1])
            exp_day = int(exp_parts[2])
        except ValueError:
            logger.error(f"Non-numeric expiration components: {expiration}")
            return {}

        endpoint = self.OPTION_CHAINS
        params = {
            'symbol': symbol,
            'expiryYear': exp_year,
            'expiryMonth': exp_month,
            'expiryDay': exp_day,
            'includeWeekly': 'true',
            'noOfStrikes': 50,
            'strikePriceNear': 0,
            'optionCategory': 'STANDARD'
        }

        response = self._request('GET', endpoint, params=params)

        chain_response = response.get('OptionChainResponse', response)
        option_pairs = chain_response.get('OptionPair', [])

        if not isinstance(option_pairs, list):
            option_pairs = [option_pairs]

        def _sf(val, default=0):
            """Safe float conversion for API values that may be non-numeric."""
            try:
                return float(val) if val is not None else default
            except (ValueError, TypeError):
                return default

        def _si(val, default=0):
            """Safe int conversion."""
            try:
                return int(float(val)) if val is not None else default
            except (ValueError, TypeError):
                return default

        chain = {}
        for pair in option_pairs:
            call = pair.get('Call', {})
            put = pair.get('Put', {})

            strike = _sf(call.get('strikePrice') or put.get('strikePrice'))

            if strike > 0:
                chain[strike] = {
                    'call_bid': _sf(call.get('bid')),
                    'call_ask': _sf(call.get('ask')),
                    'call_last': _sf(call.get('lastPrice')),
                    'call_volume': _si(call.get('volume')),
                    'call_oi': _si(call.get('openInterest')),
                    'call_symbol': call.get('optionSymbol', ''),
                    'put_bid': _sf(put.get('bid')),
                    'put_ask': _sf(put.get('ask')),
                    'put_last': _sf(put.get('lastPrice')),
                    'put_volume': _si(put.get('volume')),
                    'put_oi': _si(put.get('openInterest')),
                    'put_symbol': put.get('optionSymbol', '')
                }

        return dict(sorted(chain.items()))

    # =========================================================================
    # ORDER METHODS - WITH SAFETY FEATURES
    # =========================================================================

    def _log_order_details(
        self,
        action: str,
        spread: CreditSpread,
        quantity: int,
        limit_price: float,
        dry_run: bool
    ) -> None:
        """Log full order details BEFORE submission"""
        logger.info("=" * 60)
        logger.info(f"ORDER {'PREVIEW' if dry_run else 'SUBMISSION'}: {action}")
        logger.info("=" * 60)
        logger.info(f"  Direction:      {spread.direction.value.upper()}")
        logger.info(f"  Short Strike:   ${spread.short_leg.strike:,.0f} ({spread.short_leg.option_type})")
        logger.info(f"  Long Strike:    ${spread.long_leg.strike:,.0f} ({spread.long_leg.option_type})")
        logger.info(f"  Spread Width:   ${spread.spread_width:,.2f}")
        logger.info(f"  Quantity:       {quantity} contracts")
        logger.info(f"  Limit Price:    ${limit_price:.2f} net credit")
        logger.info(f"  Total Credit:   ${limit_price * quantity * 100:,.2f}")
        logger.info(f"  Max Risk:       ${spread.max_risk * quantity:,.2f}")
        logger.info(f"  Expiration:     {spread.expiration}")
        logger.info(f"  Underlying:     ${spread.underlying_price_at_entry:,.2f}")
        logger.info(f"  Dry Run:        {dry_run}")
        logger.info("=" * 60)

    def _build_spread_order(
        self,
        spread: CreditSpread,
        quantity: int,
        limit_price: float,
        action: str = 'OPEN',
        client_order_id: Optional[str] = None
    ) -> Dict:
        """
        Build order request for vertical spread.

        CRITICAL: Always uses LIMIT orders, never MARKET.
        """
        if action == 'OPEN':
            short_action = 'SELL_OPEN'
            long_action = 'BUY_OPEN'
            price_type = 'NET_CREDIT'
        else:
            short_action = 'BUY_CLOSE'
            long_action = 'SELL_CLOSE'
            price_type = 'NET_DEBIT'

        order_id = client_order_id or f"spx_{datetime.now().strftime('%Y%m%d%H%M%S')}"

        # Determine option symbols from chain if available
        short_symbol = spread.short_leg.symbol or 'SPX'
        long_symbol = spread.long_leg.symbol or 'SPX'

        order_data = {
            'PreviewOrderRequest': {
                'orderType': 'SPREADS',
                'clientOrderId': order_id,
                'Order': [{
                    'allOrNone': 'false',
                    'priceType': price_type,
                    'limitPrice': limit_price,
                    'orderTerm': 'GOOD_FOR_DAY',
                    'marketSession': 'REGULAR',
                    'Instrument': [
                        {
                            'Product': {
                                'symbol': short_symbol,
                                'securityType': 'OPTN',
                                'callPut': spread.short_leg.option_type.upper(),
                                'strikePrice': spread.short_leg.strike,
                                'expiryYear': spread.expiration.year,
                                'expiryMonth': spread.expiration.month,
                                'expiryDay': spread.expiration.day
                            },
                            'orderAction': short_action,
                            'orderedQuantity': quantity,
                            'quantity': quantity
                        },
                        {
                            'Product': {
                                'symbol': long_symbol,
                                'securityType': 'OPTN',
                                'callPut': spread.long_leg.option_type.upper(),
                                'strikePrice': spread.long_leg.strike,
                                'expiryYear': spread.expiration.year,
                                'expiryMonth': spread.expiration.month,
                                'expiryDay': spread.expiration.day
                            },
                            'orderAction': long_action,
                            'orderedQuantity': quantity,
                            'quantity': quantity
                        }
                    ]
                }]
            }
        }

        return order_data

    def _preview_order(self, order_data: Dict) -> Tuple[bool, str, Dict]:
        """
        Preview order before placement.

        Returns: (success, preview_id, preview_response)
        """
        try:
            endpoint = self.ORDER_PREVIEW.format(account_id_key=self.account_id_key)
            response = self._request('POST', endpoint, json_data=order_data)

            preview_response = response.get('PreviewOrderResponse', response)
            preview_ids = preview_response.get('PreviewIds', [])

            if isinstance(preview_ids, list) and preview_ids:
                preview_id = preview_ids[0].get('previewId')
            elif isinstance(preview_ids, dict):
                preview_id = preview_ids.get('previewId')
            else:
                preview_id = None

            if not preview_id:
                return False, '', {'error': 'No preview ID returned'}

            return True, preview_id, preview_response

        except ETradeAPIError as e:
            logger.error(f"Order preview failed: {e.message}")
            return False, '', {'error': e.message, 'response': e.response}

    def _place_order(self, order_data: Dict, preview_id: str) -> Tuple[bool, str, Dict]:
        """
        Place order after preview.

        Returns: (success, order_id, place_response)
        """
        # Convert preview request to place request
        place_data = {
            'PlaceOrderRequest': order_data['PreviewOrderRequest'].copy()
        }
        place_data['PlaceOrderRequest']['PreviewIds'] = [{'previewId': preview_id}]

        try:
            endpoint = self.ORDER_PLACE.format(account_id_key=self.account_id_key)
            response = self._request('POST', endpoint, json_data=place_data)

            place_response = response.get('PlaceOrderResponse', response)
            order_ids = place_response.get('OrderIds', [])

            if isinstance(order_ids, list) and order_ids:
                order_id = str(order_ids[0].get('orderId'))
            elif isinstance(order_ids, dict):
                order_id = str(order_ids.get('orderId'))
            else:
                order_id = None

            if not order_id:
                return False, '', {'error': 'No order ID returned'}

            return True, order_id, place_response

        except ETradeAPIError as e:
            logger.error(f"Order placement failed: {e.message}")
            return False, '', {'error': e.message, 'response': e.response}

    def _wait_for_fill(
        self,
        order_id: str,
        expected_price: float,
        timeout: Optional[int] = None
    ) -> OrderResult:
        """
        Poll order status until filled or timeout.

        Returns OrderResult with fill details.
        """
        timeout = timeout or self.order_timeout
        start_time = time.time()
        polls = 0

        while polls < self.MAX_ORDER_POLLS and (time.time() - start_time) < timeout:
            polls += 1
            time.sleep(self.ORDER_POLL_INTERVAL)

            status = self.get_order_status(order_id)
            raw_status = status.get('status', 'not_found')
            order_status = self._NORMALIZED_TO_ENUM.get(raw_status, OrderStatus.NOT_FOUND)

            logger.debug(f"Order {order_id} poll {polls}: {order_status.value}")

            if order_status == OrderStatus.EXECUTED:
                fill_price = status.get('fill_price', 0)
                filled_qty = status.get('filled_quantity', 0)

                # Log fill price vs expected
                price_diff = fill_price - expected_price
                logger.info(
                    f"Order {order_id} FILLED: {filled_qty} contracts "
                    f"@ ${fill_price:.2f} (expected ${expected_price:.2f}, "
                    f"diff ${price_diff:+.2f})"
                )

                return OrderResult(
                    success=True,
                    order_id=order_id,
                    status=order_status,
                    fill_price=fill_price,
                    filled_quantity=filled_qty,
                    message=f"Filled at ${fill_price:.2f}"
                )

            elif order_status == OrderStatus.PARTIAL:
                filled_qty = status.get('filled_quantity', 0)
                fill_price = status.get('fill_price', 0)
                logger.warning(
                    f"Order {order_id} PARTIALLY filled: {filled_qty} contracts "
                    f"@ ${fill_price:.2f}. Treating as filled."
                )
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    status=OrderStatus.EXECUTED,
                    fill_price=fill_price,
                    filled_quantity=filled_qty,
                    message=f"Partially filled {filled_qty} @ ${fill_price:.2f}"
                )

            elif order_status in (OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
                logger.warning(f"Order {order_id} ended with status: {order_status.value}")
                return OrderResult(
                    success=False,
                    order_id=order_id,
                    status=order_status,
                    fill_price=0,
                    filled_quantity=0,
                    message=f"Order {order_status.value}"
                )

        # Timeout - cancel the order
        logger.warning(f"Order {order_id} did not fill within {timeout}s, cancelling...")
        cancelled = self._cancel_order(order_id)
        if not cancelled:
            logger.error(
                f"ALERT: Failed to cancel order {order_id}! "
                f"Order may still be live on E*TRADE. Manual review required."
            )

        return OrderResult(
            success=False,
            order_id=order_id,
            status=OrderStatus.CANCELLED,
            fill_price=0,
            filled_quantity=0,
            message=f"Timeout after {timeout}s - order cancelled"
        )

    def _cancel_order(self, order_id: str) -> bool:
        """Cancel an open order"""
        try:
            endpoint = self.ORDER_CANCEL.format(account_id_key=self.account_id_key)
            cancel_data = {
                'CancelOrderRequest': {
                    'orderId': int(order_id)
                }
            }
            response = self._request('PUT', endpoint, json_data=cancel_data)
            logger.info(f"Order {order_id} cancelled")
            return True
        except ETradeAPIError as e:
            logger.error(f"Failed to cancel order {order_id}: {e.message}")
            return False

    def place_spread_order(
        self,
        spread: CreditSpread,
        quantity: int,
        limit_price: Optional[float] = None,
        metadata: Optional[Dict] = None,
        dry_run: bool = False
    ) -> str:
        """
        Place vertical spread order.

        Args:
            spread: The credit spread to trade
            quantity: Number of contracts
            limit_price: Limit price (net credit). Defaults to spread.credit_received
            metadata: Optional extra context for logging
            dry_run: If True, preview only - do not place order

        Returns:
            order_id if successful, empty string if failed

        SAFETY FEATURES:
        - Always uses LIMIT orders
        - Logs full details before submission
        - Previews order first
        - Polls for fill confirmation
        - Cancels unfilled orders after timeout
        """
        if not self.account_id_key:
            self._load_accounts()

        # Validate inputs
        if quantity <= 0:
            logger.error(f"Invalid quantity: {quantity} (must be positive)")
            return ''

        # Default limit price to the credit received
        limit_price = limit_price if limit_price is not None else spread.credit_received

        if limit_price <= 0:
            logger.error(f"Invalid limit price: {limit_price} (must be positive)")
            return ''

        # SAFETY: Log full order details before any action
        self._log_order_details('OPEN', spread, quantity, limit_price, dry_run)

        # Build order request
        order_data = self._build_spread_order(spread, quantity, limit_price, action='OPEN')

        # Preview order
        success, preview_id, preview_response = self._preview_order(order_data)

        if not success:
            logger.error(f"Order preview failed: {preview_response}")
            return ''

        logger.info(f"Order preview successful, preview_id={preview_id}")

        # If dry_run, stop here
        if dry_run:
            logger.info("[DRY RUN] Order previewed but NOT placed")
            return f"PREVIEW-{preview_id}"

        # SAFETY: Place order
        success, order_id, place_response = self._place_order(order_data, preview_id)

        if not success:
            logger.error(f"Order placement failed: {place_response}")
            return ''

        logger.info(f"Order placed, order_id={order_id}")

        # SAFETY: Wait for fill confirmation
        result = self._wait_for_fill(order_id, limit_price)

        if not result.success:
            logger.warning(f"Order {order_id} did not fill: {result.message}")
            # Return empty string to indicate failure - let bot decide next action
            return ''

        return order_id

    def close_position(
        self,
        spread: CreditSpread,
        quantity: int,
        limit_price: float = 0.05,
        dry_run: bool = False
    ) -> str:
        """
        Close an existing spread position.

        Args:
            spread: The spread to close
            quantity: Number of contracts
            limit_price: Maximum debit to pay (for buying back the spread)
            dry_run: If True, preview only

        Returns:
            order_id if successful, empty string if failed
        """
        if not self.account_id_key:
            self._load_accounts()

        # Validate inputs
        if quantity <= 0:
            logger.error(f"Invalid close quantity: {quantity}")
            return ''
        if limit_price < 0:
            logger.error(f"Invalid close limit price: {limit_price}")
            return ''

        # SAFETY: Log full order details before any action
        self._log_order_details('CLOSE', spread, quantity, limit_price, dry_run)

        # Build close order request
        order_data = self._build_spread_order(spread, quantity, limit_price, action='CLOSE')

        # Preview order
        success, preview_id, preview_response = self._preview_order(order_data)

        if not success:
            logger.error(f"Close order preview failed: {preview_response}")
            return ''

        logger.info(f"Close order preview successful, preview_id={preview_id}")

        if dry_run:
            logger.info("[DRY RUN] Close order previewed but NOT placed")
            return f"PREVIEW-{preview_id}"

        # Place order
        success, order_id, place_response = self._place_order(order_data, preview_id)

        if not success:
            logger.error(f"Close order placement failed: {place_response}")
            return ''

        logger.info(f"Close order placed, order_id={order_id}")

        # Wait for fill confirmation
        result = self._wait_for_fill(order_id, limit_price)

        if not result.success:
            logger.warning(f"Close order {order_id} did not fill: {result.message}")
            return ''

        return order_id

    # Alias for interface compatibility
    def close_spread(
        self,
        spread: CreditSpread,
        quantity: int,
        limit_price: float = 0.05,
        dry_run: bool = False
    ) -> str:
        """Alias for close_position for interface compatibility"""
        return self.close_position(spread, quantity, limit_price, dry_run)

    # Map E*TRADE status strings to the normalized values position_manager expects
    _STATUS_MAP = {
        'EXECUTED': 'filled',
        'OPEN': 'open',
        'PENDING': 'pending',
        'CANCELLED': 'cancelled',
        'CANCEL_REQUESTED': 'cancel_requested',
        'EXPIRED': 'expired',
        'REJECTED': 'rejected',
        'PARTIAL': 'partial',
    }

    # Reverse map: normalized lowercase strings -> OrderStatus enum
    _NORMALIZED_TO_ENUM = {
        'filled': OrderStatus.EXECUTED,
        'open': OrderStatus.OPEN,
        'pending': OrderStatus.PENDING,
        'cancelled': OrderStatus.CANCELLED,
        'cancel_requested': OrderStatus.CANCEL_REQUESTED,
        'expired': OrderStatus.EXPIRED,
        'rejected': OrderStatus.REJECTED,
        'partial': OrderStatus.PARTIAL,
        'not_found': OrderStatus.NOT_FOUND,
    }

    def get_order_status(self, order_id: str) -> Dict:
        """Get order status and fill information.

        Returns normalized status strings (lowercase) that match what
        position_manager expects: 'filled', 'open', 'cancelled', etc.
        """
        endpoint = self.ORDERS_LIST.format(account_id_key=self.account_id_key)
        params = {'orderId': order_id}

        try:
            response = self._request('GET', endpoint, params=params)

            orders_response = response.get('OrdersResponse', response)
            orders = orders_response.get('Order', [])

            if not isinstance(orders, list):
                orders = [orders]

            for order in orders:
                if str(order.get('orderId')) == str(order_id):
                    order_detail = order.get('OrderDetail', [{}])
                    if isinstance(order_detail, list):
                        order_detail = order_detail[0]

                    raw_status = order_detail.get('status', 'UNKNOWN')
                    normalized = self._STATUS_MAP.get(raw_status, raw_status.lower())

                    return {
                        'status': normalized,
                        'fill_price': float(order_detail.get('executedPrice', 0) or 0),
                        'filled_quantity': int(order_detail.get('filledQuantity', 0) or 0),
                        'order_type': order_detail.get('orderType'),
                        'placed_time': order_detail.get('placedTime'),
                        'executed_time': order_detail.get('executedTime')
                    }

        except ETradeAPIError as e:
            logger.error(f"Error getting order status: {e.message}")

        return {'status': 'not_found', 'fill_price': 0, 'filled_quantity': 0}

    def get_position_value(self, spread: CreditSpread) -> float:
        """Get conservative cost to close a short credit spread.

        Uses ask-side pricing: buy back short at ASK, sell long at BID.
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
            short_ask = chain[short_strike]['put_ask']
            long_bid = chain[long_strike]['put_bid']
        else:
            short_ask = chain[short_strike]['call_ask']
            long_bid = chain[long_strike]['call_bid']

        # Cost to close = buy back short at ASK, sell long at BID
        spread_value = max(0.0, short_ask - long_bid)
        return spread_value

    def get_orders(self, status: Optional[str] = None) -> List[Dict]:
        """Get orders for account, optionally filtered by status"""
        endpoint = self.ORDERS_LIST.format(account_id_key=self.account_id_key)
        params = {}
        if status:
            params['status'] = status

        response = self._request('GET', endpoint, params=params)

        orders_response = response.get('OrdersResponse', response)
        orders = orders_response.get('Order', [])

        if not isinstance(orders, list):
            orders = [orders]

        return orders
