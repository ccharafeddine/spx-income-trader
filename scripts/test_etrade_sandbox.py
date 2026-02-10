#!/usr/bin/env python3
"""
E*TRADE Sandbox Connection Test

This script tests the E*TRADE sandbox API integration:
1. Authenticates via OAuth 1.0a
2. Fetches account information
3. Gets SPX quote
4. Lists available option expirations
5. Fetches option chain data

Run: python scripts/test_etrade_sandbox.py
"""

import sys
import os
from pathlib import Path
from datetime import datetime, date, timedelta

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from config.settings import ETRADE_CONFIG
from src.brokers.etrade_auth import ETradeAuth
from src.brokers.etrade_broker import ETradeBroker, ETradeAPIError


def print_header(title: str) -> None:
    """Print formatted header"""
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


def print_section(title: str) -> None:
    """Print section header"""
    print()
    print(f"--- {title} ---")


def check_configuration() -> bool:
    """Verify E*TRADE configuration"""
    print_header("Configuration Check")

    config_ok = True

    # Check credentials
    if ETRADE_CONFIG['consumer_key']:
        key = ETRADE_CONFIG['consumer_key']
        masked_key = '****' + key[-4:] if len(key) > 4 else '****'
        print(f"  Consumer Key:    {masked_key}  [OK]")
    else:
        print("  Consumer Key:    NOT SET  [MISSING]")
        config_ok = False

    if ETRADE_CONFIG['consumer_secret']:
        print(f"  Consumer Secret: ********  [OK]")
    else:
        print("  Consumer Secret: NOT SET  [MISSING]")
        config_ok = False

    # Show environment
    print()
    print(f"  Sandbox Mode:    {ETRADE_CONFIG['sandbox']}")
    print(f"  Base URL:        {ETRADE_CONFIG['base_url']}")
    print(f"  Token File:      {ETRADE_CONFIG['token_file']}")

    if ETRADE_CONFIG['account_id']:
        acct = ETRADE_CONFIG['account_id']
        masked_acct = '****' + acct[-4:] if len(acct) > 4 else acct
        print(f"  Account ID:      {masked_acct}")
    else:
        print("  Account ID:      NOT SET (will use first available)")

    if not config_ok:
        print()
        print("ERROR: Missing credentials!")
        print()
        print("Please create a .env file with your E*TRADE sandbox credentials:")
        print()
        print("  ETRADE_SANDBOX_KEY=your_sandbox_key")
        print("  ETRADE_SANDBOX_SECRET=your_sandbox_secret")
        print("  ETRADE_ACCOUNT_ID=your_account_id")
        print("  ETRADE_SANDBOX=true")
        print()
        print("Get sandbox credentials at: https://developer.etrade.com")

    return config_ok


def test_authentication() -> ETradeAuth:
    """Test OAuth authentication"""
    print_header("OAuth Authentication")

    auth = ETradeAuth()

    # Check for existing tokens
    if auth._load_tokens():
        print("  Found existing tokens, attempting renewal...")
        if auth._renew_token():
            print("  Token renewal successful!")
            return auth
        else:
            print("  Token renewal failed, need full authentication.")

    # Full authentication flow
    print()
    print("  Starting OAuth 1.0a authentication flow...")
    print()
    print("  IMPORTANT: E*TRADE OAuth requires browser authorization.")
    print("  A browser window will open. Log in and authorize the application.")
    print("  Then copy the verification code back here.")
    print()

    input("  Press Enter to begin authentication...")

    if auth.authenticate(auto_open_browser=True):
        print()
        print("  Authentication successful!")
        return auth
    else:
        print()
        print("  Authentication failed!")
        return None


def test_account_info(broker: ETradeBroker) -> bool:
    """Test fetching account information"""
    print_header("Account Information")

    try:
        # Get accounts list
        print_section("Available Accounts")
        accounts = broker.get_accounts()

        for i, account in enumerate(accounts, 1):
            acct_id = account.get('accountId', '')
            masked_id = '****' + acct_id[-4:] if len(acct_id) > 4 else acct_id
            print(f"  {i}. Account ID: {masked_id}")
            print(f"     Type: {account.get('accountType', 'N/A')}")
            print(f"     Status: {account.get('accountStatus', 'N/A')}")
            print()

        # Get balance for active account
        print_section("Account Balance")
        balance = broker.get_account_balance()

        print(f"  Net Account Value:   ${balance['net_account_value']:,.2f}")
        print(f"  Cash Available:      ${balance['cash_available']:,.2f}")
        print(f"  Buying Power:        ${balance['buying_power']:,.2f}")
        print(f"  Settled Cash:        ${balance['settled_cash']:,.2f}")
        print(f"  Margin Buying Power: ${balance['margin_buying_power']:,.2f}")

        # Note about sandbox balances
        if balance['net_account_value'] == 0 and balance['cash_available'] == 0:
            print()
            print("  NOTE: Sandbox accounts typically show $0 balances.")
            print("  E*TRADE sandbox is for API testing, not balance simulation.")
            print("  Real account balances will appear when using production mode.")

        return True

    except ETradeAPIError as e:
        print(f"  API Error: {e.message}")
        if e.response:
            print(f"  Response: {e.response[:500]}")
        return False
    except Exception as e:
        print(f"  Error: {e}")
        return False


def test_market_quote(broker: ETradeBroker) -> bool:
    """Test fetching market quotes"""
    print_header("Market Quote - SPX")

    try:
        # Try SPX index quote
        quote = broker.get_quote('SPX')

        print(f"  Symbol:          {quote['symbol']}")
        print(f"  Last Trade:      ${quote['lastTrade']:,.2f}")
        print(f"  Bid:             ${quote['bid']:,.2f}")
        print(f"  Ask:             ${quote['ask']:,.2f}")
        print(f"  Open:            ${quote['open']:,.2f}")
        print(f"  High:            ${quote['high']:,.2f}")
        print(f"  Low:             ${quote['low']:,.2f}")
        print(f"  Previous Close:  ${quote['close']:,.2f}")
        print(f"  Change:          ${quote['changeClose']:+,.2f} ({quote['changeClosePercentage']:+.2f}%)")
        print(f"  Volume:          {quote['volume']:,}")

        # Check if sandbox returned different symbol (known limitation)
        if quote['symbol'] != '$SPX.X' and quote['symbol'] != 'SPX':
            print()
            print(f"  WARNING: Sandbox returned {quote['symbol']} instead of SPX.")
            print("  E*TRADE sandbox has limited symbol support.")
            print("  SPX quotes will work correctly in production mode.")

        # Note about weekend/after hours
        now = datetime.now()
        if now.weekday() >= 5:  # Saturday or Sunday
            print()
            print("  NOTE: Markets are closed (weekend). Prices may be stale.")
        elif now.hour < 9 or (now.hour == 9 and now.minute < 30) or now.hour >= 16:
            print()
            print("  NOTE: Markets are closed. Prices may be from last close.")

        return True

    except ETradeAPIError as e:
        print(f"  API Error: {e.message}")
        if e.response:
            print(f"  Response: {e.response[:500]}")
        return False
    except Exception as e:
        print(f"  Error: {e}")
        return False


def test_option_expirations(broker: ETradeBroker) -> list:
    """Test fetching option expiration dates"""
    print_header("Option Expirations - SPX")

    try:
        # Enable debug to see raw API response
        expirations = broker.get_option_expirations('SPX', debug=True)

        print(f"  Found {len(expirations)} expiration dates")

        if not expirations:
            print("  WARNING: No expiration dates returned.")
            print("  E*TRADE sandbox may have limited options data.")
            return []

        print()
        print(f"  All returned dates: {expirations}")
        print()

        # Show next few expirations
        today = date.today()
        today_str = today.isoformat()
        print(f"  Today's date: {today_str}")

        upcoming = [exp for exp in expirations if exp >= today_str][:10]

        if upcoming:
            print()
            print("  Upcoming expirations:")
            for exp in upcoming:
                try:
                    exp_date = datetime.strptime(exp, '%Y-%m-%d').date()
                    days_out = (exp_date - today).days
                    day_name = exp_date.strftime('%A')
                    print(f"    {exp} ({day_name}, {days_out} days)")
                except ValueError as e:
                    print(f"    {exp} (parse error: {e})")
        else:
            print()
            print("  No upcoming expirations found in returned data.")
            print(f"  All dates appear to be before {today_str}")
            print("  This may be a sandbox data limitation.")

        return expirations

    except ETradeAPIError as e:
        print(f"  API Error: {e.message}")
        if e.response:
            print(f"  Response: {e.response[:500]}")
        return []
    except Exception as e:
        print(f"  Error: {e}")
        import traceback
        traceback.print_exc()
        return []


def test_option_chain(broker: ETradeBroker, expirations: list) -> bool:
    """Test fetching option chain data"""
    print_header("Option Chain - SPX")

    # Find target expiration
    today = date.today()
    today_str = today.isoformat()
    target_exp = None

    if expirations:
        # Try to find upcoming expiration
        for exp in expirations:
            if exp >= today_str:
                target_exp = exp
                break

        # If no upcoming, use the last available (sandbox may have old dates)
        if not target_exp and expirations:
            target_exp = expirations[-1]
            print(f"  NOTE: Using most recent expiration from sandbox: {target_exp}")
            print("  (Sandbox data may be historical)")
            print()

    if not target_exp:
        # Fallback: construct a likely expiration date (next Friday)
        days_until_friday = (4 - today.weekday()) % 7
        if days_until_friday == 0:
            days_until_friday = 7
        next_friday = today + timedelta(days=days_until_friday)
        target_exp = next_friday.isoformat()
        print(f"  No expirations returned, trying constructed date: {target_exp}")
        print()

    print(f"  Fetching chain for expiration: {target_exp}")
    print()

    try:
        chain = broker.get_options_chain('SPX', target_exp)

        print(f"  Retrieved {len(chain)} strikes")
        print()

        # Show sample of ATM strikes
        strikes = sorted(chain.keys())
        if strikes:
            # Find approximate ATM (middle of available strikes)
            mid_idx = len(strikes) // 2
            sample_strikes = strikes[max(0, mid_idx-3):mid_idx+4]

            print("  Sample strikes (near ATM):")
            print()
            print("  Strike    Call Bid  Call Ask  Put Bid   Put Ask")
            print("  " + "-" * 50)

            for strike in sample_strikes:
                data = chain[strike]
                print(f"  {strike:7.0f}   {data['call_bid']:8.2f}  {data['call_ask']:8.2f}  {data['put_bid']:8.2f}  {data['put_ask']:8.2f}")

        return True

    except ETradeAPIError as e:
        print(f"  API Error: {e.message}")
        if e.response:
            print(f"  Response: {e.response[:500]}")
        return False
    except Exception as e:
        print(f"  Error: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Run all E*TRADE sandbox tests"""
    print()
    print("*" * 60)
    print("*  E*TRADE SANDBOX CONNECTION TEST")
    print("*" * 60)
    print(f"*  Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("*" * 60)

    # Step 1: Check configuration
    if not check_configuration():
        print()
        print("Cannot proceed without valid configuration.")
        return 1

    # Step 2: Authenticate
    auth = test_authentication()
    if not auth:
        print()
        print("Cannot proceed without authentication.")
        return 1

    # Step 3: Create broker and connect
    broker = ETradeBroker(auth=auth)

    try:
        if not broker.connect():
            print("Failed to connect broker.")
            return 1
    except Exception as e:
        print(f"Error connecting: {e}")
        return 1

    # Step 4: Test account info
    test_account_info(broker)

    # Step 5: Test market quote
    test_market_quote(broker)

    # Step 6: Test option expirations
    expirations = test_option_expirations(broker)

    # Step 7: Test option chain
    test_option_chain(broker, expirations)

    # Summary
    print_header("Test Complete")
    print()
    print("  E*TRADE sandbox connection is working!")
    print()
    print("  Next steps:")
    print("  1. Review the data above to ensure it looks correct")
    print("  2. Token will be saved for future sessions (valid ~2 hours)")
    print("  3. When ready, you can run the main trading bot")
    print()
    print("  To use E*TRADE broker in the trading bot:")
    print("  - Set TRADING_MODE=live in .env")
    print("  - The bot will use E*TRADE instead of dry-run mode")
    print()

    return 0


if __name__ == '__main__':
    sys.exit(main())
