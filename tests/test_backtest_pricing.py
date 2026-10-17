"""0DTE pricing model in the backtest broker: trading-time expiry, VIX-scaled
ATM vol (VIX1D when known), put skew, and no butterfly arbitrage."""
import math
from datetime import datetime, time

import pytest
import pytz

from src.backtest.sim_broker import (BacktestBroker, smile_vol, zero_dte_atm_vol,
                                     TRADING_MINUTES_PER_YEAR, VIX_TO_0DTE_IV)

ET = pytz.timezone('America/New_York')


def _broker(price=6000.0, vix=17.0, hh=10, mm=30, vix1d=None, close_time=None):
    b = BacktestBroker()
    b.set_market_state(price=price, bar_high=price, bar_low=price, vix=vix,
                       dt=ET.localize(datetime(2026, 3, 18, hh, mm)), vix1d=vix1d, close_time=close_time)
    return b


def _vertical(chain, short, long, kind):
    return chain[short][f'{kind}_last'] - chain[long][f'{kind}_last']


def test_atm_vol_scaling():
    assert zero_dte_atm_vol(20.0) == pytest.approx(VIX_TO_0DTE_IV * 0.20)
    assert zero_dte_atm_vol(20.0, vix1d=11.0) == pytest.approx(0.11)


def test_time_to_expiry_is_trading_minutes():
    """ATM straddle ~ S*sigma*sqrt(T)*sqrt(2/pi) with T in trading minutes."""
    b = _broker()
    chain = b.get_options_chain('SPX', '2026-03-18')
    straddle = chain[6000]['call_last'] + chain[6000]['put_last']
    sigma = VIX_TO_0DTE_IV * 0.17
    expected = 6000 * sigma * math.sqrt(330 / TRADING_MINUTES_PER_YEAR) * math.sqrt(2 / math.pi)
    assert straddle == pytest.approx(expected, rel=0.03)
    # the old calendar-hours model gave roughly half this value
    old = 6000 * 0.17 * math.sqrt((5.5 / 24) / 365) * math.sqrt(2 / math.pi)
    assert straddle > 1.4 * old


def test_put_skew_makes_atm_call_spreads_richer_than_put_spreads():
    chain = _broker().get_options_chain('SPX', '2026-03-18')
    put_spread = _vertical(chain, 6000, 5995, 'put')
    call_spread = _vertical(chain, 6000, 6005, 'call')
    assert 2.0 < put_spread < 2.4 < call_spread < 2.7
    assert call_spread - put_spread == pytest.approx(0.24, abs=0.08)


def test_otm_puts_have_higher_vol_than_atm():
    T = 330 / TRADING_MINUTES_PER_YEAR
    atm = smile_vol(6000, 6000, 0.1, T)
    assert atm == pytest.approx(0.1)
    assert smile_vol(5940, 6000, 0.1, T) > 1.1 * atm
    assert smile_vol(5700, 6000, 0.1, T) < 1.71 * atm   # soft cap on the wings


def test_no_butterfly_arbitrage():
    chain = _broker().get_options_chain('SPX', '2026-03-18')
    ks = sorted(chain)
    for kind in ('put', 'call'):
        p = [chain[k][f'{kind}_last'] for k in ks]
        for i in range(1, len(p) - 1):
            assert p[i - 1] - 2 * p[i] + p[i + 1] >= -1e-6


def test_half_day_close_shortens_expiry():
    full = _broker(hh=12, mm=0).get_options_chain('SPX', 'x')[6000]['call_last']
    half = _broker(hh=12, mm=0, close_time=time(13, 0)).get_options_chain('SPX', 'x')[6000]['call_last']
    assert half == pytest.approx(full * math.sqrt(60 / 240), rel=0.02)


def test_vix1d_overrides_vix():
    lo = _broker(vix1d=8.0).get_options_chain('SPX', 'x')[6000]['call_last']
    hi = _broker(vix1d=16.0).get_options_chain('SPX', 'x')[6000]['call_last']
    assert hi == pytest.approx(2 * lo, rel=0.02)
