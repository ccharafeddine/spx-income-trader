#!/usr/bin/env python3
"""
Run SPX Income Trader in Dry Run Mode

This script runs the trading bot with:
- Real market data from Yahoo Finance
- NO actual order execution
- Full signal logging to logs/dry_run_signals.json

Use this to validate strategy logic during market hours
before going live.

Usage:
    python scripts/run_dry_run.py              # Interactive mode
    python scripts/run_dry_run.py --no-confirm # Unattended mode (auto-accept signals)
"""

import sys
import os
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Set environment
os.environ['TRADING_MODE'] = 'paper'

if __name__ == "__main__":
    # Import and modify sys.argv to add --dry-run
    original_argv = sys.argv.copy()

    # Build new argv with --dry-run
    sys.argv = [original_argv[0], '--dry-run']

    # Pass through any additional arguments (like --no-confirm)
    for arg in original_argv[1:]:
        sys.argv.append(arg)

    print()
    print("=" * 60)
    print("  SPX INCOME TRADER - DRY RUN MODE")
    print("=" * 60)
    print()
    print("  This mode uses REAL market data but does NOT place orders.")
    print("  All trading signals will be logged to:")
    print(f"    {project_root / 'logs' / 'dry_run_signals.json'}")
    print()
    print("  Use --no-confirm to auto-accept all signals (unattended mode)")
    print()
    print("  Press Ctrl+C to stop")
    print()
    print("=" * 60)
    print()

    # Run main
    from src.main import main
    sys.exit(main())
