"""
Comprehensive unit tests for ETradeBroker

Tests all safety features:
- Limit orders only (never market)
- Order preview before placement
- Order confirmation polling
- Timeout and cancellation
- Exponential backoff retry
- dry_run mode
"""

import pytest
from unittest.mock import Mock, MagicMock, patch, PropertyMock
from datetime import datetime, date
import time

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.brokers.etrade_broker import (
    ETradeBroker,
    ETradeAPIError,
    OrderStatus,
    OrderResult
)
from src.brokers.etrade_auth import ETradeAuth
from src.models.spread import CreditSpread, OptionLeg, TradeDirection


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
def mock_auth():
    """Create mock authenticated ETradeAuth"""
    auth = Mock(spec=ETradeAuth)
    auth.is_authenticated.return_value = True
    auth.get_session.return_value = Mock()
    return auth


@pytest.fixture
def broker(mock_auth):
    """Create ETradeBroker with mocked auth"""
    with patch.dict('os.environ', {}, clear=False):
        broker = ETradeBroker(auth=mock_auth, order_timeout=5)
        broker.account_id_key = 'test_account_key'
        return broker


@pytest.fixture
def sample_spread():
    """Create sample credit spread for testing"""
    short_leg = OptionLeg(
        strike=6900.0,
        option_type='put',
        action='sell',
        price=2.50,
        quantity=1,
        symbol='SPX'
    )
    long_leg = OptionLeg(
        strike=6895.0,
        option_type='put',
        action='buy',
        price=1.50,
        quantity=1,
        symbol='SPX'
    )
    return CreditSpread(
        direction=TradeDirection.BULLISH,
        short_leg=short_leg,
        long_leg=long_leg,
        credit_received=1.00,
        entry_time=datetime.now(),
        expiration=datetime(2026, 2, 7, 16, 0, 0),
        underlying_price_at_entry=6890.0
    )


# ============================================================================
# MOCK RESPONSE BUILDERS
# ============================================================================

def build_preview_response(preview_id: str = 'preview123'):
    """Build mock preview order response"""
    return {
        'PreviewOrderResponse': {
            'PreviewIds': [{'previewId': preview_id}],
            'Order': [{'estimatedCommission': 0.65}]
        }
    }


def build_place_response(order_id: str = '12345'):
    """Build mock place order response"""
    return {
        'PlaceOrderResponse': {
            'OrderIds': [{'orderId': order_id}],
            'Order': [{'orderStatus': 'OPEN'}]
        }
    }


def build_order_status_response(order_id: str, status: str, fill_price: float = 0, filled_qty: int = 0):
    """Build mock order status response"""
    return {
        'OrdersResponse': {
            'Order': [{
                'orderId': order_id,
                'OrderDetail': [{
                    'status': status,
                    'executedPrice': fill_price,
                    'filledQuantity': filled_qty,
                    'orderType': 'SPREADS',
                    'placedTime': '2026-02-07T10:00:00',
                    'executedTime': '2026-02-07T10:00:05' if status == 'EXECUTED' else None
                }]
            }]
        }
    }


def build_quote_response(price: float = 6890.0):
    """Build mock quote response"""
    return {
        'QuoteResponse': {
            'QuoteData': [{
                'Product': {'symbol': '$SPX.X'},
                'All': {
                    'lastTrade': price,
                    'bid': price - 0.5,
                    'ask': price + 0.5,
                    'high': price + 10,
                    'low': price - 10,
                    'open': price - 5,
                    'previousClose': price - 3,
                    'totalVolume': 1000000,
                    'changeClose': 3.0,
                    'changeClosePercentage': 0.04
                }
            }]
        }
    }


def build_options_chain_response():
    """Build mock options chain response"""
    return {
        'OptionChainResponse': {
            'OptionPair': [
                {
                    'Call': {
                        'strikePrice': 6900.0,
                        'bid': 5.0,
                        'ask': 5.5,
                        'lastPrice': 5.25,
                        'volume': 100,
                        'openInterest': 500,
                        'optionSymbol': 'SPX_020726C6900'
                    },
                    'Put': {
                        'strikePrice': 6900.0,
                        'bid': 2.5,
                        'ask': 3.0,
                        'lastPrice': 2.75,
                        'volume': 200,
                        'openInterest': 800,
                        'optionSymbol': 'SPX_020726P6900'
                    }
                },
                {
                    'Call': {
                        'strikePrice': 6895.0,
                        'bid': 7.0,
                        'ask': 7.5,
                        'lastPrice': 7.25,
                        'volume': 80,
                        'openInterest': 400,
                        'optionSymbol': 'SPX_020726C6895'
                    },
                    'Put': {
                        'strikePrice': 6895.0,
                        'bid': 1.5,
                        'ask': 2.0,
                        'lastPrice': 1.75,
                        'volume': 150,
                        'openInterest': 600,
                        'optionSymbol': 'SPX_020726P6895'
                    }
                }
            ]
        }
    }


# ============================================================================
# TEST: SAFETY - LIMIT ORDERS ONLY
# ============================================================================

class TestLimitOrdersOnly:
    """Verify all orders are LIMIT orders, never MARKET"""

    def test_build_spread_order_uses_net_credit_for_open(self, broker, sample_spread):
        """Opening orders must use NET_CREDIT price type"""
        order_data = broker._build_spread_order(sample_spread, 5, 1.00, action='OPEN')

        order = order_data['PreviewOrderRequest']['Order'][0]
        assert order['priceType'] == 'NET_CREDIT'
        assert order['limitPrice'] == 1.00

    def test_build_spread_order_uses_net_debit_for_close(self, broker, sample_spread):
        """Closing orders must use NET_DEBIT price type"""
        order_data = broker._build_spread_order(sample_spread, 5, 0.20, action='CLOSE')

        order = order_data['PreviewOrderRequest']['Order'][0]
        assert order['priceType'] == 'NET_DEBIT'
        assert order['limitPrice'] == 0.20

    def test_no_market_order_type_in_build(self, broker, sample_spread):
        """Ensure MARKET is never used as price type"""
        for action in ['OPEN', 'CLOSE']:
            order_data = broker._build_spread_order(sample_spread, 5, 1.00, action=action)
            order = order_data['PreviewOrderRequest']['Order'][0]
            assert 'MARKET' not in order['priceType']


# ============================================================================
# TEST: ORDER PREVIEW BEFORE PLACEMENT
# ============================================================================

class TestOrderPreview:
    """Verify orders are always previewed before placement"""

    def test_preview_called_before_place(self, broker, sample_spread):
        """place_spread_order must call preview first"""
        with patch.object(broker, '_request') as mock_request:
            mock_request.side_effect = [
                build_preview_response('preview123'),
                build_place_response('12345'),
                build_order_status_response('12345', 'EXECUTED', 1.00, 5)
            ]

            broker.place_spread_order(sample_spread, 5, 1.00)

            # First call should be preview
            first_call = mock_request.call_args_list[0]
            assert 'preview' in first_call[0][1].lower()

    def test_preview_failure_stops_order(self, broker, sample_spread):
        """If preview fails, order should not be placed"""
        with patch.object(broker, '_request') as mock_request:
            mock_request.side_effect = ETradeAPIError("Preview failed", 400)

            result = broker.place_spread_order(sample_spread, 5, 1.00)

            assert result == ''
            # Should only have one call (failed preview), not place
            assert mock_request.call_count == 1

    def test_dry_run_stops_after_preview(self, broker, sample_spread):
        """dry_run=True should preview but not place"""
        with patch.object(broker, '_request') as mock_request:
            mock_request.return_value = build_preview_response('preview123')

            result = broker.place_spread_order(sample_spread, 5, 1.00, dry_run=True)

            assert 'PREVIEW' in result
            assert mock_request.call_count == 1  # Only preview, no place


# ============================================================================
# TEST: ORDER CONFIRMATION POLLING
# ============================================================================

class TestOrderPolling:
    """Verify order fill confirmation via polling"""

    def test_polls_until_filled(self, broker, sample_spread):
        """Should poll order status until EXECUTED"""
        with patch.object(broker, '_request') as mock_request:
            mock_request.side_effect = [
                build_preview_response(),
                build_place_response('12345'),
                build_order_status_response('12345', 'OPEN'),
                build_order_status_response('12345', 'OPEN'),
                build_order_status_response('12345', 'EXECUTED', 1.00, 5)
            ]

            with patch('time.sleep'):  # Speed up test
                result = broker.place_spread_order(sample_spread, 5, 1.00)

            assert result == '12345'

    def test_logs_fill_price_vs_expected(self, broker, sample_spread, caplog):
        """Should log fill price compared to expected price"""
        import logging
        caplog.set_level(logging.INFO)

        with patch.object(broker, '_request') as mock_request:
            mock_request.side_effect = [
                build_preview_response(),
                build_place_response('12345'),
                build_order_status_response('12345', 'EXECUTED', 1.05, 5)  # Filled at better price
            ]

            with patch('time.sleep'):
                broker.place_spread_order(sample_spread, 5, 1.00)

            # Check logging captured fill vs expected
            assert any('FILLED' in record.message for record in caplog.records)

    def test_handles_rejected_order(self, broker, sample_spread):
        """Should return empty string if order is rejected"""
        with patch.object(broker, '_request') as mock_request:
            mock_request.side_effect = [
                build_preview_response(),
                build_place_response('12345'),
                build_order_status_response('12345', 'REJECTED')
            ]

            with patch('time.sleep'):
                result = broker.place_spread_order(sample_spread, 5, 1.00)

            assert result == ''


# ============================================================================
# TEST: TIMEOUT AND CANCELLATION
# ============================================================================

class TestTimeoutAndCancellation:
    """Verify unfilled orders are cancelled after timeout"""

    def test_cancels_order_on_timeout(self, broker, sample_spread):
        """Should cancel order if it doesn't fill within timeout"""
        broker.order_timeout = 2  # Short timeout for test
        broker.MAX_ORDER_POLLS = 3

        with patch.object(broker, '_request') as mock_request:
            # Preview, place, then always return OPEN status
            mock_request.side_effect = [
                build_preview_response(),
                build_place_response('12345'),
                build_order_status_response('12345', 'OPEN'),
                build_order_status_response('12345', 'OPEN'),
                build_order_status_response('12345', 'OPEN'),
                {'CancelOrderResponse': {'orderId': '12345'}}  # Cancel response
            ]

            with patch('time.sleep'):
                result = broker.place_spread_order(sample_spread, 5, 1.00)

            # Should return empty string (failure)
            assert result == ''

            # Verify cancel was called
            cancel_calls = [c for c in mock_request.call_args_list if 'cancel' in str(c).lower()]
            assert len(cancel_calls) >= 1

    def test_does_not_retry_after_timeout(self, broker, sample_spread):
        """After timeout, should NOT automatically retry"""
        broker.order_timeout = 1
        broker.MAX_ORDER_POLLS = 2

        with patch.object(broker, '_request') as mock_request:
            mock_request.side_effect = [
                build_preview_response(),
                build_place_response('12345'),
                build_order_status_response('12345', 'OPEN'),
                build_order_status_response('12345', 'OPEN'),
                {'CancelOrderResponse': {'orderId': '12345'}}
            ]

            with patch('time.sleep'):
                result = broker.place_spread_order(sample_spread, 5, 1.00)

            # Result should be failure, not a retry
            assert result == ''

            # Should only have one place call (no retry)
            place_calls = [c for c in mock_request.call_args_list
                          if 'place' in str(c[0][1]).lower() and 'preview' not in str(c[0][1]).lower()]
            assert len(place_calls) == 1


# ============================================================================
# TEST: EXPONENTIAL BACKOFF RETRY
# ============================================================================

class TestExponentialBackoff:
    """Verify retry logic with exponential backoff for transient errors"""

    def test_retries_on_500_error(self, broker):
        """Should retry on 500 server error with backoff"""
        mock_session = Mock()
        mock_response_500 = Mock()
        mock_response_500.status_code = 500
        mock_response_500.text = 'Server Error'

        mock_response_ok = Mock()
        mock_response_ok.status_code = 200
        mock_response_ok.headers = {'Content-Type': 'application/json'}
        mock_response_ok.json.return_value = {'success': True}

        mock_session.get.side_effect = [mock_response_500, mock_response_ok]
        broker.auth.get_session.return_value = mock_session

        with patch('time.sleep') as mock_sleep:
            result = broker._request('GET', '/test')

        assert result == {'success': True}
        assert mock_sleep.called  # Backoff sleep was called
        assert mock_session.get.call_count == 2

    def test_backoff_increases_exponentially(self, broker):
        """Backoff delay should double each retry"""
        mock_session = Mock()
        mock_response_500 = Mock()
        mock_response_500.status_code = 503
        mock_response_500.text = 'Service Unavailable'

        mock_response_ok = Mock()
        mock_response_ok.status_code = 200
        mock_response_ok.headers = {'Content-Type': 'application/json'}
        mock_response_ok.json.return_value = {'success': True}

        mock_session.get.side_effect = [
            mock_response_500,
            mock_response_500,
            mock_response_ok
        ]
        broker.auth.get_session.return_value = mock_session

        sleep_times = []
        with patch('time.sleep', side_effect=lambda x: sleep_times.append(x)):
            broker._request('GET', '/test')

        # First retry: 1s, second retry: 2s (exponential)
        assert len(sleep_times) == 2
        assert sleep_times[1] > sleep_times[0]

    def test_gives_up_after_max_retries(self, broker):
        """Should raise error after MAX_ORDER_RETRIES attempts"""
        mock_session = Mock()
        mock_response_500 = Mock()
        mock_response_500.status_code = 500
        mock_response_500.text = 'Server Error'

        mock_session.get.return_value = mock_response_500
        broker.auth.get_session.return_value = mock_session

        with patch('time.sleep'):
            with pytest.raises(ETradeAPIError):
                broker._request('GET', '/test')

        assert mock_session.get.call_count == broker.MAX_ORDER_RETRIES + 1


# ============================================================================
# TEST: DRY RUN MODE
# ============================================================================

class TestDryRunMode:
    """Verify dry_run parameter works correctly"""

    def test_dry_run_returns_preview_id(self, broker, sample_spread):
        """dry_run should return PREVIEW-{id} format"""
        with patch.object(broker, '_request') as mock_request:
            mock_request.return_value = build_preview_response('abc123')

            result = broker.place_spread_order(sample_spread, 5, dry_run=True)

            assert result == 'PREVIEW-abc123'

    def test_dry_run_does_not_place_order(self, broker, sample_spread):
        """dry_run should only call preview, never place"""
        with patch.object(broker, '_request') as mock_request:
            mock_request.return_value = build_preview_response()

            broker.place_spread_order(sample_spread, 5, dry_run=True)

            # Should only have 1 call (preview)
            assert mock_request.call_count == 1

    def test_close_position_dry_run(self, broker, sample_spread):
        """close_position with dry_run should preview only"""
        with patch.object(broker, '_request') as mock_request:
            mock_request.return_value = build_preview_response('close123')

            result = broker.close_position(sample_spread, 5, 0.20, dry_run=True)

            assert 'PREVIEW' in result
            assert mock_request.call_count == 1


# ============================================================================
# TEST: LOGGING BEFORE SUBMISSION
# ============================================================================

class TestOrderLogging:
    """Verify all order details are logged before submission"""

    def test_logs_order_details_before_preview(self, broker, sample_spread, caplog):
        """Should log full order details before any API call"""
        import logging
        caplog.set_level(logging.INFO)

        with patch.object(broker, '_request') as mock_request:
            mock_request.return_value = build_preview_response()

            broker.place_spread_order(sample_spread, 5, 1.00, dry_run=True)

        # Check key details are logged
        log_text = ' '.join(record.message for record in caplog.records)
        assert 'BULLISH' in log_text.upper() or 'Direction' in log_text
        assert '6,900' in log_text  # Short strike (formatted with comma)
        assert '6,895' in log_text  # Long strike (formatted with comma)
        assert '5' in log_text  # Quantity

    def test_logs_include_dry_run_flag(self, broker, sample_spread, caplog):
        """Logs should indicate if this is a dry run"""
        import logging
        caplog.set_level(logging.INFO)

        with patch.object(broker, '_request') as mock_request:
            mock_request.return_value = build_preview_response()

            broker.place_spread_order(sample_spread, 5, dry_run=True)

        log_text = ' '.join(record.message for record in caplog.records)
        assert 'Dry Run' in log_text or 'DRY RUN' in log_text or 'dry_run' in log_text.lower()


# ============================================================================
# TEST: MARKET DATA METHODS
# ============================================================================

class TestMarketData:
    """Test market data retrieval methods"""

    def test_get_current_price(self, broker):
        """get_current_price should return lastTrade from quote"""
        with patch.object(broker, '_request') as mock_request:
            mock_request.return_value = build_quote_response(6890.50)

            price = broker.get_current_price('SPX')

            assert price == 6890.50

    def test_get_options_chain_format(self, broker):
        """get_options_chain should return properly formatted chain"""
        with patch.object(broker, '_request') as mock_request:
            mock_request.return_value = build_options_chain_response()

            chain = broker.get_options_chain('SPX', '2026-02-07')

            assert 6900.0 in chain
            assert 6895.0 in chain
            assert 'call_bid' in chain[6900.0]
            assert 'put_bid' in chain[6900.0]
            assert chain[6900.0]['put_bid'] == 2.5

    def test_get_position_value(self, broker, sample_spread):
        """get_position_value should calculate spread value from chain"""
        with patch.object(broker, '_request') as mock_request:
            mock_request.return_value = build_options_chain_response()

            value = broker.get_position_value(sample_spread)

            # Put spread: short 6900 mid = 2.75, long 6895 mid = 1.75
            # Spread value = 2.75 - 1.75 = 1.00 (per-share; callers multiply by 100)
            assert value == 1.0


# ============================================================================
# TEST: ACCOUNT METHODS
# ============================================================================

class TestAccountMethods:
    """Test account-related methods"""

    def test_get_account_balance(self, broker):
        """get_account_balance should return balance dict"""
        with patch.object(broker, '_request') as mock_request:
            mock_request.return_value = {
                'BalanceResponse': {
                    'Computed': {
                        'RealTimeValues': {'totalAccountValue': 50000.0},
                        'cashAvailableForInvestment': 45000.0,
                        'settledCashForInvestment': 44000.0,
                        'marginBuyingPower': 100000.0
                    }
                }
            }

            balance = broker.get_account_balance()

            assert balance['net_account_value'] == 50000.0
            assert balance['cash_available'] == 45000.0
            assert balance['margin_buying_power'] == 100000.0


# ============================================================================
# TEST: ORDER RESULT DATACLASS
# ============================================================================

class TestOrderResult:
    """Test OrderResult dataclass"""

    def test_order_result_success(self):
        """OrderResult should properly represent successful fill"""
        result = OrderResult(
            success=True,
            order_id='12345',
            status=OrderStatus.EXECUTED,
            fill_price=1.05,
            filled_quantity=5,
            message='Filled at $1.05'
        )

        assert result.success is True
        assert result.order_id == '12345'
        assert result.fill_price == 1.05

    def test_order_result_failure(self):
        """OrderResult should properly represent failed order"""
        result = OrderResult(
            success=False,
            order_id='12345',
            status=OrderStatus.CANCELLED,
            fill_price=0,
            filled_quantity=0,
            message='Timeout after 30s'
        )

        assert result.success is False
        assert result.status == OrderStatus.CANCELLED


# ============================================================================
# TEST: ERROR HANDLING
# ============================================================================

class TestErrorHandling:
    """Test error handling scenarios"""

    def test_api_error_includes_response(self):
        """ETradeAPIError should include response text"""
        error = ETradeAPIError(
            message="Bad request",
            status_code=400,
            response='{"error": "Invalid symbol"}'
        )

        assert error.status_code == 400
        assert 'Invalid symbol' in error.response

    def test_handles_not_authenticated(self, broker):
        """Should raise error if not authenticated"""
        broker.auth.is_authenticated.return_value = False

        with pytest.raises(ETradeAPIError) as exc_info:
            broker._request('GET', '/test')

        assert 'authenticated' in str(exc_info.value).lower()


# ============================================================================
# INTEGRATION-STYLE TESTS
# ============================================================================

class TestFullOrderFlow:
    """Test complete order flow from start to finish"""

    def test_successful_order_flow(self, broker, sample_spread):
        """Test complete successful order: preview -> place -> fill"""
        with patch.object(broker, '_request') as mock_request:
            mock_request.side_effect = [
                build_preview_response('prev123'),
                build_place_response('ord456'),
                build_order_status_response('ord456', 'EXECUTED', 1.00, 5)
            ]

            with patch('time.sleep'):
                result = broker.place_spread_order(sample_spread, 5, 1.00)

            assert result == 'ord456'
            assert mock_request.call_count == 3

    def test_close_position_flow(self, broker, sample_spread):
        """Test complete close position flow"""
        with patch.object(broker, '_request') as mock_request:
            mock_request.side_effect = [
                build_preview_response('prev789'),
                build_place_response('close123'),
                build_order_status_response('close123', 'EXECUTED', 0.15, 5)
            ]

            with patch('time.sleep'):
                result = broker.close_position(sample_spread, 5, 0.20)

            assert result == 'close123'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
