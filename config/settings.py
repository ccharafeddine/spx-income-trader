import os
from pathlib import Path
from dotenv import load_dotenv
import yaml

# Load environment variables
load_dotenv()

# Base directory
BASE_DIR = Path(__file__).parent.parent

# E*TRADE Configuration
_etrade_sandbox = os.getenv('ETRADE_SANDBOX', 'true').lower() == 'true'

# Use sandbox credentials when in sandbox mode, production credentials otherwise
ETRADE_CONFIG = {
    'consumer_key': os.getenv('ETRADE_SANDBOX_KEY') if _etrade_sandbox else os.getenv('ETRADE_CONSUMER_KEY'),
    'consumer_secret': os.getenv('ETRADE_SANDBOX_SECRET') if _etrade_sandbox else os.getenv('ETRADE_CONSUMER_SECRET'),
    'account_id': os.getenv('ETRADE_ACCOUNT_ID'),
    'sandbox': _etrade_sandbox,
    'base_url': 'https://apisb.etrade.com' if _etrade_sandbox else 'https://api.etrade.com',
    # Authorization URL is ALWAYS us.etrade.com, even for sandbox
    'auth_base_url': 'https://us.etrade.com',
    'token_file': str(BASE_DIR / 'tokens' / ('sandbox_tokens.json' if _etrade_sandbox else 'tokens.json'))
}

# Trading Configuration
TRADING_MODE = os.getenv('TRADING_MODE', 'paper')

# Database Configuration
DATABASE_PATH = os.getenv('DATABASE_PATH', str(BASE_DIR / 'database' / 'trades.db'))

# Logging Configuration
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
LOG_FILE = os.getenv('LOG_FILE', str(BASE_DIR / 'logs' / 'trading.log'))

# Dashboard Configuration
DASHBOARD_PORT = int(os.getenv('DASHBOARD_PORT', 5000))
DASHBOARD_HOST = os.getenv('DASHBOARD_HOST', '0.0.0.0')

# Load strategy parameters
def load_strategy_params():
    """Load strategy parameters from YAML file"""
    params_file = BASE_DIR / 'config' / 'strategy_params.yaml'
    
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
    
    if TRADING_MODE not in ['paper', 'live']:
        errors.append(f"Invalid TRADING_MODE: {TRADING_MODE}")
    
    if errors:
        error_msg = "\n".join(errors)
        raise ValueError(f"Configuration errors:\n{error_msg}")

# Validate on import (but only if not in paper mode)
if TRADING_MODE == 'live':
    validate_settings()