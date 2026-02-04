"""
E*TRADE Broker Implementation

Implements the BrokerInterface for E*TRADE API integration.
Supports both sandbox and production environments.
"""

import json
import re
from datetime import datetime, date
from typing import Dict, List, Optional, Any
from xml.etree import ElementTree

from .base import BrokerInterface
from .etrade_auth import ETradeAuth
from ..models.spread import CreditSpread, OptionLeg, TradeDirection

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config.settings import ETRADE_CONFIG


class ETradeAPIError(Exception):
    """E*TRADE API error"""
    def __init__(self, message: str, status_code: int = None, response: str = None):
        self.message = message
        self.status_code = status_code
        self.response = response
        super().__init__(self.message)


class ETradeBroker(BrokerInterface):
    """E*TRADE implementation of BrokerInterface"""

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

    def __init__(self, auth: Optional[ETradeAuth] = None, account_id: Optional[str] = None):
        self.auth = auth or ETradeAuth()
        self.configured_account_id = account_id or ETRADE_CONFIG['account_id']
        self.base_url = ETRADE_CONFIG['base_url']

        # Will be populated after authentication
        self.account_id_key: Optional[str] = None
        self.accounts: List[Dict] = []

    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
        headers: Optional[Dict] = None
    ) -> Dict:
        """Make authenticated API request"""
        if not self.auth.is_authenticated():
            raise ETradeAPIError("Not authenticated")

        url = f"{self.base_url}{endpoint}"

        default_headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        if headers:
            default_headers.update(headers)

        session = self.auth.get_session()

        if method.upper() == 'GET':
            response = session.get(url, params=params, headers=default_headers)
        elif method.upper() == 'POST':
            response = session.post(url, params=params, json=json_data, headers=default_headers)
        else:
            raise ValueError(f"Unsupported method: {method}")

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
            # Try JSON first
            try:
                return response.json()
            except:
                return {'raw': response.text}

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

    def connect(self) -> bool:
        """
        Authenticate and connect to E*TRADE.
        Returns True if successful.
        """
        if not self.auth.is_authenticated():
            if not self.auth.authenticate():
                return False

        # Fetch accounts to get account_id_key
        try:
            self._load_accounts()
            return True
        except Exception as e:
            print(f"Error loading accounts: {e}")
            return False

    def _load_accounts(self) -> None:
        """Load account list and find account_id_key"""
        response = self._request('GET', self.ACCOUNTS_LIST)

        # Parse accounts from response
        accounts_response = response.get('AccountListResponse', response)
        accounts = accounts_response.get('Accounts', {}).get('Account', [])

        if not isinstance(accounts, list):
            accounts = [accounts]

        self.accounts = accounts

        # Find matching account
        for account in accounts:
            account_id = account.get('accountId')
            if account_id == self.configured_account_id:
                self.account_id_key = account.get('accountIdKey')
                break

        if not self.account_id_key and accounts:
            # Use first account if no match
            self.account_id_key = accounts[0].get('accountIdKey')
            print(f"Warning: Configured account not found, using: {accounts[0].get('accountId')}")

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

        if isinstance(quote_data, list) and quote_data:
            quote_data = quote_data[0]

        all_data = quote_data.get('All', {})
        product = quote_data.get('Product', {})

        return {
            'symbol': product.get('symbol', symbol),
            'lastTrade': float(all_data.get('lastTrade', 0)),
            'last': float(all_data.get('lastTrade', 0)),
            'bid': float(all_data.get('bid', 0)),
            'ask': float(all_data.get('ask', 0)),
            'high': float(all_data.get('high', 0)),
            'low': float(all_data.get('low', 0)),
            'open': float(all_data.get('open', 0)),
            'close': float(all_data.get('previousClose', 0)),
            'volume': int(all_data.get('totalVolume', 0)),
            'changeClose': float(all_data.get('changeClose', 0)),
            'changeClosePercentage': float(all_data.get('changeClosePercentage', 0)),
            'timestamp': all_data.get('dateTimeUTC', '')
        }

    def get_option_expirations(self, symbol: str, debug: bool = False) -> List[str]:
        """Get available option expiration dates"""
        if symbol.upper() == 'SPX':
            symbol = 'SPX'  # Use SPX for options, not $SPX.X

        endpoint = self.OPTION_EXPIRE_DATE
        params = {'symbol': symbol}

        response = self._request('GET', endpoint, params=params)

        if debug:
            print(f"  DEBUG: Raw expiration response: {response}")

        expire_response = response.get('OptionExpireDateResponse', response)
        expire_dates = expire_response.get('ExpirationDate', [])

        if not isinstance(expire_dates, list):
            expire_dates = [expire_dates]

        if debug:
            print(f"  DEBUG: Parsed expire_dates: {expire_dates}")

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
                            print(f"  DEBUG: Failed to parse date {exp}: {e}")
            elif isinstance(exp, str):
                # Handle string format if returned that way
                dates.append(exp)

        return sorted(dates)

    def get_options_chain(self, symbol: str, expiration: str) -> Dict[float, Dict]:
        """
        Get options chain for symbol
        Returns: {strike: {'call_bid': x, 'call_ask': x, 'put_bid': x, 'put_ask': x}}
        """
        if symbol.upper() == 'SPX':
            symbol = 'SPX'

        # Parse expiration date
        exp_parts = expiration.split('-')
        exp_year = exp_parts[0]
        exp_month = int(exp_parts[1])
        exp_day = int(exp_parts[2])

        endpoint = self.OPTION_CHAINS
        params = {
            'symbol': symbol,
            'expiryYear': exp_year,
            'expiryMonth': exp_month,
            'expiryDay': exp_day,
            'includeWeekly': 'true',
            'noOfStrikes': 50,  # Get 50 strikes around ATM
            'strikePriceNear': 0,  # Auto-detect ATM
            'optionCategory': 'STANDARD'
        }

        response = self._request('GET', endpoint, params=params)

        chain_response = response.get('OptionChainResponse', response)
        option_pairs = chain_response.get('OptionPair', [])

        if not isinstance(option_pairs, list):
            option_pairs = [option_pairs]

        chain = {}
        for pair in option_pairs:
            call = pair.get('Call', {})
            put = pair.get('Put', {})

            strike = float(call.get('strikePrice', 0) or put.get('strikePrice', 0))

            if strike > 0:
                chain[strike] = {
                    'call_bid': float(call.get('bid', 0) or 0),
                    'call_ask': float(call.get('ask', 0) or 0),
                    'call_last': float(call.get('lastPrice', 0) or 0),
                    'call_volume': int(call.get('volume', 0) or 0),
                    'call_oi': int(call.get('openInterest', 0) or 0),
                    'call_symbol': call.get('optionSymbol', ''),
                    'put_bid': float(put.get('bid', 0) or 0),
                    'put_ask': float(put.get('ask', 0) or 0),
                    'put_last': float(put.get('lastPrice', 0) or 0),
                    'put_volume': int(put.get('volume', 0) or 0),
                    'put_oi': int(put.get('openInterest', 0) or 0),
                    'put_symbol': put.get('optionSymbol', '')
                }

        return dict(sorted(chain.items()))

    def place_spread_order(
        self,
        spread: CreditSpread,
        quantity: int,
        limit_price: Optional[float] = None,
        metadata: Optional[Dict] = None
    ) -> str:
        """Place vertical spread order"""
        # Build order request
        order_data = self._build_spread_order(spread, quantity, limit_price, action='OPEN')

        # Preview order first
        preview_endpoint = self.ORDER_PREVIEW.format(account_id_key=self.account_id_key)
        preview_response = self._request('POST', preview_endpoint, json_data=order_data)

        # Extract preview ID
        preview_id = preview_response.get('PreviewOrderResponse', {}).get('PreviewIds', [{}])[0].get('previewId')

        if not preview_id:
            raise ETradeAPIError("Could not get preview ID for order")

        # Place order
        order_data['PlaceOrderRequest']['PreviewIds'] = [{'previewId': preview_id}]
        place_endpoint = self.ORDER_PLACE.format(account_id_key=self.account_id_key)
        place_response = self._request('POST', place_endpoint, json_data=order_data)

        # Extract order ID
        order_id = place_response.get('PlaceOrderResponse', {}).get('OrderIds', [{}])[0].get('orderId')

        return str(order_id)

    def _build_spread_order(
        self,
        spread: CreditSpread,
        quantity: int,
        limit_price: Optional[float],
        action: str = 'OPEN'
    ) -> Dict:
        """Build order request for spread"""
        # Determine order action based on spread direction and open/close
        if action == 'OPEN':
            # Opening: sell short leg, buy long leg
            short_action = 'SELL_OPEN'
            long_action = 'BUY_OPEN'
        else:
            # Closing: buy short leg, sell long leg
            short_action = 'BUY_CLOSE'
            long_action = 'SELL_CLOSE'

        order_data = {
            'PlaceOrderRequest' if action == 'CLOSE' else 'PreviewOrderRequest': {
                'orderType': 'SPREADS',
                'clientOrderId': f"spx_{datetime.now().strftime('%Y%m%d%H%M%S')}",
                'Order': [{
                    'allOrNone': 'false',
                    'priceType': 'NET_CREDIT' if limit_price else 'MARKET',
                    'limitPrice': limit_price,
                    'orderTerm': 'GOOD_FOR_DAY',
                    'marketSession': 'REGULAR',
                    'Instrument': [
                        {
                            'Product': {
                                'symbol': spread.short_leg.symbol or 'SPX',
                                'securityType': 'OPTN'
                            },
                            'orderAction': short_action,
                            'orderedQuantity': quantity,
                            'quantity': quantity
                        },
                        {
                            'Product': {
                                'symbol': spread.long_leg.symbol or 'SPX',
                                'securityType': 'OPTN'
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

    def get_order_status(self, order_id: str) -> Dict:
        """Get order status and fill information"""
        endpoint = self.ORDERS_LIST.format(account_id_key=self.account_id_key)
        params = {'orderId': order_id}

        response = self._request('GET', endpoint, params=params)

        orders_response = response.get('OrdersResponse', response)
        orders = orders_response.get('Order', [])

        if not isinstance(orders, list):
            orders = [orders]

        for order in orders:
            if str(order.get('orderId')) == str(order_id):
                order_detail = order.get('OrderDetail', [{}])[0]

                return {
                    'status': order_detail.get('status', 'UNKNOWN'),
                    'fill_price': float(order_detail.get('executedPrice', 0) or 0),
                    'filled_quantity': int(order_detail.get('filledQuantity', 0) or 0),
                    'order_type': order_detail.get('orderType'),
                    'placed_time': order_detail.get('placedTime'),
                    'executed_time': order_detail.get('executedTime')
                }

        return {'status': 'NOT_FOUND', 'fill_price': 0, 'filled_quantity': 0}

    def close_spread(
        self,
        spread: CreditSpread,
        quantity: int,
        limit_price: float = 0.20
    ) -> str:
        """Close existing spread"""
        order_data = self._build_spread_order(spread, quantity, limit_price, action='CLOSE')

        # Preview then place
        preview_endpoint = self.ORDER_PREVIEW.format(account_id_key=self.account_id_key)
        preview_response = self._request('POST', preview_endpoint, json_data=order_data)

        preview_id = preview_response.get('PreviewOrderResponse', {}).get('PreviewIds', [{}])[0].get('previewId')

        if not preview_id:
            raise ETradeAPIError("Could not get preview ID for close order")

        order_data['PlaceOrderRequest']['PreviewIds'] = [{'previewId': preview_id}]
        place_endpoint = self.ORDER_PLACE.format(account_id_key=self.account_id_key)
        place_response = self._request('POST', place_endpoint, json_data=order_data)

        order_id = place_response.get('PlaceOrderResponse', {}).get('OrderIds', [{}])[0].get('orderId')

        return str(order_id)

    def get_position_value(self, spread: CreditSpread) -> float:
        """Get current market value of spread"""
        # Get current options chain
        expiration = spread.expiration.strftime('%Y-%m-%d')
        chain = self.get_options_chain('SPX', expiration)

        short_strike = spread.short_leg.strike
        long_strike = spread.long_leg.strike

        if short_strike not in chain or long_strike not in chain:
            return 0

        if spread.direction == TradeDirection.BULLISH:
            # Put spread
            short_mid = (chain[short_strike]['put_bid'] + chain[short_strike]['put_ask']) / 2
            long_mid = (chain[long_strike]['put_bid'] + chain[long_strike]['put_ask']) / 2
        else:
            # Call spread
            short_mid = (chain[short_strike]['call_bid'] + chain[short_strike]['call_ask']) / 2
            long_mid = (chain[long_strike]['call_bid'] + chain[long_strike]['call_ask']) / 2

        # Spread value (what it would cost to close)
        spread_value = short_mid - long_mid

        return spread_value * 100  # Convert to dollars per contract

    def get_orders(self) -> List[Dict]:
        """Get all orders for account"""
        endpoint = self.ORDERS_LIST.format(account_id_key=self.account_id_key)

        response = self._request('GET', endpoint)

        orders_response = response.get('OrdersResponse', response)
        orders = orders_response.get('Order', [])

        if not isinstance(orders, list):
            orders = [orders]

        return orders
