"""
Tests for POST /api/settings/validate-live endpoint.

Validates the 6-check pre-live checklist: credentials, environment,
connection, options permissions, max contracts, and account size.
"""

import pytest
from unittest.mock import patch, MagicMock
import json

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dashboard.app import app


@pytest.fixture
def client():
    """Flask test client."""
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def _post_validate(client):
    """Helper to POST /api/settings/validate-live and return parsed JSON."""
    resp = client.post('/api/settings/validate-live')
    assert resp.status_code == 200
    return resp.get_json()


def _find_check(data, check_id):
    """Find a check by id in the response."""
    for c in data['checks']:
        if c['id'] == check_id:
            return c
    return None


# --------------------------------------------------------------------------
# Test 1: Missing credentials blocks live trading
# --------------------------------------------------------------------------

@patch('dashboard.app.get_etrade_credentials')
@patch('dashboard.app._load_settings')
def test_missing_credentials_blocks(mock_settings, mock_creds, client):
    mock_creds.return_value = {
        'consumer_key': '',
        'consumer_secret': 'secret',
        'account_id': 'acct',
        'sandbox': False,
    }
    mock_settings.return_value = {
        'portfolio': {'account_size': 50000, 'position_sizing': {'max_contracts': 1}},
    }

    data = _post_validate(client)

    assert data['can_proceed'] is False
    assert _find_check(data, 'credentials')['passed'] is False
    # connection + options should be skipped/failed
    assert _find_check(data, 'connection')['passed'] is False
    assert _find_check(data, 'options')['passed'] is False
    assert 'Skipped' in _find_check(data, 'connection')['detail']


# --------------------------------------------------------------------------
# Test 2: Sandbox warning allows proceed
# --------------------------------------------------------------------------

@patch('dashboard.app.get_etrade_credentials')
@patch('dashboard.app._load_settings')
def test_sandbox_warning_allows_proceed(mock_settings, mock_creds, client):
    mock_creds.return_value = {
        'consumer_key': 'key',
        'consumer_secret': 'secret',
        'account_id': 'acct',
        'sandbox': True,
    }
    mock_settings.return_value = {
        'portfolio': {'account_size': 50000, 'position_sizing': {'max_contracts': 1}},
    }

    mock_broker = MagicMock()
    mock_broker.connect.return_value = True
    mock_broker.get_account_balance.return_value = {'net_account_value': 50000.0}
    mock_broker.get_option_expirations.return_value = ['2026-02-20', '2026-02-27']

    with patch('src.brokers.etrade_broker.ETradeBroker', return_value=mock_broker):
        data = _post_validate(client)

    assert data['can_proceed'] is True
    env_check = _find_check(data, 'environment')
    assert env_check['passed'] is False  # sandbox = warning
    assert env_check['severity'] == 'warning'


# --------------------------------------------------------------------------
# Test 3: Connection failure blocks
# --------------------------------------------------------------------------

@patch('dashboard.app.get_etrade_credentials')
@patch('dashboard.app._load_settings')
def test_connection_failure_blocks(mock_settings, mock_creds, client):
    mock_creds.return_value = {
        'consumer_key': 'key',
        'consumer_secret': 'secret',
        'account_id': 'acct',
        'sandbox': False,
    }
    mock_settings.return_value = {
        'portfolio': {'account_size': 50000, 'position_sizing': {'max_contracts': 1}},
    }

    mock_broker = MagicMock()
    mock_broker.connect.return_value = False

    with patch('src.brokers.etrade_broker.ETradeBroker', return_value=mock_broker):
        data = _post_validate(client)

    assert data['can_proceed'] is False
    assert _find_check(data, 'connection')['passed'] is False


# --------------------------------------------------------------------------
# Test 4: Empty options expirations blocks
# --------------------------------------------------------------------------

@patch('dashboard.app.get_etrade_credentials')
@patch('dashboard.app._load_settings')
def test_options_failure_blocks(mock_settings, mock_creds, client):
    mock_creds.return_value = {
        'consumer_key': 'key',
        'consumer_secret': 'secret',
        'account_id': 'acct',
        'sandbox': False,
    }
    mock_settings.return_value = {
        'portfolio': {'account_size': 50000, 'position_sizing': {'max_contracts': 1}},
    }

    mock_broker = MagicMock()
    mock_broker.connect.return_value = True
    mock_broker.get_account_balance.return_value = {'net_account_value': 50000.0}
    mock_broker.get_option_expirations.return_value = []

    with patch('src.brokers.etrade_broker.ETradeBroker', return_value=mock_broker):
        data = _post_validate(client)

    assert data['can_proceed'] is False
    assert _find_check(data, 'options')['passed'] is False


# --------------------------------------------------------------------------
# Test 5: All checks pass
# --------------------------------------------------------------------------

@patch('dashboard.app.get_etrade_credentials')
@patch('dashboard.app._load_settings')
def test_all_checks_pass(mock_settings, mock_creds, client):
    mock_creds.return_value = {
        'consumer_key': 'key',
        'consumer_secret': 'secret',
        'account_id': 'acct',
        'sandbox': False,
    }
    mock_settings.return_value = {
        'portfolio': {'account_size': 50000, 'position_sizing': {'max_contracts': 2}},
    }

    mock_broker = MagicMock()
    mock_broker.connect.return_value = True
    mock_broker.get_account_balance.return_value = {'net_account_value': 50000.0}
    mock_broker.get_option_expirations.return_value = ['2026-02-20', '2026-02-27']

    with patch('src.brokers.etrade_broker.ETradeBroker', return_value=mock_broker):
        data = _post_validate(client)

    assert data['can_proceed'] is True
    assert data['all_passed'] is True
    assert len(data['checks']) == 6


# --------------------------------------------------------------------------
# Test 6: Warnings only still allows proceed
# --------------------------------------------------------------------------

@patch('dashboard.app.get_etrade_credentials')
@patch('dashboard.app._load_settings')
def test_warnings_only_allows_proceed(mock_settings, mock_creds, client):
    mock_creds.return_value = {
        'consumer_key': 'key',
        'consumer_secret': 'secret',
        'account_id': 'acct',
        'sandbox': True,  # warning: sandbox
    }
    mock_settings.return_value = {
        'portfolio': {
            'account_size': 50000,
            'position_sizing': {'max_contracts': 10},  # warning: >2
        },
    }

    mock_broker = MagicMock()
    mock_broker.connect.return_value = True
    # 20% diff from config -> warning
    mock_broker.get_account_balance.return_value = {'net_account_value': 40000.0}
    mock_broker.get_option_expirations.return_value = ['2026-02-20']

    with patch('src.brokers.etrade_broker.ETradeBroker', return_value=mock_broker):
        data = _post_validate(client)

    assert data['can_proceed'] is True
    assert data['all_passed'] is False

    # Verify specific warnings
    assert _find_check(data, 'environment')['passed'] is False
    assert _find_check(data, 'contracts')['passed'] is False
    assert _find_check(data, 'account_size')['passed'] is False
