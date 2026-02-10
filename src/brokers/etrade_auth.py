"""
E*TRADE OAuth 1.0a Authentication Handler

E*TRADE uses three-legged OAuth 1.0a:
1. Get request token
2. User authorizes in browser, gets verifier code
3. Exchange for access token

Access tokens are valid for 2 hours (midnight ET invalidates all tokens).
Tokens can be refreshed/renewed without re-authorization if still valid.
"""

import json
import logging
import os
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Tuple
from urllib.parse import parse_qs, urlencode

from requests_oauthlib import OAuth1Session

import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config.settings import ETRADE_CONFIG

logger = logging.getLogger(__name__)


class ETradeAuth:
    """Handles E*TRADE OAuth 1.0a authentication flow"""

    # API endpoints
    REQUEST_TOKEN_URL = "/oauth/request_token"
    ACCESS_TOKEN_URL = "/oauth/access_token"
    RENEW_TOKEN_URL = "/oauth/renew_access_token"
    REVOKE_TOKEN_URL = "/oauth/revoke_access_token"
    AUTHORIZE_URL = "/e/t/etws/authorize"

    REQUEST_TIMEOUT = 30  # seconds per HTTP request

    def __init__(
        self,
        consumer_key: Optional[str] = None,
        consumer_secret: Optional[str] = None,
        sandbox: bool = True,
        token_file: Optional[str] = None
    ):
        self.consumer_key = consumer_key or ETRADE_CONFIG['consumer_key']
        self.consumer_secret = consumer_secret or ETRADE_CONFIG['consumer_secret']
        self.sandbox = sandbox if sandbox is not None else ETRADE_CONFIG['sandbox']
        self.token_file = token_file or ETRADE_CONFIG['token_file']

        # Set base URLs based on environment
        # NOTE: Authorization URL is ALWAYS us.etrade.com, even for sandbox
        # Only the token endpoints use the sandbox domain
        if self.sandbox:
            self.base_url = "https://apisb.etrade.com"
        else:
            self.base_url = "https://api.etrade.com"

        # Authorization always goes through production domain
        self.auth_base_url = "https://us.etrade.com"

        # Token storage
        self.access_token: Optional[str] = None
        self.access_token_secret: Optional[str] = None
        self.token_timestamp: Optional[float] = None

        # OAuth session
        self._session: Optional[OAuth1Session] = None

    @property
    def session(self) -> OAuth1Session:
        """Get or create authenticated OAuth session"""
        if self._session is None:
            if not self.access_token or not self.access_token_secret:
                raise ValueError("Not authenticated. Call authenticate() first.")

            self._session = OAuth1Session(
                self.consumer_key,
                client_secret=self.consumer_secret,
                resource_owner_key=self.access_token,
                resource_owner_secret=self.access_token_secret
            )
        return self._session

    def _get_request_token(self) -> Tuple[str, str]:
        """Step 1: Get request token"""
        oauth = OAuth1Session(
            self.consumer_key,
            client_secret=self.consumer_secret,
            callback_uri="oob"  # Out-of-band for desktop apps
        )

        url = f"{self.base_url}{self.REQUEST_TOKEN_URL}"
        response = oauth.fetch_request_token(url, timeout=self.REQUEST_TIMEOUT)

        return response['oauth_token'], response['oauth_token_secret']

    def _get_authorization_url(self, request_token: str) -> str:
        """Step 2: Generate authorization URL for user"""
        params = {
            'key': self.consumer_key,
            'token': request_token
        }
        return f"{self.auth_base_url}{self.AUTHORIZE_URL}?{urlencode(params)}"

    def _get_access_token(
        self,
        request_token: str,
        request_token_secret: str,
        verifier: str
    ) -> Tuple[str, str]:
        """Step 3: Exchange request token + verifier for access token"""
        oauth = OAuth1Session(
            self.consumer_key,
            client_secret=self.consumer_secret,
            resource_owner_key=request_token,
            resource_owner_secret=request_token_secret,
            verifier=verifier
        )

        url = f"{self.base_url}{self.ACCESS_TOKEN_URL}"
        response = oauth.fetch_access_token(url, timeout=self.REQUEST_TIMEOUT)

        return response['oauth_token'], response['oauth_token_secret']

    def authenticate(self, auto_open_browser: bool = True) -> bool:
        """
        Run full OAuth authentication flow.

        1. Tries to load existing tokens
        2. If valid, refreshes them
        3. If no valid tokens, runs full browser-based auth

        Returns True if authentication successful.
        """
        # Try to load and refresh existing tokens
        if self._load_tokens():
            if self._renew_token():
                logger.info("Existing tokens renewed successfully.")
                return True
            else:
                logger.warning("Token renewal failed, need full re-authentication.")

        # Full authentication flow
        print("\n=== E*TRADE OAuth Authentication ===")
        print(f"Environment: {'SANDBOX' if self.sandbox else 'PRODUCTION'}")
        print()

        # Step 1: Get request token
        print("Step 1: Getting request token...")
        try:
            request_token, request_token_secret = self._get_request_token()
            print("  Request token obtained.")
        except Exception as e:
            logger.error(f"Error getting request token: {type(e).__name__}")
            return False

        # Step 2: User authorization
        auth_url = self._get_authorization_url(request_token)
        print("\nStep 2: User Authorization Required")
        print("-" * 50)
        print("Please authorize this application:")
        print(f"\n  {auth_url}\n")

        if auto_open_browser:
            print("Opening browser automatically...")
            try:
                webbrowser.open(auth_url)
            except Exception:
                print("Could not open browser automatically.")

        print("-" * 50)
        print("\nAfter authorizing, E*TRADE will display a verification code.")
        verifier = input("Enter the verification code: ").strip()

        if not verifier:
            print("No verification code entered.")
            return False

        # Step 3: Get access token
        print("\nStep 3: Getting access token...")
        try:
            self.access_token, self.access_token_secret = self._get_access_token(
                request_token, request_token_secret, verifier
            )
            self.token_timestamp = time.time()
            self._session = None  # Reset session with new tokens
            print("  Access token obtained!")
        except Exception as e:
            logger.error(f"Error getting access token: {type(e).__name__}")
            return False

        # Save tokens
        self._save_tokens()
        print("\nAuthentication successful! Tokens saved.")
        return True

    def _renew_token(self) -> bool:
        """Renew existing access token (extends validity)"""
        if not self.access_token or not self.access_token_secret:
            return False

        try:
            oauth = OAuth1Session(
                self.consumer_key,
                client_secret=self.consumer_secret,
                resource_owner_key=self.access_token,
                resource_owner_secret=self.access_token_secret
            )

            url = f"{self.base_url}{self.RENEW_TOKEN_URL}"
            response = oauth.get(url, timeout=self.REQUEST_TIMEOUT)

            if response.status_code == 200:
                self.token_timestamp = time.time()
                self._session = None
                self._save_tokens()
                return True
            else:
                logger.warning(f"Token renewal failed: HTTP {response.status_code}")
                return False

        except Exception as e:
            logger.error(f"Token renewal error: {type(e).__name__}")
            return False

    def revoke_token(self) -> bool:
        """Revoke current access token"""
        if not self.access_token:
            return True

        try:
            url = f"{self.base_url}{self.REVOKE_TOKEN_URL}"
            response = self.session.get(url, timeout=self.REQUEST_TIMEOUT)

            if response.status_code == 200:
                self.access_token = None
                self.access_token_secret = None
                self._session = None

                # Remove token file
                if os.path.exists(self.token_file):
                    os.remove(self.token_file)

                return True
            return False

        except Exception:
            return False

    def _save_tokens(self) -> None:
        """Save tokens to file"""
        token_data = {
            'access_token': self.access_token,
            'access_token_secret': self.access_token_secret,
            'timestamp': self.token_timestamp,
            'sandbox': self.sandbox
        }

        # Ensure directory exists
        token_path = Path(self.token_file)
        token_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self.token_file, 'w') as f:
            json.dump(token_data, f, indent=2)

    def _load_tokens(self) -> bool:
        """Load tokens from file if they exist and are valid"""
        if not os.path.exists(self.token_file):
            return False

        try:
            with open(self.token_file, 'r') as f:
                token_data = json.load(f)

            # Check sandbox mode matches
            if token_data.get('sandbox') != self.sandbox:
                logger.warning("Token file is for different environment.")
                return False

            # Check token age (E*TRADE tokens expire at midnight ET or after 2 hours of inactivity)
            timestamp = token_data.get('timestamp', 0)
            age_hours = (time.time() - timestamp) / 3600

            if age_hours > 1.5:  # Conservative: refresh if over 1.5 hours old
                logger.info(f"Tokens are {age_hours:.1f} hours old, will try to renew.")

            self.access_token = token_data['access_token']
            self.access_token_secret = token_data['access_token_secret']
            self.token_timestamp = timestamp
            self._session = None

            return True

        except Exception as e:
            logger.error(f"Error loading tokens: {type(e).__name__}")
            return False

    def is_authenticated(self) -> bool:
        """Check if we have valid authentication"""
        return bool(self.access_token and self.access_token_secret)

    def get_session(self) -> OAuth1Session:
        """Get authenticated session for making API calls"""
        if not self.is_authenticated():
            raise ValueError("Not authenticated. Call authenticate() first.")
        return self.session


if __name__ == "__main__":
    # Quick test
    auth = ETradeAuth()

    if not auth.consumer_key or not auth.consumer_secret:
        print("Error: E*TRADE credentials not configured.")
        print("Please set ETRADE_SANDBOX_KEY and ETRADE_SANDBOX_SECRET in .env")
    else:
        masked_key = ('****' + auth.consumer_key[-4:]) if len(auth.consumer_key) > 4 else '****'
        print(f"Consumer Key: {masked_key}")
        print(f"Sandbox Mode: {auth.sandbox}")
        print(f"Base URL: {auth.base_url}")
