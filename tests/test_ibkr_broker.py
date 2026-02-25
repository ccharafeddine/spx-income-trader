"""
Tests for IBKR broker integration.

All tests use mocked ib_insync - zero real API calls or connections.
"""

import math
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.brokers.ibkr_broker import IBKRBroker, IBKRError, TWS_LIVE_PORT, TWS_PAPER_PORT
from src.models.spread import CreditSpread, OptionLeg, TradeDirection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_ib_module():
    """Create a mock ib_insync module with IB class."""
    mock_ib_class = MagicMock()
    mock_module = MagicMock()
    mock_module.IB = mock_ib_class
    return mock_module, mock_ib_class


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestIBKRConnect:
    def test_connect_calls_ib_with_correct_params(self):
        """Verify connect() creates IB and calls connect with correct args."""
        mock_module, mock_ib_class = _make_mock_ib_module()
        mock_ib_instance = MagicMock()
        mock_ib_instance.isConnected.return_value = False
        mock_ib_class.return_value = mock_ib_instance

        broker = IBKRBroker(host='192.168.1.10', port=4001, client_id=5)

        with patch.dict(sys.modules, {'ib_insync': mock_module}):
            result = broker.connect()

        assert result is True
        mock_ib_instance.connect.assert_called_once_with(
            '192.168.1.10', 4001, clientId=5
        )


class TestIBKRDisconnect:
    def test_disconnect_when_connected(self):
        """Verify disconnect() calls ib.disconnect() and clears _ib."""
        broker = IBKRBroker()
        mock_ib = MagicMock()
        broker._ib = mock_ib

        broker.disconnect()

        mock_ib.disconnect.assert_called_once()
        assert broker._ib is None


class TestIBKRPaperTrading:
    def test_paper_trading_auto_switches_port(self):
        """paper_trading=True with default port -> TWS_PAPER_PORT."""
        broker = IBKRBroker(paper_trading=True)
        assert broker.port == TWS_PAPER_PORT

    def test_explicit_port_respected_with_paper_trading(self):
        """paper_trading=True with explicit port -> user's port wins."""
        broker = IBKRBroker(port=4001, paper_trading=True)
        assert broker.port == 4001

    def test_paper_trading_false_keeps_live_port(self):
        """paper_trading=False -> TWS_LIVE_PORT."""
        broker = IBKRBroker(paper_trading=False)
        assert broker.port == TWS_LIVE_PORT


class TestIBKRAccountBalance:
    def test_account_balance_parses_summary(self):
        """Verify accountSummary() items are parsed into the expected dict."""
        broker = IBKRBroker()
        mock_ib = MagicMock()
        mock_ib.isConnected.return_value = True
        mock_ib.accountSummary.return_value = [
            SimpleNamespace(tag='NetLiquidation', value='125000.50'),
            SimpleNamespace(tag='AvailableFunds', value='98000.00'),
            SimpleNamespace(tag='BuyingPower', value='350000.75'),
            SimpleNamespace(tag='SomeOtherTag', value='999.99'),
        ]
        broker._ib = mock_ib

        result = broker.get_account_balance()

        assert result['net_account_value'] == 125000.50
        assert result['cash_available'] == 98000.00
        assert result['buying_power'] == 350000.75

    def test_balance_when_disconnected_raises(self):
        """get_account_balance() raises IBKRError when not connected."""
        broker = IBKRBroker()
        broker._ib = None

        with pytest.raises(IBKRError, match="Not connected"):
            broker.get_account_balance()


# ---------------------------------------------------------------------------
# Price & contract tests
# ---------------------------------------------------------------------------

def _connected_broker():
    """Return an IBKRBroker with a mocked, connected _ib."""
    broker = IBKRBroker()
    mock_ib = MagicMock()
    mock_ib.isConnected.return_value = True
    broker._ib = mock_ib
    return broker, mock_ib


def _mock_ib_insync_module():
    """Create a mock ib_insync module suitable for sys.modules patching."""
    mod = MagicMock()
    # Contract / Option just return whatever MagicMock gives us
    return mod


class TestGetCurrentPrice:
    def test_returns_last_price(self):
        """Normal case: ticker.last is a valid float."""
        broker, mock_ib = _connected_broker()
        ticker = MagicMock(last=5987.25, close=5950.00)
        mock_ib.reqMktData.return_value = ticker

        with patch.dict(sys.modules, {'ib_insync': _mock_ib_insync_module()}):
            price = broker.get_current_price('SPX')

        assert price == 5987.25
        mock_ib.reqMktData.assert_called_once()
        # snapshot=True is the 3rd positional arg
        args = mock_ib.reqMktData.call_args
        assert args[0][1] == ''    # genericTickList
        assert args[0][2] is True  # snapshot
        assert args[0][3] is False # regulatorySnapshot

    def test_falls_back_to_close_when_last_is_nan(self):
        """Pre-market: ticker.last is NaN, should return ticker.close."""
        broker, mock_ib = _connected_broker()
        ticker = MagicMock(last=float('nan'), close=5950.00)
        mock_ib.reqMktData.return_value = ticker

        with patch.dict(sys.modules, {'ib_insync': _mock_ib_insync_module()}):
            price = broker.get_current_price('SPX')

        assert price == 5950.00

    def test_raises_when_both_last_and_close_are_nan(self):
        """Both last and close are NaN -> IBKRError."""
        broker, mock_ib = _connected_broker()
        ticker = MagicMock(last=float('nan'), close=float('nan'))
        mock_ib.reqMktData.return_value = ticker

        with patch.dict(sys.modules, {'ib_insync': _mock_ib_insync_module()}):
            with pytest.raises(IBKRError, match="No price available"):
                broker.get_current_price('SPX')

    def test_raises_when_disconnected(self):
        """get_current_price() raises IBKRError when not connected."""
        broker = IBKRBroker()
        broker._ib = None

        with pytest.raises(IBKRError, match="Not connected"):
            broker.get_current_price('SPX')


class TestContractHelpers:
    def test_spx_contract_fields(self):
        """_make_spx_contract returns correct IND contract for SPX."""
        mock_contract_cls = MagicMock()
        mock_module = MagicMock()
        mock_module.Contract = mock_contract_cls

        broker = IBKRBroker()

        with patch.dict(sys.modules, {'ib_insync': mock_module}):
            broker._make_spx_contract()

        mock_contract_cls.assert_called_once_with(
            symbol='SPX', secType='IND', exchange='CBOE', currency='USD'
        )

    def test_option_contract_fields(self):
        """_make_option_contract returns correct Option contract."""
        mock_option_cls = MagicMock()
        mock_module = MagicMock()
        mock_module.Option = mock_option_cls

        broker = IBKRBroker()

        with patch.dict(sys.modules, {'ib_insync': mock_module}):
            broker._make_option_contract(6000.0, 'C', '20260224')

        mock_option_cls.assert_called_once_with(
            symbol='SPX',
            lastTradeDateOrContractMonth='20260224',
            strike=6000.0,
            right='C',
            exchange='SMART',
            currency='USD',
        )


# ---------------------------------------------------------------------------
# Options chain tests
# ---------------------------------------------------------------------------

def _setup_chain_broker(strikes, current_price=6000.0):
    """Wire up a broker with mocked reqSecDefOptParams and reqMktData.

    Args:
        strikes: list of floats for available strikes.
        current_price: price returned by get_current_price().

    Returns (broker, mock_ib, mock_module).
    """
    broker, mock_ib = _connected_broker()

    # get_current_price needs a snapshot ticker
    price_ticker = MagicMock(last=current_price, close=current_price)

    # reqSecDefOptParams returns param objects
    opt_params = SimpleNamespace(
        expirations={'20260224'},
        strikes=set(strikes),
    )
    mock_ib.reqSecDefOptParams.return_value = [opt_params]

    def _qualify(contract, *args):
        # qualifyContracts populates conId on the contract object
        contract.conId = 416904
    mock_ib.qualifyContracts.side_effect = _qualify

    # Track per-contract tickers: build lookup keyed on (strike, right)
    ticker_map = {}
    for s in strikes:
        ticker_map[(s, 'C')] = MagicMock(
            bid=round(max(0.05, (current_price - s) + 5.0), 2),
            ask=round(max(0.15, (current_price - s) + 5.5), 2),
            last=round(max(0.10, (current_price - s) + 5.25), 2),
            volume=100,
        )
        ticker_map[(s, 'P')] = MagicMock(
            bid=round(max(0.05, (s - current_price) + 5.0), 2),
            ask=round(max(0.15, (s - current_price) + 5.5), 2),
            last=round(max(0.10, (s - current_price) + 5.25), 2),
            volume=200,
        )

    def _req_mkt_data(contract, *args, **kwargs):
        # price snapshot for SPX index (get_current_price)
        if getattr(contract, 'secType', None) == 'IND':
            return price_ticker
        # option snapshot
        strike = getattr(contract, 'strike', None)
        right = getattr(contract, 'right', None)
        if strike is not None and (strike, right) in ticker_map:
            return ticker_map[(strike, right)]
        return MagicMock(bid=0.0, ask=0.0, last=0.0, volume=0)

    mock_ib.reqMktData.side_effect = _req_mkt_data

    mock_module = _mock_ib_insync_module()
    # Make Contract/Option return objects with the kwargs as attributes
    mock_module.Contract.side_effect = lambda **kw: SimpleNamespace(**kw)
    mock_module.Option.side_effect = lambda **kw: SimpleNamespace(**kw)

    return broker, mock_ib, mock_module


class TestGetOptionsChain:
    def test_chain_format_matches_project_standard(self):
        """Chain dict has all required keys per strike."""
        strikes = [5990.0, 5995.0, 6000.0, 6005.0, 6010.0]
        broker, mock_ib, mock_mod = _setup_chain_broker(strikes)

        with patch.dict(sys.modules, {'ib_insync': mock_mod}):
            chain = broker.get_options_chain('SPX', '2026-02-24')

        assert len(chain) == 5
        required_keys = {
            'call_bid', 'call_ask', 'call_last', 'call_volume', 'call_oi',
            'put_bid', 'put_ask', 'put_last', 'put_volume', 'put_oi',
        }
        for strike, data in chain.items():
            assert required_keys.issubset(data.keys()), f"Missing keys at strike {strike}"
            for key in required_keys:
                assert isinstance(data[key], (int, float)), f"{key} not numeric at {strike}"

    def test_expiry_converted_to_yyyymmdd(self):
        """YYYY-MM-DD input is converted to YYYYMMDD for IB calls."""
        strikes = [6000.0]
        broker, mock_ib, mock_mod = _setup_chain_broker(strikes)

        with patch.dict(sys.modules, {'ib_insync': mock_mod}):
            broker.get_options_chain('SPX', '2026-02-24')

        # Check that _make_option_contract was called with YYYYMMDD
        option_calls = mock_mod.Option.call_args_list
        for call in option_calls:
            assert call.kwargs['lastTradeDateOrContractMonth'] == '20260224'

    def test_strike_filtering_within_range(self):
        """Only strikes within +/-50 of current price are included."""
        # Mix of in-range and out-of-range strikes
        all_strikes = [5900.0, 5940.0, 5950.0, 6000.0, 6050.0, 6060.0, 6100.0]
        broker, mock_ib, mock_mod = _setup_chain_broker(
            all_strikes, current_price=6000.0
        )

        with patch.dict(sys.modules, {'ib_insync': mock_mod}):
            chain = broker.get_options_chain('SPX', '2026-02-24')

        # 5900 is 100 away (out), 5940 is 60 away (out),
        # 5950 is 50 away (in), 6000 (in), 6050 is 50 away (in),
        # 6060 is 60 away (out), 6100 is 100 away (out)
        assert sorted(chain.keys()) == [5950.0, 6000.0, 6050.0]

    def test_raises_when_disconnected(self):
        """get_options_chain raises IBKRError when not connected."""
        broker = IBKRBroker()
        broker._ib = None

        with pytest.raises(IBKRError, match="Not connected"):
            broker.get_options_chain('SPX', '2026-02-24')

    def test_mid_price_calculation(self):
        """Verify mid = (bid + ask) / 2 from returned chain data."""
        strikes = [6000.0]
        broker, mock_ib, mock_mod = _setup_chain_broker(strikes, current_price=6000.0)

        with patch.dict(sys.modules, {'ib_insync': mock_mod}):
            chain = broker.get_options_chain('SPX', '2026-02-24')

        data = chain[6000.0]
        call_mid = (data['call_bid'] + data['call_ask']) / 2
        put_mid = (data['put_bid'] + data['put_ask']) / 2
        # Mid should be between bid and ask
        assert data['call_bid'] <= call_mid <= data['call_ask']
        assert data['put_bid'] <= put_mid <= data['put_ask']
        # Bid/ask should be positive
        assert data['call_bid'] > 0
        assert data['put_bid'] > 0


# ---------------------------------------------------------------------------
# Order tests
# ---------------------------------------------------------------------------

def _make_spread(direction=TradeDirection.BULLISH, short_strike=6000.0,
                 long_strike=5995.0, credit=1.50, option_type='put',
                 expiration=None):
    """Create a CreditSpread for testing."""
    exp = expiration or datetime(2026, 2, 24, 16, 0, 0)
    return CreditSpread(
        direction=direction,
        short_leg=OptionLeg(
            strike=short_strike, option_type=option_type,
            action='sell', price=3.00,
        ),
        long_leg=OptionLeg(
            strike=long_strike, option_type=option_type,
            action='buy', price=1.50,
        ),
        credit_received=credit,
        entry_time=datetime(2026, 2, 24, 10, 0, 0),
        expiration=exp,
        underlying_price_at_entry=6000.0,
    )


def _make_mock_trade(order_id=1001, status='Filled', avg_fill=1.45, filled=5):
    """Create a mock ib_insync Trade returned by placeOrder."""
    trade = MagicMock()
    trade.order.orderId = order_id
    trade.orderStatus.status = status
    trade.orderStatus.avgFillPrice = avg_fill
    trade.orderStatus.filled = filled
    return trade


def _order_ib_module():
    """Mock ib_insync module wired for order tests."""
    mod = MagicMock()
    mod.Option.side_effect = lambda **kw: SimpleNamespace(**kw)
    # Track Contract / ComboLeg / LimitOrder calls via SimpleNamespace
    mod.Contract.side_effect = lambda **kw: SimpleNamespace(**kw)
    mod.ComboLeg.side_effect = lambda **kw: SimpleNamespace(**kw)
    mod.LimitOrder.side_effect = lambda *a, **kw: SimpleNamespace(
        action=a[0] if len(a) > 0 else kw.get('action'),
        totalQuantity=a[1] if len(a) > 1 else kw.get('totalQuantity'),
        lmtPrice=a[2] if len(a) > 2 else kw.get('lmtPrice'),
    )
    return mod


def _setup_order_broker(trade_result=None):
    """Wire up a connected broker for order tests.

    Returns (broker, mock_ib, mock_module).
    """
    broker, mock_ib = _connected_broker()

    # qualifyContracts sets conId on each contract
    conid_counter = [100]

    def _qualify(*contracts):
        for c in contracts:
            c.conId = conid_counter[0]
            conid_counter[0] += 1

    mock_ib.qualifyContracts.side_effect = _qualify

    # placeOrder returns the mock trade
    if trade_result is None:
        trade_result = _make_mock_trade()
    mock_ib.placeOrder.return_value = trade_result

    mock_mod = _order_ib_module()
    return broker, mock_ib, mock_mod


class TestPlaceSpreadOrder:
    def test_combo_leg_construction(self):
        """ComboLegs have correct conIds, ratio=1, actions, exchange."""
        spread = _make_spread()
        broker, mock_ib, mock_mod = _setup_order_broker()

        with patch.dict(sys.modules, {'ib_insync': mock_mod}):
            broker.place_spread_order(spread, 5)

        # placeOrder was called with (combo_contract, limit_order)
        combo = mock_ib.placeOrder.call_args[0][0]
        legs = combo.comboLegs
        assert len(legs) == 2

        short_leg, long_leg = legs[0], legs[1]
        # Short leg: SELL, ratio=1, exchange=SMART
        assert short_leg.action == 'SELL'
        assert short_leg.ratio == 1
        assert short_leg.exchange == 'SMART'
        # Long leg: BUY, ratio=1, exchange=SMART
        assert long_leg.action == 'BUY'
        assert long_leg.ratio == 1
        assert long_leg.exchange == 'SMART'
        # Both have conIds (set by qualifyContracts)
        assert short_leg.conId is not None
        assert long_leg.conId is not None
        assert short_leg.conId != long_leg.conId

    def test_bull_put_uses_put_options(self):
        """BULLISH direction uses 'P' right for both legs."""
        spread = _make_spread(
            direction=TradeDirection.BULLISH,
            option_type='put',
            short_strike=6000.0, long_strike=5995.0,
        )
        broker, mock_ib, mock_mod = _setup_order_broker()

        with patch.dict(sys.modules, {'ib_insync': mock_mod}):
            broker.place_spread_order(spread, 3)

        # Check Option calls used right='P'
        option_calls = mock_mod.Option.call_args_list
        for c in option_calls:
            assert c.kwargs['right'] == 'P'

    def test_bear_call_uses_call_options(self):
        """BEARISH direction uses 'C' right for both legs."""
        spread = _make_spread(
            direction=TradeDirection.BEARISH,
            option_type='call',
            short_strike=6050.0, long_strike=6055.0,
        )
        broker, mock_ib, mock_mod = _setup_order_broker()

        with patch.dict(sys.modules, {'ib_insync': mock_mod}):
            broker.place_spread_order(spread, 2)

        option_calls = mock_mod.Option.call_args_list
        for c in option_calls:
            assert c.kwargs['right'] == 'C'

    def test_limit_price_negative_for_credit(self):
        """Opening order uses negative lmtPrice (= receive credit)."""
        spread = _make_spread(credit=1.50)
        broker, mock_ib, mock_mod = _setup_order_broker()

        with patch.dict(sys.modules, {'ib_insync': mock_mod}):
            broker.place_spread_order(spread, 5, limit_price=1.50)

        order = mock_ib.placeOrder.call_args[0][1]
        assert order.lmtPrice == -1.50
        assert order.action == 'BUY'
        assert order.totalQuantity == 5

    def test_timeout_cancels_and_returns_empty(self):
        """Unfilled order after timeout -> cancel -> return ''."""
        trade = _make_mock_trade(status='Submitted', avg_fill=0.0, filled=0)
        spread = _make_spread()
        broker, mock_ib, mock_mod = _setup_order_broker(trade_result=trade)
        broker.ORDER_TIMEOUT = 0  # instant timeout

        with patch.dict(sys.modules, {'ib_insync': mock_mod}):
            result = broker.place_spread_order(spread, 5)

        assert result == ''
        mock_ib.cancelOrder.assert_called_once_with(trade.order)

    def test_partial_fill_accepted_when_quantity_matches(self):
        """Partial fills completing to full quantity -> accepted."""
        # IB reports status='Filled' once all quantity is filled
        # even if filled via multiple partial executions
        trade = _make_mock_trade(
            order_id=2002, status='Filled', avg_fill=1.48, filled=7
        )
        spread = _make_spread()
        broker, mock_ib, mock_mod = _setup_order_broker(trade_result=trade)

        with patch.dict(sys.modules, {'ib_insync': mock_mod}):
            result = broker.place_spread_order(spread, 7)

        assert result == '2002'

    def test_order_id_returned_as_string(self):
        """Returned order_id matches trade.order.orderId as string."""
        trade = _make_mock_trade(order_id=9876)
        spread = _make_spread()
        broker, mock_ib, mock_mod = _setup_order_broker(trade_result=trade)

        with patch.dict(sys.modules, {'ib_insync': mock_mod}):
            result = broker.place_spread_order(spread, 3)

        assert result == '9876'

    def test_fill_price_extracted_from_avg_fill(self):
        """avgFillPrice from the trade is used (absolute value)."""
        # IB may return negative avgFillPrice for credits
        trade = _make_mock_trade(
            order_id=3003, status='Filled', avg_fill=-1.55, filled=4
        )
        spread = _make_spread()
        broker, mock_ib, mock_mod = _setup_order_broker(trade_result=trade)

        with patch.dict(sys.modules, {'ib_insync': mock_mod}):
            order_id = broker.place_spread_order(spread, 4)

        assert order_id == '3003'
        # Verify get_order_status extracts abs(avgFillPrice)
        mock_ib.trades.return_value = [trade]
        status = broker.get_order_status('3003')
        assert status['fill_price'] == 1.55
        assert status['filled_quantity'] == 4
        assert status['status'] == 'filled'


class TestCloseSpread:
    def test_close_reverses_combo_legs(self):
        """Close order has BUY on short leg, SELL on long leg."""
        spread = _make_spread()
        broker, mock_ib, mock_mod = _setup_order_broker()

        with patch.dict(sys.modules, {'ib_insync': mock_mod}):
            broker.close_spread(spread, 5, limit_price=0.20)

        combo = mock_ib.placeOrder.call_args[0][0]
        legs = combo.comboLegs
        short_leg, long_leg = legs[0], legs[1]
        # Reversed from open: short is now BUY (to close), long is SELL
        assert short_leg.action == 'BUY'
        assert long_leg.action == 'SELL'

    def test_close_limit_price_positive_for_debit(self):
        """Closing order uses positive lmtPrice (= pay debit)."""
        spread = _make_spread()
        broker, mock_ib, mock_mod = _setup_order_broker()

        with patch.dict(sys.modules, {'ib_insync': mock_mod}):
            broker.close_spread(spread, 3, limit_price=0.15)

        order = mock_ib.placeOrder.call_args[0][1]
        assert order.lmtPrice == 0.15
        assert order.action == 'BUY'
        assert order.totalQuantity == 3


# ---------------------------------------------------------------------------
# Order status tests
# ---------------------------------------------------------------------------

class TestGetOrderStatus:
    def test_status_mapping_for_all_states(self):
        """Each IB status string maps to the correct project status."""
        broker, mock_ib = _connected_broker()

        mappings = {
            'Filled': 'filled',
            'Submitted': 'open',
            'PreSubmitted': 'pending',
            'Cancelled': 'cancelled',
            'ApiCancelled': 'cancelled',
            'Inactive': 'rejected',
        }

        for ib_status, expected_status in mappings.items():
            trade = _make_mock_trade(
                order_id=100, status=ib_status, avg_fill=1.25, filled=3
            )
            mock_ib.openTrades.return_value = []
            mock_ib.trades.return_value = [trade]

            result = broker.get_order_status('100')
            assert result['status'] == expected_status, (
                f"IB status '{ib_status}' should map to '{expected_status}', "
                f"got '{result['status']}'"
            )

    def test_checks_open_trades_first(self):
        """openTrades() is checked before trades() for live orders."""
        broker, mock_ib = _connected_broker()

        open_trade = _make_mock_trade(
            order_id=200, status='Submitted', avg_fill=0.0, filled=0
        )
        filled_trade = _make_mock_trade(
            order_id=200, status='Filled', avg_fill=1.50, filled=5
        )
        mock_ib.openTrades.return_value = [open_trade]
        mock_ib.trades.return_value = [filled_trade]

        result = broker.get_order_status('200')
        # Should find the open trade first
        assert result['status'] == 'open'

    def test_not_found_when_disconnected(self):
        """Returns not_found when broker is disconnected."""
        broker = IBKRBroker()
        broker._ib = None

        result = broker.get_order_status('999')
        assert result == {'status': 'not_found', 'fill_price': 0, 'filled_quantity': 0}

    def test_not_found_for_unknown_order(self):
        """Returns not_found when order ID doesn't match any trade."""
        broker, mock_ib = _connected_broker()
        mock_ib.openTrades.return_value = []
        mock_ib.trades.return_value = []

        result = broker.get_order_status('12345')
        assert result['status'] == 'not_found'


# ---------------------------------------------------------------------------
# Position value tests
# ---------------------------------------------------------------------------

class TestGetPositionValue:
    def test_position_value_calculation(self):
        """Value = ask(long_leg) - bid(short_leg)."""
        broker, mock_ib = _connected_broker()
        spread = _make_spread(short_strike=6000.0, long_strike=5995.0)

        short_ticker = MagicMock(bid=0.50, ask=0.70)
        long_ticker = MagicMock(bid=0.10, ask=0.25)

        def _req_mkt_data(contract, *args, **kwargs):
            strike = getattr(contract, 'strike', None)
            if strike == 6000.0:
                return short_ticker
            elif strike == 5995.0:
                return long_ticker
            return MagicMock(bid=0.0, ask=0.0)

        mock_ib.reqMktData.side_effect = _req_mkt_data
        mock_mod = _mock_ib_insync_module()
        mock_mod.Option.side_effect = lambda **kw: SimpleNamespace(**kw)

        with patch.dict(sys.modules, {'ib_insync': mock_mod}):
            value = broker.get_position_value(spread)

        # long_ask (0.25) - short_bid (0.50) = -0.25 -> clamped to 0.0
        assert value == 0.0

    def test_position_value_positive_when_spread_has_value(self):
        """When long leg ask > short leg bid, value is positive."""
        broker, mock_ib = _connected_broker()
        spread = _make_spread(short_strike=6000.0, long_strike=5995.0)

        short_ticker = MagicMock(bid=0.10, ask=0.20)
        long_ticker = MagicMock(bid=0.40, ask=0.55)

        def _req_mkt_data(contract, *args, **kwargs):
            strike = getattr(contract, 'strike', None)
            if strike == 6000.0:
                return short_ticker
            elif strike == 5995.0:
                return long_ticker
            return MagicMock(bid=0.0, ask=0.0)

        mock_ib.reqMktData.side_effect = _req_mkt_data
        mock_mod = _mock_ib_insync_module()
        mock_mod.Option.side_effect = lambda **kw: SimpleNamespace(**kw)

        with patch.dict(sys.modules, {'ib_insync': mock_mod}):
            value = broker.get_position_value(spread)

        # long_ask (0.55) - short_bid (0.10) = 0.45
        assert value == pytest.approx(0.45)

    def test_position_value_zero_when_disconnected(self):
        """Returns 0.0 when not connected."""
        broker = IBKRBroker()
        broker._ib = None
        spread = _make_spread()

        value = broker.get_position_value(spread)
        assert value == 0.0
