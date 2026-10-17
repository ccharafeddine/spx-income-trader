"""
Dry-run SPXW 0DTE option-chain recorder (read-only market data).

Purpose: collect real 0DTE SPX bid/ask snapshots so backtests can be checked
against real quotes (see docs/strategy_review/README.md). It never trades.

SAFETY:
- The recorder only ever receives a ReadOnlyMarketData wrapper. The wrapper
  exposes an explicit allowlist of quote endpoints (get_option_chain,
  get_quote, get_quotes). Any other attribute, including every order and
  account endpoint, raises PermissionError.
- This module does not import SchwabBroker or any order code, and does not
  look up account numbers or account hashes.
- Data goes to a separate SQLite file (default database/chain_snapshots.db),
  not the trades database.
"""

from __future__ import annotations

import logging
import sqlite3
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

import pytz

logger = logging.getLogger(__name__)

ET = pytz.timezone("US/Eastern")

# Only these client methods can be reached through the wrapper.
READ_ONLY_METHODS = frozenset({"get_option_chain", "get_quote", "get_quotes"})


def _plain(fn, name):
    """Wrap a bound method so the result has no __self__ pointing at the client."""
    def call(*args, **kwargs):
        return fn(*args, **kwargs)
    call.__name__ = name
    return call


class ReadOnlyViolation(PermissionError):
    """Raised when anything other than a quote endpoint is requested."""


class ReadOnlyMarketData:
    """Wrap a market-data client and expose only quote endpoints.

    Example: ``ReadOnlyMarketData(schwab_client).get_option_chain(...)`` works;
    ``.place_order(...)``, ``.get_account(...)``, ``.cancel_order(...)``, etc.
    raise PermissionError. This guards against accidental use in this code
    path; it is not a sandbox against deliberately malicious Python.
    """

    __slots__ = ("__methods",)

    def __init__(self, client):
        # Bind only the allowed methods; the client object itself is not kept,
        # so there is no attribute path back to order endpoints.
        methods = {}
        for name in READ_ONLY_METHODS:
            fn = getattr(client, name, None)
            if callable(fn):
                methods[name] = _plain(fn, name)
        object.__setattr__(self, "_ReadOnlyMarketData__methods", methods)

    def __getattr__(self, name):
        methods = object.__getattribute__(self, "_ReadOnlyMarketData__methods")
        if name in methods:
            return methods[name]
        raise ReadOnlyViolation(
            f"ReadOnlyMarketData: '{name}' is not allowed (read-only quotes only: "
            f"{', '.join(sorted(READ_ONLY_METHODS))})"
        )

    def __setattr__(self, name, value):
        raise ReadOnlyViolation("ReadOnlyMarketData is immutable")

    def __dir__(self):
        return sorted(object.__getattribute__(self, "_ReadOnlyMarketData__methods"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS chain_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    ts_et TEXT NOT NULL,
    expiration TEXT NOT NULL,
    underlying REAL,
    strike REAL NOT NULL,
    put_call TEXT NOT NULL,
    symbol TEXT,
    bid REAL, ask REAL, last REAL, mark REAL,
    bid_size INTEGER, ask_size INTEGER,
    delta REAL, gamma REAL, theta REAL, vega REAL, iv REAL,
    volume INTEGER, open_interest INTEGER,
    quote_time_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_chain_ts ON chain_snapshots (ts_utc);
CREATE INDEX IF NOT EXISTS idx_chain_exp_strike ON chain_snapshots (expiration, strike, put_call);
CREATE TABLE IF NOT EXISTS chain_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc TEXT NOT NULL,
    error TEXT NOT NULL
);
"""

COLUMNS = ("ts_utc", "ts_et", "expiration", "underlying", "strike", "put_call", "symbol",
           "bid", "ask", "last", "mark", "bid_size", "ask_size", "delta", "gamma", "theta",
           "vega", "iv", "volume", "open_interest", "quote_time_ms")


def _f(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    # Schwab uses -999 / NaN for "not available"
    return None if x != x or x <= -999 else x


def _i(v) -> Optional[int]:
    x = _f(v)
    return None if x is None else int(x)


def parse_chain(raw: dict, expiration: str, ts: datetime) -> List[dict]:
    """Flatten a Schwab option-chain response into rows for one expiration."""
    ts_utc = ts.astimezone(pytz.utc)
    underlying = _f(raw.get("underlyingPrice")) or _f((raw.get("underlying") or {}).get("last"))
    rows = []
    for side, key in (("C", "callExpDateMap"), ("P", "putExpDateMap")):
        for exp_key, strikes in (raw.get(key) or {}).items():
            if not str(exp_key).startswith(expiration):
                continue
            for strike_str, opts in (strikes or {}).items():
                opt = opts[0] if isinstance(opts, list) and opts else (opts or {})
                strike = _f(strike_str)
                if not strike:
                    continue
                rows.append(dict(
                    ts_utc=ts_utc.isoformat(), ts_et=ts.astimezone(ET).isoformat(),
                    expiration=expiration, underlying=underlying, strike=strike, put_call=side,
                    symbol=opt.get("symbol"), bid=_f(opt.get("bid")), ask=_f(opt.get("ask")),
                    last=_f(opt.get("last")), mark=_f(opt.get("mark")),
                    bid_size=_i(opt.get("bidSize")), ask_size=_i(opt.get("askSize")),
                    delta=_f(opt.get("delta")), gamma=_f(opt.get("gamma")),
                    theta=_f(opt.get("theta")), vega=_f(opt.get("vega")),
                    iv=_f(opt.get("volatility")), volume=_i(opt.get("totalVolume")),
                    open_interest=_i(opt.get("openInterest")),
                    quote_time_ms=_i(opt.get("quoteTimeInLong")),
                ))
    return rows


@dataclass
class RecorderConfig:
    symbol: str = "$SPX"
    interval_seconds: int = 60
    start: time = time(11, 45)
    end: time = time(16, 0)
    strike_count: int = 80
    db_path: str = "database/chain_snapshots.db"

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "RecorderConfig":
        d = d or {}

        def _t(v, default):
            if v is None:
                return default
            if isinstance(v, time):
                return v
            if isinstance(v, int):  # YAML may parse 11:45 as sexagesimal minutes
                return time(v // 60, v % 60)
            hh, mm = str(v).split(":")[:2]
            return time(int(hh), int(mm))

        c = cls()
        return cls(
            symbol=d.get("symbol", c.symbol),
            interval_seconds=max(15, int(d.get("interval_seconds", c.interval_seconds))),
            start=_t(d.get("start"), c.start),
            end=_t(d.get("end"), c.end),
            strike_count=int(d.get("strike_count", c.strike_count)),
            db_path=d.get("db_path", c.db_path),
        )


class ChainRecorder:
    """Poll the 0DTE chain on a schedule and store every quote in SQLite."""

    def __init__(self, market_data: ReadOnlyMarketData, config: RecorderConfig,
                 db_path: Optional[Path] = None,
                 now: Callable[[], datetime] = lambda: datetime.now(ET),
                 is_trading_day: Optional[Callable[[date], bool]] = None,
                 close_time: Optional[Callable[[date], time]] = None):
        if not isinstance(market_data, ReadOnlyMarketData):
            raise TypeError("ChainRecorder requires a ReadOnlyMarketData wrapper")
        self.md = market_data
        self.cfg = config
        self.db_path = Path(db_path or config.db_path)
        self.now = now
        if is_trading_day is None or close_time is None:
            from src.utils.market_calendar import is_trading_day as _itd, get_market_close_time as _gct
            is_trading_day = is_trading_day or _itd
            close_time = close_time or _gct
        self.is_trading_day = is_trading_day
        self.close_time = close_time
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as con:
            con.executescript(SCHEMA)

    # ---- schedule -------------------------------------------------------
    def in_window(self, ts: datetime) -> bool:
        ts = ts.astimezone(ET)
        d = ts.date()
        if not self.is_trading_day(d):
            return False
        end = min(self.cfg.end, self.close_time(d))
        return self.cfg.start <= ts.time() <= end

    # ---- one snapshot ---------------------------------------------------
    def fetch_raw(self, expiration: date) -> dict:
        kwargs = dict(from_date=expiration, to_date=expiration, strike_count=self.cfg.strike_count)
        try:
            from schwab.client import Client
            kwargs["contract_type"] = Client.Options.ContractType.ALL
            kwargs["include_underlying_quote"] = True
        except ImportError:  # tests / environments without schwab-py
            pass
        resp = self.md.get_option_chain(self.cfg.symbol, **kwargs)
        if hasattr(resp, "raise_for_status"):
            resp.raise_for_status()
        return resp.json() if hasattr(resp, "json") else resp

    def snapshot(self, ts: Optional[datetime] = None) -> int:
        """Record one snapshot of today's (0DTE) expiration. Returns rows written."""
        ts = ts or self.now()
        exp = ts.astimezone(ET).date()
        try:
            raw = self.fetch_raw(exp)
            rows = parse_chain(raw, exp.isoformat(), ts)
        except Exception as e:  # keep recording on transient API errors
            logger.warning(f"Chain snapshot failed: {e}")
            with sqlite3.connect(self.db_path) as con:
                con.execute("INSERT INTO chain_errors (ts_utc, error) VALUES (?, ?)",
                            (ts.astimezone(pytz.utc).isoformat(), str(e)[:500]))
            return 0
        if rows:
            with sqlite3.connect(self.db_path) as con:
                con.executemany(
                    f"INSERT INTO chain_snapshots ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                    [tuple(r[c] for c in COLUMNS) for r in rows])
        logger.info(f"Recorded {len(rows)} quotes for {exp} at {ts.astimezone(ET):%H:%M:%S} ET")
        return len(rows)

    # ---- loop -----------------------------------------------------------
    def run(self, once: bool = False, sleep: Callable[[float], None] = _time.sleep,
            max_iterations: Optional[int] = None) -> int:
        """Record every interval inside the window until the window ends today.

        Before the window opens it waits; after it closes (or on a non-trading
        day) it returns. Returns the number of snapshots taken.
        """
        taken = 0
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            iterations += 1
            ts = self.now()
            if once:
                self.snapshot(ts)
                return 1
            d = ts.astimezone(ET).date()
            if not self.is_trading_day(d):
                logger.info(f"{d} is not a trading day; nothing to record")
                return taken
            end = min(self.cfg.end, self.close_time(d))
            if ts.astimezone(ET).time() > end:
                logger.info("Recording window is over for today")
                return taken
            if self.in_window(ts):
                self.snapshot(ts)
                taken += 1
            sleep(self.cfg.interval_seconds)
        return taken


def build_read_only_schwab(token_path: Optional[str] = None) -> ReadOnlyMarketData:
    """Create a read-only market-data client from the bot's existing Schwab OAuth.

    Uses the same app key/secret (keyring first, then local config) and the same
    token file as the bot (database/schwab_token.json by default). It does not
    read the account number and does not create a broker.
    """
    from config.settings import get_schwab_credentials
    from src.brokers.schwab_auth import SchwabAuth

    creds = get_schwab_credentials()
    auth = SchwabAuth(app_key=creds.get("app_key", ""), app_secret=creds.get("app_secret", ""),
                      callback_url=creds.get("callback_url", "https://127.0.0.1"),
                      token_path=token_path)
    if not auth.is_authenticated():
        raise RuntimeError(
            "Schwab is not authenticated. Connect Schwab in the app (or run "
            "`python -m src.brokers.schwab_auth`) to create the token file, then retry.")
    return ReadOnlyMarketData(auth.get_client())
