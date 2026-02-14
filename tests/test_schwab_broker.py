"""
Comprehensive tests for Schwab broker integration.

All tests use fully mocked schwab-py client - zero real API calls.
"""

import json
import os
import sys
import tempfile
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models.spread import CreditSpread, OptionLeg, TradeDirection
from src.brokers.schwab_auth import SchwabAuth, REFRESH_TOKEN_LIFETIME_DAYS
from src.brokers.schwab_broker import (
    SchwabBroker, SchwabAPIError, OrderStatus,
    _SCHWAB_STATUS_MAP, _NORMALIZED_TO_ENUM,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_spread(
    direction=TradeDirection.BEARISH,
    short_strike=6050.0,
    long_strike=6055.0,
    credit=1.50,
    option_type='call',
    expiration=None,
):
    """Create a CreditSpread for testing."""
    exp = expiration or datetime(2026, 2, 13, 16, 0, 0)
    return CreditSpread(
        direction=direction,
        short_leg=OptionLeg(
            strike=short_strike,
            option_type=option_type,
            action='sell',
            price=3.00,
        ),
        long_leg=OptionLeg(
            strike=long_strike,
            option_type=option_type,
            action='buy',
            price=1.50,
        ),
        credit_received=credit,
        entry_time=datetime(2026, 2, 13, 10, 0, 0),
        expiration=exp,
        underlying_price_at_entry=6048.0,
    )


def _make_put_spread():
    """Create a bullish put credit spread for testing."""
    return _make_spread(
        direction=TradeDirection.BULLISH,
        short_strike=6040.0,
        long_strike=6035.0,
        credit=1.20,
        option_type='put',
    )


def _mock_auth():
    """Create a mock SchwabAuth."""
    auth = MagicMock(spec=SchwabAuth)
    auth.is_authenticated.return_value = True
    auth.get_client.return_value = MagicMock()
    auth.get_token_status.return_value = {
        'valid': True,
        'created_at': datetime.now().isoformat(),
        'expires_at': (datetime.now() + timedelta(days=6)).isoformat(),
        'hours_remaining': 144.0,
        'expiring_soon': False,
    }
    return auth


def _make_broker(auth=None):
    """Create a SchwabBroker with mocked auth."""
    config = {
        'app_key': 'test_key',
        'app_secret': 'test_secret',
        'callback_url': 'https://127.0.0.1',
        'token_path': 'test_token.json',
        'account_number': '12345678',
    }
    return SchwabBroker(config=config, auth=auth or _mock_auth())


# ===========================================================================
# TestSchwabOptionSymbol
# ===========================================================================

class TestSchwabOptionSymbol:
    """Test option symbol building for Schwab OCC format."""

    def test_build_call_symbol_whole_strike(self):
        broker = _make_broker()
        sym = broker._build_option_symbol(6050, 'call', date(2026, 2, 13))
        assert 'SPXW' in sym
        assert '260213' in sym
        assert 'C' in sym
        assert '06050000' in sym

    def test_build_put_symbol(self):
        broker = _make_broker()
        sym = broker._build_option_symbol(6045, 'put', date(2026, 2, 13))
        assert 'P' in sym
        assert '06045000' in sym

    def test_build_symbol_with_decimal_strike(self):
        broker = _make_broker()
        sym = broker._build_option_symbol(6047.5, 'call', date(2026, 3, 20))
        assert 'SPXW' in sym
        assert 'C' in sym

    def test_build_symbol_from_datetime(self):
        broker = _make_broker()
        exp_dt = datetime(2026, 2, 13, 16, 0, 0)
        sym = broker._build_option_symbol(6050, 'call', exp_dt)
        assert '260213' in sym

    def test_build_symbol_from_string(self):
        broker = _make_broker()
        sym = broker._build_option_symbol(6050, 'call', '2026-02-13')
        assert '260213' in sym

    def test_different_expirations(self):
        broker = _make_broker()
        sym1 = broker._build_option_symbol(6050, 'call', date(2026, 2, 13))
        sym2 = broker._build_option_symbol(6050, 'call', date(2026, 3, 20))
        assert sym1 != sym2
        assert '260213' in sym1
        assert '260320' in sym2


# ===========================================================================
# TestSchwabOptionsChain
# ===========================================================================

class TestSchwabOptionsChain:
    """Test options chain response transformation."""

    def _raw_chain(self):
        """Realistic Schwab option chain response."""
        return {
            'symbol': '$SPX',
            'status': 'SUCCESS',
            'callExpDateMap': {
                '2026-02-13:0': {
                    '6050.0': [{
                        'bid': 3.20,
                        'ask': 3.40,
                        'last': 3.30,
                        'delta': 0.45,
                        'totalVolume': 1200,
                        'openInterest': 5000,
                        'symbol': 'SPXW  260213C06050000',
                    }],
                    '6055.0': [{
                        'bid': 1.80,
                        'ask': 2.00,
                        'last': 1.90,
                        'delta': 0.35,
                        'totalVolume': 800,
                        'openInterest': 3000,
                        'symbol': 'SPXW  260213C06055000',
                    }],
                }
            },
            'putExpDateMap': {
                '2026-02-13:0': {
                    '6050.0': [{
                        'bid': 2.80,
                        'ask': 3.00,
                        'last': 2.90,
                        'delta': -0.55,
                        'totalVolume': 900,
                        'openInterest': 4000,
                        'symbol': 'SPXW  260213P06050000',
                    }],
                    '6045.0': [{
                        'bid': 1.60,
                        'ask': 1.80,
                        'last': 1.70,
                        'delta': -0.40,
                        'totalVolume': 600,
                        'openInterest': 2500,
                        'symbol': 'SPXW  260213P06045000',
                    }],
                }
            },
        }

    def test_transform_chain_standard(self):
        broker = _make_broker()
        chain = broker._transform_chain(self._raw_chain(), '2026-02-13')
        assert 6050.0 in chain
        assert chain[6050.0]['call_bid'] == 3.20
        assert chain[6050.0]['call_ask'] == 3.40
        assert chain[6050.0]['put_bid'] == 2.80
        assert chain[6050.0]['put_ask'] == 3.00

    def test_transform_chain_deltas(self):
        broker = _make_broker()
        chain = broker._transform_chain(self._raw_chain(), '2026-02-13')
        assert chain[6050.0]['call_delta'] == 0.45
        assert chain[6050.0]['put_delta'] == -0.55

    def test_transform_empty_chain(self):
        broker = _make_broker()
        chain = broker._transform_chain({'callExpDateMap': {}, 'putExpDateMap': {}}, '2026-02-13')
        assert chain == {}

    def test_transform_chain_sorted(self):
        broker = _make_broker()
        chain = broker._transform_chain(self._raw_chain(), '2026-02-13')
        strikes = list(chain.keys())
        assert strikes == sorted(strikes)

    def test_transform_chain_fills_missing_sides(self):
        """A strike with only calls should still have put fields defaulted to 0."""
        raw = {
            'callExpDateMap': {
                '2026-02-13:0': {
                    '6060.0': [{
                        'bid': 1.00,
                        'ask': 1.20,
                        'last': 1.10,
                        'delta': 0.25,
                        'totalVolume': 100,
                        'openInterest': 500,
                        'symbol': 'SPXW  260213C06060000',
                    }]
                }
            },
            'putExpDateMap': {},
        }
        broker = _make_broker()
        chain = broker._transform_chain(raw, '2026-02-13')
        assert 6060.0 in chain
        assert chain[6060.0]['call_bid'] == 1.00
        assert chain[6060.0]['put_bid'] == 0
        assert chain[6060.0]['put_ask'] == 0


# ===========================================================================
# TestSchwabOrderBuilding
# ===========================================================================

class TestSchwabOrderBuilding:
    """Test order construction using schwab-py templates."""

    def test_bear_call_vertical_open(self):
        """BEARISH direction with calls -> bear_call_vertical_open."""
        import schwab.orders.options as opts
        spread = _make_spread(direction=TradeDirection.BEARISH, option_type='call')

        broker = _make_broker()
        short_sym = broker._build_option_symbol(spread.short_leg.strike, 'call', spread.expiration)
        long_sym = broker._build_option_symbol(spread.long_leg.strike, 'call', spread.expiration)
        # schwab-py expects string prices now
        order = opts.bear_call_vertical_open(short_sym, long_sym, 2, '1.50')
        order_dict = order.build()

        assert order_dict['orderType'] == 'NET_CREDIT'
        assert order_dict['session'] == 'NORMAL'
        assert order_dict['duration'] == 'DAY'
        assert order_dict['price'] == '1.50'

    def test_bull_put_vertical_open(self):
        """BULLISH direction with puts -> bull_put_vertical_open."""
        import schwab.orders.options as opts
        spread = _make_put_spread()
        broker = _make_broker()

        long_sym = broker._build_option_symbol(spread.long_leg.strike, 'put', spread.expiration)
        short_sym = broker._build_option_symbol(spread.short_leg.strike, 'put', spread.expiration)
        order = opts.bull_put_vertical_open(long_sym, short_sym, 3, 1.20)
        order_dict = order.build()

        assert order_dict['orderType'] == 'NET_CREDIT'
        assert order_dict['session'] == 'NORMAL'
        assert order_dict['duration'] == 'DAY'

    def test_bear_call_vertical_close(self):
        """Closing a bear call spread -> bear_call_vertical_close."""
        import schwab.orders.options as opts
        broker = _make_broker()
        spread = _make_spread()
        short_sym = broker._build_option_symbol(spread.short_leg.strike, 'call', spread.expiration)
        long_sym = broker._build_option_symbol(spread.long_leg.strike, 'call', spread.expiration)
        order = opts.bear_call_vertical_close(short_sym, long_sym, 2, 0.20)
        order_dict = order.build()

        assert order_dict['orderType'] == 'NET_DEBIT'

    def test_bull_put_vertical_close(self):
        """Closing a bull put spread -> bull_put_vertical_close."""
        import schwab.orders.options as opts
        broker = _make_broker()
        spread = _make_put_spread()
        long_sym = broker._build_option_symbol(spread.long_leg.strike, 'put', spread.expiration)
        short_sym = broker._build_option_symbol(spread.short_leg.strike, 'put', spread.expiration)
        order = opts.bull_put_vertical_close(long_sym, short_sym, 1, 0.10)
        order_dict = order.build()

        assert order_dict['orderType'] == 'NET_DEBIT'

    def test_quantity_passed_through(self):
        """Verify quantity is correctly set in order."""
        import schwab.orders.options as opts
        broker = _make_broker()
        spread = _make_spread()
        short_sym = broker._build_option_symbol(spread.short_leg.strike, 'call', spread.expiration)
        long_sym = broker._build_option_symbol(spread.long_leg.strike, 'call', spread.expiration)

        for qty in [1, 5, 10]:
            order = opts.bear_call_vertical_open(short_sym, long_sym, qty, 1.50)
            order_dict = order.build()
            assert order_dict['quantity'] == qty

    def test_net_credit_price(self):
        """Verify net credit price is set correctly."""
        import schwab.orders.options as opts
        broker = _make_broker()
        spread = _make_spread()
        short_sym = broker._build_option_symbol(spread.short_leg.strike, 'call', spread.expiration)
        long_sym = broker._build_option_symbol(spread.long_leg.strike, 'call', spread.expiration)

        order = opts.bear_call_vertical_open(short_sym, long_sym, 1, 2.35)
        order_dict = order.build()
        assert order_dict['price'] == '2.35'

    def test_order_uses_limit_type(self):
        """Orders must use LIMIT (NET_CREDIT or NET_DEBIT), never MARKET."""
        import schwab.orders.options as opts
        broker = _make_broker()
        spread = _make_spread()
        short_sym = broker._build_option_symbol(spread.short_leg.strike, 'call', spread.expiration)
        long_sym = broker._build_option_symbol(spread.long_leg.strike, 'call', spread.expiration)

        open_order = opts.bear_call_vertical_open(short_sym, long_sym, 1, 1.50)
        close_order = opts.bear_call_vertical_close(short_sym, long_sym, 1, 0.20)

        assert open_order.build()['orderType'] in ('NET_CREDIT', 'LIMIT')
        assert close_order.build()['orderType'] in ('NET_DEBIT', 'LIMIT')

    def test_session_normal_duration_day(self):
        """All orders should use Session.NORMAL and Duration.DAY."""
        import schwab.orders.options as opts
        broker = _make_broker()
        spread = _make_spread()
        short_sym = broker._build_option_symbol(spread.short_leg.strike, 'call', spread.expiration)
        long_sym = broker._build_option_symbol(spread.long_leg.strike, 'call', spread.expiration)

        order = opts.bear_call_vertical_open(short_sym, long_sym, 1, 1.50)
        order_dict = order.build()
        assert order_dict['session'] == 'NORMAL'
        assert order_dict['duration'] == 'DAY'


# ===========================================================================
# TestSchwabOrderStatus
# ===========================================================================

class TestSchwabOrderStatus:
    """Test order status mapping from Schwab API to our normalized format."""

    def test_filled_maps_to_executed(self):
        assert _SCHWAB_STATUS_MAP['FILLED'] == 'filled'
        assert _NORMALIZED_TO_ENUM['filled'] == OrderStatus.EXECUTED

    def test_working_maps_to_open(self):
        assert _SCHWAB_STATUS_MAP['WORKING'] == 'open'
        assert _NORMALIZED_TO_ENUM['open'] == OrderStatus.OPEN

    def test_canceled_maps_to_cancelled(self):
        assert _SCHWAB_STATUS_MAP['CANCELED'] == 'cancelled'
        assert _NORMALIZED_TO_ENUM['cancelled'] == OrderStatus.CANCELLED

    def test_extract_fill_price(self):
        broker = _make_broker()
        order_data = {
            'status': 'FILLED',
            'filledQuantity': 3,
            'orderActivityCollection': [{
                'executionLegs': [
                    {'price': 1.45, 'quantity': 3},
                ]
            }]
        }
        result = broker._parse_order_response(order_data)
        assert result['status'] == 'filled'
        assert result['fill_price'] == 1.45
        assert result['filled_quantity'] == 3

    def test_missing_fields_default(self):
        broker = _make_broker()
        order_data = {'status': 'WORKING'}
        result = broker._parse_order_response(order_data)
        assert result['status'] == 'open'
        assert result['fill_price'] == 0.0
        assert result['filled_quantity'] == 0


# ===========================================================================
# TestSchwabOrderFlow
# ===========================================================================

class TestSchwabOrderFlow:
    """Test end-to-end order placement flow."""

    def _setup_broker_with_account(self):
        """Create a broker with mocked account hash resolution."""
        auth = _mock_auth()
        broker = _make_broker(auth)

        # Mock account numbers response
        acct_resp = MagicMock()
        acct_resp.status_code = 200
        acct_resp.json.return_value = [
            {'accountNumber': '12345678', 'hashValue': 'HASH123'}
        ]
        auth.get_client.return_value.get_account_numbers.return_value = acct_resp

        return broker, auth

    def test_full_place_order_flow(self):
        """Build -> place -> poll -> filled."""
        broker, auth = self._setup_broker_with_account()
        client = auth.get_client.return_value

        # Mock place_order response
        place_resp = MagicMock()
        place_resp.status_code = 201
        place_resp.is_error = False
        place_resp.headers = {
            'Location': 'https://api.schwabapi.com/trader/v1/accounts/HASH123/orders/99999'
        }
        client.place_order.return_value = place_resp

        # Mock get_order (polled) - filled immediately
        order_resp = MagicMock()
        order_resp.status_code = 200
        order_resp.json.return_value = {
            'status': 'FILLED',
            'filledQuantity': 2,
            'price': 1.50,
            'orderActivityCollection': [{
                'executionLegs': [{'price': 1.48, 'quantity': 2}]
            }],
        }
        client.get_order.return_value = order_resp

        spread = _make_spread()
        order_id = broker.place_spread_order(spread, 2, 1.50)
        assert order_id == '99999'

    def test_order_timeout_cancels(self):
        """Order not filled within timeout -> cancel."""
        broker, auth = self._setup_broker_with_account()
        broker.ORDER_TIMEOUT = 1  # Very short for test
        broker.ORDER_POLL_INTERVAL = 0.1
        client = auth.get_client.return_value

        # Mock place_order
        place_resp = MagicMock()
        place_resp.status_code = 201
        place_resp.is_error = False
        place_resp.headers = {
            'Location': 'https://api.schwabapi.com/trader/v1/accounts/HASH123/orders/88888'
        }
        client.place_order.return_value = place_resp

        # Mock get_order - always WORKING (never fills)
        order_resp = MagicMock()
        order_resp.status_code = 200
        order_resp.json.return_value = {'status': 'WORKING', 'filledQuantity': 0}
        client.get_order.return_value = order_resp

        # Mock cancel
        cancel_resp = MagicMock()
        cancel_resp.status_code = 200
        client.cancel_order.return_value = cancel_resp

        spread = _make_spread()
        order_id = broker.place_spread_order(spread, 1, 1.50)
        assert order_id == ''  # Failed due to timeout
        client.cancel_order.assert_called()

    def test_not_authenticated_raises_error(self):
        """If auth fails, should raise."""
        auth = _mock_auth()
        auth.get_client.side_effect = Exception("Not authenticated")
        broker = _make_broker(auth)

        spread = _make_spread()
        with pytest.raises(Exception, match="Not authenticated"):
            broker.place_spread_order(spread, 1, 1.50)

    def test_invalid_quantity_rejected(self):
        broker = _make_broker()
        spread = _make_spread()
        assert broker.place_spread_order(spread, 0) == ''
        assert broker.place_spread_order(spread, -1) == ''

    def test_invalid_price_rejected(self):
        broker = _make_broker()
        spread = _make_spread()
        assert broker.place_spread_order(spread, 1, limit_price=0) == ''
        assert broker.place_spread_order(spread, 1, limit_price=-1.0) == ''


# ===========================================================================
# TestSchwabAuth
# ===========================================================================

class TestSchwabAuth:
    """Test Schwab authentication manager."""

    def test_token_fresh_not_expiring(self):
        """A 1-day-old token should not be expiring soon."""
        with tempfile.TemporaryDirectory() as tmpdir:
            meta_path = os.path.join(tmpdir, 'schwab_token_meta.json')
            auth = SchwabAuth(
                app_key='test', app_secret='test',
                token_path=os.path.join(tmpdir, 'schwab_token.json'),
            )
            auth._meta_path = meta_path

            # Write metadata with token created 1 day ago
            with open(meta_path, 'w') as f:
                json.dump({
                    'token_created_at': (datetime.now() - timedelta(days=1)).isoformat(),
                    'refresh_token_lifetime_days': 7,
                }, f)

            status = auth.get_token_status()
            assert status['hours_remaining'] > 100
            assert not status['expiring_soon']

    def test_token_6_days_old_expiring_soon(self):
        """A 6-day-old token should be expiring soon (< 48 hours left)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            meta_path = os.path.join(tmpdir, 'schwab_token_meta.json')
            auth = SchwabAuth(
                app_key='test', app_secret='test',
                token_path=os.path.join(tmpdir, 'schwab_token.json'),
            )
            auth._meta_path = meta_path

            with open(meta_path, 'w') as f:
                json.dump({
                    'token_created_at': (datetime.now() - timedelta(days=6)).isoformat(),
                }, f)

            status = auth.get_token_status()
            assert status['hours_remaining'] < 48
            assert status['expiring_soon']

    def test_token_7_days_old_expired(self):
        """A 7+ day old token should show as expired."""
        with tempfile.TemporaryDirectory() as tmpdir:
            meta_path = os.path.join(tmpdir, 'schwab_token_meta.json')
            auth = SchwabAuth(
                app_key='test', app_secret='test',
                token_path=os.path.join(tmpdir, 'schwab_token.json'),
            )
            auth._meta_path = meta_path

            with open(meta_path, 'w') as f:
                json.dump({
                    'token_created_at': (datetime.now() - timedelta(days=8)).isoformat(),
                }, f)

            status = auth.get_token_status()
            assert status['hours_remaining'] == 0
            assert not status['valid']

    def test_is_token_expiring_soon_custom_threshold(self):
        """Custom threshold for expiring_soon check."""
        with tempfile.TemporaryDirectory() as tmpdir:
            meta_path = os.path.join(tmpdir, 'schwab_token_meta.json')
            token_path = os.path.join(tmpdir, 'schwab_token.json')
            auth = SchwabAuth(app_key='test', app_secret='test', token_path=token_path)
            auth._meta_path = meta_path

            # Create token file so get_token_status reports valid=True
            with open(token_path, 'w') as f:
                f.write('{}')

            # Token created 5 days ago (48 hours remaining)
            with open(meta_path, 'w') as f:
                json.dump({
                    'token_created_at': (datetime.now() - timedelta(days=5)).isoformat(),
                }, f)

            # Within 72 hours? Yes (48h remaining < 72)
            assert auth.is_token_expiring_soon(hours=72)
            # Within 24 hours? No (48h remaining > 24)
            assert not auth.is_token_expiring_soon(hours=24)

    def test_missing_token_file(self):
        """No token file -> not authenticated."""
        auth = SchwabAuth(
            app_key='test', app_secret='test',
            token_path='/nonexistent/path/token.json',
        )
        assert not auth.is_authenticated()

    def test_missing_metadata_file(self):
        """No metadata file -> token status shows invalid."""
        with tempfile.TemporaryDirectory() as tmpdir:
            auth = SchwabAuth(
                app_key='test', app_secret='test',
                token_path=os.path.join(tmpdir, 'token.json'),
            )
            auth._meta_path = os.path.join(tmpdir, 'nonexistent_meta.json')
            status = auth.get_token_status()
            assert not status['valid']
            assert status['expiring_soon']

    def test_get_token_status_structure(self):
        """Verify token status dict has all expected keys."""
        with tempfile.TemporaryDirectory() as tmpdir:
            meta_path = os.path.join(tmpdir, 'schwab_token_meta.json')
            auth = SchwabAuth(app_key='test', app_secret='test',
                              token_path=os.path.join(tmpdir, 'token.json'))
            auth._meta_path = meta_path

            with open(meta_path, 'w') as f:
                json.dump({
                    'token_created_at': datetime.now().isoformat(),
                }, f)

            status = auth.get_token_status()
            assert 'valid' in status
            assert 'created_at' in status
            assert 'expires_at' in status
            assert 'hours_remaining' in status
            assert 'expiring_soon' in status


# ===========================================================================
# TestBrokerFactory
# ===========================================================================

class TestBrokerFactory:
    """Test broker factory function."""

    def test_dry_run_returns_dry_run_broker(self):
        from src.brokers.broker_factory import get_broker
        from src.brokers.dry_run_broker import DryRunBroker

        config = {
            'broker': {'active': 'dry_run'},
            'portfolio': {'account_size': 75000.0},
        }
        broker = get_broker(config)
        assert isinstance(broker, DryRunBroker)
        assert broker.initial_balance == 75000.0

    @patch('src.brokers.etrade_auth.ETradeAuth')
    @patch('src.brokers.etrade_broker.ETradeBroker')
    def test_etrade_returns_etrade_broker(self, mock_broker_cls, mock_auth_cls):
        from src.brokers.broker_factory import get_broker

        mock_broker_cls.return_value = MagicMock()
        config = {'broker': {'active': 'etrade'}}
        broker = get_broker(config)
        mock_auth_cls.assert_called_once()
        mock_broker_cls.assert_called_once()

    @patch('src.brokers.schwab_auth.SchwabAuth')
    @patch('src.brokers.schwab_broker.SchwabBroker')
    def test_schwab_returns_schwab_broker(self, mock_broker_cls, mock_auth_cls):
        from src.brokers.broker_factory import get_broker

        mock_broker_cls.return_value = MagicMock()
        config = {
            'broker': {
                'active': 'schwab',
                'schwab': {
                    'app_key': 'key',
                    'app_secret': 'secret',
                    'callback_url': 'https://127.0.0.1',
                    'token_path': 'test.json',
                },
            }
        }
        broker = get_broker(config)
        mock_auth_cls.assert_called_once()
        mock_broker_cls.assert_called_once()

    def test_invalid_broker_raises_error(self):
        from src.brokers.broker_factory import get_broker

        config = {'broker': {'active': 'invalid_broker'}}
        with pytest.raises(ValueError, match='Unknown broker'):
            get_broker(config)

    def test_missing_broker_config_defaults_to_dry_run(self):
        from src.brokers.broker_factory import get_broker
        from src.brokers.dry_run_broker import DryRunBroker

        config = {}  # No broker section at all
        broker = get_broker(config)
        assert isinstance(broker, DryRunBroker)


# ===========================================================================
# TestSchwabAccountBalance
# ===========================================================================

class TestSchwabAccountBalance:
    """Test account balance retrieval."""

    def test_get_account_balance(self):
        auth = _mock_auth()
        broker = _make_broker(auth)
        client = auth.get_client.return_value

        # Mock account numbers
        acct_resp = MagicMock()
        acct_resp.status_code = 200
        acct_resp.json.return_value = [
            {'accountNumber': '12345678', 'hashValue': 'HASH123'}
        ]
        client.get_account_numbers.return_value = acct_resp

        # Mock account balance
        balance_resp = MagicMock()
        balance_resp.status_code = 200
        balance_resp.json.return_value = {
            'securitiesAccount': {
                'currentBalances': {
                    'liquidationValue': 75000.50,
                    'availableFunds': 50000.00,
                    'buyingPower': 100000.00,
                }
            }
        }
        client.get_account.return_value = balance_resp

        balance = broker.get_account_balance()
        assert balance['net_account_value'] == 75000.50
        assert balance['cash_available'] == 50000.00
        assert balance['buying_power'] == 100000.00


# ===========================================================================
# TestSchwabGetPrice
# ===========================================================================

class TestSchwabGetPrice:
    """Test price retrieval."""

    def test_get_spx_price(self):
        auth = _mock_auth()
        broker = _make_broker(auth)
        client = auth.get_client.return_value

        quote_resp = MagicMock()
        quote_resp.status_code = 200
        quote_resp.json.return_value = {
            '$SPX': {
                'quote': {
                    'lastPrice': 6048.25,
                    'closePrice': 6040.00,
                },
                'reference': {},
            }
        }
        client.get_quote.return_value = quote_resp

        price = broker.get_current_price('SPX')
        assert price == 6048.25
        client.get_quote.assert_called_with('$SPX')

    def test_get_price_market_closed(self):
        """When market is closed, should return close price."""
        auth = _mock_auth()
        broker = _make_broker(auth)
        client = auth.get_client.return_value

        quote_resp = MagicMock()
        quote_resp.status_code = 200
        quote_resp.json.return_value = {
            '$SPX': {
                'quote': {
                    'lastPrice': 0,
                    'closePrice': 6040.00,
                },
                'reference': {},
            }
        }
        client.get_quote.return_value = quote_resp

        price = broker.get_current_price('SPX')
        assert price == 6040.00


# ===========================================================================
# TestSchwabPositionValue
# ===========================================================================

class TestSchwabPositionValue:
    """Test position value calculation."""

    def test_get_position_value_bearish(self):
        auth = _mock_auth()
        broker = _make_broker(auth)
        client = auth.get_client.return_value

        # Mock options chain
        chain_resp = MagicMock()
        chain_resp.status_code = 200
        chain_resp.json.return_value = {
            'callExpDateMap': {
                '2026-02-13:0': {
                    '6050.0': [{'bid': 3.00, 'ask': 3.40, 'last': 3.20, 'delta': 0.45, 'totalVolume': 100, 'openInterest': 500, 'symbol': 'X'}],
                    '6055.0': [{'bid': 1.60, 'ask': 2.00, 'last': 1.80, 'delta': 0.35, 'totalVolume': 100, 'openInterest': 500, 'symbol': 'Y'}],
                }
            },
            'putExpDateMap': {
                '2026-02-13:0': {
                    '6050.0': [{'bid': 2.80, 'ask': 3.00, 'last': 2.90, 'delta': -0.55, 'totalVolume': 100, 'openInterest': 500, 'symbol': 'Z'}],
                    '6055.0': [{'bid': 3.80, 'ask': 4.00, 'last': 3.90, 'delta': -0.65, 'totalVolume': 100, 'openInterest': 500, 'symbol': 'W'}],
                }
            },
        }
        client.get_option_chain.return_value = chain_resp

        spread = _make_spread()  # BEARISH call spread 6050/6055
        value = broker.get_position_value(spread)

        # For call spread: short_mid - long_mid
        # short_mid = (3.00 + 3.40) / 2 = 3.20
        # long_mid = (1.60 + 2.00) / 2 = 1.80
        # value = 3.20 - 1.80 = 1.40
        assert abs(value - 1.40) < 0.01


# ===========================================================================
# TestSchwabCloseSpread
# ===========================================================================

class TestSchwabCloseSpread:
    """Test spread closing."""

    def test_close_spread_invalid_quantity(self):
        broker = _make_broker()
        spread = _make_spread()
        assert broker.close_spread(spread, 0) == ''
        assert broker.close_spread(spread, -1) == ''

    def test_close_spread_invalid_price(self):
        broker = _make_broker()
        spread = _make_spread()
        assert broker.close_spread(spread, 1, limit_price=-0.5) == ''


# ===========================================================================
# TestSchwabStatusMapping
# ===========================================================================

class TestSchwabStatusMapping:
    """Test exhaustive status mapping coverage."""

    def test_all_schwab_statuses_mapped(self):
        """Every Schwab status should map to a known normalized status."""
        from schwab.client import Client
        for status in Client.Order.Status:
            assert status.value in _SCHWAB_STATUS_MAP, f"Missing mapping for {status.value}"

    def test_all_normalized_statuses_have_enum(self):
        """Every normalized status value should map to an OrderStatus enum."""
        normalized_values = set(_SCHWAB_STATUS_MAP.values())
        for nv in normalized_values:
            assert nv in _NORMALIZED_TO_ENUM, f"Missing enum for normalized status '{nv}'"
