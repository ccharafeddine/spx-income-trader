"""Tests for the dry-run 0DTE chain recorder: read-only, never orders."""
import ast
import sqlite3
from datetime import date, datetime, time
from pathlib import Path

import pytest
import pytz

from src.data.chain_recorder import (
    READ_ONLY_METHODS, ChainRecorder, ReadOnlyMarketData, RecorderConfig, parse_chain,
)

ET = pytz.timezone("US/Eastern")
ROOT = Path(__file__).resolve().parent.parent

ORDER_AND_ACCOUNT_METHODS = [
    "place_order", "replace_order", "cancel_order", "get_order", "get_orders_for_account",
    "get_orders_for_all_linked_accounts", "get_account", "get_accounts", "get_account_numbers",
    "get_transactions", "get_user_preferences", "place_spread_order", "close_spread",
    "session", "token_metadata", "_client",
]


def raw_chain(exp="2026-10-16", underlying=5800.0):
    def opt(sym, bid, ask, delta):
        return [{"symbol": sym, "bid": bid, "ask": ask, "last": (bid + ask) / 2, "mark": (bid + ask) / 2,
                 "bidSize": 10, "askSize": 12, "delta": delta, "gamma": 0.01, "theta": -1.0,
                 "vega": 0.2, "volatility": 14.5, "totalVolume": 100, "openInterest": 500,
                 "quoteTimeInLong": 1760630400000}]
    return {
        "underlyingPrice": underlying,
        "callExpDateMap": {f"{exp}:0": {"5800.0": opt("SPXW C5800", 4.0, 4.3, 0.5),
                                        "5850.0": opt("SPXW C5850", 0.10, 0.15, 0.05)}},
        "putExpDateMap": {f"{exp}:0": {"5800.0": opt("SPXW P5800", 4.4, 4.7, -0.5),
                                       "5750.0": opt("SPXW P5750", 0.20, 0.25, -0.06)},
                          "2026-10-17:1": {"5800.0": opt("SPXW P5800 next", 9.0, 9.5, -0.5)}},
    }


class Resp:
    def __init__(self, data): self._d = data
    def json(self): return self._d
    def raise_for_status(self): pass


class TrapClient:
    """Fake schwab client: quotes work, any order/account call fails the test."""
    def __init__(self, data=None, fail=False):
        self.data = data or raw_chain()
        self.fail = fail
        self.chain_calls = []

    def get_option_chain(self, symbol, **kw):
        self.chain_calls.append((symbol, kw))
        if self.fail:
            raise RuntimeError("HTTP 503")
        return Resp(self.data)

    def get_quote(self, symbol): return Resp({symbol: {"quote": {"lastPrice": 5800.0}}})

    def __getattr__(self, name):
        def trap(*a, **k):
            raise AssertionError(f"recorder called forbidden method {name}")
        return trap


def at(h, m, d=date(2026, 10, 16)):
    return ET.localize(datetime.combine(d, time(h, m)))


def make_rec(tmp_path, client=None, now=None, **cfg):
    return ChainRecorder(ReadOnlyMarketData(client or TrapClient()), RecorderConfig(**cfg),
                         db_path=tmp_path / "chains.db", now=now or (lambda: at(12, 0)),
                         is_trading_day=lambda d: d.weekday() < 5,
                         close_time=lambda d: time(16, 0))


# ---------------- read-only wrapper ----------------

class TestReadOnlyWrapper:
    def test_allowlist_is_quotes_only(self):
        assert READ_ONLY_METHODS == {"get_option_chain", "get_quote", "get_quotes"}

    @pytest.mark.parametrize("name", ORDER_AND_ACCOUNT_METHODS)
    def test_order_and_account_methods_raise(self, name):
        md = ReadOnlyMarketData(TrapClient())
        with pytest.raises(PermissionError):
            getattr(md, name)

    def test_allowed_methods_work(self):
        c = TrapClient()
        md = ReadOnlyMarketData(c)
        assert md.get_option_chain("$SPX").json()["underlyingPrice"] == 5800.0
        assert md.get_quote("$SPX").json()["$SPX"]["quote"]["lastPrice"] == 5800.0

    def test_no_path_back_to_client(self):
        md = ReadOnlyMarketData(TrapClient())
        for attr in ("__dict__", "place_order", "_ReadOnlyMarketData__client"):
            with pytest.raises(PermissionError):
                getattr(md, attr)
        assert set(dir(md)) <= READ_ONLY_METHODS
        assert not hasattr(md.get_option_chain, "__self__")
        with pytest.raises(PermissionError):
            md.place_order = lambda *a: None
        with pytest.raises(PermissionError):
            getattr(md, "client")

    def test_real_schwab_client_order_methods_blocked(self):
        schwab = pytest.importorskip("schwab")
        from schwab.client import Client
        order_methods = [m for m in dir(Client) if "order" in m.lower() or "account" in m.lower()]
        assert "place_order" in order_methods
        md = ReadOnlyMarketData(object.__new__(Client))
        for m in order_methods:
            with pytest.raises(PermissionError):
                getattr(md, m)

    def test_recorder_requires_wrapper(self, tmp_path):
        with pytest.raises(TypeError):
            ChainRecorder(TrapClient(), RecorderConfig(), db_path=tmp_path / "x.db")


class TestModuleHasNoOrderCode:
    @pytest.mark.parametrize("path", ["src/data/chain_recorder.py", "scripts/record_chains.py"])
    def test_no_order_calls_or_broker_imports(self, path):
        tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for bad in ("place_order", "replace_order", "cancel_order", "place_spread_order",
                    "close_spread", "SchwabBroker", "get_broker", "get_account_numbers",
                    "_get_account_hash", "account_number"):
            assert bad not in names, f"{path} references {bad}"
        imports = [getattr(n, "module", None) or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        assert not any(m.endswith("schwab_broker") or m.endswith("broker_factory") for m in imports)


# ---------------- parsing and storage ----------------

class TestParseAndStore:
    def test_parse_keeps_only_requested_expiration(self):
        rows = parse_chain(raw_chain(), "2026-10-16", at(12, 0))
        assert len(rows) == 4
        assert {r["put_call"] for r in rows} == {"C", "P"}
        p = next(r for r in rows if r["put_call"] == "P" and r["strike"] == 5800.0)
        assert (p["bid"], p["ask"], p["iv"], p["underlying"]) == (4.4, 4.7, 14.5, 5800.0)

    def test_missing_values_become_null(self):
        raw = raw_chain()
        raw["callExpDateMap"]["2026-10-16:0"]["5800.0"][0].update(delta=-999.0, volatility="NaN")
        r = next(x for x in parse_chain(raw, "2026-10-16", at(12, 0)) if x["put_call"] == "C" and x["strike"] == 5800)
        assert r["delta"] is None and r["iv"] is None

    def test_snapshot_writes_rows(self, tmp_path):
        client = TrapClient()
        rec = make_rec(tmp_path, client)
        assert rec.snapshot(at(12, 0)) == 4
        with sqlite3.connect(tmp_path / "chains.db") as con:
            assert con.execute("SELECT COUNT(*) FROM chain_snapshots").fetchone()[0] == 4
            assert con.execute("SELECT DISTINCT expiration FROM chain_snapshots").fetchall() == [("2026-10-16",)]
        sym, kw = client.chain_calls[0]
        assert sym == "$SPX" and kw["from_date"] == kw["to_date"] == date(2026, 10, 16)

    def test_api_error_is_logged_not_raised(self, tmp_path):
        rec = make_rec(tmp_path, TrapClient(fail=True))
        assert rec.snapshot(at(12, 0)) == 0
        with sqlite3.connect(tmp_path / "chains.db") as con:
            assert con.execute("SELECT COUNT(*) FROM chain_errors").fetchone()[0] == 1


# ---------------- schedule ----------------

class TestSchedule:
    def test_window(self, tmp_path):
        rec = make_rec(tmp_path)
        assert not rec.in_window(at(11, 44))
        assert rec.in_window(at(11, 45)) and rec.in_window(at(16, 0))
        assert not rec.in_window(at(16, 1))
        assert not rec.in_window(at(12, 0, d=date(2026, 10, 17)))  # Saturday

    def test_early_close_ends_window(self, tmp_path):
        rec = ChainRecorder(ReadOnlyMarketData(TrapClient()), RecorderConfig(), db_path=tmp_path / "c.db",
                            is_trading_day=lambda d: True, close_time=lambda d: time(13, 0))
        assert rec.in_window(at(12, 59)) and not rec.in_window(at(13, 1))

    def test_run_loop_records_until_end(self, tmp_path):
        clock = [at(15, 57)]
        def sleep(s):
            clock[0] = clock[0] + __import__("datetime").timedelta(seconds=s)
        rec = make_rec(tmp_path, now=lambda: clock[0])
        assert rec.run(sleep=sleep, max_iterations=50) == 4   # 15:57, 15:58, 15:59, 16:00

    def test_non_trading_day_returns(self, tmp_path):
        rec = make_rec(tmp_path, now=lambda: at(12, 0, d=date(2026, 10, 17)))
        assert rec.run(sleep=lambda s: None, max_iterations=5) == 0

    def test_config_parsing(self):
        c = RecorderConfig.from_dict({"interval_seconds": 5, "start": "11:30", "end": 960})
        assert c.interval_seconds == 15            # floor to protect rate limits
        assert c.start == time(11, 30) and c.end == time(16, 0)
        assert RecorderConfig.from_dict(None) == RecorderConfig()


class TestScript:
    def test_cli_overrides_config(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("record_chains", ROOT / "scripts" / "record_chains.py")
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        args = mod.parse_args(["--interval", "30", "--start", "12:00"])
        cfg = mod.resolve_config(args, {"chain_recorder": {"interval_seconds": 90, "end": "15:30"}})
        assert cfg.interval_seconds == 30 and cfg.start == time(12, 0) and cfg.end == time(15, 30)

    def test_example_config_section(self):
        import yaml
        ex = yaml.safe_load((ROOT / "config" / "strategy_params.example.yaml").read_text())
        cfg = RecorderConfig.from_dict(ex["chain_recorder"])
        assert cfg.start == time(11, 45) and cfg.end == time(16, 0) and cfg.interval_seconds == 60


class TestScriptAuthHandling:
    def _mod(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("record_chains", ROOT / "scripts" / "record_chains.py")
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        return mod

    def test_new_flags_parse(self):
        args = self._mod().parse_args(["--check", "--token-path", "x/tok.json"])
        assert args.check and args.token_path == "x/tok.json"

    def test_check_missing_token_exits_2(self, tmp_path):
        assert self._mod().main(["--check", "--token-path", str(tmp_path / "none.json")]) == 2

    def test_token_report_never_includes_contents(self, tmp_path):
        tok = tmp_path / "schwab_token.json"
        tok.write_text('{"token": {"refresh_token": "SECRET-VALUE"}}')
        mod = self._mod()
        line = mod.token_report({"valid": True, "hours_remaining": 10.0, "created_at": "a", "expires_at": "b"}, tok)
        assert "SECRET-VALUE" not in line and "10.0 h left" in line
        assert "not found" in mod.token_report({}, tmp_path / "missing.json")

    def test_unauthenticated_run_exits_2_without_traceback(self, tmp_path, monkeypatch):
        import src.data.chain_recorder as cr
        import src.utils.market_calendar as mc
        monkeypatch.setattr(mc, "is_trading_day", lambda d: True)
        def boom(token_path=None):
            raise RuntimeError("Schwab is not authenticated.")
        monkeypatch.setattr(cr, "build_read_only_schwab", boom)
        assert self._mod().main(["--token-path", str(tmp_path / "none.json"), "--db", str(tmp_path / "c.db")]) == 2
        assert not (tmp_path / "c.db").exists()

    def test_non_trading_day_exits_0_before_auth(self, tmp_path, monkeypatch):
        import src.data.chain_recorder as cr
        import src.utils.market_calendar as mc
        monkeypatch.setattr(mc, "is_trading_day", lambda d: False)
        def must_not_call(token_path=None):
            raise AssertionError("auth attempted on a non-trading day")
        monkeypatch.setattr(cr, "build_read_only_schwab", must_not_call)
        assert self._mod().main(["--token-path", str(tmp_path / "none.json")]) == 0
