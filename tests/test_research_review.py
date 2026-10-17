"""Tests for the Daily Melt review toolkit (research/daily_melt_review).

Runs on synthetic minute days, so no market data is needed.
"""
import importlib
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

RESEARCH = Path(__file__).resolve().parent.parent / "research" / "daily_melt_review"


@pytest.fixture(scope="module")
def rv():
    """Import the research modules (they use flat imports) without leaking them."""
    names = ["paths", "options", "metrics", "engine", "analysis"]
    saved = {n: sys.modules.pop(n) for n in names if n in sys.modules}
    sys.path.insert(0, str(RESEARCH))
    try:
        mods = {n: importlib.import_module(n) for n in names}
        yield type("RV", (), mods)
    finally:
        sys.path.remove(str(RESEARCH))
        for n in names:
            sys.modules.pop(n, None)
        sys.modules.update(saved)


def make_day(day, path, vix=16.0, prev_close=None):
    c = np.asarray(path, dtype=float)
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + 0.1
    l = np.minimum(o, c) - 0.1
    return dict(date=day, n=len(c), o=o, h=h, l=l, c=c, vix_open=vix, vix_prev=vix,
                vix3m_prev=vix * 1.1, spx_prev_close=prev_close or c[0], spx_close=float(c[-1]),
                fomc=False)


def trend_day(day, start=5000.0, slope=0.05, seed=0):
    rng = np.random.default_rng(seed)
    return make_day(day, start + slope * np.arange(390) + rng.normal(0, 0.3, 390).cumsum() * 0.2)


def synth_days(n, seed=1, start=date(2020, 1, 2)):
    rng = np.random.default_rng(seed)
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            steps = rng.normal(0, 0.8, 390) + rng.choice([-0.03, 0.03])
            out.append(make_day(d, 5000 + steps.cumsum(), vix=float(rng.uniform(12, 25))))
        d += timedelta(days=1)
    return out


# ---------------- engine rules ----------------

class TestResearchEngineRules:
    def test_no_lookahead_entry_unchanged_by_later_minutes(self, rv):
        d = trend_day(date(2024, 3, 4), slope=0.08, seed=3)
        p = dict(rv.engine.BASE, _k=0.62)
        ent = rv.engine.find_entry(d, p)
        if not ent:
            pytest.skip("synthetic day produced no entry")
        i0 = ent[0][0]
        d2 = dict(d)
        for k in ("o", "h", "l", "c"):
            d2[k] = d[k].copy()
            d2[k][i0 + 1:] = d[k][i0 + 1:] * 0.9  # crash after entry
        assert rv.engine.find_entry(d2, p)[0] == ent[0]

    def test_eleven_oclock_bar_cannot_arm_setup(self, rv):
        # flat until 11:00, then the 11:00 bar closes at its high and price breaks out
        path = np.full(390, 5000.0)
        path[90:120] = np.linspace(5000, 5010, 30)
        path[120:] = np.linspace(5011, 5030, 270)
        d = make_day(date(2024, 3, 5), path)
        assert rv.engine.find_entry(d, dict(rv.engine.BASE, _k=0.62)) == []

    def test_min_range_gate(self, rv):
        # 9:30 bar closes at its top but the range is under 2 points
        path = np.full(390, 5000.0)
        path[:30] = np.linspace(5000, 5001.2, 30)
        path[30:] = np.linspace(5002, 5004, 360)
        d = make_day(date(2024, 3, 6), path)
        d["h"] = np.maximum(d["o"], d["c"]); d["l"] = np.minimum(d["o"], d["c"])
        assert rv.engine.find_entry(d, dict(rv.engine.BASE, _k=0.62)) == []

    def test_condor_loss_bounded_by_width(self, rv):
        for d in synth_days(40, seed=5):
            r = rv.engine.simulate_day(d, dict(rv.engine.BASE, _k=0.62, signal="fixed", entry_min=150,
                                                structure="condor", short_mode="delta", short_delta=0.10,
                                                width=10.0, min_credit=0.05, profit_target=None, bias_filter=False))
            if r:
                assert r["pnl"] >= -(10.0 * 100) - 8 * 1.30 - 1e-6
                assert r["pnl"] <= r["credit"] * 100


# ---------------- analysis tools ----------------

class TestIsOos:
    def test_split_is_disjoint_and_complete(self, rv):
        days = synth_days(30, start=date(2022, 12, 1))
        tr = rv.engine.run(days, _k=0.62, signal="fixed", entry_min=60, structure="condor",
                           short_mode="delta", short_delta=0.15, min_credit=0.05, bias_filter=False)
        res = rv.analysis.is_oos(tr, days)
        assert res["IS"]["trades"] + res["OOS"]["trades"] == len(tr)
        assert res["IS"]["trades"] > 0 and res["OOS"]["trades"] > 0


class TestCoinflip:
    def test_deterministic_and_same_entry_minutes(self, rv):
        days = synth_days(60, seed=2)
        tr = rv.engine.run(days, _k=0.62, bias_filter=False, min_credit=0.30)
        if len(tr) < 5:
            pytest.skip("too few synthetic trades")
        a = rv.analysis.coinflip_benchmark(days, tr, n_draws=10, seed=1, _k=0.62, min_credit=0.30)
        b = rv.analysis.coinflip_benchmark(days, tr, n_draws=10, seed=1, _k=0.62, min_credit=0.30)
        assert a == b
        assert a["p5"] <= a["mean"] <= a["p95"]
        assert 0 <= a["signal_percentile"] <= 100

    def test_forced_side_is_used(self, rv):
        d = trend_day(date(2024, 4, 1))
        for side in ("put", "call"):
            r = rv.engine.simulate_day(d, dict(rv.engine.BASE, _k=0.62, forced={d["date"]: (60, side)}, min_credit=0.0))
            assert r is not None and r["side"] == side and r["entry_min"] == 60

    def test_empty(self, rv):
        assert rv.analysis.coinflip_benchmark([], [], n_draws=3)["draws"] == 0


class TestCostAttribution:
    def test_pieces_add_up_and_costs_are_negative(self, rv):
        days = synth_days(40, seed=4)
        ca = rv.analysis.cost_attribution(days, _k=0.62, signal="fixed", entry_min=90, structure="condor",
                                          short_mode="delta", short_delta=0.15, min_credit=0.05,
                                          profit_target=0.5, bias_filter=False)
        assert ca["commissions"] < 0
        assert ca["bid_ask"] <= 0
        assert abs(ca["gross_at_mid"] + ca["bid_ask"] + ca["stop_slippage"] + ca["commissions"] - ca["net"]) <= 3


class TestWalkForward:
    @staticmethod
    def fake(years_good):
        """Fake trade lists: setting 0 is good only before 2022, setting 1 only from 2022."""
        days = [dict(date=date(y, m, 1)) for y in range(2019, 2025) for m in range(1, 13)]

        def runner(p):
            out = []
            for d in days:
                good = d["date"].year in years_good[p["id"]]
                for k in range(3):
                    out.append(dict(date=d["date"], pnl=(50.0 if good else -50.0) + k * 7 + d["date"].month * 3, credit=1.0, risk=400.0))
            return out
        return days, runner

    def test_pick_uses_only_past_years(self, rv):
        days, runner = self.fake({0: {2019, 2020, 2021}, 1: {2022, 2023, 2024}})
        res = rv.analysis.walk_forward(days, [dict(id=0), dict(id=1)], range(2021, 2025), min_trades=10, runner=runner)
        picks = {p["year"]: p["pick"]["id"] for p in res["picks"]}
        # 2022 is chosen from 2019-2021, where only setting 0 was good -> it loses out of sample
        assert picks[2021] == 0 and picks[2022] == 0
        assert res["picks"][1]["oos_year"]["exp_per_trade"] < 0
        # the future never leaks: changing 2024 results does not change the 2022 pick
        days2, runner2 = self.fake({0: {2019, 2020, 2021}, 1: {2022, 2023}})
        res2 = rv.analysis.walk_forward(days2, [dict(id=0), dict(id=1)], range(2021, 2025), min_trades=10, runner=runner2)
        assert res2["picks"][1]["pick"]["id"] == picks[2022]

    def test_min_trades_respected(self, rv):
        days, runner = self.fake({0: {2019}, 1: set()})
        res = rv.analysis.walk_forward(days, [dict(id=0)], [2020], min_trades=10_000, runner=runner)
        assert res["picks"][0]["pick"] is None
        assert res["combined_oos"]["trades"] == 0
