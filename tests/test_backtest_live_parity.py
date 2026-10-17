"""Backtester vs live-trading parity: setup window, pending-setup
replacement, minimum pulse range, entry at the trigger, and fees.

The parity check replays the live decision sequence (SPXIncomeStrategy +
morning-bias filter, as wired in main.py: triggers checked before new
setups, newer pulse replaces older, setups only from bars completing
before 11:30) over the same recorded bars and compares with the engine."""
from datetime import date, datetime, time, timedelta

import pandas as pd
import pytest
import pytz

from src.backtest.engine import BacktestEngine
from src.core.strategy import SPXIncomeStrategy
from src.models.bar import Bar
from src.models.spread import TradeDirection

ET = pytz.timezone('America/New_York')
DAY = date(2024, 3, 6)


def _bars(rows):
    """rows: list of (open, high, low, close) for consecutive 30-min bars from 9:30."""
    idx = [ET.localize(datetime.combine(DAY, time(9, 30)) + timedelta(minutes=30 * i)) for i in range(len(rows))]
    return pd.DataFrame(rows, columns=['open', 'high', 'low', 'close'], index=pd.DatetimeIndex(idx)).assign(volume=0)


def _flat(n, px=5000.0):
    return [(px, px + 1, px - 1, px)] * n


def _engine(df, **kw):
    kw.setdefault('min_bar_range_points', 2.0)
    e = BacktestEngine(bars_df=df, vix_daily={DAY: 15.0}, initial_capital=50000, max_contracts=1,
                       strategies={'daily_income': {'enabled': True}}, **kw)
    return e


BULL = (4990.0, 5012.0, 4990.0, 5011.0)   # bullish pulse (close at 95% of range)
BEAR = (5010.0, 5012.0, 4990.0, 4991.0)   # bearish pulse


class TestWindowFlags:
    @pytest.mark.parametrize('hhmm,expected', [
        ((9, 30), (True, True, False)),
        ((10, 0), (True, True, False)),
        ((10, 30), (True, True, False)),
        ((11, 0), (True, False, True)),    # 11:00 bar completes at 11:30 -> never arms
        ((11, 30), (False, False, True)),
        ((12, 0), (False, False, True)),
    ])
    def test_flags(self, hhmm, expected):
        e = _engine(_bars(_flat(13)))
        assert e._di_window_flags(ET.localize(datetime.combine(DAY, time(*hhmm)))) == expected


class TestSetupRules:
    def test_11am_pulse_never_trades(self):
        rows = _flat(3) + [BULL] + [(5011, 5030, 5010, 5025)] + _flat(8, 5025)
        e = _engine(_bars(rows)); e.run()
        assert [t for t in e.trades if t.strategy_type == 'daily_income'] == []

    def test_newer_pulse_replaces_pending(self):
        # 9:30 bullish pulse (not triggered), 10:00 bearish pulse replaces it,
        # 10:30 bar breaks the bearish low -> bearish (call spread) trade.
        rows = [BULL, BEAR, (4991, 4992, 4980, 4982)] + _flat(10, 4982)
        e = _engine(_bars(rows)); e.run()
        di = [t for t in e.trades if t.strategy_type == 'daily_income']
        assert len(di) == 1 and di[0].direction == 'bearish'

    def test_min_range_filter_applies(self):
        tiny = (5000.0, 5001.5, 5000.0, 5001.45)   # bullish shape but 1.5 pt range
        rows = [tiny, (5001.5, 5010, 5001, 5009)] + _flat(11, 5009)
        e = _engine(_bars(rows)); e.run()
        assert [t for t in e.trades if t.strategy_type == 'daily_income'] == []
        e0 = _engine(_bars(rows), min_bar_range_points=0.0); e0.run()
        assert len([t for t in e0.trades if t.strategy_type == 'daily_income']) == 1

    def test_entry_priced_at_trigger_not_bar_close(self):
        rows = [BULL, (5011.0, 5040.0, 5010.0, 5038.0)] + _flat(11, 5038)
        e = _engine(_bars(rows)); e.run()
        di = [t for t in e.trades if t.strategy_type == 'daily_income']
        assert len(di) == 1
        assert di[0].spx_at_entry == pytest.approx(5012.0)   # pulse high, not 5038 close
        assert di[0].short_strike == 5010.0


class TestFees:
    def test_fees_charged_on_settlement_entry_only(self):
        rows = [BULL, (5011.0, 5040.0, 5010.0, 5038.0)] + _flat(11, 5038)
        a = _engine(_bars(rows), commission_per_leg=0.0); a.run()
        b = _engine(_bars(rows), commission_per_leg=1.30); b.run()
        ta = [t for t in a.trades if t.strategy_type == 'daily_income'][0]
        tb = [t for t in b.trades if t.strategy_type == 'daily_income'][0]
        closing = 'expir' not in tb.exit_reason.lower()
        expected = (4 if closing else 2) * 1.30 * tb.quantity
        assert ta.pnl - tb.pnl == pytest.approx(expected)


def _live_replay(df):
    """Live decision sequence from main.py on completed 30-min bars."""
    strat = SPXIncomeStrategy(min_bar_range_points=2.0)
    day_open = float(df.iloc[0]['open'])
    pending = None
    for ts, r in df.iterrows():
        start = ts.to_pydatetime()
        end = start + timedelta(minutes=30)
        bar = Bar(timestamp=start, open=r.open, high=r.high, low=r.low, close=r.close)
        # trigger check during the bar (before 11:30)
        if pending and start.time() < time(11, 30):
            if pending[0] == TradeDirection.BULLISH and bar.high > pending[1]:
                return pending[0], start
            if pending[0] == TradeDirection.BEARISH and bar.low < pending[1]:
                return pending[0], start
        # bar completes: setup if bar started in window and completed before expiry
        if time(9, 30) <= start.time() < time(11, 30) and end.time() < time(11, 30):
            d = strat.evaluate_setup(bar, bar.close)
            if d:
                ok, _, _ = SPXIncomeStrategy.check_morning_bias_filter(d, day_open, bar.close)
                if ok:
                    pending = (d, bar.high if d == TradeDirection.BULLISH else bar.low)
        if end.time() >= time(11, 30):
            pending = None
    return None, None


@pytest.mark.parametrize('rows', [
    [BULL, (5011.0, 5040.0, 5010.0, 5038.0)] + _flat(11, 5038),
    [BEAR, (4991.0, 4995.0, 4970.0, 4975.0)] + _flat(11, 4975),
    [BULL, BEAR, (4991, 4992, 4980, 4982)] + _flat(10, 4982),
    _flat(3) + [BULL] + [(5011, 5030, 5010, 5025)] + _flat(8, 5025),
    [(5000, 5001.5, 5000, 5001.45), (5001.5, 5010, 5001, 5009)] + _flat(11, 5009),
    _flat(13),
])
def test_engine_matches_live_decision_sequence(rows):
    df = _bars(rows)
    live_dir, live_bar = _live_replay(df)
    e = _engine(df); e.run()
    di = [t for t in e.trades if t.strategy_type == 'daily_income']
    if live_dir is None:
        assert di == []
    else:
        assert len(di) == 1
        assert di[0].direction == live_dir.value
        assert di[0].entry_time.replace(tzinfo=None) == live_bar.replace(tzinfo=None)
