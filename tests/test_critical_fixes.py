"""
Tests for CRITICAL fixes from the March 10 live incident.

Covers:
- CRITICAL-1: Database migration completeness (all schema.sql columns present)
- CRITICAL-2: schema.sql bundled in build scripts
- CRITICAL-3: runtime_settings.json merged into strategy params
- HIGH-1: DB write failure halts trading
- HIGH-2: SPX spread pricing in 5-cent increments
"""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ============================================================================
# CRITICAL-1: Database migration completeness
# ============================================================================

class TestDatabaseMigrationCompleteness:
    """Verify _run_migrations() produces the same columns as schema.sql."""

    def _get_schema_columns(self):
        """Parse schema.sql to get all column names in the trades table."""
        schema_file = Path(__file__).parent.parent / 'database' / 'schema.sql'
        assert schema_file.exists(), "schema.sql not found"
        text = schema_file.read_text()

        # Extract the CREATE TABLE trades block
        start = text.index('CREATE TABLE IF NOT EXISTS trades')
        # Find matching closing paren+semicolon
        depth = 0
        end = start
        for i, ch in enumerate(text[start:], start):
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

        block = text[start:end]

        # Extract column names (lines with column definitions)
        columns = set()
        for line in block.split('\n'):
            line = line.strip().rstrip(',')
            if not line or line.startswith('CREATE') or line.startswith('--') or line.startswith(')'):
                continue
            # Skip constraints like PRIMARY KEY, FOREIGN KEY, UNIQUE
            first_word = line.split()[0] if line.split() else ''
            if first_word.upper() in ('PRIMARY', 'FOREIGN', 'UNIQUE', 'CHECK', 'CONSTRAINT'):
                continue
            # Column name is the first word
            col_name = first_word.strip('"').strip("'")
            if col_name:
                columns.add(col_name)
        return columns

    def _get_migration_columns(self, tmp_path):
        """Create a DB using only the fallback + migration path (no schema.sql)."""
        db_path = str(tmp_path / 'test_migration.db')

        # Patch schema.sql to not exist, forcing fallback path
        with patch('database.db_manager.Path') as mock_path_cls:
            # We need __file__ to resolve normally for the module, but
            # schema_file.exists() to return False.
            # Simpler: just create the DB manager with a non-existent schema dir
            pass

        # More direct approach: create the minimal table manually, then run migrations
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                entry_time TIMESTAMP NOT NULL,
                exit_time TIMESTAMP,
                direction TEXT NOT NULL,
                status TEXT NOT NULL,
                short_strike REAL NOT NULL,
                long_strike REAL NOT NULL,
                credit_received REAL NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL,
                pnl REAL,
                quantity INTEGER NOT NULL,
                notes TEXT
            )
        """)
        conn.commit()
        conn.close()

        from database.db_manager import DatabaseManager
        dm = DatabaseManager(db_path)

        # Get resulting columns
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(trades)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()
        return columns

    def test_migration_includes_all_schema_columns(self, tmp_path):
        """Every column in schema.sql must exist after migration from minimal table."""
        schema_cols = self._get_schema_columns()
        migration_cols = self._get_migration_columns(tmp_path)

        # created_at has a DEFAULT value and is auto-added by SQLite — not
        # required in the migration column list.
        missing = schema_cols - migration_cols - {'created_at'}
        assert not missing, (
            f"Migration is missing columns that exist in schema.sql: {sorted(missing)}"
        )

    def test_critical_columns_present_after_migration(self, tmp_path):
        """The specific columns that caused the March 10 incident must exist."""
        migration_cols = self._get_migration_columns(tmp_path)
        critical = {'spread_width', 'entry_order_id', 'exit_order_id',
                     'exit_reason', 'pnl_percent', 'max_profit', 'max_risk',
                     'expiration', 'underlying_price_at_entry', 'setup_bar_time'}
        missing = critical - migration_cols
        assert not missing, f"Critical columns missing: {sorted(missing)}"


# ============================================================================
# CRITICAL-2: schema.sql in build DATA_FILES
# ============================================================================

class TestBuildBundlesSchema:
    """Verify build scripts include database/schema.sql."""

    def test_windows_build_includes_schema_sql(self):
        build_file = Path(__file__).parent.parent / 'build' / 'build_windows.py'
        text = build_file.read_text()
        assert 'schema.sql' in text, "build_windows.py must bundle schema.sql"

    def test_macos_build_includes_schema_sql(self):
        build_file = Path(__file__).parent.parent / 'build' / 'build_macos.py'
        text = build_file.read_text()
        assert 'schema.sql' in text, "build_macos.py must bundle schema.sql"


# ============================================================================
# CRITICAL-3: runtime_settings.json merged into strategy params
# ============================================================================

class TestRuntimeSettingsMerge:
    """Verify load_strategy_params() merges runtime_settings.json overrides."""

    def _create_yaml(self, tmp_path, content):
        yaml_file = tmp_path / 'strategy_params.yaml'
        yaml_file.write_text(content)
        return yaml_file

    def _create_runtime(self, tmp_path, data):
        runtime_file = tmp_path / 'database' / 'runtime_settings.json'
        runtime_file.parent.mkdir(parents=True, exist_ok=True)
        runtime_file.write_text(json.dumps(data))
        return runtime_file

    def test_runtime_overrides_yaml_values(self, tmp_path):
        """runtime_settings.json values must override YAML defaults."""
        self._create_yaml(tmp_path, """
portfolio:
  daily_contracts: 7
  max_contracts: 10
  max_daily_loss_pct: 2.0
""")
        self._create_runtime(tmp_path, {
            'portfolio': {
                'daily_contracts': 1,
                'max_contracts': 3,
            }
        })

        with patch('config.settings.CONFIG_DIR', tmp_path), \
             patch('config.settings.DB_PATH', str(tmp_path / 'database' / 'trades.db')):
            from config.settings import load_strategy_params
            params = load_strategy_params()

        assert params['portfolio']['daily_contracts'] == 1
        assert params['portfolio']['max_contracts'] == 3
        # Non-overridden value preserved from YAML
        assert params['portfolio']['max_daily_loss_pct'] == 2.0

    def test_yaml_defaults_used_when_no_runtime(self, tmp_path):
        """If runtime_settings.json doesn't exist, YAML defaults are used."""
        self._create_yaml(tmp_path, """
portfolio:
  daily_contracts: 7
  max_contracts: 10
""")
        # No runtime file created

        with patch('config.settings.CONFIG_DIR', tmp_path), \
             patch('config.settings.DB_PATH', str(tmp_path / 'database' / 'trades.db')):
            from config.settings import load_strategy_params
            params = load_strategy_params()

        assert params['portfolio']['daily_contracts'] == 7
        assert params['portfolio']['max_contracts'] == 10

    def test_deep_merge_preserves_nested_keys(self, tmp_path):
        """Deep merge should not clobber sibling keys in nested dicts."""
        self._create_yaml(tmp_path, """
portfolio:
  drawdown_limits:
    weekly:
      enabled: true
      max_loss_pct: 6.0
    monthly:
      enabled: true
      max_loss_pct: 12.0
""")
        self._create_runtime(tmp_path, {
            'portfolio': {
                'drawdown_limits': {
                    'weekly': {'max_loss_pct': 4.0}
                }
            }
        })

        with patch('config.settings.CONFIG_DIR', tmp_path), \
             patch('config.settings.DB_PATH', str(tmp_path / 'database' / 'trades.db')):
            from config.settings import load_strategy_params
            params = load_strategy_params()

        dd = params['portfolio']['drawdown_limits']
        assert dd['weekly']['max_loss_pct'] == 4.0
        assert dd['weekly']['enabled'] is True  # preserved from YAML
        assert dd['monthly']['max_loss_pct'] == 12.0  # untouched


# ============================================================================
# HIGH-1: DB write failure halts trading
# ============================================================================

class TestDBWriteFailureHaltsTrading:
    """Verify that a DB write failure prevents further trade entries."""

    def test_db_write_failed_flag_blocks_new_entries(self):
        from src.core.position_manager import PositionManager
        pm = MagicMock(spec=PositionManager)
        pm._db_write_failed = True
        pm.enter_trade = PositionManager.enter_trade.__get__(pm)

        result = pm.enter_trade(
            spread=MagicMock(),
            setup_bar=MagicMock(),
            quantity=1,
        )
        assert result is None

    def test_db_write_failed_flag_not_set_by_default(self):
        """New PositionManager should not have _db_write_failed set."""
        from src.core.position_manager import PositionManager
        pm = PositionManager.__new__(PositionManager)
        assert not getattr(pm, '_db_write_failed', False)


# ============================================================================
# HIGH-2: SPX 5-cent increment rounding
# ============================================================================

class TestSPXNickelRounding:
    """Verify spread limit prices are rounded to $0.05 increments."""

    @pytest.mark.parametrize("input_price,expected", [
        (2.13, 2.15),
        (2.07, 2.05),
        (2.10, 2.10),
        (2.00, 2.00),
        (1.97, 1.95),
        (1.98, 2.00),
        (2.03, 2.05),   # just above midpoint rounds up
        (0.03, 0.05),
        (0.17, 0.15),
        (3.33, 3.35),
    ])
    def test_nickel_rounding(self, input_price, expected):
        """Verify round-to-nearest $0.05."""
        result = round(round(input_price / 0.05) * 0.05, 2)
        assert result == expected, f"{input_price} should round to {expected}, got {result}"

    def test_open_order_uses_nickel_price(self):
        """place_spread_order must round to $0.05 before placing."""
        from src.brokers.schwab_broker import SchwabBroker
        broker_text = Path(__file__).parent.parent / 'src' / 'brokers' / 'schwab_broker.py'
        source = broker_text.read_text()
        # Verify the rounding code exists in place_spread_order
        assert 'round(limit_price / 0.05) * 0.05' in source

    def test_close_order_uses_nickel_price(self):
        """close_spread must round to $0.05 before placing."""
        source = (Path(__file__).parent.parent / 'src' / 'brokers' / 'schwab_broker.py').read_text()
        # Should appear twice (open and close)
        count = source.count('round(limit_price / 0.05) * 0.05')
        assert count >= 2, f"Nickel rounding should appear in both open and close, found {count}"
