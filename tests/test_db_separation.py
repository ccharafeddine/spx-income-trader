"""
Tests for dry-run / live database separation.

Covers:
- get_database_path() returns mode-specific paths
- Trade isolation between dry-run and live databases
- Migration from shared trades.db to trades_dryrun.db
- Backtest data stored in dedicated backtest.db
- DatabaseManager.ensure_schema() creates valid databases
"""

import sys
import os
import sqlite3
import shutil
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db_dir(tmp_path):
    """Temporary database directory mimicking database/ layout."""
    db_dir = tmp_path / 'database'
    db_dir.mkdir()
    return db_dir


# ---------------------------------------------------------------------------
# get_database_path() routing
# ---------------------------------------------------------------------------

class TestGetDatabasePath:
    def test_dryrun_returns_dryrun_db(self):
        from config.settings import get_database_path
        path = get_database_path('dry-run')
        assert path.endswith('trades_dryrun.db')

    def test_live_returns_live_db(self):
        from config.settings import get_database_path
        path = get_database_path('live')
        assert path.endswith('trades_live.db')

    def test_default_uses_current_mode(self):
        from config.settings import get_database_path
        # Default mode is dry-run in test env
        path = get_database_path()
        assert 'trades_dryrun.db' in path or 'trades_live.db' in path

    def test_dryrun_and_live_are_different(self):
        from config.settings import get_database_path
        assert get_database_path('dry-run') != get_database_path('live')


# ---------------------------------------------------------------------------
# BACKTEST_DB_PATH
# ---------------------------------------------------------------------------

class TestBacktestDbPath:
    def test_backtest_db_path_is_separate(self):
        from config.settings import BACKTEST_DB_PATH, get_database_path
        assert BACKTEST_DB_PATH.endswith('backtest.db')
        assert BACKTEST_DB_PATH != get_database_path('dry-run')
        assert BACKTEST_DB_PATH != get_database_path('live')


# ---------------------------------------------------------------------------
# DatabaseManager.ensure_schema()
# ---------------------------------------------------------------------------

class TestEnsureSchema:
    def test_creates_db_with_tables(self, tmp_db_dir):
        from database.db_manager import DatabaseManager
        db_path = str(tmp_db_dir / 'test.db')
        DatabaseManager.ensure_schema(db_path)

        conn = sqlite3.connect(db_path)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()

        assert 'trades' in tables
        assert 'daily_stats' in tables
        assert 'system_events' in tables

    def test_idempotent_on_existing_db(self, tmp_db_dir):
        from database.db_manager import DatabaseManager
        db_path = str(tmp_db_dir / 'test.db')
        DatabaseManager.ensure_schema(db_path)
        # Second call should not raise
        DatabaseManager.ensure_schema(db_path)

    def test_creates_parent_directory(self, tmp_path):
        from database.db_manager import DatabaseManager
        db_path = str(tmp_path / 'nested' / 'dir' / 'test.db')
        DatabaseManager.ensure_schema(db_path)
        assert Path(db_path).exists()


# ---------------------------------------------------------------------------
# DB Isolation: writes to one mode's DB are invisible in the other
# ---------------------------------------------------------------------------

class TestDbIsolation:
    def test_trade_isolation(self, tmp_db_dir):
        from database.db_manager import DatabaseManager

        dryrun_path = str(tmp_db_dir / 'trades_dryrun.db')
        live_path = str(tmp_db_dir / 'trades_live.db')

        # Create both DBs
        DatabaseManager.ensure_schema(dryrun_path)
        DatabaseManager.ensure_schema(live_path)

        # Insert a trade into dry-run DB
        conn = sqlite3.connect(dryrun_path)
        conn.execute("""
            INSERT INTO trades (id, entry_time, direction, status,
                short_strike, long_strike, spread_width, credit_received,
                entry_price, quantity, expiration)
            VALUES ('dry-001', '2025-01-01 10:00:00', 'PUT', 'closed',
                5800, 5795, 5, 1.50, 1.50, 1, '2025-01-01')
        """)
        conn.commit()
        conn.close()

        # Verify it exists in dry-run DB
        conn = sqlite3.connect(dryrun_path)
        row = conn.execute("SELECT id FROM trades WHERE id = 'dry-001'").fetchone()
        conn.close()
        assert row is not None

        # Verify it does NOT exist in live DB
        conn = sqlite3.connect(live_path)
        row = conn.execute("SELECT id FROM trades WHERE id = 'dry-001'").fetchone()
        conn.close()
        assert row is None


# ---------------------------------------------------------------------------
# Migration: trades.db -> trades_dryrun.db
# ---------------------------------------------------------------------------

class TestMigration:
    def test_rename_trades_db_to_dryrun(self, tmp_db_dir):
        """Simulates the migration: old trades.db -> trades_dryrun.db."""
        old_path = tmp_db_dir / 'trades.db'

        # Create a fake trades.db with a trade in it
        conn = sqlite3.connect(str(old_path))
        conn.execute("CREATE TABLE trades (id TEXT PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO trades VALUES ('t1', 'old trade')")
        conn.commit()
        conn.close()

        dryrun_path = tmp_db_dir / 'trades_dryrun.db'
        assert not dryrun_path.exists()

        # Simulate migration logic
        if old_path.exists() and not dryrun_path.exists():
            shutil.copy2(str(old_path), str(dryrun_path))

        assert dryrun_path.exists()

        # Verify data carried over
        conn = sqlite3.connect(str(dryrun_path))
        row = conn.execute("SELECT note FROM trades WHERE id = 't1'").fetchone()
        conn.close()
        assert row[0] == 'old trade'

    def test_backtest_runs_extracted(self, tmp_db_dir):
        """Simulates extracting backtest_runs from old DB into backtest.db."""
        source_path = tmp_db_dir / 'trades_dryrun.db'
        backtest_path = tmp_db_dir / 'backtest.db'

        # Create source with backtest_runs table
        conn = sqlite3.connect(str(source_path))
        conn.execute("""
            CREATE TABLE backtest_runs (
                id TEXT PRIMARY KEY,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                params TEXT,
                results_summary TEXT,
                total_trades INTEGER DEFAULT 0,
                total_return_pct REAL DEFAULT 0,
                sharpe_ratio REAL DEFAULT 0,
                max_drawdown_pct REAL DEFAULT 0,
                win_rate REAL DEFAULT 0,
                full_results TEXT
            )
        """)
        conn.execute(
            "INSERT INTO backtest_runs (id, total_trades) VALUES ('bt-001', 42)"
        )
        conn.commit()
        conn.close()

        assert not backtest_path.exists()

        # Simulate extraction
        src_conn = sqlite3.connect(str(source_path))
        rows = src_conn.execute("SELECT * FROM backtest_runs").fetchall()
        cols = [d[0] for d in src_conn.execute("SELECT * FROM backtest_runs LIMIT 0").description]
        src_conn.close()

        dst_conn = sqlite3.connect(str(backtest_path))
        dst_conn.execute("""
            CREATE TABLE IF NOT EXISTS backtest_runs (
                id TEXT PRIMARY KEY,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                params TEXT,
                results_summary TEXT,
                total_trades INTEGER DEFAULT 0,
                total_return_pct REAL DEFAULT 0,
                sharpe_ratio REAL DEFAULT 0,
                max_drawdown_pct REAL DEFAULT 0,
                win_rate REAL DEFAULT 0,
                full_results TEXT
            )
        """)
        placeholders = ','.join(['?'] * len(cols))
        dst_conn.executemany(
            f"INSERT OR IGNORE INTO backtest_runs ({','.join(cols)}) VALUES ({placeholders})",
            rows,
        )
        dst_conn.commit()
        dst_conn.close()

        assert backtest_path.exists()

        # Verify data in backtest.db
        conn = sqlite3.connect(str(backtest_path))
        row = conn.execute(
            "SELECT total_trades FROM backtest_runs WHERE id = 'bt-001'"
        ).fetchone()
        conn.close()
        assert row[0] == 42

    def test_no_migration_if_dryrun_exists(self, tmp_db_dir):
        """If trades_dryrun.db already exists, migration should not overwrite it."""
        old_path = tmp_db_dir / 'trades.db'
        dryrun_path = tmp_db_dir / 'trades_dryrun.db'

        # Create both files
        conn = sqlite3.connect(str(old_path))
        conn.execute("CREATE TABLE trades (id TEXT)")
        conn.execute("INSERT INTO trades VALUES ('old')")
        conn.commit()
        conn.close()

        conn = sqlite3.connect(str(dryrun_path))
        conn.execute("CREATE TABLE trades (id TEXT)")
        conn.execute("INSERT INTO trades VALUES ('existing')")
        conn.commit()
        conn.close()

        # Migration should NOT overwrite
        if old_path.exists() and not dryrun_path.exists():
            shutil.copy2(str(old_path), str(dryrun_path))

        # Verify original dryrun data preserved
        conn = sqlite3.connect(str(dryrun_path))
        row = conn.execute("SELECT id FROM trades").fetchone()
        conn.close()
        assert row[0] == 'existing'
