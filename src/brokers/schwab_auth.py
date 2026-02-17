"""
Charles Schwab OAuth2 Authentication Manager

Uses schwab-py for OAuth2 authentication with Schwab's API.
Refresh tokens expire after 7 days and require re-authentication.

Token lifecycle:
- Access tokens: ~30 minutes (schwab-py handles refresh automatically)
- Refresh tokens: 7 days (requires manual re-auth via browser)
- This module tracks refresh token age via a metadata file

SAFETY FEATURES:
- Token expiry warnings at 48h and 12h before refresh token death
- Metadata file tracks token creation time independently
- Keyring storage for app credentials (same pattern as etrade_auth.py)
- CLI entrypoint for initial auth and re-auth
"""

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict

import pytz

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config.settings import BASE_DIR

_ET = pytz.timezone('US/Eastern')

logger = logging.getLogger(__name__)

KEYRING_SERVICE = 'spx-income-trader-schwab'

# Schwab refresh tokens expire after 7 days
REFRESH_TOKEN_LIFETIME_DAYS = 7


class SchwabAuth:
    """Handles Schwab OAuth2 authentication using schwab-py."""

    def __init__(
        self,
        app_key: Optional[str] = None,
        app_secret: Optional[str] = None,
        callback_url: str = 'https://127.0.0.1',
        token_path: Optional[str] = None,
    ):
        # Try keyring first, fall back to provided values
        kr_key = _get_keyring_credential('schwab_app_key')
        kr_secret = _get_keyring_credential('schwab_app_secret')

        self.app_key = kr_key or app_key or ''
        self.app_secret = kr_secret or app_secret or ''
        self.callback_url = callback_url
        self.token_path = token_path or str(BASE_DIR / 'database' / 'schwab_token.json')
        self._meta_path = str(Path(self.token_path).with_name('schwab_token_meta.json'))

        self._client = None

    def is_authenticated(self) -> bool:
        """Check if we have a valid token file and can create a client."""
        if not os.path.exists(self.token_path):
            return False
        try:
            self._get_or_create_client()
            return True
        except Exception as e:
            logger.warning(f"Schwab auth check failed: {e}")
            return False

    def is_token_expiring_soon(self, hours: int = 24) -> bool:
        """Check if refresh token is within N hours of 7-day expiry."""
        status = self.get_token_status()
        if not status['valid']:
            return True
        return status['hours_remaining'] <= hours

    def get_token_status(self) -> Dict:
        """Get detailed token status including expiry information."""
        if not os.path.exists(self._meta_path):
            return {
                'valid': False,
                'created_at': None,
                'expires_at': None,
                'hours_remaining': 0,
                'expiring_soon': True,
            }

        try:
            with open(self._meta_path, 'r') as f:
                meta = json.load(f)

            created_str = meta.get('token_created_at')
            if not created_str:
                return {
                    'valid': False,
                    'created_at': None,
                    'expires_at': None,
                    'hours_remaining': 0,
                    'expiring_soon': True,
                }

            created_at = datetime.fromisoformat(created_str)
            # Ensure timezone-aware comparison (ET-aware)
            if created_at.tzinfo is None:
                created_at = _ET.localize(created_at)
            expires_at = created_at + timedelta(days=REFRESH_TOKEN_LIFETIME_DAYS)
            now = datetime.now(_ET)
            remaining = expires_at - now
            hours_remaining = max(0, remaining.total_seconds() / 3600)

            valid = hours_remaining > 0 and os.path.exists(self.token_path)
            expiring_soon = hours_remaining <= 48

            # Log warnings based on remaining time
            if hours_remaining <= 0:
                logger.error("Schwab refresh token has EXPIRED. Re-authentication required.")
            elif hours_remaining <= 12:
                logger.error(
                    f"Schwab refresh token expires in {hours_remaining:.1f} hours! "
                    "Re-authenticate immediately."
                )
            elif hours_remaining <= 48:
                logger.warning(
                    f"Schwab refresh token expires in {hours_remaining:.1f} hours. "
                    "Plan to re-authenticate soon."
                )

            return {
                'valid': valid,
                'created_at': created_str,
                'expires_at': expires_at.isoformat(),
                'hours_remaining': round(hours_remaining, 1),
                'expiring_soon': expiring_soon,
            }

        except Exception as e:
            logger.error(f"Error reading token metadata: {e}")
            return {
                'valid': False,
                'created_at': None,
                'expires_at': None,
                'hours_remaining': 0,
                'expiring_soon': True,
            }

    def get_client(self):
        """Get the authenticated schwab Client. Raises if not authenticated."""
        return self._get_or_create_client()

    def _get_or_create_client(self):
        """Load client from token file, caching the result."""
        if self._client is not None:
            return self._client

        import schwab.auth
        self._client = schwab.auth.client_from_token_file(
            self.token_path,
            api_key=self.app_key,
            app_secret=self.app_secret,
        )
        logger.info("Schwab client loaded from token file")
        return self._client

    def initial_auth(
        self,
        app_key: Optional[str] = None,
        app_secret: Optional[str] = None,
        callback_url: Optional[str] = None,
        token_path: Optional[str] = None,
    ):
        """Run the manual OAuth2 flow (opens browser).

        This is called once after getting API keys, and again every 7 days
        to refresh the token. Returns the authenticated client.
        """
        import schwab.auth

        key = app_key or self.app_key
        secret = app_secret or self.app_secret
        cb_url = callback_url or self.callback_url
        t_path = token_path or self.token_path

        # Ensure directory exists
        Path(t_path).parent.mkdir(parents=True, exist_ok=True)

        client = schwab.auth.client_from_manual_flow(
            api_key=key,
            app_secret=secret,
            callback_url=cb_url,
            token_path=t_path,
        )

        # Write metadata
        self._write_metadata()

        # Save credentials to keyring if available
        _save_keyring_credential('schwab_app_key', key)
        _save_keyring_credential('schwab_app_secret', secret)

        # Cache the client
        self._client = client
        self.app_key = key
        self.app_secret = secret
        self.callback_url = cb_url
        self.token_path = t_path

        logger.info("Schwab initial authentication completed successfully")
        return client

    def _write_metadata(self):
        """Write token creation timestamp to metadata file with restricted permissions."""
        meta = {
            'token_created_at': datetime.now(_ET).isoformat(),
            'refresh_token_lifetime_days': REFRESH_TOKEN_LIFETIME_DAYS,
        }
        Path(self._meta_path).parent.mkdir(parents=True, exist_ok=True)
        with open(self._meta_path, 'w') as f:
            json.dump(meta, f, indent=2)

        # Restrict token files to owner-only read/write
        for path in [self._meta_path, self.token_path]:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass  # Windows may not support Unix permissions

        logger.info("Token metadata written")


def _get_keyring_credential(key: str) -> Optional[str]:
    """Try to load a credential from the OS keychain."""
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, key)
    except ImportError:
        return None
    except Exception:
        return None


def _save_keyring_credential(key: str, value: str) -> bool:
    """Save a credential to the OS keychain."""
    try:
        import keyring
        keyring.set_password(KEYRING_SERVICE, key, value)
        return True
    except ImportError:
        logger.debug("keyring not available, skipping credential save")
        return False
    except Exception as e:
        logger.warning(f"Failed to save to keyring: {e}")
        return False


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import yaml

    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

    # Load config
    config_path = Path(__file__).parent.parent.parent / 'config' / 'strategy_params.yaml'
    if config_path.exists():
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
    else:
        config = {}

    schwab_cfg = config.get('broker', {}).get('schwab', {})

    app_key = schwab_cfg.get('app_key', '')
    app_secret = schwab_cfg.get('app_secret', '')
    callback_url = schwab_cfg.get('callback_url', 'https://127.0.0.1')
    token_path = schwab_cfg.get('token_path', 'database/schwab_token.json')

    if not app_key or not app_secret:
        print("\nSchwab API credentials not configured.")
        print("Add your credentials to config/strategy_params.yaml under broker.schwab")
        print("or enter them now:\n")
        app_key = input("App Key: ").strip()
        app_secret = input("App Secret: ").strip()

    if not app_key or not app_secret:
        print("Error: App key and secret are required.")
        sys.exit(1)

    print("\n=== Schwab OAuth2 Authentication ===")
    print(f"Callback URL: {callback_url}")
    print(f"Token path: {token_path}")
    print()

    auth = SchwabAuth(
        app_key=app_key,
        app_secret=app_secret,
        callback_url=callback_url,
        token_path=token_path,
    )

    try:
        client = auth.initial_auth()
        status = auth.get_token_status()
        print("\nAuthentication successful!")
        print(f"Token created: {status['created_at']}")
        print(f"Token expires: {status['expires_at']}")
        print(f"Hours remaining: {status['hours_remaining']}")
        print(f"\nRe-run this script before the token expires ({REFRESH_TOKEN_LIFETIME_DAYS} days).")
    except Exception as e:
        print(f"\nAuthentication failed: {e}")
        sys.exit(1)
