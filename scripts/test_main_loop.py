"""
Quick diagnostic to test the main loop components
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

from datetime import datetime
import pytz

print("=" * 60)
print("DIAGNOSTIC: Testing main loop components")
print("=" * 60)

# Test 1: Timezone and market hours
print("\n[1] Testing timezone and market hours...")
tz = pytz.timezone("America/New_York")
now = datetime.now(tz)
print(f"  Current time (ET): {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
print(f"  Weekday: {now.strftime('%A')} ({now.weekday()})")
print(f"  Is weekday: {now.weekday() < 5}")

from datetime import time
market_open = time(9, 30)
market_close = time(16, 0)
is_market_hours = market_open <= now.time() < market_close
print(f"  Market hours (9:30-16:00): {is_market_hours}")
print(f"  Market is open: {now.weekday() < 5 and is_market_hours}")

# Test 2: Yahoo Finance
print("\n[2] Testing Yahoo Finance data provider...")
try:
    from src.data.yahoo_finance import YahooFinanceProvider
    provider = YahooFinanceProvider()

    quote = provider.get_spx_quote()
    if quote:
        print(f"  SPX Price: ${quote['price']:,.2f}")
        print(f"  Change: ${quote['change']:+,.2f} ({quote['change_pct']:+.2f}%)")
    else:
        print("  ERROR: Failed to get SPX quote")
except Exception as e:
    print(f"  ERROR: {e}")

# Test 3: DryRunBroker
print("\n[3] Testing DryRunBroker...")
try:
    from src.brokers.dry_run_broker import DryRunBroker
    broker = DryRunBroker(initial_balance=50000.0)

    price = broker.get_current_price("SPX")
    print(f"  SPX via broker: ${price:,.2f}")

    # Test options chain - using correct format
    expiration = datetime.now(tz).strftime("%Y-%m-%d")
    print(f"  Expiration format: {expiration}")
    chain = broker.get_options_chain("SPX", expiration)
    print(f"  Options chain strikes: {len(chain)}")
except Exception as e:
    print(f"  ERROR: {e}")
    import traceback
    traceback.print_exc()

# Test 4: Bar Builder
print("\n[4] Testing BarBuilder...")
try:
    from src.core.bar_builder import BarBuilder
    bb = BarBuilder(interval_minutes=30)

    # Add a price
    bar = bb.add_price(now, 5900.0)
    print(f"  Added price, bar returned: {bar}")
    print(f"  Current bar start: {bb.current_bar_start}")
    print(f"  Current bar end: {bb.current_bar_end}")
    print(f"  Ticks in current bar: {bb.tick_count}")
except Exception as e:
    print(f"  ERROR: {e}")

# Test 5: Strategy
print("\n[5] Testing Strategy...")
try:
    from src.core.strategy import SPXIncomeStrategy
    strategy = SPXIncomeStrategy()
    print(f"  Strategy initialized OK")
except Exception as e:
    print(f"  ERROR: {e}")

print("\n" + "=" * 60)
print("DIAGNOSTIC COMPLETE")
print("=" * 60)
