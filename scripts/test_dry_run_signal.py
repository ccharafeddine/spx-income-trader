#!/usr/bin/env python3
"""
Test Dry Run Signal Detection

Simulates what happens when a pulse bar is detected and a trade
signal is generated in dry-run mode.

This is useful to verify the signal logging before running during
market hours.
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.brokers.dry_run_broker import DryRunBroker
from src.models.spread import CreditSpread, OptionLeg, TradeDirection


def main():
    print()
    print("=" * 60)
    print("  DRY RUN SIGNAL TEST")
    print("=" * 60)
    print()

    # Create dry run broker
    broker = DryRunBroker(initial_balance=50000.0)

    # Get current SPX price
    spx_price = broker.get_current_price('SPX')
    print(f"Current SPX Price: ${spx_price:,.2f}")
    print()

    # Simulate a bullish pulse bar detected
    # This would construct a put credit spread
    print("Simulating: BULLISH pulse bar detected at 10:30 AM")
    print("Strategy: Sell put credit spread below current price")
    print()

    # Get options chain for tomorrow (simulating 0DTE)
    tomorrow = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d')
    chain = broker.get_options_chain('SPX', tomorrow)

    # Find ATM strike
    atm_strike = round(spx_price / 5) * 5

    # Construct put credit spread (short strike below ATM)
    short_strike = atm_strike - 5
    long_strike = short_strike - 5  # $5 wide spread

    short_leg = OptionLeg(
        strike=short_strike,
        option_type='put',
        action='sell',
        price=chain[short_strike]['put_bid'],
        quantity=1
    )

    long_leg = OptionLeg(
        strike=long_strike,
        option_type='put',
        action='buy',
        price=chain[long_strike]['put_ask'],
        quantity=1
    )

    credit = short_leg.price - long_leg.price

    spread = CreditSpread(
        direction=TradeDirection.BULLISH,
        short_leg=short_leg,
        long_leg=long_leg,
        credit_received=credit,
        entry_time=datetime.now(),
        expiration=datetime.strptime(tomorrow, '%Y-%m-%d'),
        underlying_price_at_entry=spx_price
    )

    print(f"Constructed Spread:")
    print(f"  Direction:    BULLISH (Put Credit Spread)")
    print(f"  Short Put:    ${short_strike} @ ${short_leg.price:.2f}")
    print(f"  Long Put:     ${long_strike} @ ${long_leg.price:.2f}")
    print(f"  Net Credit:   ${credit:.2f} per spread")
    print(f"  Max Profit:   ${spread.max_profit:.2f}")
    print(f"  Max Risk:     ${spread.max_risk:.2f}")
    print()

    # Place the simulated order (this will log but not execute)
    print("Placing order in DRY RUN mode...")
    print()

    order_id = broker.place_spread_order(spread, quantity=5)

    print()
    print(f"Order ID: {order_id}")

    # Check order status
    status = broker.get_order_status(order_id)
    print(f"Order Status: {status['status']}")
    print(f"Fill Price: ${status['fill_price']:.2f}")

    # Show account balance
    balance = broker.get_account_balance()
    print()
    print("Account Summary:")
    print(f"  Balance:          ${balance['net_account_value']:,.2f}")
    print(f"  Hypothetical P&L: ${balance['hypothetical_pnl']:+,.2f}")
    print(f"  Open Positions:   {balance['open_positions']}")

    # Show where signals are logged
    print()
    print(f"Signal log: {broker.log_file}")
    print()
    print("View signals with:")
    print(f"  type {broker.log_file}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
