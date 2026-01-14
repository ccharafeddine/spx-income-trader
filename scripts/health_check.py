#!/usr/bin/env python3
"""
Health Check Script

Verifies all system components are working
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import ETRADE_CONFIG, DATABASE_PATH, TRADING_MODE
from database.db_manager import DatabaseManager
from src.brokers.paper_trader import PaperBroker
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def check_configuration():
    """Check configuration is valid"""
    print("\n🔧 Checking Configuration...")
    
    issues = []
    
    if TRADING_MODE == 'live':
        if not ETRADE_CONFIG['consumer_key']:
            issues.append("❌ ETRADE_CONSUMER_KEY not set")
        else:
            print("✅ ETRADE_CONSUMER_KEY configured")
        
        if not ETRADE_CONFIG['consumer_secret']:
            issues.append("❌ ETRADE_CONSUMER_SECRET not set")
        else:
            print("✅ ETRADE_CONSUMER_SECRET configured")
        
        if not ETRADE_CONFIG['account_id']:
            issues.append("❌ ETRADE_ACCOUNT_ID not set")
        else:
            print("✅ ETRADE_ACCOUNT_ID configured")
    else:
        print(f"✅ Running in {TRADING_MODE} mode")
    
    return issues


def check_database():
    """Check database is accessible"""
    print("\n💾 Checking Database...")
    
    try:
        db = DatabaseManager(DATABASE_PATH)
        print(f"✅ Database accessible at {DATABASE_PATH}")
        
        # Try a simple query
        stats = db.get_performance_summary(days=1)
        print("✅ Database queries working")
        
        return []
    except Exception as e:
        return [f"❌ Database error: {e}"]


def check_broker():
    """Check broker connection"""
    print("\n🏦 Checking Broker...")
    
    try:
        if TRADING_MODE == 'paper':
            broker = PaperBroker()
            price = broker.get_current_price("SPX")
            print(f"✅ Paper broker working (SPX: ${price:.2f})")
        else:
            print("⚠️  Live broker check skipped (not implemented)")
        
        return []
    except Exception as e:
        return [f"❌ Broker error: {e}"]


def check_directories():
    """Check required directories exist"""
    print("\n📁 Checking Directories...")
    
    issues = []
    required_dirs = ['logs', 'tokens', 'database']
    
    for dir_name in required_dirs:
        dir_path = Path(dir_name)
        if dir_path.exists():
            print(f"✅ {dir_name}/ exists")
        else:
            issues.append(f"❌ {dir_name}/ missing")
    
    return issues


def main():
    print("=" * 60)
    print("SPX Income Trading System - Health Check")
    print("=" * 60)
    
    all_issues = []
    
    all_issues.extend(check_configuration())
    all_issues.extend(check_database())
    all_issues.extend(check_broker())
    all_issues.extend(check_directories())
    
    print("\n" + "=" * 60)
    
    if all_issues:
        print("❌ HEALTH CHECK FAILED")
        print("\nIssues found:")
        for issue in all_issues:
            print(f"  {issue}")
        return 1
    else:
        print("✅ ALL CHECKS PASSED")
        print("\nSystem is ready to trade!")
        return 0


if __name__ == "__main__":
    sys.exit(main())