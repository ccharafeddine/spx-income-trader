#!/usr/bin/env python3
"""
Run SPX Income Trading System in Paper Trading Mode

This simulates trades without executing real orders
"""

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import logging
import os

# Force paper trading mode
os.environ['TRADING_MODE'] = 'paper'

from src.main import main

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

if __name__ == "__main__":
    print("=" * 60)
    print("SPX Income Trading System - Paper Trading Mode")
    print("=" * 60)
    print("\nThis mode simulates trades without executing real orders.")
    print("Great for testing and learning the system!\n")
    
    sys.exit(main())