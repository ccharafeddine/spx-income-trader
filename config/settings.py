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


def is_schwab_configured() -> bool:
    """Check if Schwab credentials are configured (keyring or YAML fallback)."""
    creds = get_schwab_credentials()
    return bool(creds.get('app_key') and creds.get('app_secret'))


def is_ibkr_configured() -> bool:
    """Check if IBKR is configured. IBKR uses TWS/Gateway (no API keys)."""
    params = load_strategy_params()
    ibkr_cfg = params.get('broker', {}).get('ibkr', {})
    return bool(ibkr_cfg.get('port'))


def is_any_broker_configured() -> bool:
    """Check if any broker is configured (Schwab, E*TRADE, IBKR, or dry_run).

    Returns True if:
    - broker.active is 'dry_run' (always valid)
    - broker.active is 'schwab' and Schwab creds are set
    - broker.active is 'etrade' and E*TRADE creds are set
    - broker.active is 'ibkr' and IBKR is configured
    - E*TRADE creds exist (backward compat)
    - Schwab creds exist
    - IBKR is configured
    """
    params = load_strategy_params()
    active = params.get('broker', {}).get('active', '')
    if active == 'dry_run':
        return True
    if active == 'schwab' and is_schwab_configured():
        return True
    if active == 'etrade' and is_etrade_configured():
        return True
    if active == 'ibkr' and is_ibkr_configured():
        return True
    # Fallback: any broker has creds
    return is_etrade_configured() or is_schwab_configured() or is_ibkr_configured()


def get_etrade_credentials() -> dict:
    """Public accessor for E*TRADE credential details."""
    return _get_etrade_credentials()


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


# ---------------------------------------------------------------------------
# Schwab credential management (OS keyring)
# ---------------------------------------------------------------------------

SCHWAB_KEYRING_SERVICE = 'spx-income-trader-schwab'


def save_schwab_credentials(
    app_key: str,
    app_secret: str,
    account_number: str = '',
    callback_url: str = 'https://127.0.0.1',
) -> bool:
    """Save Schwab credentials to the OS keychain."""
    try:
        import keyring
        keyring.set_password(SCHWAB_KEYRING_SERVICE, 'schwab_app_key', app_key)
        keyring.set_password(SCHWAB_KEYRING_SERVICE, 'schwab_app_secret', app_secret)
        if account_number:
            keyring.set_password(SCHWAB_KEYRING_SERVICE, 'schwab_account_number', account_number)
        keyring.set_password(SCHWAB_KEYRING_SERVICE, 'schwab_callback_url', callback_url)
        return True
    except ImportError:
        logger.error("keyring module not installed - cannot save Schwab credentials")
        return False
    except Exception as e:
        logger.error(f"Failed to save Schwab credentials to keyring: {e}")
        return False


def get_schwab_credentials() -> dict:
    """Load Schwab credentials from OS keychain, fall back to YAML config."""
    try:
        import keyring
        key = keyring.get_password(SCHWAB_KEYRING_SERVICE, 'schwab_app_key')
        secret = keyring.get_password(SCHWAB_KEYRING_SERVICE, 'schwab_app_secret')
        if key and secret:
            return {
                'app_key': key,
                'app_secret': secret,
                'account_number': keyring.get_password(SCHWAB_KEYRING_SERVICE, 'schwab_account_number') or '',
                'callback_url': keyring.get_password(SCHWAB_KEYRING_SERVICE, 'schwab_callback_url') or 'https://127.0.0.1',
                'source': 'keyring',
            }
    except (ImportError, Exception):
        pass

    # Fall back to YAML config (legacy / migration)
    params = load_strategy_params()
    schwab_cfg = params.get('broker', {}).get('schwab', {})
    return {
        'app_key': schwab_cfg.get('app_key', ''),
        'app_secret': schwab_cfg.get('app_secret', ''),
        'account_number': schwab_cfg.get('account_number', ''),
        'callback_url': schwab_cfg.get('callback_url', 'https://127.0.0.1'),
        'source': 'yaml' if schwab_cfg.get('app_key') else 'none',
    }


def clear_schwab_credentials() -> bool:
    """Remove Schwab credentials from the OS keychain."""
    try:
        import keyring
        for key in ['schwab_app_key', 'schwab_app_secret', 'schwab_account_number', 'schwab_callback_url']:
            try:
                keyring.delete_password(SCHWAB_KEYRING_SERVICE, key)
            except keyring.errors.PasswordDeleteError:
                pass
        return True
    except ImportError:
        return False
    except Exception as e:
        logger.error(f"Failed to clear Schwab credentials from keyring: {e}")
        return False


# ---------------------------------------------------------------------------
# FRED API key management (OS keyring)
# ---------------------------------------------------------------------------

FRED_KEYRING_SERVICE = 'spx-income-trader-fred'


def save_fred_api_key(api_key: str) -> bool:
    """Save FRED API key to the OS keychain."""
    try:
        import keyring
        keyring.set_password(FRED_KEYRING_SERVICE, 'fred_api_key', api_key)
        return True
    except ImportError:
        logger.error("keyring module not installed - cannot save FRED API key")
        return False
    except Exception as e:
        logger.error(f"Failed to save FRED API key to keyring: {e}")
        return False


def get_fred_api_key() -> str | None:
    """Load FRED API key from OS keychain."""
    try:
        import keyring
        key = keyring.get_password(FRED_KEYRING_SERVICE, 'fred_api_key')
        return key if key else None
    except (ImportError, Exception):
        return None


def clear_fred_api_key() -> bool:
    """Remove FRED API key from the OS keychain."""
    try:
        import keyring
        try:
            keyring.delete_password(FRED_KEYRING_SERVICE, 'fred_api_key')
        except keyring.errors.PasswordDeleteError:
            pass
        return True
    except ImportError:
        return False
    except Exception as e:
        logger.error(f"Failed to clear FRED API key from keyring: {e}")
        return False


def _get_trading_mode() -> str:
    """
    Load trading mode with priority:
    1. OS Keychain (via keyring)
    2. Environment variable (TRADING_MODE)
    3. Default: 'dry-run'
    """
    kr_mode = _get_keyring_credential('trading_mode')
    if kr_mode and kr_mode in ('dry-run', 'live'):
        return kr_mode
    return os.getenv('TRADING_MODE', 'dry-run')


def get_trading_mode() -> str:
    """Public accessor for the current trading mode."""
    return _get_trading_mode()


def save_trading_mode(mode: str) -> bool:
    """
    Save trading mode to the OS keychain.

    Returns True if successful, False otherwise.
    """
    if mode not in ('dry-run', 'live'):
        return False
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, 'trading_mode', mode)
        return True
    except ImportError:
        logger.error("keyring module not installed - cannot save trading mode")
        return False
    except Exception as e:
        logger.error(f"Failed to save trading mode to keyring: {e}")
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
TRADING_MODE = _get_trading_mode()

# Mode-specific database paths
_DB_DIR = str(Path(DB_PATH).parent)
BACKTEST_DB_PATH = os.path.join(_DB_DIR, 'backtest.db')


def get_database_path(mode: str = None) -> str:
    """Return mode-specific database path.

    Args:
        mode: 'dry-run' or 'live'. Defaults to current TRADING_MODE.
    """
    if mode is None:
        mode = TRADING_MODE
    if mode == 'live':
        return os.path.join(_DB_DIR, 'trades_live.db')
    return os.path.join(_DB_DIR, 'trades_dryrun.db')


# Database Configuration - mode-specific (dry-run vs live)
DATABASE_PATH = os.getenv('DATABASE_PATH') or get_database_path()

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
                'max_daily_trades': 1
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

    with open(params_file, 'r', encoding='utf-8') as f:
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
    """Validate that critical settings are configured for the active broker."""
    errors = []

    if TRADING_MODE not in ['dry-run', 'live']:
        errors.append(f"Invalid TRADING_MODE: {TRADING_MODE}")

    active_broker = STRATEGY_PARAMS.get('broker', {}).get('active', 'dry_run')

    if active_broker == 'etrade':
        if not ETRADE_CONFIG['consumer_key']:
            errors.append("E*TRADE consumer_key not configured (set via keyring or .env)")
        if not ETRADE_CONFIG['consumer_secret']:
            errors.append("E*TRADE consumer_secret not configured (set via keyring or .env)")
        if not ETRADE_CONFIG['account_id']:
            errors.append("E*TRADE account_id not configured (set via keyring or .env)")
    elif active_broker == 'schwab':
        schwab_creds = get_schwab_credentials()
        if not schwab_creds.get('app_key'):
            errors.append("Schwab app_key not configured (set via Settings page or keyring)")
        if not schwab_creds.get('app_secret'):
            errors.append("Schwab app_secret not configured (set via Settings page or keyring)")
    elif active_broker == 'ibkr':
        pass  # IBKR uses TWS/Gateway, no API keys to validate
    elif active_broker != 'dry_run':
        errors.append(f"Unknown broker.active value: '{active_broker}'")

    if errors:
        error_msg = "\n".join(errors)
        raise ValueError(f"Configuration errors:\n{error_msg}")

# Validate on import (only in live mode)
if TRADING_MODE == 'live':
    validate_settings()
