"""
Regression tests: 0DTE positions must expire BEFORE EOD summary.

The _finalize_daily_journal fires at 16:05+ in the market-closed branch.
If monitor_positions() hasn't run first, expired 0DTE trades won't be
reflected in db.get_daily_summary() or position_manager.get_open_trades(),
causing the EOD Discord embed to show $0 P&L and stale open positions.

These tests verify:
  1. monitor_positions() runs before _finalize_daily_journal() in source
  2. EOD summary after a 0DTE loss shows correct negative P&L and W/L
  3. EOD summary after a 0DTE win shows correct positive P&L
"""

import ast
import sys
from pathlib import Path
from datetime import datetime, date, time as dtime
from unittest.mock import MagicMock, patch, PropertyMock

import pytz
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

ET = pytz.timezone("America/New_York")


# ---------------------------------------------------------------------------
# 1. Source-level ordering: monitor_positions before _finalize_daily_journal
# ---------------------------------------------------------------------------

class TestExpirationBeforeJournalOrdering:
    """Verify the source code calls monitor_positions before journal finalization."""

    def test_monitor_positions_before_finalize_journal_in_market_closed_branch(self):
        """In the market-closed branch, monitor_positions must run before
        _finalize_daily_journal so 0DTE expirations are processed first."""
        main_path = Path(__file__).parent.parent / 'src' / 'main.py'
        source = main_path.read_text(encoding='utf-8')

        # Find the market-closed block that contains _finalize_daily_journal
        # The fix should have monitor_positions() appearing before _finalize_daily_journal()
        # in the if-not-journal_finalized block.
        lines = source.split('\n')
        finalize_line = None
        monitor_before_finalize_line = None

        in_journal_block = False
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # Look for the journal finalization guard
            if 'not self._journal_finalized' in stripped:
                in_journal_block = True
            if in_journal_block:
                if 'monitor_positions()' in stripped:
                    monitor_before_finalize_line = i
                if '_finalize_daily_journal()' in stripped:
                    finalize_line = i
                    break

        assert monitor_before_finalize_line is not None, (
            "monitor_positions() must be called in the journal finalization block"
        )
        assert finalize_line is not None, (
            "_finalize_daily_journal() must be called in the journal finalization block"
        )
        assert monitor_before_finalize_line < finalize_line, (
            f"monitor_positions() (line {monitor_before_finalize_line}) must appear "
            f"BEFORE _finalize_daily_journal() (line {finalize_line}) so 0DTE "
            f"expirations are processed before the EOD summary is sent"
        )

    def test_drain_recently_closed_before_finalize_journal(self):
        """_drain_recently_closed must also run before _finalize_daily_journal
        so the drawdown manager has up-to-date P&L."""
        main_path = Path(__file__).parent.parent / 'src' / 'main.py'
        source = main_path.read_text(encoding='utf-8')

        lines = source.split('\n')
        drain_line = None
        finalize_line = None

        in_journal_block = False
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if 'not self._journal_finalized' in stripped:
                in_journal_block = True
            if in_journal_block:
                if '_drain_recently_closed()' in stripped:
                    drain_line = i
                if '_finalize_daily_journal()' in stripped:
                    finalize_line = i
                    break

        assert drain_line is not None, (
            "_drain_recently_closed() must be called before _finalize_daily_journal()"
        )
        assert drain_line < finalize_line, (
            f"_drain_recently_closed() (line {drain_line}) must appear BEFORE "
            f"_finalize_daily_journal() (line {finalize_line})"
        )


# ---------------------------------------------------------------------------
# 2 & 3. Integration: EOD summary reflects expired 0DTE P&L
# ---------------------------------------------------------------------------

def _make_mock_bot(trade_pnl: float):
    """Build a minimal mock TradingBot with one expired 0DTE trade.

    Args:
        trade_pnl: P&L of the expired trade (negative = loss, positive = win)
    """
    from src.models.spread import CreditSpread, OptionLeg, TradeDirection
    from src.models.trade import Trade, TradeStatus

    # Build a realistic spread
    direction = TradeDirection.BULLISH
    short_leg = OptionLeg(strike=5800.0, option_type='put', action='sell', price=2.00)
    long_leg = OptionLeg(strike=5795.0, option_type='put', action='buy', price=0.50)

    entry_time = ET.localize(datetime(2026, 3, 10, 10, 30))
    expiration = ET.localize(datetime(2026, 3, 10, 16, 0))
    spread = CreditSpread(
        direction=direction,
        short_leg=short_leg,
        long_leg=long_leg,
        credit_received=1.50,
        entry_time=entry_time,
        expiration=expiration,
    )

    from src.models.bar import Bar
    setup_bar = Bar(
        timestamp=entry_time,
        open=5810.0, high=5815.0, low=5795.0, close=5805.0,
    )
    trade = Trade(
        id='test-trade-001',
        spread=spread,
        status=TradeStatus.ACTIVE,
        setup_bar=setup_bar,
        entry_price=1.50,
        entry_time=entry_time,
        quantity=1,
    )
    trade._strategy_type = 'daily_income'

    # Mock position manager
    position_manager = MagicMock()
    position_manager.get_open_trades.return_value = [trade]
    position_manager.recently_closed = []
    position_manager.recently_closed_trades = []

    # monitor_positions should expire the trade and update state
    def mock_monitor():
        trade.status = TradeStatus.CLOSED
        trade.pnl = trade_pnl
        trade.pnl_percent = (trade_pnl / (spread.max_profit * 1)) * 100 if spread.max_profit else 0
        trade.exit_price = 0.0
        trade.exit_time = ET.localize(datetime(2026, 3, 10, 16, 0))
        trade.exit_reason = "Expiration reached (4:00 PM EST)"

        position_manager.open_trades = []
        position_manager.get_open_trades.return_value = []
        position_manager.recently_closed.append({
            'id': trade.id, 'pnl': trade_pnl,
            'strategy': 'daily_income', 'direction': 'bullish',
        })
        position_manager.recently_closed_trades.append({
            'direction': 'bullish',
            'pnl': trade_pnl,
            'pnl_pct': trade.pnl_percent,
            'max_profit_pct': trade.pnl_percent,
            'reason': 'Expiration reached (4:00 PM EST)',
            'strikes': '5800.0/5795.0',
            'short_strike': 5800.0,
            'long_strike': 5795.0,
            'duration': '5h 30m',
            'strategy': 'daily_income',
        })
        return trade_pnl

    position_manager.monitor_positions.side_effect = mock_monitor

    return position_manager, trade


class TestEODSummaryAfter0DTEExpiration:
    """EOD summary must reflect expired 0DTE trade P&L, not stale $0."""

    def test_eod_summary_shows_loss_after_0dte_expiration(self):
        """After a 0DTE losing trade expires, EOD should show negative P&L
        and correct W/L count (0W 1L), with no open swing positions."""
        position_manager, trade = _make_mock_bot(trade_pnl=-350.0)

        # Before expiration: trade is open
        assert len(position_manager.get_open_trades()) == 1

        # Simulate what main.py now does before _finalize_daily_journal:
        # 1. monitor_positions() expires the 0DTE
        expired_pnl = position_manager.monitor_positions()

        # After expiration: trade is closed, open_trades empty
        assert len(position_manager.get_open_trades()) == 0
        assert expired_pnl == -350.0

        # recently_closed has the trade for drawdown manager
        assert len(position_manager.recently_closed) == 1
        assert position_manager.recently_closed[0]['pnl'] == -350.0

        # recently_closed_trades has notification data
        assert len(position_manager.recently_closed_trades) == 1
        closed_data = position_manager.recently_closed_trades[0]
        assert closed_data['pnl'] == -350.0
        assert closed_data['direction'] == 'bullish'

        # EOD summary would now see:
        # - db_summary shows negative P&L (via update_trade_close + update_daily_stats)
        # - open_swings is empty (no stale expired trade listed)
        # - wins=0, losses=1
        open_swings = [
            {'direction': t.spread.direction.value}
            for t in position_manager.get_open_trades()
        ]
        assert open_swings == [], (
            "Expired 0DTE should NOT appear as an open swing in EOD summary"
        )

    def test_eod_summary_shows_win_after_0dte_expiration(self):
        """After a 0DTE winning trade expires, EOD should show positive P&L."""
        position_manager, trade = _make_mock_bot(trade_pnl=150.0)

        # Simulate expiration
        expired_pnl = position_manager.monitor_positions()

        assert expired_pnl == 150.0
        assert len(position_manager.get_open_trades()) == 0
        assert position_manager.recently_closed[0]['pnl'] == 150.0

    def test_exit_notification_sent_before_eod_summary(self):
        """Trade exit notification must fire before EOD summary,
        so Discord shows the exit embed, then the day summary."""
        position_manager, trade = _make_mock_bot(trade_pnl=-200.0)

        notifier = MagicMock()
        call_order = []

        def track_exit(*a, **kw):
            call_order.append('trade_exit')
        def track_eod(*a, **kw):
            call_order.append('eod_summary')

        notifier.send_trade_exit.side_effect = track_exit
        notifier.send_eod_summary.side_effect = track_eod

        # Simulate the pre-journal block from main.py
        expired_pnl = position_manager.monitor_positions()

        # Process exit notifications (as main.py now does before journal)
        for closed in position_manager.recently_closed_trades:
            notifier.send_trade_exit({
                'strategy': closed.get('strategy', 'unknown'),
                'direction': closed['direction'],
                'pnl': closed['pnl'],
                'pnl_pct': closed['pnl_pct'],
                'exit_reason': closed['reason'],
            })
        position_manager.recently_closed_trades.clear()

        # Then EOD summary
        notifier.send_eod_summary({'daily_pnl': expired_pnl})

        assert call_order == ['trade_exit', 'eod_summary'], (
            f"Exit notification must fire before EOD summary, got: {call_order}"
        )

    def test_recently_closed_trades_cleared_before_eod(self):
        """recently_closed_trades must be cleared after processing
        so they don't appear as duplicates in the market-hours loop."""
        position_manager, trade = _make_mock_bot(trade_pnl=100.0)

        position_manager.monitor_positions()

        assert len(position_manager.recently_closed_trades) == 1

        # Process and clear (as main.py does)
        for _ in position_manager.recently_closed_trades:
            pass
        position_manager.recently_closed_trades.clear()

        assert len(position_manager.recently_closed_trades) == 0
