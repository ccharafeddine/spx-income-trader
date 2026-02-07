#!/usr/bin/env python3
"""
Run SPX Income Trader - Web Dashboard

Launches a read-only monitoring dashboard on localhost:5000.
The dashboard reads from the bot's existing data stores
(SQLite DB, signal log, trading log, Yahoo Finance API).

Usage:
    python scripts/run_dashboard.py
    python scripts/run_dashboard.py --port 8080
    python scripts/run_dashboard.py --host 127.0.0.1 --port 8080
"""

import sys
import os
import argparse
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Set environment to avoid E*TRADE validation on import
os.environ['TRADING_MODE'] = 'paper'

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='SPX Income Trader Dashboard')
    parser.add_argument('--port', type=int, default=None, help='Port to run on (default: 5000)')
    parser.add_argument('--host', type=str, default=None, help='Host to bind to (default: 127.0.0.1)')
    args = parser.parse_args()

    from config.settings import DASHBOARD_PORT, DASHBOARD_HOST
    host = args.host or DASHBOARD_HOST
    port = args.port or DASHBOARD_PORT

    print()
    print("=" * 60)
    print("  SPX INCOME TRADER - WEB DASHBOARD")
    print("=" * 60)
    print()
    print(f"  URL: http://{host}:{port}")
    if host == '0.0.0.0':
        print(f"  Local: http://127.0.0.1:{port}")
    print()
    print("  This is a read-only dashboard.")
    print("  It does NOT place trades or modify any data.")
    print()
    print("  Press Ctrl+C to stop")
    print()
    print("=" * 60)
    print()

    from dashboard.app import run
    run(host=host, port=port)
