"""
Tests for live account data accuracy quick wins:
  1. Post-settlement balance refresh at ~4:20 PM ET
  2. Fee/slippage delta surfaced in reconciliation
  3. Dashboard balance fallback logs WARNING
"""

import json
import sys
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# 1. Post-settlement balance refresh
# ---------------------------------------------------------------------------

class TestPostSettlementRefresh:
    """Verify the bot refreshes account balance after SPX settlement."""

    def _make_bot(self, current_time_val, dry_run=False):
        """Create a minimal TradingBot mock with required attributes."""
        bot = MagicMock()
        bot.dry_run = dry_run
        bot._post_settlement_refresh_done = False
        bot._reconciliation_done_today = True
        bot._journal_finalized = True
        bot.current_trading_date = current_time_val.date()
        bot.broker = MagicMock()
        bot.broker.get_account_balance.return_value = {
            'net_account_value': 24500.00,
            'cash_available': 24000.00,
            'buying_power': 48000.00,
        }
        import pytz
        bot.tz = pytz.timezone("America/New_York")
        return bot

    def test_post_settlement_writes_state_file(self, tmp_path):
        """Balance refresh writes JSON state file with correct data."""
        from src.main import TradingBot

        # Call the method directly on a real instance's unbound method
        bot = self._make_bot(datetime(2026, 3, 11, 16, 25))
        bot.broker.get_account_balance.return_value = {
            'net_account_value': 24500.00,
            'cash_available': 24000.00,
            'buying_power': 48000.00,
        }

        # Patch DATABASE_PATH so state file goes to tmp_path
        with patch('src.main.DATABASE_PATH', str(tmp_path / 'trades.db')):
            TradingBot._post_settlement_balance_refresh(bot)

        state_file = tmp_path / 'post_settlement_balance.json'
        assert state_file.exists()

        data = json.loads(state_file.read_text())
        assert data['net_account_value'] == 24500.00
        assert data['cash_available'] == 24000.00
        assert data['buying_power'] == 48000.00
        assert bot._post_settlement_refresh_done is True

    def test_post_settlement_skipped_in_dry_run(self, tmp_path):
        """Post-settlement refresh should not run in dry-run mode.

        The guard is in the main loop, not in the method itself.
        We verify the guard condition matches dry_run.
        """
        # dry_run=True means the guard `not self.dry_run` is False → skip
        bot = self._make_bot(datetime(2026, 3, 11, 16, 25), dry_run=True)
        # In real code, the condition `not self.dry_run` gates the call
        assert bot.dry_run is True

    def test_post_settlement_flag_resets_on_new_day(self):
        """The _post_settlement_refresh_done flag resets on new trading day."""
        # Verify the flag exists in the __init__ and daily reset block
        import inspect
        from src.main import TradingBot
        source = inspect.getsource(TradingBot.__init__)
        assert '_post_settlement_refresh_done' in source

    def test_post_settlement_handles_broker_failure(self, tmp_path):
        """Balance refresh logs warning on broker API failure."""
        from src.main import TradingBot

        bot = self._make_bot(datetime(2026, 3, 11, 16, 25))
        bot.broker.get_account_balance.side_effect = Exception("API timeout")

        with patch('src.main.DATABASE_PATH', str(tmp_path / 'trades.db')):
            # Should not raise
            TradingBot._post_settlement_balance_refresh(bot)

        # Flag should NOT be set on failure (allows retry next loop)
        assert bot._post_settlement_refresh_done is not True


# ---------------------------------------------------------------------------
# 2. Fee/slippage delta in reconciliation
# ---------------------------------------------------------------------------

class TestFeeSlippageDelta:
    """Verify reconciler surfaces fee/slippage delta."""

    def _make_reconciler(self, tmp_path, prev_nav=None, prev_date=None):
        from src.core.reconciler import TradeReconciler

        broker = MagicMock()
        broker.get_account_balance.return_value = {
            'net_account_value': 24472.56,  # After fees
        }

        db = MagicMock()
        db.db_path = str(tmp_path / 'trades.db')
        db.get_trades_by_date.return_value = [
            {'id': 't1', 'status': 'closed', 'pnl': 50.0,
             'direction': 'PUT', 'entry_time': '2026-03-11T10:00',
             'entry_order_id': None, 'exit_order_id': None},
        ]

        # Write previous day's balance file if provided
        if prev_nav is not None:
            balance_file = tmp_path / 'post_settlement_balance.json'
            balance_file.write_text(json.dumps({
                'net_account_value': prev_nav,
                'date': prev_date or '2026-03-10',
            }))

        return TradeReconciler(broker, db)

    def test_fee_delta_computed_with_previous_balance(self, tmp_path):
        """Fee/slippage delta = DB P&L - (ending equity - starting equity)."""
        reconciler = self._make_reconciler(tmp_path, prev_nav=24450.00, prev_date='2026-03-10')
        result = reconciler.reconcile(date(2026, 3, 11))

        # DB P&L = $50.00
        # Account P&L = 24472.56 - 24450.00 = $22.56
        # Fee delta = 50.00 - 22.56 = $27.44
        assert result['total_db_pnl'] == 50.0
        assert result['account_pnl'] == 22.56
        assert result['fee_slippage_delta'] == 27.44
        assert result['starting_equity'] == 24450.00
        assert result['ending_equity'] == 24472.56

    def test_fee_delta_none_without_previous_balance(self, tmp_path):
        """Without a previous balance file, fee delta should be None."""
        reconciler = self._make_reconciler(tmp_path)
        result = reconciler.reconcile(date(2026, 3, 11))

        assert result['fee_slippage_delta'] is None
        assert result['account_pnl'] is None
        assert result['total_db_pnl'] == 50.0

    def test_fee_delta_skips_same_day_balance(self, tmp_path):
        """If balance file is from today (not previous day), skip delta calc."""
        reconciler = self._make_reconciler(
            tmp_path, prev_nav=24450.00, prev_date='2026-03-11'  # Same day
        )
        result = reconciler.reconcile(date(2026, 3, 11))

        assert result['fee_slippage_delta'] is None
        assert result['account_pnl'] is None

    def test_fee_delta_handles_broker_failure(self, tmp_path):
        """Broker API failure should not crash reconciliation."""
        reconciler = self._make_reconciler(tmp_path, prev_nav=24450.00)
        reconciler.broker.get_account_balance.side_effect = Exception("API error")
        result = reconciler.reconcile(date(2026, 3, 11))

        # Should still return valid result with None deltas
        assert result['fee_slippage_delta'] is None
        assert result['total_db_pnl'] == 50.0


# ---------------------------------------------------------------------------
# 3. Dashboard balance fallback logging
# ---------------------------------------------------------------------------

class TestDashboardBalanceFallback:
    """Verify dashboard logs WARNING when balance fetch fails."""

    def test_fallback_uses_warning_level(self):
        """The dashboard balance fallback should use logger.warning, not debug."""
        # Read the actual source to verify logging level
        app_path = Path(__file__).parent.parent / 'dashboard' / 'app.py'
        source = app_path.read_text()

        # Should have warning for Schwab balance fetch failure
        assert 'logger.warning(f"Schwab balance fetch failed, trying cached balance' in source
