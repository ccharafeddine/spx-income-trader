from __future__ import annotations

import os
import logging
from pathlib import Path
from dotenv import load_dotenv
import yaml

from src.utils.app_paths import (
    DATA_DIR, LOG_DIR, DB_PATH, TOKENS_DIR, CONFIG_DIR
)

# Load environment variables
load_dotenv()

# Base directory - points to DATA_DIR for full backward compatibility.
# Source mode: project root.  Packaged mode: platformdirs user data dir.
BASE_DIR = DATA_DIR

# Keyring service name for credential storage
KEYRING_SERVICE = 'spx-income-trader'

logger = logging.getLogger(__name__)


def _get_keyring_credential(key: str) -> str | None:
    """
    Try to load a credential from the OS keychain.

    Returns None if keyring is not available or credential doesn't exist.
    """
    try:
        import keyring
        value = keyring.get_password(KEYRING_SERVICE, key)
        return value
    except ImportError:
        return None
    except Exception:
        return None


def _get_etrade_credentials() -> dict:
    """
    Load E*TRADE credentials with priority:
    1. OS Keychain (via keyring)
    2. Environment variables (.env file)
    3. None (not configured)

    This ensures backward compatibility - developers using .env files
    see zero behavior change.
    """
    # First, try keyring
    kr_key = _get_keyring_credential('consumer_key')
    kr_secret = _get_keyring_credential('consumer_secret')
    kr_account = _get_keyring_credential('account_id')
    kr_sandbox = _get_keyring_credential('sandbox')

    # If keyring has credentials, use them
    if kr_key and kr_secret:
        sandbox = (kr_sandbox or 'true').lower() == 'true'
        return {
            'consumer_key': kr_key,
            'consumer_secret': kr_secret,
            'account_id': kr_account,
            'sandbox': sandbox,
            'source': 'keyring',
        }

    # Fall back to environment variables
    env_sandbox = os.getenv('ETRADE_SANDBOX', 'true').lower() == 'true'

    # Use sandbox credentials when in sandbox mode, production credentials otherwise
    if env_sandbox:
        env_key = os.getenv('ETRADE_SANDBOX_KEY')
        env_secret = os.getenv('ETRADE_SANDBOX_SECRET')
    else:
        env_key = os.getenv('ETRADE_CONSUMER_KEY')
        env_secret = os.getenv('ETRADE_CONSUMER_SECRET')

    env_account = os.getenv('ETRADE_ACCOUNT_ID')

    return {
        'consumer_key': env_key,
        'consumer_secret': env_secret,
        'account_id': env_account,
        'sandbox': env_sandbox,
        'source': 'env' if (env_key and env_secret) else 'none',
    }


def is_etrade_configured() -> bool:
    """Check if E*TRADE credentials are configured (either keyring or .env)."""
    creds = _get_etrade_credentials()
    return bool(creds['consumer_key'] and creds['consumer_secret'])


def save_etrade_credentials(
    consumer_key: str,
    consumer_secret: str,
    account_id: str,
    sandbox: bool = True
) -> bool:
    """
    Save E*TRADE credentials to the OS keychain.

    Returns True if successful, False otherwise.
    """
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, 'consumer_key', consumer_key)
        keyring.set_password(KEYRING_SERVICE, 'consumer_secret', consumer_secret)
        keyring.set_password(KEYRING_SERVICE, 'account_id', account_id)
        keyring.set_password(KEYRING_SERVICE, 'sandbox', 'true' if sandbox else 'false')
        return True
    except ImportError:
        logger.error("keyring module not installed - cannot save credentials")
        return False
    except Exception as e:
        logger.error(f"Failed to save credentials to keyring: {e}")
        return False


def clear_etrade_credentials() -> bool:
    """Remove E*TRADE credentials from the OS keychain."""
    try:
        import keyring
        for key in ['consumer_key', 'consumer_secret', 'account_id', 'sandbox']:
            try:
                keyring.delete_password(KEYRING_SERVICE, key)
            except keyring.errors.PasswordDeleteError:
                pass  # Key doesn't exist, that's fine
        return True
    except ImportError:
        return False
    except Exception as e:
        logger.error(f"Failed to clear credentials from keyring: {e}")
        return False


# Load credentials
_creds = _get_etrade_credentials()
_etrade_sandbox = _creds['sandbox']

# E*TRADE Configuration
ETRADE_CONFIG = {
    'consumer_key': _creds['consumer_key'],
    'consumer_secret': _creds['consumer_secret'],
    'account_id': _creds['account_id'],
    'sandbox': _etrade_sandbox,
    'base_url': 'https://apisb.etrade.com' if _etrade_sandbox else 'https://api.etrade.com',
    # Authorization URL is ALWAYS us.etrade.com, even for sandbox
    'auth_base_url': 'https://us.etrade.com',
    'token_file': str(TOKENS_DIR / ('sandbox_tokens.json' if _etrade_sandbox else 'tokens.json')),
    'credential_source': _creds['source'],
}

# Trading Configuration
TRADING_MODE = os.getenv('TRADING_MODE', 'dry-run')

# Database Configuration
DATABASE_PATH = os.getenv('DATABASE_PATH', str(DB_PATH))

# Logging Configuration
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
LOG_FILE = os.getenv('LOG_FILE', str(LOG_DIR / 'trading.log'))

# Dashboard Configuration
DASHBOARD_PORT = int(os.getenv('DASHBOARD_PORT', 5000))
DASHBOARD_HOST = os.getenv('DASHBOARD_HOST', '127.0.0.1')

# Load strategy parameters
def load_strategy_params():
    """Load strategy parameters from YAML file"""
    params_file = CONFIG_DIR / 'strategy_params.yaml'

    if not params_file.exists():
        return {
            'strategy': {
                'pulse_threshold': 10.0,
                'spread_width': 5.0,
                'profit_target_pct': 80.0,
                'max_daily_trades': 1,
                'contracts_per_trade': 5
            },
            'risk': {
                'max_position_size': 5,
                'max_daily_loss': 1000,
                'max_account_risk_pct': 2.0
            },
            'timing': {
                'morning_start': '09:30',
                'morning_end': '11:30',
                'market_close': '16:00'
            }
        }

    with open(params_file, 'r') as f:
        return yaml.safe_load(f)

STRATEGY_PARAMS = load_strategy_params()

# Notification Configuration
EMAIL_CONFIG = {
    'enabled': os.getenv('EMAIL_ENABLED', 'false').lower() == 'true',
    'address': os.getenv('EMAIL_ADDRESS'),
    'smtp_server': os.getenv('SMTP_SERVER', 'smtp.gmail.com'),
    'smtp_port': int(os.getenv('SMTP_PORT', 587)),
    'username': os.getenv('SMTP_USERNAME'),
    'password': os.getenv('SMTP_PASSWORD')
}

SMS_CONFIG = {
    'enabled': os.getenv('SMS_ENABLED', 'false').lower() == 'true',
    'twilio_sid': os.getenv('TWILIO_ACCOUNT_SID'),
    'twilio_token': os.getenv('TWILIO_AUTH_TOKEN'),
    'from_number': os.getenv('TWILIO_FROM_NUMBER'),
    'to_number': os.getenv('TWILIO_TO_NUMBER')
}

# Validate critical settings on import
def validate_settings():
    """Validate that critical settings are configured"""
    errors = []

    if not ETRADE_CONFIG['consumer_key']:
        errors.append("ETRADE_CONSUMER_KEY not set in .env")

    if not ETRADE_CONFIG['consumer_secret']:
        errors.append("ETRADE_CONSUMER_SECRET not set in .env")

    if not ETRADE_CONFIG['account_id']:
        errors.append("ETRADE_ACCOUNT_ID not set in .env")

    if TRADING_MODE not in ['dry-run', 'live']:
        errors.append(f"Invalid TRADING_MODE: {TRADING_MODE}")

    if errors:
        error_msg = "\n".join(errors)
        raise ValueError(f"Configuration errors:\n{error_msg}")

# Validate on import (only in live mode)
if TRADING_MODE == 'live':
    validate_settings()
