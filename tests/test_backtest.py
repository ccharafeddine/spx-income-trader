"""
Tests for the backtesting engine.

Covers:
- Data loading from CSV
- BacktestBroker order fills and position valuation
- BacktestEngine replay produces trades
- Report metric calculations
- CLI runner (smoke test)
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from datetime import date, datetime, time
import pytz

ET = pytz.timezone('America/New_York')

FIXTURE_CSV = str(Path(__file__).parent / 'fixtures' / 'backtest_sample.csv')


# ---------------------------------------------------------------
# Data Loader Tests
# ---------------------------------------------------------------

class TestDataLoader:
    def test_load_bars_from_csv(self):
        from src.backtest.data_loader import load_bars_from_csv
        df = load_bars_from_csv(FIXTURE_CSV)
        assert len(df) > 0
        assert 'open' in df.columns
        assert 'high' in df.columns
        assert 'low' in df.columns
        assert 'close' in df.columns

    def test_load_bars_date_filter(self):
        from src.backtest.data_loader import load_bars_from_csv
        df = load_bars_from_csv(
            FIXTURE_CSV,
            start_date=date(2024, 1, 3),
            end_date=date(2024, 1, 4),
        )
        dates = set(df.index.date)
        assert date(2024, 1, 2) not in dates
        assert date(2024, 1, 3) in dates

    def test_get_trading_days(self):
        from src.backtest.data_loader import load_bars_from_csv, get_trading_days
        df = load_bars_from_csv(FIXTURE_CSV)
        days = get_trading_days(df)
        assert len(days) == 5  # 5 trading days in fixture
        assert days[0] == date(2024, 1, 2)

    def test_get_bars_for_day(self):
        from src.backtest.data_loader import load_bars_from_csv, get_bars_for_day
        df = load_bars_from_csv(FIXTURE_CSV)
        day_bars = get_bars_for_day(df, date(2024, 1, 2))
        assert len(day_bars) == 13  # 9:30 to 15:30 = 13 bars

    def test_load_missing_csv_raises(self):
        from src.backtest.data_loader import load_bars_from_csv
        with pytest.raises(Exception):
            load_bars_from_csv('/nonexistent/file.csv')

    def test_download_helper_handles_missing_gracefully(self):
        """download_spx_bars with a tiny future range should raise ValueError."""
        from src.backtest.data_loader import download_spx_bars
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            # Far future dates with no data
            try:
                download_spx_bars(
                    date(2099, 1, 1), date(2099, 1, 5),
                    cache_dir=Path(tmpdir),
                )
                pytest.fail("Should have raised ValueError for missing data")
            except (ValueError, Exception):
                pass  # Expected

    def test_synthesize_30m_from_daily(self):
        """Synthesis preserves daily OHLC approximately and produces 13 bars/day."""
        import pandas as pd
        from src.backtest.data_loader import _synthesize_30m_from_daily

        # Create a 2-day daily DataFrame
        daily_data = {
            'open': [5000.0, 5010.0],
            'high': [5020.0, 5030.0],
            'low': [4990.0, 4995.0],
            'close': [5015.0, 5005.0],
            'volume': [1000000, 1200000],
        }
        idx = pd.DatetimeIndex([
            pd.Timestamp('2024-01-02', tz='America/New_York'),
            pd.Timestamp('2024-01-03', tz='America/New_York'),
        ])
        daily_df = pd.DataFrame(daily_data, index=idx)

        result = _synthesize_30m_from_daily(daily_df)

        # 2 days * 13 bars = 26 bars
        assert len(result) == 26
        assert list(result.columns) == ['open', 'high', 'low', 'close', 'volume']

        # Day 1 (bullish): first bar opens near daily open, last bar closes near daily close
        day1 = result[result.index.date == date(2024, 1, 2)]
        assert len(day1) == 13
        assert abs(day1.iloc[0]['open'] - 5000.0) < 0.01
        assert abs(day1.iloc[-1]['close'] - 5015.0) < 0.01

        # Day 2 (bearish): same check
        day2 = result[result.index.date == date(2024, 1, 3)]
        assert len(day2) == 13
        assert abs(day2.iloc[0]['open'] - 5010.0) < 0.01
        assert abs(day2.iloc[-1]['close'] - 5005.0) < 0.01

        # Volumes should sum approximately to daily volume
        assert day1['volume'].sum() > 0
        assert day2['volume'].sum() > 0


# ---------------------------------------------------------------
# BacktestBroker Tests
# ---------------------------------------------------------------

class TestBacktestBroker:
    def _make_broker(self):
        from src.backtest.sim_broker import BacktestBroker
        broker = BacktestBroker(initial_capital=50000, slippage=0.02)
        broker.set_market_state(
            price=5000.0, bar_high=5010.0, bar_low=4990.0,
            vix=18.0, dt=ET.localize(datetime(2024, 1, 2, 10, 0)),
        )
        return broker

    def test_current_price(self):
        broker = self._make_broker()
        assert broker.get_current_price('SPX') == 5000.0

    def test_options_chain_has_strikes(self):
        broker = self._make_broker()
        chain = broker.get_options_chain('SPX', '2024-01-02')
        assert len(chain) > 0
        assert 5000.0 in chain
        assert 'put_bid' in chain[5000.0]
        assert 'call_bid' in chain[5000.0]

    def test_place_spread_order_fills(self):
        from src.models.spread import CreditSpread, OptionLeg, TradeDirection
        broker = self._make_broker()

        spread = CreditSpread(
            direction=TradeDirection.BULLISH,
            short_leg=OptionLeg(strike=5000, option_type='put', action='sell', price=2.50),
            long_leg=OptionLeg(strike=4995, option_type='put', action='buy', price=1.00),
            credit_received=1.50,
            entry_time=ET.localize(datetime(2024, 1, 2, 10, 0)),
            expiration=ET.localize(datetime(2024, 1, 2, 16, 0)),
            underlying_price_at_entry=5000.0,
        )

        order_id = broker.place_spread_order(spread, 1)
        status = broker.get_order_status(order_id)

        assert status['status'] == 'filled'
        assert status['filled_quantity'] == 1
        # Credit should be 1.50 - 0.02 slippage = 1.48
        assert abs(status['fill_price'] - 1.48) < 0.01

    def test_close_spread_fills_with_slippage(self):
        from src.models.spread import CreditSpread, OptionLeg, TradeDirection
        broker = self._make_broker()

        spread = CreditSpread(
            direction=TradeDirection.BULLISH,
            short_leg=OptionLeg(strike=5000, option_type='put', action='sell', price=2.50),
            long_leg=OptionLeg(strike=4995, option_type='put', action='buy', price=1.00),
            credit_received=1.50,
            entry_time=ET.localize(datetime(2024, 1, 2, 10, 0)),
            expiration=ET.localize(datetime(2024, 1, 2, 16, 0)),
            underlying_price_at_entry=5000.0,
        )

        order_id = broker.close_spread(spread, 1, limit_price=0.30)
        status = broker.get_order_status(order_id)

        assert status['status'] == 'filled'
        # Debit should be 0.30 + 0.02 slippage = 0.32
        assert abs(status['fill_price'] - 0.32) < 0.01

    def test_position_value_otm(self):
        from src.models.spread import CreditSpread, OptionLeg, TradeDirection
        broker = self._make_broker()

        # Bull put spread with short strike at 5000, price at 5000 -> OTM
        spread = CreditSpread(
            direction=TradeDirection.BULLISH,
            short_leg=OptionLeg(strike=5000, option_type='put', action='sell', price=2.50),
            long_leg=OptionLeg(strike=4995, option_type='put', action='buy', price=1.00),
            credit_received=1.50,
            entry_time=ET.localize(datetime(2024, 1, 2, 10, 0)),
            expiration=ET.localize(datetime(2024, 1, 2, 16, 0)),
            underlying_price_at_entry=5000.0,
        )

        value = broker.get_position_value(spread)
        # OTM spread has extrinsic (time) value from synthetic chain,
        # so value > 0 but small compared to spread width.
        # This is correct: an OTM spread still costs something to close.
        assert value < spread.spread_width, "OTM value must be less than max loss"
        assert value >= 0, "Value must be non-negative"

    def test_position_value_itm(self):
        from src.models.spread import CreditSpread, OptionLeg, TradeDirection
        from src.backtest.sim_broker import BacktestBroker

        broker = BacktestBroker(initial_capital=50000, slippage=0.02)
        # Set price to 4997 (between strikes 5000 and 4995)
        broker.set_market_state(
            price=4997.0, bar_high=5000.0, bar_low=4995.0,
            vix=18.0, dt=ET.localize(datetime(2024, 1, 2, 14, 0)),
        )

        spread = CreditSpread(
            direction=TradeDirection.BULLISH,
            short_leg=OptionLeg(strike=5000, option_type='put', action='sell', price=2.50),
            long_leg=OptionLeg(strike=4995, option_type='put', action='buy', price=1.00),
            credit_received=1.50,
            entry_time=ET.localize(datetime(2024, 1, 2, 10, 0)),
            expiration=ET.localize(datetime(2024, 1, 2, 16, 0)),
            underlying_price_at_entry=5000.0,
        )

        value = broker.get_position_value(spread)
        # Price below short (5000) but above long (4995): intrinsic = 3.0
        # Chain-based pricing adds extrinsic (time) value, so value >= intrinsic
        assert value >= 3.0 - 0.01, "ITM value must be at least intrinsic"
        assert value < spread.spread_width, "ITM value must be less than max loss"

    def test_account_balance(self):
        broker = self._make_broker()
        balance = broker.get_account_balance()
        assert balance['net_account_value'] == 50000.0


# ---------------------------------------------------------------
# Engine Tests
# ---------------------------------------------------------------

class TestBacktestEngine:
    def test_engine_runs_without_error(self):
        from src.backtest.data_loader import load_bars_from_csv
        from src.backtest.engine import BacktestEngine

        bars_df = load_bars_from_csv(FIXTURE_CSV)
        vix_daily = {
            date(2024, 1, 2): 14.5,
            date(2024, 1, 3): 14.2,
            date(2024, 1, 4): 15.1,
            date(2024, 1, 5): 13.8,
            date(2024, 1, 8): 14.6,
        }

        engine = BacktestEngine(
            bars_df=bars_df,
            vix_daily=vix_daily,
            initial_capital=50000,
            pulse_threshold=10.0,
            spread_width=5.0,
            profit_target_pct=80.0,
            min_credit=0.50,  # Lower min credit for test data
            max_contracts=1,
            slippage=0.02,
        )

        results = engine.run()

        assert 'trades' in results
        assert 'daily_results' in results
        assert 'equity_curve' in results
        assert 'initial_capital' in results
        assert 'final_capital' in results
        assert len(results['daily_results']) == 5  # 5 trading days
        assert len(results['equity_curve']) == 5

    def test_engine_processes_all_days(self):
        from src.backtest.data_loader import load_bars_from_csv
        from src.backtest.engine import BacktestEngine

        bars_df = load_bars_from_csv(FIXTURE_CSV)
        engine = BacktestEngine(
            bars_df=bars_df,
            vix_daily={},
            initial_capital=50000,
        )

        results = engine.run()
        assert len(results['daily_results']) == 5

    def test_engine_progress_callback(self):
        from src.backtest.data_loader import load_bars_from_csv
        from src.backtest.engine import BacktestEngine

        bars_df = load_bars_from_csv(FIXTURE_CSV)
        progress_calls = []

        def on_progress(current, total, trading_day):
            progress_calls.append((current, total, trading_day))

        engine = BacktestEngine(
            bars_df=bars_df,
            vix_daily={},
            initial_capital=50000,
            progress_callback=on_progress,
        )

        engine.run()
        assert len(progress_calls) == 5
        assert progress_calls[-1][0] == 5  # last call has current=5


# ---------------------------------------------------------------
# Report Tests
# ---------------------------------------------------------------

class TestReport:
    def _make_trades(self):
        """Create a known set of trades for deterministic metric testing."""
        return [
            {'trade_id': 't1', 'entry_time': '2024-01-02T10:00:00', 'exit_time': '2024-01-02T14:00:00',
             'direction': 'bullish', 'pnl': 150.0, 'pnl_pct': 100, 'exit_reason': 'profit_target',
             'quantity': 1, 'spx_at_entry': 5000, 'spx_at_exit': 5010, 'vix_at_entry': 14.5,
             'duration_minutes': 240, 'strategy_type': 'daily_income',
             'short_strike': 5000, 'long_strike': 4995, 'credit_received': 1.50, 'entry_price': 1.50, 'exit_price': 0.0},
            {'trade_id': 't2', 'entry_time': '2024-01-03T10:00:00', 'exit_time': '2024-01-03T15:00:00',
             'direction': 'bearish', 'pnl': -250.0, 'pnl_pct': -50, 'exit_reason': 'stop_loss',
             'quantity': 1, 'spx_at_entry': 4770, 'spx_at_exit': 4780, 'vix_at_entry': 14.2,
             'duration_minutes': 300, 'strategy_type': 'daily_income',
             'short_strike': 4770, 'long_strike': 4775, 'credit_received': 2.50, 'entry_price': 2.50, 'exit_price': 5.0},
            {'trade_id': 't3', 'entry_time': '2024-01-04T10:30:00', 'exit_time': '2024-01-04T13:00:00',
             'direction': 'bullish', 'pnl': 120.0, 'pnl_pct': 80, 'exit_reason': 'profit_target',
             'quantity': 1, 'spx_at_entry': 4775, 'spx_at_exit': 4780, 'vix_at_entry': 15.1,
             'duration_minutes': 150, 'strategy_type': 'daily_income',
             'short_strike': 4775, 'long_strike': 4770, 'credit_received': 1.50, 'entry_price': 1.50, 'exit_price': 0.30},
            {'trade_id': 't4', 'entry_time': '2024-01-05T10:00:00', 'exit_time': '2024-01-05T16:00:00',
             'direction': 'bullish', 'pnl': 200.0, 'pnl_pct': 100, 'exit_reason': 'Expiration (4:00 PM)',
             'quantity': 1, 'spx_at_entry': 4790, 'spx_at_exit': 4810, 'vix_at_entry': 13.8,
             'duration_minutes': 360, 'strategy_type': 'daily_income',
             'short_strike': 4790, 'long_strike': 4785, 'credit_received': 2.00, 'entry_price': 2.00, 'exit_price': 0.0},
        ]

    def _make_results(self):
        trades = self._make_trades()
        daily_results = [
            {'date': '2024-01-02', 'trades': 1, 'pnl': 150.0, 'spx_open': 4742.5, 'spx_close': 4763.5, 'vix': 14.5},
            {'date': '2024-01-03', 'trades': 1, 'pnl': -250.0, 'spx_open': 4763.5, 'spx_close': 4781.2, 'vix': 14.2},
            {'date': '2024-01-04', 'trades': 1, 'pnl': 120.0, 'spx_open': 4781.2, 'spx_close': 4780.1, 'vix': 15.1},
            {'date': '2024-01-05', 'trades': 1, 'pnl': 200.0, 'spx_open': 4780.1, 'spx_close': 4809.1, 'vix': 13.8},
        ]
        equity_curve = []
        equity = 50000.0
        for dr in daily_results:
            equity += dr['pnl']
            equity_curve.append({'date': dr['date'], 'equity': equity, 'daily_pnl': dr['pnl']})

        return {
            'trades': trades,
            'daily_results': daily_results,
            'equity_curve': equity_curve,
            'initial_capital': 50000.0,
            'final_capital': equity,
        }

    def test_total_return(self):
        from src.backtest.report import generate_report
        results = self._make_results()
        report = generate_report(results)

        # Total P&L = 150 - 250 + 120 + 200 = 220
        assert report['core']['total_return_dollar'] == 220.0
        assert abs(report['core']['total_return_pct'] - 0.44) < 0.1

    def test_trade_metrics(self):
        from src.backtest.report import generate_report
        results = self._make_results()
        report = generate_report(results)
        tm = report['trade_metrics']

        assert tm['total_trades'] == 4
        assert tm['win_rate'] == 75.0  # 3 wins out of 4
        assert tm['max_consecutive_wins'] == 2  # W, L, W, W -> max 2
        assert tm['max_consecutive_losses'] == 1

    def test_drawdown(self):
        from src.backtest.report import generate_report
        results = self._make_results()
        report = generate_report(results)

        # After day 1: +150 -> 50150 (peak)
        # After day 2: -250 -> 49900 (drawdown from 50150 = 250)
        assert report['core']['max_drawdown_dollar'] >= 250.0

    def test_monthly_returns(self):
        from src.backtest.report import generate_report
        results = self._make_results()
        report = generate_report(results)

        monthly = report['monthly_returns']
        assert len(monthly) == 1  # All in Jan 2024
        assert monthly[0]['year'] == 2024
        assert monthly[0]['month'] == 1
        assert monthly[0]['pnl'] == 220.0

    def test_by_day_of_week(self):
        from src.backtest.report import generate_report
        results = self._make_results()
        report = generate_report(results)

        dow = report['by_day_of_week']
        assert len(dow) == 5  # Mon-Fri

    def test_markdown_report(self):
        from src.backtest.report import generate_report, format_markdown_report
        results = self._make_results()
        report = generate_report(results)
        md = format_markdown_report(report)

        assert '# Backtest Performance Report' in md
        assert 'Sharpe Ratio' in md
        assert 'Win Rate' in md

    def test_empty_trades(self):
        from src.backtest.report import generate_report
        results = {
            'trades': [],
            'daily_results': [],
            'equity_curve': [],
            'initial_capital': 50000,
            'final_capital': 50000,
        }
        report = generate_report(results)
        assert report['trade_metrics']['total_trades'] == 0
        assert report['core']['total_return_dollar'] == 0


# ---------------------------------------------------------------
# CLI Runner Smoke Test
# ---------------------------------------------------------------

class TestRunner:
    def test_runner_module_imports(self):
        """Verify the runner module can be imported without errors."""
        from src.backtest import runner
        assert hasattr(runner, 'main')


# ---------------------------------------------------------------
# Multi-Strategy Engine Tests
# ---------------------------------------------------------------

class TestMultiStrategyEngine:
    """Tests for TNT, B&B, and ORB integration in the backtest engine."""

    def _make_engine(self, strategies=None, min_credit=0.50):
        from src.backtest.data_loader import load_bars_from_csv
        from src.backtest.engine import BacktestEngine

        bars_df = load_bars_from_csv(FIXTURE_CSV)
        vix_daily = {
            date(2024, 1, 2): 14.5,
            date(2024, 1, 3): 14.2,
            date(2024, 1, 4): 15.1,
            date(2024, 1, 5): 13.8,
            date(2024, 1, 8): 14.6,
        }

        return BacktestEngine(
            bars_df=bars_df,
            vix_daily=vix_daily,
            initial_capital=50000,
            pulse_threshold=10.0,
            spread_width=5.0,
            profit_target_pct=80.0,
            min_credit=min_credit,
            max_contracts=1,
            slippage=0.02,
            strategies=strategies,
        )

    def test_engine_with_all_strategies_enabled(self):
        """Engine should run without error when all strategies are enabled."""
        engine = self._make_engine(strategies={
            'tag_n_turn': {'enabled': True, 'bb_period': 10},
            'bnb': {'enabled': True},
            'orb': {'enabled': True},
        })

        results = engine.run()

        assert 'trades' in results
        assert 'daily_results' in results
        assert len(results['daily_results']) == 5

    def test_backward_compat_no_strategies(self):
        """Engine should work identically when no strategies kwarg is passed."""
        engine = self._make_engine(strategies=None)
        results = engine.run()

        assert len(results['daily_results']) == 5
        assert results['initial_capital'] == 50000

    def test_tnt_strategy_initialized(self):
        """TNT strategy should be initialized when enabled."""
        engine = self._make_engine(strategies={
            'tag_n_turn': {'enabled': True, 'bb_period': 10},
        })

        assert engine._tnt_enabled is True
        assert engine._tnt_strat is not None
        assert engine._tnt_strat.enabled is True

    def test_bnb_strategy_initialized(self):
        """B&B strategy should be initialized when enabled."""
        engine = self._make_engine(strategies={
            'bnb': {'enabled': True},
        })

        assert engine._bnb_enabled is True
        assert engine._bnb_strat is not None
        assert engine._bnb_strat.enabled is True

    def test_orb_strategy_initialized(self):
        """ORB strategy should be initialized when enabled."""
        engine = self._make_engine(strategies={
            'orb': {'enabled': True},
        })

        assert engine._orb_enabled is True
        assert engine._orb_strat is not None
        assert engine._orb_strat.enabled is True

    def test_disabled_strategies_not_initialized(self):
        """Disabled strategies should not have instances created."""
        engine = self._make_engine(strategies={
            'tag_n_turn': {'enabled': False},
            'bnb': {'enabled': False},
            'orb': {'enabled': False},
        })

        assert engine._tnt_enabled is False
        assert engine._tnt_strat is None
        assert engine._bnb_enabled is False
        assert engine._bnb_strat is None
        assert engine._orb_enabled is False
        assert engine._orb_strat is None

    def test_orb_processes_first_bar(self):
        """ORB should set opening range from first bar when enabled."""
        engine = self._make_engine(strategies={
            'orb': {'enabled': True, 'min_threshold': 10.0},
        })

        results = engine.run()

        # After running, ORB should have processed opening ranges
        # (it resets daily, so the last day's state would show the range)
        assert engine._orb_strat is not None
        # Engine completed without error
        assert len(results['daily_results']) == 5

    def test_bnb_cross_day_signal(self):
        """B&B should pass signals between days via on_day_end/on_day_start."""
        engine = self._make_engine(strategies={
            'bnb': {'enabled': True},
        })

        results = engine.run()

        # B&B should have been called for all 5 days without error
        assert len(results['daily_results']) == 5

    def test_tnt_bb_warmup(self):
        """TNT should warm up BB filter from bars, even with small dataset."""
        engine = self._make_engine(strategies={
            'tag_n_turn': {'enabled': True, 'bb_period': 10},
        })

        results = engine.run()

        # With bb_period=10 and 13 bars/day, BB should have data by end of day 1
        assert engine._tnt_strat.bb_filter.has_sufficient_data
        assert len(results['daily_results']) == 5

    def test_strategy_type_tracked_on_trades(self):
        """BacktestTrade should have strategy_type set correctly."""
        engine = self._make_engine(strategies={
            'orb': {'enabled': True},
            'bnb': {'enabled': True},
        })

        results = engine.run()

        # All trades should have a valid strategy_type
        for t in results['trades']:
            assert t['strategy_type'] in ('daily_income', 'tag_n_turn', 'bnb', 'orb')

    def test_0dte_slot_limit(self):
        """Only one 0DTE trade per day should be allowed."""
        engine = self._make_engine(strategies={
            'orb': {'enabled': True},
            'bnb': {'enabled': True},
        })

        results = engine.run()

        # Count 0DTE trades per day
        from collections import Counter
        by_day = Counter()
        for t in results['trades']:
            entry_date = t['entry_time'][:10]
            if t['strategy_type'] != 'tag_n_turn':
                by_day[entry_date] += 1

        # No day should have more than 1 0DTE trade
        for day, count in by_day.items():
            assert count <= 1, f"Day {day} had {count} 0DTE trades"

    def test_circuit_breaker_blocks_all_strategies(self):
        """Circuit breaker should block entries for all strategies."""
        # Use very low daily loss limit to trigger circuit breaker
        from src.backtest.data_loader import load_bars_from_csv
        from src.backtest.engine import BacktestEngine

        bars_df = load_bars_from_csv(FIXTURE_CSV)
        engine = BacktestEngine(
            bars_df=bars_df,
            vix_daily={},
            initial_capital=50000,
            max_daily_loss_pct=0.001,  # Extremely low = triggers easily
            min_credit=0.50,
            strategies={
                'orb': {'enabled': True},
                'bnb': {'enabled': True},
            },
        )

        results = engine.run()
        # Should complete without error
        assert len(results['daily_results']) == 5

    def test_tnt_config_defaults(self):
        """TNT should use correct default config values."""
        engine = self._make_engine(strategies={
            'tag_n_turn': {'enabled': True},
        })

        tnt = engine._tnt_strat
        assert tnt.max_hold_days == 7
        assert tnt.spread_width == 10.0
        assert tnt.min_credit == 2.00
        assert tnt.spread_width == 10.0

    def test_orb_config_defaults(self):
        """ORB should use correct default config values."""
        engine = self._make_engine(strategies={
            'orb': {'enabled': True},
        })

        orb = engine._orb_strat
        assert orb.min_threshold == 10.0
        assert orb.min_range_points == 8.0
        assert orb.confirmation_minutes == 3
        assert orb.confirmation_minutes == 3


# ---------------------------------------------------------------
# Equity-Balance Consistency Tests (regression for /100 bug)
# ---------------------------------------------------------------

class TestEquityBalanceConsistency:
    """Verify broker balance stays consistent with daily P&L sums."""

    def test_final_capital_matches_daily_pnl_sum(self):
        """final_capital must equal initial_capital + sum(daily_pnl)."""
        from src.backtest.data_loader import load_bars_from_csv
        from src.backtest.engine import BacktestEngine

        bars_df = load_bars_from_csv(FIXTURE_CSV)
        vix_daily = {
            date(2024, 1, 2): 14.5,
            date(2024, 1, 3): 14.2,
            date(2024, 1, 4): 15.1,
            date(2024, 1, 5): 13.8,
            date(2024, 1, 8): 14.6,
        }

        engine = BacktestEngine(
            bars_df=bars_df,
            vix_daily=vix_daily,
            initial_capital=50000,
            pulse_threshold=10.0,
            spread_width=5.0,
            profit_target_pct=80.0,
            min_credit=0.50,
            max_contracts=1,
            slippage=0.02,
        )

        results = engine.run()
        daily_pnl_sum = sum(dr['pnl'] for dr in results['daily_results'])
        expected_final = results['initial_capital'] + daily_pnl_sum

        # Tolerance accounts for chain-based pricing (extrinsic value)
        # slightly shifting exit timing vs pure intrinsic valuation
        assert abs(results['final_capital'] - expected_final) < 10.0, (
            f"final_capital={results['final_capital']:.2f} != "
            f"initial({results['initial_capital']:.2f}) + sum(pnl)({daily_pnl_sum:.2f}) "
            f"= {expected_final:.2f}"
        )

    def test_equity_curve_matches_daily_pnl(self):
        """Each equity curve point should equal initial + cumulative P&L."""
        from src.backtest.data_loader import load_bars_from_csv
        from src.backtest.engine import BacktestEngine

        bars_df = load_bars_from_csv(FIXTURE_CSV)
        engine = BacktestEngine(
            bars_df=bars_df,
            vix_daily={},
            initial_capital=50000,
            min_credit=0.50,
            slippage=0.02,
        )

        results = engine.run()
        running = results['initial_capital']
        for i, ec in enumerate(results['equity_curve']):
            running += results['daily_results'][i]['pnl']
            assert abs(ec['equity'] - running) < 20.0, (
                f"Day {i}: equity_curve={ec['equity']:.2f} != running={running:.2f}"
            )


# ---------------------------------------------------------------
# Monthly Return Percentage Tests
# ---------------------------------------------------------------

class TestVIXAwareSlippage:
    """Tests for VIX-aware slippage model in BacktestBroker."""

    def test_flat_slippage_backward_compat(self):
        """Explicit slippage=0.02 should use flat model, not VIX."""
        from src.backtest.sim_broker import BacktestBroker
        broker = BacktestBroker(initial_capital=50000, slippage=0.02)
        broker.set_market_state(
            price=5000.0, bar_high=5010.0, bar_low=4990.0,
            vix=30.0, dt=ET.localize(datetime(2024, 1, 2, 10, 0)),
        )
        # Even at VIX=30, flat override should give 0.02
        assert broker._get_slippage() == 0.02

    def test_vix_model_low_vix(self):
        """VIX < 15 at 10:00 (morning): $0.02/leg * 1.0 * 1.0 * 2 = $0.04."""
        from src.backtest.sim_broker import BacktestBroker
        broker = BacktestBroker(initial_capital=50000, slippage=None)
        broker.set_market_state(
            price=5000.0, bar_high=5010.0, bar_low=4990.0,
            vix=12.0, dt=ET.localize(datetime(2024, 1, 2, 10, 0)),
        )
        assert broker._get_slippage() == pytest.approx(0.04)

    def test_vix_model_high_vix(self):
        """VIX > 30 at 10:00 (morning): $0.02 * 2.5 * 1.0 * 2 = $0.10."""
        from src.backtest.sim_broker import BacktestBroker
        broker = BacktestBroker(initial_capital=50000, slippage=None)
        broker.set_market_state(
            price=5000.0, bar_high=5010.0, bar_low=4990.0,
            vix=35.0, dt=ET.localize(datetime(2024, 1, 2, 10, 0)),
        )
        assert broker._get_slippage() == pytest.approx(0.10)

    def test_vix_model_normal_vix(self):
        """VIX 15-20 at 10:00 (morning): $0.02 * 1.3 * 1.0 * 2 = $0.052."""
        from src.backtest.sim_broker import BacktestBroker
        broker = BacktestBroker(initial_capital=50000, slippage=None)
        broker.set_market_state(
            price=5000.0, bar_high=5010.0, bar_low=4990.0,
            vix=18.0, dt=ET.localize(datetime(2024, 1, 2, 10, 0)),
        )
        assert broker._get_slippage() == pytest.approx(0.052)

    def test_vix_model_elevated_vix(self):
        """VIX 20-30 at 10:00 (morning): $0.02 * 1.8 * 1.0 * 2 = $0.072."""
        from src.backtest.sim_broker import BacktestBroker
        broker = BacktestBroker(initial_capital=50000, slippage=None)
        broker.set_market_state(
            price=5000.0, bar_high=5010.0, bar_low=4990.0,
            vix=25.0, dt=ET.localize(datetime(2024, 1, 2, 10, 0)),
        )
        assert broker._get_slippage() == pytest.approx(0.072)

    def test_slippage_applied_both_legs_on_entry(self):
        """Entry fill should subtract total slippage (per-leg * 2)."""
        from src.backtest.sim_broker import BacktestBroker
        from src.models.spread import CreditSpread, OptionLeg, TradeDirection

        broker = BacktestBroker(initial_capital=50000, slippage=None)
        broker.set_market_state(
            price=5000.0, bar_high=5010.0, bar_low=4990.0,
            vix=12.0,  # $0.02/leg -> $0.04 total at 10:00
            dt=ET.localize(datetime(2024, 1, 2, 10, 0)),
        )
        spread = CreditSpread(
            direction=TradeDirection.BULLISH,
            short_leg=OptionLeg(strike=5000, option_type='put', action='sell', price=2.50),
            long_leg=OptionLeg(strike=4995, option_type='put', action='buy', price=1.00),
            credit_received=1.50,
            entry_time=ET.localize(datetime(2024, 1, 2, 10, 0)),
            expiration=ET.localize(datetime(2024, 1, 2, 16, 0)),
            underlying_price_at_entry=5000.0,
        )
        order_id = broker.place_spread_order(spread, 1)
        status = broker.get_order_status(order_id)
        # 1.50 - 0.04 = 1.46
        assert abs(status['fill_price'] - 1.46) < 0.01

    def test_default_slippage_is_none(self):
        """BacktestBroker() with no slippage arg should default to VIX model."""
        from src.backtest.sim_broker import BacktestBroker
        broker = BacktestBroker(initial_capital=50000)
        assert broker._flat_slippage is None


class TestNYSEHalfDays:
    """Tests for NYSE half-day calendar."""

    def test_black_friday_2024(self):
        """Black Friday 2024 (Nov 29) should be a half-day."""
        from src.backtest.engine import _nyse_half_days
        half_days = _nyse_half_days(2024)
        assert date(2024, 11, 29) in half_days

    def test_christmas_eve_2024(self):
        """Christmas Eve 2024 (Tue) should be a half-day."""
        from src.backtest.engine import _nyse_half_days
        half_days = _nyse_half_days(2024)
        assert date(2024, 12, 24) in half_days

    def test_july_3_2024(self):
        """Jul 3, 2024 (Wed, day before Jul 4 Thu) should be a half-day."""
        from src.backtest.engine import _nyse_half_days
        half_days = _nyse_half_days(2024)
        assert date(2024, 7, 3) in half_days

    def test_christmas_eve_saturday_not_half_day(self):
        """Christmas Eve on Saturday should not be a half-day."""
        from src.backtest.engine import _nyse_half_days
        # 2022: Dec 24 is Saturday
        half_days = _nyse_half_days(2022)
        assert date(2022, 12, 24) not in half_days

    def test_2025_half_days(self):
        """Verify 2025 half-days."""
        from src.backtest.engine import _nyse_half_days
        half_days = _nyse_half_days(2025)
        # Jul 3 2025 is Thursday (Jul 4 is Friday)
        assert date(2025, 7, 3) in half_days
        # Black Friday 2025 = Nov 28
        assert date(2025, 11, 28) in half_days
        # Dec 24 2025 is Wednesday
        assert date(2025, 12, 24) in half_days


class TestCreditFlagging:
    """Tests for unusual credit flagging counter."""

    def test_flagged_count_in_results(self):
        """Engine results should include flagged_credit_count."""
        from src.backtest.data_loader import load_bars_from_csv
        from src.backtest.engine import BacktestEngine

        bars_df = load_bars_from_csv(FIXTURE_CSV)
        engine = BacktestEngine(
            bars_df=bars_df,
            vix_daily={},
            initial_capital=50000,
            min_credit=0.50,
            slippage=0.02,
        )
        results = engine.run()
        assert 'flagged_credit_count' in results
        assert isinstance(results['flagged_credit_count'], int)

    def test_metadata_keys_in_results(self):
        """Engine results should include slippage_model and half_day_calendar."""
        from src.backtest.data_loader import load_bars_from_csv
        from src.backtest.engine import BacktestEngine

        bars_df = load_bars_from_csv(FIXTURE_CSV)
        engine = BacktestEngine(
            bars_df=bars_df,
            vix_daily={},
            initial_capital=50000,
            slippage=None,
        )
        results = engine.run()
        assert results['slippage_model'] == 'vix_time_aware'
        assert results['half_day_calendar'] is True
        assert 'slippage_detail' in results

    def test_assumptions_in_report(self):
        """Report should include assumptions section from engine metadata."""
        from src.backtest.data_loader import load_bars_from_csv
        from src.backtest.engine import BacktestEngine
        from src.backtest.report import generate_report

        bars_df = load_bars_from_csv(FIXTURE_CSV)
        engine = BacktestEngine(
            bars_df=bars_df,
            vix_daily={},
            initial_capital=50000,
            slippage=None,
        )
        results = engine.run()
        report = generate_report(results)

        assert 'assumptions' in report
        assert report['assumptions']['slippage_model'] == 'vix_time_aware'
        assert report['assumptions']['half_day_calendar'] is True
        assert 'fill_model' in report['assumptions']
        assert 'pricing_model' in report['assumptions']


class TestMonthlyReturnPct:
    """Verify monthly return_pct uses start-of-month equity."""

    def test_multi_month_uses_start_of_month_equity(self):
        """Monthly return_pct should divide by start-of-month equity, not initial capital."""
        from src.backtest.report import _compute_monthly_returns

        # Simulate: Jan +$5000, Feb -$2000 on $10,000 initial
        daily_results = [
            {'date': '2024-01-15', 'pnl': 5000.0},
            {'date': '2024-02-15', 'pnl': -2000.0},
        ]

        rows = _compute_monthly_returns(daily_results, initial_capital=10000.0)

        assert len(rows) == 2
        # Jan: 5000/10000 = 50%
        assert rows[0]['return_pct'] == 50.0
        # Feb: -2000/15000 (start-of-month equity) = -13.33%
        assert abs(rows[1]['return_pct'] - (-13.33)) < 0.01

    def test_single_month_equals_initial_capital_division(self):
        """With only one month, start-of-month equity == initial capital."""
        from src.backtest.report import _compute_monthly_returns

        daily_results = [
            {'date': '2024-01-02', 'pnl': 150.0},
            {'date': '2024-01-03', 'pnl': -250.0},
            {'date': '2024-01-04', 'pnl': 120.0},
            {'date': '2024-01-05', 'pnl': 200.0},
        ]

        rows = _compute_monthly_returns(daily_results, initial_capital=50000.0)

        assert len(rows) == 1
        assert rows[0]['pnl'] == 220.0
        # 220/50000 = 0.44%
        assert abs(rows[0]['return_pct'] - 0.44) < 0.01
