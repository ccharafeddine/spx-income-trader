"""
Tests for POST /api/settings/validate-live endpoint.

Validates the 6-check pre-live checklist: credentials, environment,
connection, options permissions, max contracts, and account size.

Now broker-aware: tests cover both E*TRADE and Schwab code paths.
"""

import pytest
from unittest.mock import patch, MagicMock
import json

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dashboard.app import app, API_TOKEN


@pytest.fixture
def client():
    """Flask test client."""
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def _post_validate(client):
    """Helper to POST /api/settings/validate-live and return parsed JSON."""
    resp = client.post('/api/settings/validate-live', headers={'X-API-Token': API_TOKEN})
    assert resp.status_code == 200
    return resp.get_json()


def _find_check(data, check_id):
    """Find a check by id in the response."""
    for c in data['checks']:
        if c['id'] == check_id:
            return c
    return None


def _etrade_settings(**overrides):
    """Build a settings dict with E*TRADE as active broker."""
    base = {
        'broker': {'active': 'etrade'},
        'portfolio': {'account_size': 50000, 'max_contracts': 1},
    }
    base.update(overrides)
    return base


def _schwab_settings(**overrides):
    """Build a settings dict with Schwab as active broker."""
    base = {
        'broker': {
            'active': 'schwab',
            'schwab': {'app_key': 'key', 'app_secret': 'secret', 'account_number': 'acct'},
        },
        'portfolio': {'account_size': 50000, 'max_contracts': 1},
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# E*TRADE Tests
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
    mock_settings.return_value = _etrade_settings()

    data = _post_validate(client)

    assert data['can_proceed'] is False
    assert _find_check(data, 'credentials')['passed'] is False
    # connection + options should be skipped/failed
    assert _find_check(data, 'connection')['passed'] is False
    assert _find_check(data, 'options')['passed'] is False
    assert 'Skipped' in _find_check(data, 'connection')['detail']


@patch('dashboard.app.get_etrade_credentials')
@patch('dashboard.app._load_settings')
def test_sandbox_warning_allows_proceed(mock_settings, mock_creds, client):
    mock_creds.return_value = {
        'consumer_key': 'key',
        'consumer_secret': 'secret',
        'account_id': 'acct',
        'sandbox': True,
    }
    mock_settings.return_value = _etrade_settings()

    mock_broker = MagicMock()
    mock_broker.connect.return_value = True
    mock_broker.get_account_balance.return_value = {'net_account_value': 50000.0}
    mock_broker.get_options_chain.return_value = [{'strike': 5000}, {'strike': 5010}]

    with patch('src.brokers.broker_factory.get_broker', return_value=mock_broker):
        data = _post_validate(client)

    assert data['can_proceed'] is True
    env_check = _find_check(data, 'environment')
    assert env_check['passed'] is False  # sandbox = warning
    assert env_check['severity'] == 'warning'


@patch('dashboard.app.get_etrade_credentials')
@patch('dashboard.app._load_settings')
def test_connection_failure_blocks(mock_settings, mock_creds, client):
    mock_creds.return_value = {
        'consumer_key': 'key',
        'consumer_secret': 'secret',
        'account_id': 'acct',
        'sandbox': False,
    }
    mock_settings.return_value = _etrade_settings()

    mock_broker = MagicMock()
    mock_broker.connect.return_value = False

    with patch('src.brokers.broker_factory.get_broker', return_value=mock_broker):
        data = _post_validate(client)

    assert data['can_proceed'] is False
    assert _find_check(data, 'connection')['passed'] is False


@patch('dashboard.app.get_etrade_credentials')
@patch('dashboard.app._load_settings')
def test_options_failure_blocks(mock_settings, mock_creds, client):
    mock_creds.return_value = {
        'consumer_key': 'key',
        'consumer_secret': 'secret',
        'account_id': 'acct',
        'sandbox': False,
    }
    mock_settings.return_value = _etrade_settings()

    mock_broker = MagicMock()
    mock_broker.connect.return_value = True
    mock_broker.get_account_balance.return_value = {'net_account_value': 50000.0}
    mock_broker.get_options_chain.return_value = []

    with patch('src.brokers.broker_factory.get_broker', return_value=mock_broker):
        data = _post_validate(client)

    assert data['can_proceed'] is False
    assert _find_check(data, 'options')['passed'] is False


@patch('dashboard.app.get_etrade_credentials')
@patch('dashboard.app._load_settings')
def test_all_checks_pass(mock_settings, mock_creds, client):
    mock_creds.return_value = {
        'consumer_key': 'key',
        'consumer_secret': 'secret',
        'account_id': 'acct',
        'sandbox': False,
    }
    mock_settings.return_value = _etrade_settings(
        portfolio={'account_size': 50000, 'max_contracts': 2}
    )

    mock_broker = MagicMock()
    mock_broker.connect.return_value = True
    mock_broker.get_account_balance.return_value = {'net_account_value': 50000.0}
    mock_broker.get_options_chain.return_value = [{'strike': 5000}, {'strike': 5010}]

    with patch('src.brokers.broker_factory.get_broker', return_value=mock_broker):
        data = _post_validate(client)

    assert data['can_proceed'] is True
    assert data['all_passed'] is True
    assert len(data['checks']) == 6


@patch('dashboard.app.get_etrade_credentials')
@patch('dashboard.app._load_settings')
def test_warnings_only_allows_proceed(mock_settings, mock_creds, client):
    mock_creds.return_value = {
        'consumer_key': 'key',
        'consumer_secret': 'secret',
        'account_id': 'acct',
        'sandbox': True,  # warning: sandbox
    }
    mock_settings.return_value = _etrade_settings(
        portfolio={
            'account_size': 50000,
            'max_contracts': 10,  # warning: >2
        }
    )

    mock_broker = MagicMock()
    mock_broker.connect.return_value = True
    # 20% diff from config -> warning
    mock_broker.get_account_balance.return_value = {'net_account_value': 40000.0}
    mock_broker.get_options_chain.return_value = [{'strike': 5000}]

    with patch('src.brokers.broker_factory.get_broker', return_value=mock_broker):
        data = _post_validate(client)

    assert data['can_proceed'] is True
    assert data['all_passed'] is False

    # Verify specific warnings
    assert _find_check(data, 'environment')['passed'] is False
    assert _find_check(data, 'contracts')['passed'] is False
    assert _find_check(data, 'account_size')['passed'] is True


# --------------------------------------------------------------------------
# Schwab Tests
# --------------------------------------------------------------------------

@patch('dashboard.app.is_schwab_configured', return_value=True)
@patch('dashboard.app._load_settings')
def test_schwab_all_checks_pass(mock_settings, mock_schwab_cfg, client):
    mock_settings.return_value = _schwab_settings(
        portfolio={'account_size': 50000, 'max_contracts': 1}
    )

    mock_broker = MagicMock()
    mock_broker.auth.is_authenticated.return_value = True
    mock_broker.get_account_balance.return_value = {'net_account_value': 50000.0}
    mock_broker.get_options_chain.return_value = [{'strike': 5000}, {'strike': 5010}]

    with patch('src.brokers.broker_factory.get_broker', return_value=mock_broker):
        data = _post_validate(client)

    assert data['can_proceed'] is True
    assert data['all_passed'] is True
    assert len(data['checks']) == 6
    # Schwab always passes environment check (no sandbox concept)
    assert _find_check(data, 'environment')['passed'] is True


@patch('dashboard.app.is_schwab_configured', return_value=False)
@patch('dashboard.app._load_settings')
def test_schwab_missing_creds_blocks(mock_settings, mock_schwab_cfg, client):
    mock_settings.return_value = {
        'broker': {'active': 'schwab', 'schwab': {}},
        'portfolio': {'account_size': 50000, 'max_contracts': 1},
    }

    data = _post_validate(client)

    assert data['can_proceed'] is False
    assert _find_check(data, 'credentials')['passed'] is False


# --------------------------------------------------------------------------
# Dry Run broker test
# --------------------------------------------------------------------------

@patch('dashboard.app._load_settings')
def test_dry_run_blocks_live(mock_settings, client):
    mock_settings.return_value = {
        'broker': {'active': 'dry_run'},
        'portfolio': {'account_size': 50000, 'max_contracts': 1},
    }

    data = _post_validate(client)

    assert data['can_proceed'] is False
    assert _find_check(data, 'credentials')['passed'] is False
    assert 'dry_run' in _find_check(data, 'credentials')['detail']
