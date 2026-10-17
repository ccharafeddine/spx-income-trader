"""Risk sizing: skip the trade instead of forcing min_contracts when one
contract's max loss exceeds the daily loss budget (risk gets tighter, never looser)."""
import ast
import random
from pathlib import Path

import pytest

from src.core.portfolio_manager import (
    INSUFFICIENT_BUYING_POWER, RISK_BUDGET_EXCEEDED, PortfolioManager, StrategyType,
)

ROOT = Path(__file__).resolve().parent.parent
DI = StrategyType.DAILY_INCOME


def pm(**kw):
    base = dict(account_size=46000, max_daily_loss_pct=2.0, daily_contracts=7, max_contracts=10,
                min_contracts=1, swing_contracts=2)
    base.update(kw)
    return PortfolioManager(**base)


class TestPortfolioManagerSizing:
    def test_skip_when_one_contract_exceeds_budget(self):
        p = pm()                                   # budget $920
        assert p.calculate_position_size(DI, 921.0) == 0
        assert p.last_sizing_reason == RISK_BUDGET_EXCEEDED

    def test_boundary_one_contract_fits(self):
        p = pm()
        assert p.calculate_position_size(DI, 920.0) == 1
        assert p.last_sizing_reason is None

    def test_min_contracts_not_forced_above_budget(self):
        p = pm(account_size=5000, min_contracts=3)  # budget $100
        assert p.calculate_position_size(DI, 400.0) == 0

    def test_reason_resets_on_next_call(self):
        p = pm()
        p.calculate_position_size(DI, 5000.0)
        assert p.last_sizing_reason == RISK_BUDGET_EXCEEDED
        assert p.calculate_position_size(DI, 400.0) == 2
        assert p.last_sizing_reason is None

    def test_buying_power_reason(self):
        p = pm()
        assert p.calculate_position_size(DI, 400.0, available_buying_power=100.0) == 0
        assert p.last_sizing_reason == INSUFFICIENT_BUYING_POWER

    def test_unchanged_when_budget_allows(self):
        assert pm(account_size=50000, max_daily_loss_pct=3.0).calculate_position_size(DI, 500) == 3
        assert pm(account_size=200000, max_daily_loss_pct=3.0).calculate_position_size(DI, 500) == 7

    def test_swing_strategies_unchanged(self):
        p = pm(account_size=1000)
        assert p.calculate_position_size(StrategyType.TAG_N_TURN, 5000.0) == 2

    def test_never_exceeds_budget_with_min_one(self):
        rng = random.Random(0)
        for _ in range(500):
            acct = rng.uniform(1000, 300000); pct = rng.choice([1.0, 2.0, 3.0]); risk = rng.uniform(50, 2000)
            p = pm(account_size=acct, max_daily_loss_pct=pct)
            n = p.calculate_position_size(DI, risk)
            assert n * risk <= acct * pct / 100 + 1e-6

    def test_display_shows_zero_when_skipping(self):
        p = pm(account_size=10000, spread_width=5.0)  # budget $200 < $500
        assert p.get_calculated_di_contracts() == (0, 7)
        assert p.get_position_sizing_summary()["di_contracts_display"] == "0 / 7"


class TestBacktestEngineSizing:
    def test_engine_skips_trade_over_budget(self):
        from tests.test_backtest_live_parity import BULL, _bars, _engine, _flat
        rows = [BULL, (5011.0, 5040.0, 5010.0, 5038.0)] + _flat(11, 5038)
        e = _engine(_bars(rows), max_daily_loss_pct=0.2)   # $100 budget
        e.run()
        assert [t for t in e.trades if t.strategy_type == 'daily_income'] == []
        assert e.risk_budget_skips == 1

    def test_engine_trades_when_budget_allows(self):
        from tests.test_backtest_live_parity import BULL, _bars, _engine, _flat
        rows = [BULL, (5011.0, 5040.0, 5010.0, 5038.0)] + _flat(11, 5038)
        e = _engine(_bars(rows))
        e.run()
        assert len([t for t in e.trades if t.strategy_type == 'daily_income']) == 1
        assert e.risk_budget_skips == 0

    def test_engine_size_function(self):
        from tests.test_backtest_live_parity import _bars, _engine, _flat
        e = _engine(_bars(_flat(13)), max_daily_loss_pct=0.2)
        assert e._calculate_di_position_size(400.0) == 0
        assert e._calculate_di_position_size(100.0) == 1


class TestBotRejection:
    def test_both_entry_paths_record_risk_budget_exceeded(self):
        src = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", "") == "_record_rejection"
                 and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant)
                 and n.args[1].value == "risk_budget_exceeded"]
        assert len(calls) == 2
