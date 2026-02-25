"""
Interactive Brokers (IBKR) Broker Implementation

Fully functional implementation using ib_insync:
connect(), disconnect(), is_connected, get_account_balance(),
get_current_price(), get_options_chain(), place_spread_order(),
close_spread(), get_order_status(), get_position_value().

Requires TWS or IB Gateway running locally. ib_insync is lazy-imported
so the app won't crash if it's not installed and IBKR isn't the active broker.
"""

import logging
import math
import time
from typing import Dict, List, Optional

from .base import BrokerInterface

logger = logging.getLogger(__name__)

# Default connection ports
TWS_LIVE_PORT = 7496
TWS_PAPER_PORT = 7497
GATEWAY_LIVE_PORT = 4001
GATEWAY_PAPER_PORT = 4002


class IBKRError(Exception):
    """IBKR API error."""
    MAX_RESPONSE_LENGTH = 200

    def __init__(self, message: str, status_code: int = None, response: str = None):
        self.message = message
        self.status_code = status_code
        if response and len(response) > self.MAX_RESPONSE_LENGTH:
            self.response = response[:self.MAX_RESPONSE_LENGTH] + '...[truncated]'
        else:
            self.response = response
        super().__init__(self.message)


class IBKRBroker(BrokerInterface):
    """Interactive Brokers broker via ib_insync."""

    def __init__(self, host: str = '127.0.0.1', port: int = 7496,
                 client_id: int = 1, paper_trading: bool = False):
        self.host = host
        self.client_id = client_id
        self.paper_trading = paper_trading
        self._ib = None

        # Auto-switch to paper port if paper_trading is enabled and user
        # didn't override the port (i.e. left it at the default live port).
        if paper_trading and port == TWS_LIVE_PORT:
            self.port = TWS_PAPER_PORT
        else:
            self.port = port

    def connect(self) -> bool:
        """Connect to TWS / IB Gateway.

        Returns True on success. Raises IBKRError on failure.
        """
        try:
            from ib_insync import IB
        except ImportError:
            raise IBKRError(
                "ib_insync is not installed. Install it with: pip install ib_insync"
            )

        if self.is_connected:
            return True

        try:
            self._ib = IB()
            self._ib.connect(self.host, self.port, clientId=self.client_id)
            logger.info(
                f"Connected to IBKR at {self.host}:{self.port} "
                f"(client_id={self.client_id}, paper={self.paper_trading})"
            )
            return True
        except Exception as e:
            self._ib = None
            raise IBKRError(f"Failed to connect to IBKR: {e}")

    def disconnect(self):
        """Disconnect from TWS / IB Gateway."""
        if self._ib is not None:
            try:
                self._ib.disconnect()
            except Exception as e:
                logger.warning(f"Error during IBKR disconnect: {e}")
            self._ib = None
            logger.info("Disconnected from IBKR")

    @property
    def is_connected(self) -> bool:
        """True if actively connected to TWS / IB Gateway."""
        return self._ib is not None and self._ib.isConnected()

    def get_account_balance(self) -> Dict:
        """Get account balance from IBKR.

        Returns dict with net_account_value, cash_available, buying_power.
        """
        if not self.is_connected:
            raise IBKRError("Not connected to IBKR")

        summary = self._ib.accountSummary()

        result = {
            'net_account_value': 0.0,
            'cash_available': 0.0,
            'buying_power': 0.0,
        }

        tag_map = {
            'NetLiquidation': 'net_account_value',
            'AvailableFunds': 'cash_available',
            'BuyingPower': 'buying_power',
        }

        for item in summary:
            key = tag_map.get(item.tag)
            if key:
                try:
                    result[key] = float(item.value)
                except (ValueError, TypeError):
                    pass

        return result

    # ------------------------------------------------------------------
    # Contract helpers
    # ------------------------------------------------------------------

    def _make_spx_contract(self):
        """Create an ib_insync Contract for the SPX index."""
        from ib_insync import Contract
        return Contract(symbol='SPX', secType='IND', exchange='CBOE', currency='USD')

    def _make_option_contract(self, strike: float, right: str, expiry: str):
        """Create an ib_insync Option contract for SPX.

        Args:
            strike: Option strike price.
            right: 'C' for call, 'P' for put.
            expiry: Expiration date as 'YYYYMMDD'.
        """
        from ib_insync import Option
        return Option(
            symbol='SPX',
            lastTradeDateOrContractMonth=expiry,
            strike=strike,
            right=right,
            exchange='SMART',
            currency='USD',
        )

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_current_price(self, symbol: str) -> float:
        """Get current SPX price via snapshot market data.

        Uses reqMktData with snapshot=True. Returns ticker.last when
        available, falls back to ticker.close (handles pre-market when
        last is NaN).
        """
        if not self.is_connected:
            raise IBKRError("Not connected to IBKR")

        contract = self._make_spx_contract()
        ticker = self._ib.reqMktData(contract, '', True, False)
        self._ib.sleep(2)  # allow snapshot to fill

        price = ticker.last
        if price is None or (isinstance(price, float) and math.isnan(price)):
            price = ticker.close

        if price is None or (isinstance(price, float) and math.isnan(price)):
            raise IBKRError(
                f"No price available for {symbol}: last and close both NaN"
            )

        return float(price)

    # ------------------------------------------------------------------
    # Options chain
    # ------------------------------------------------------------------

    # How far above/below current price to include strikes (points).
    STRIKE_RANGE = 50

    def get_options_chain(self, symbol: str, expiration: str) -> Dict[float, Dict]:
        """Get options chain in the project-standard format.

        Args:
            symbol: Underlying symbol (e.g. 'SPX').
            expiration: Expiration date as 'YYYY-MM-DD'.

        Returns:
            {strike: {'call_bid': x, 'call_ask': x, 'put_bid': x, ...}}
            sorted by strike.  Returns empty dict on error.
        """
        if not self.is_connected:
            raise IBKRError("Not connected to IBKR")

        ib_expiry = expiration.replace('-', '')  # YYYY-MM-DD -> YYYYMMDD

        # Get current price for strike filtering
        current_price = self.get_current_price(symbol)

        # Discover available strikes via reqSecDefOptParams
        spx_contract = self._make_spx_contract()
        self._ib.qualifyContracts(spx_contract)
        params_list = self._ib.reqSecDefOptParams(
            spx_contract.symbol, '', spx_contract.secType, spx_contract.conId
        )

        # Collect strikes for our target expiry within range
        all_strikes = set()
        for params in params_list:
            if ib_expiry in params.expirations:
                for s in params.strikes:
                    if abs(s - current_price) <= self.STRIKE_RANGE:
                        all_strikes.add(s)

        if not all_strikes:
            logger.warning(
                f"No strikes found for {symbol} expiry {expiration} "
                f"within +/-{self.STRIKE_RANGE} of {current_price:.2f}"
            )
            return {}

        strikes = sorted(all_strikes)
        chain: Dict[float, Dict] = {}

        # Request snapshots for each strike (calls and puts)
        for strike in strikes:
            call_contract = self._make_option_contract(strike, 'C', ib_expiry)
            put_contract = self._make_option_contract(strike, 'P', ib_expiry)

            call_ticker = self._ib.reqMktData(call_contract, '', True, False)
            put_ticker = self._ib.reqMktData(put_contract, '', True, False)

        self._ib.sleep(2)  # allow all snapshots to fill

        # Now read the tickers back (reqMktData returns same ticker object)
        for strike in strikes:
            call_contract = self._make_option_contract(strike, 'C', ib_expiry)
            put_contract = self._make_option_contract(strike, 'P', ib_expiry)

            call_ticker = self._ib.reqMktData(call_contract, '', True, False)
            put_ticker = self._ib.reqMktData(put_contract, '', True, False)

            call_bid = self._safe_price(call_ticker.bid)
            call_ask = self._safe_price(call_ticker.ask)
            put_bid = self._safe_price(put_ticker.bid)
            put_ask = self._safe_price(put_ticker.ask)

            chain[strike] = {
                'call_bid': call_bid,
                'call_ask': call_ask,
                'call_last': self._safe_price(call_ticker.last),
                'call_volume': self._safe_int(call_ticker.volume),
                'call_oi': 0,
                'put_bid': put_bid,
                'put_ask': put_ask,
                'put_last': self._safe_price(put_ticker.last),
                'put_volume': self._safe_int(put_ticker.volume),
                'put_oi': 0,
            }

        return chain

    @staticmethod
    def _safe_price(val) -> float:
        """Return val as float, or 0.0 if NaN/None."""
        if val is None:
            return 0.0
        try:
            f = float(val)
            return 0.0 if math.isnan(f) else f
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _safe_int(val) -> int:
        """Return val as int, or 0 if NaN/None."""
        if val is None:
            return 0
        try:
            f = float(val)
            return 0 if math.isnan(f) else int(f)
        except (ValueError, TypeError):
            return 0

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    ORDER_TIMEOUT = 30  # seconds to wait for fill

    def _build_combo_contract(self, spread, short_action: str, long_action: str):
        """Build a BAG combo Contract with qualified option legs.

        Args:
            spread: CreditSpread with short_leg/long_leg.
            short_action: 'SELL' to open, 'BUY' to close.
            long_action: 'BUY' to open, 'SELL' to close.

        Returns ib_insync Contract with secType='BAG' and two ComboLegs.
        """
        from ib_insync import Contract, ComboLeg

        right = 'C' if spread.short_leg.option_type == 'call' else 'P'
        expiry = spread.expiration.strftime('%Y%m%d')

        short_opt = self._make_option_contract(spread.short_leg.strike, right, expiry)
        long_opt = self._make_option_contract(spread.long_leg.strike, right, expiry)
        self._ib.qualifyContracts(short_opt, long_opt)

        combo = Contract(
            symbol='SPX', secType='BAG', exchange='SMART', currency='USD',
            comboLegs=[
                ComboLeg(conId=short_opt.conId, ratio=1,
                         action=short_action, exchange='SMART'),
                ComboLeg(conId=long_opt.conId, ratio=1,
                         action=long_action, exchange='SMART'),
            ],
        )
        return combo

    def _wait_for_fill(self, trade, timeout: int):
        """Wait for a Trade to fill within timeout.

        Returns (order_id_str, fill_price, filled_qty, status_str).
        """
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            self._ib.waitOnUpdate(timeout=1)
            status = trade.orderStatus.status

            if status == 'Filled':
                return (
                    str(trade.order.orderId),
                    abs(trade.orderStatus.avgFillPrice),
                    int(trade.orderStatus.filled),
                    'filled',
                )
            if status in ('Cancelled', 'ApiCancelled', 'Inactive'):
                return (str(trade.order.orderId), 0.0, 0, 'cancelled')

        # Timed out, cancel the unfilled order
        self._ib.cancelOrder(trade.order)
        logger.warning(f"Order {trade.order.orderId} timed out after {timeout}s, cancelled")
        return (str(trade.order.orderId), 0.0, 0, 'timeout')

    def place_spread_order(self, spread, quantity: int,
                           limit_price: Optional[float] = None,
                           metadata: Optional[Dict] = None) -> str:
        """Place vertical credit spread as an IB BAG combo order.

        Builds two ComboLegs (SELL short, BUY long) and submits a
        LimitOrder with a negative limit price (= receive credit).

        Returns order_id string on fill, empty string on failure.
        """
        if not self.is_connected:
            raise IBKRError("Not connected to IBKR")

        from ib_insync import LimitOrder

        credit = limit_price if limit_price is not None else spread.credit_received
        if credit <= 0:
            logger.error(f"Invalid credit amount: {credit}")
            return ''

        combo = self._build_combo_contract(spread, 'SELL', 'BUY')
        order = LimitOrder('BUY', quantity, -credit)

        logger.info(
            f"Placing IBKR spread: {spread.direction.value} "
            f"${spread.short_leg.strike}/{spread.long_leg.strike} "
            f"x{quantity} @ ${credit:.2f} credit"
        )

        trade = self._ib.placeOrder(combo, order)
        order_id, fill_price, filled_qty, status = self._wait_for_fill(
            trade, self.ORDER_TIMEOUT
        )

        if status == 'filled' and filled_qty >= quantity:
            logger.info(f"Order {order_id} filled: {filled_qty} @ ${fill_price:.2f}")
            return order_id

        logger.warning(
            f"Order {order_id} not filled: status={status}, filled={filled_qty}"
        )
        return ''

    def close_spread(self, spread, quantity: int,
                     limit_price: float = 0.20) -> str:
        """Close an existing spread by buying it back.

        Builds reversed ComboLegs (BUY short back, SELL long) and
        submits a LimitOrder with a positive limit price (= pay debit).

        Returns order_id string on fill, empty string on failure.
        """
        if not self.is_connected:
            raise IBKRError("Not connected to IBKR")

        from ib_insync import LimitOrder

        if limit_price < 0:
            logger.error(f"Invalid close limit price: {limit_price}")
            return ''

        # Reverse the legs: BUY back the short, SELL the long
        combo = self._build_combo_contract(spread, 'BUY', 'SELL')
        order = LimitOrder('BUY', quantity, limit_price)

        logger.info(
            f"Closing IBKR spread: {spread.direction.value} "
            f"${spread.short_leg.strike}/{spread.long_leg.strike} "
            f"x{quantity} @ ${limit_price:.2f} debit"
        )

        trade = self._ib.placeOrder(combo, order)
        order_id, fill_price, filled_qty, status = self._wait_for_fill(
            trade, self.ORDER_TIMEOUT
        )

        if status == 'filled' and filled_qty >= quantity:
            logger.info(f"Close order {order_id} filled: {filled_qty} @ ${fill_price:.2f}")
            return order_id

        logger.warning(
            f"Close order {order_id} not filled: status={status}, filled={filled_qty}"
        )
        return ''

    _STATUS_MAP = {
        'Filled': 'filled',
        'Submitted': 'open',
        'PreSubmitted': 'pending',
        'Cancelled': 'cancelled',
        'ApiCancelled': 'cancelled',
        'Inactive': 'rejected',
    }

    def get_order_status(self, order_id: str) -> Dict:
        """Get order status from IB by order ID.

        Checks openTrades() first (live/working orders), then trades()
        (includes completed orders). Returns the first match.

        Returns: {'status': str, 'fill_price': float, 'filled_quantity': int}
        """
        if not self.is_connected:
            return {'status': 'not_found', 'fill_price': 0, 'filled_quantity': 0}

        # Check open (live) trades first, then all trades (includes filled)
        for trade_list in (self._ib.openTrades(), self._ib.trades()):
            for trade in trade_list:
                if str(trade.order.orderId) == order_id:
                    ib_status = trade.orderStatus.status
                    return {
                        'status': self._STATUS_MAP.get(ib_status, 'not_found'),
                        'fill_price': abs(trade.orderStatus.avgFillPrice),
                        'filled_quantity': int(trade.orderStatus.filled),
                    }

        return {'status': 'not_found', 'fill_price': 0, 'filled_quantity': 0}

    # ------------------------------------------------------------------
    # Position value
    # ------------------------------------------------------------------

    def get_position_value(self, spread) -> float:
        """Get current market value of a spread (cost to close).

        Requests snapshot market data on both legs and computes:
            value = ask(long_leg) - bid(short_leg)

        This represents the debit needed to close the position.
        Returns 0.0 if not connected or data is unavailable.
        """
        if not self.is_connected:
            return 0.0

        right = 'C' if spread.short_leg.option_type == 'call' else 'P'
        expiry = spread.expiration.strftime('%Y%m%d')

        short_contract = self._make_option_contract(
            spread.short_leg.strike, right, expiry
        )
        long_contract = self._make_option_contract(
            spread.long_leg.strike, right, expiry
        )

        short_ticker = self._ib.reqMktData(short_contract, '', True, False)
        long_ticker = self._ib.reqMktData(long_contract, '', True, False)
        self._ib.sleep(2)  # allow snapshots to fill

        short_bid = self._safe_price(short_ticker.bid)
        long_ask = self._safe_price(long_ticker.ask)

        # Cost to close = buy back short (at ask) minus sell long (at bid)
        # But from the spread holder's perspective:
        # value = what you'd pay to close = ask(long) - bid(short)
        # If negative (spread deeply ITM), return 0.0
        value = long_ask - short_bid
        return max(0.0, value)
