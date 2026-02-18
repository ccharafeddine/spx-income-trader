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
        assert value == 0  # Price >= short strike, both OTM

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
        # Price below short (5000) but above long (4995): value = 5000 - 4997 = 3.0
        assert abs(value - 3.0) < 0.01

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
