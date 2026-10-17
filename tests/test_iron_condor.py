"""IronCondor0DTE: off by default, paper-only in dry-run, refuses live, never orders."""
import ast
import logging
import math
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytz
import yaml

from src.core.iron_condor import (
    MODE_OFF, MODE_PAPER, MODE_REFUSED, CondorLegs, IronCondor0DTE, IronCondorConfig,
    implied_delta, resolve_mode,
)

ET = pytz.timezone("US/Eastern")
ROOT = Path(__file__).resolve().parent.parent
D = date(2026, 10, 16)


def at(h, m, d=D):
    return ET.localize(datetime.combine(d, time(h, m)))


def chain(spx=5800.0, with_delta=True, scale=1.0):
    """Simple chain: put/call prices fall off with distance from spx."""
    out = {}
    for k in range(5650, 5955, 5):
        k = float(k)
        dist = abs(k - spx)
        otm = max(0.05, 12.0 * math.exp(-dist / 18.0)) * scale
        put_mid = otm + max(k - spx, 0)
        call_mid = otm + max(spx - k, 0)
        delta = 0.5 * math.exp(-dist / 18.0)
        q = {"put_bid": round(put_mid - 0.05, 2), "put_ask": round(put_mid + 0.05, 2),
             "call_bid": round(call_mid - 0.05, 2), "call_ask": round(call_mid + 0.05, 2)}
        if with_delta:
            q["put_delta"] = -(delta if k <= spx else 1 - delta)
            q["call_delta"] = delta if k >= spx else 1 - delta
        out[k] = q
    return out


def paper():
    return IronCondor0DTE(IronCondorConfig(enabled=True), dry_run=True)


# ---------------- off by default / gating ----------------

class TestOffByDefault:
    def test_defaults_disabled(self):
        assert IronCondorConfig().enabled is False
        assert IronCondorConfig.from_params({}).enabled is False
        assert IronCondorConfig.from_params(None).enabled is False
        assert IronCondor0DTE().mode == MODE_OFF

    def test_example_config_disabled(self):
        ex = yaml.safe_load((ROOT / "config" / "strategy_params.example.yaml").read_text())
        assert ex["iron_condor"]["enabled"] is False
        assert IronCondor0DTE.from_params(ex, dry_run=True).mode == MODE_OFF

    @pytest.mark.parametrize("val", ["true", "yes", 1, "1", None])
    def test_only_boolean_true_enables(self, val):
        assert IronCondorConfig.from_params({"iron_condor": {"enabled": val}}).enabled is False

    def test_modes(self, caplog):
        on = IronCondorConfig(enabled=True)
        assert resolve_mode(IronCondorConfig(), dry_run=True) == MODE_OFF
        assert resolve_mode(IronCondorConfig(), dry_run=False) == MODE_OFF
        assert resolve_mode(on, dry_run=True) == MODE_PAPER
        with caplog.at_level(logging.WARNING):
            assert resolve_mode(on, dry_run=False) == MODE_REFUSED
        assert "Refusing" in caplog.text

    def test_disabled_does_nothing(self):
        ic = IronCondor0DTE()
        assert ic.on_tick(at(12, 0), 5800.0, chain()) is None
        assert ic.position is None and ic.history == []

    def test_live_refuses_even_in_window(self):
        ic = IronCondor0DTE(IronCondorConfig(enabled=True), dry_run=False)
        assert not ic.active
        assert ic.on_tick(at(12, 0), 5800.0, chain()) is None
        assert ic.position is None


class TestNoOrderCode:
    def test_module_has_no_order_or_broker_references(self):
        tree = ast.parse((ROOT / "src" / "core" / "iron_condor.py").read_text(encoding="utf-8"))
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for bad in ("place_order", "place_spread_order", "close_spread", "submit_order", "broker",
                    "get_broker", "SchwabBroker"):
            assert bad not in names
        mods = [getattr(n, "module", "") or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        mods += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not any("broker" in m for m in mods)


# ---------------- paper mechanics ----------------

class TestPaperMechanics:
    def test_strikes_near_target_delta_with_wings(self):
        ic = paper()
        legs = ic.select_strikes(chain(), 5800.0, 240)
        assert legs.short_put < 5800 < legs.short_call
        assert legs.long_put == legs.short_put - 10 and legs.long_call == legs.short_call + 10
        c = chain()
        assert abs(abs(c[legs.short_put]["put_delta"]) - 0.10) < 0.03
        assert abs(c[legs.short_call]["call_delta"] - 0.10) < 0.03

    def test_implied_delta_fallback_without_greeks(self):
        ic = paper()
        legs = ic.select_strikes(chain(with_delta=False), 5800.0, 240)
        assert legs is not None and legs.short_put < 5800 < legs.short_call

    def test_implied_delta_sane(self):
        d_atm = implied_delta(5800, 5800, 15.0, 240, call=True)
        d_otm = implied_delta(5800, 5850, 1.0, 240, call=True)
        assert 0.45 < d_atm < 0.56 and 0 < d_otm < d_atm
        assert implied_delta(5800, 5850, 0.0, 240, call=True) is None

    def test_entry_once_in_window(self):
        ic = paper()
        assert ic.on_tick(at(11, 59), 5800.0, chain()) is None
        ev = ic.on_tick(at(12, 0), 5800.0, chain())
        assert ev["event"] == "iron_condor_paper_entry" and ev["credit"] > 0
        assert ic.on_tick(at(12, 1), 5800.0, chain()) is None   # already open, no stop
        ic.settle(5800.0)
        assert ic.on_tick(at(12, 5), 5800.0, chain()) is None   # one per day

    def test_no_entry_after_deadline(self):
        assert paper().on_tick(at(12, 31), 5800.0, chain()) is None

    def test_min_credit(self):
        ic = IronCondor0DTE(IronCondorConfig(enabled=True, min_credit=50.0), dry_run=True)
        assert ic.on_tick(at(12, 0), 5800.0, chain()) is None

    def test_stop_at_twice_credit(self):
        ic = paper()
        ic.on_tick(at(12, 0), 5800.0, chain())
        credit = ic.position.credit
        ev = ic.on_tick(at(13, 0), 5835.0, chain(spx=5835.0))   # rally into the call side
        assert ev and ev["event"] == "iron_condor_paper_stop"
        assert ev["exit_cost"] >= 2 * credit
        assert ev["pnl"] == pytest.approx((credit - ev["exit_cost"]) * 100 - 8 * 1.30)
        assert ic.position is None

    @pytest.mark.parametrize("close,expected_value", [
        (5800.0, 0.0),                      # between shorts: keep the credit
        (5700.0, None),                     # put side partly/fully in the money
        (5950.0, 10.0),                     # beyond the long call: max loss = wing
    ])
    def test_settlement(self, close, expected_value):
        ic = paper()
        ic.position = None
        ic.on_tick(at(12, 0), 5800.0, chain())
        L = ic.position.legs
        credit = ic.position.credit
        ev = ic.settle(close)
        value = (max(L.short_put - close, 0) - max(L.long_put - close, 0)
                 + max(close - L.short_call, 0) - max(close - L.long_call, 0))
        if expected_value is not None:
            assert value == expected_value
        assert ev["pnl"] == pytest.approx((credit - value) * 100 - 4 * 1.30)
        assert -10.0 * 100 - 5.2 - 1e-9 <= ev["pnl"] <= credit * 100


# ---------------- bot wiring ----------------

class TestBotWiring:
    def _bot(self, ic, dry_run=True):
        from src.main import TradingBot
        bot = object.__new__(TradingBot)
        bot.iron_condor = ic
        bot.dry_run = dry_run
        bot._current_spx_price = 5800.0
        bot.broker = MagicMock()
        bot.broker._last_chain_source = "real"
        bot.broker.get_options_chain.return_value = chain()
        bot._log_signal = MagicMock()
        bot.price_feed = MagicMock()
        bot.price_feed.get_latest_price.return_value = 5800.0
        return bot

    @staticmethod
    def _assert_no_orders(broker):
        called = {c[0].split(".")[0] for c in broker.method_calls}
        assert called <= {"get_options_chain"}, called

    def test_paper_signal_logged_never_orders(self):
        bot = self._bot(paper())
        bot._run_iron_condor_paper(at(12, 0))
        bot._log_signal.assert_called_once()
        assert bot._log_signal.call_args[0][0] == "iron_condor_paper_entry"
        bot._settle_iron_condor_paper(at(16, 1))
        assert bot._log_signal.call_args[0][0] == "iron_condor_paper_settle"
        self._assert_no_orders(bot.broker)

    def test_disabled_never_touches_broker(self):
        for ic in (None, IronCondor0DTE()):
            bot = self._bot(ic)
            bot._run_iron_condor_paper(at(12, 0))
            bot._settle_iron_condor_paper(at(16, 1))
            assert bot.broker.method_calls == []
            bot._log_signal.assert_not_called()

    def test_live_bot_never_runs_condor(self):
        bot = self._bot(paper(), dry_run=False)
        bot._run_iron_condor_paper(at(12, 0))
        assert bot.broker.method_calls == []

    def test_missing_attribute_is_safe(self):
        from src.main import TradingBot
        bot = object.__new__(TradingBot)
        bot._run_iron_condor_paper(at(12, 0))
        bot._settle_iron_condor_paper(at(16, 1))

    def test_synthetic_chain_ignored(self):
        bot = self._bot(paper())
        bot.broker._last_chain_source = "synthetic"
        bot._run_iron_condor_paper(at(12, 0))
        bot._log_signal.assert_not_called()
        self._assert_no_orders(bot.broker)
