"""
IronCondor0DTE: non-directional 0DTE SPX iron condor (paper signals only).

The candidate from docs/strategy_review/README.md: enter at 12:00 ET, short
strikes near 10 delta on each side, $10 wings, close when the cost to close
reaches 2x the credit, otherwise hold to settlement.

SAFETY / STATUS:
- Disabled by default (iron_condor.enabled: false). With the flag off,
  nothing runs.
- This module never places orders. It imports no broker or order code; it
  only reads an option chain dict that it is given and returns paper
  signals.
- With the flag on in dry-run, the bot logs paper entries and exits.
- With the flag on in live mode, it refuses to run (logs a warning). Live
  trading would need a separate, reviewed change, made only after the
  real-quote check passes.
- Circuit breakers and risk limits are untouched; this strategy does not
  consume them because it never trades.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_PAPER = "paper"
MODE_REFUSED = "refused_live"

TRADING_MINUTES_PER_YEAR = 390 * 252


@dataclass
class IronCondorConfig:
    enabled: bool = False
    entry_time: time = time(12, 0)
    entry_deadline: time = time(12, 30)   # don't open a paper trade after this
    short_delta: float = 0.10
    wing_width: float = 10.0
    stop_mult: Optional[float] = 2.0      # close when cost-to-close >= stop_mult x credit
    min_credit: float = 0.10              # total credit for the condor, per share
    commission_per_leg: float = 1.30

    @classmethod
    def from_params(cls, params: Optional[dict]) -> "IronCondorConfig":
        d = dict((params or {}).get("iron_condor") or {})
        c = cls()

        def _t(v, default):
            if v is None:
                return default
            if isinstance(v, time):
                return v
            if isinstance(v, int):
                return time(v // 60, v % 60)
            hh, mm = str(v).split(":")[:2]
            return time(int(hh), int(mm))

        return cls(
            enabled=d.get("enabled") is True,   # only a real boolean true enables it
            entry_time=_t(d.get("entry_time"), c.entry_time),
            entry_deadline=_t(d.get("entry_deadline"), c.entry_deadline),
            short_delta=float(d.get("short_delta", c.short_delta)),
            wing_width=float(d.get("wing_width", c.wing_width)),
            stop_mult=d.get("stop_mult", c.stop_mult),
            min_credit=float(d.get("min_credit", c.min_credit)),
            commission_per_leg=float(d.get("commission_per_leg", c.commission_per_leg)),
        )


@dataclass
class CondorLegs:
    short_put: float
    long_put: float
    short_call: float
    long_call: float


@dataclass
class PaperCondor:
    trade_date: date
    entry_time: datetime
    spx_at_entry: float
    legs: CondorLegs
    credit: float                         # natural credit per share
    status: str = "open"                  # open / stopped / settled
    exit_time: Optional[datetime] = None
    exit_cost: Optional[float] = None
    pnl: Optional[float] = None           # per 1 contract, after fees
    events: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(trade_date=self.trade_date.isoformat(), entry_time=self.entry_time.isoformat(),
                    spx_at_entry=self.spx_at_entry, short_put=self.legs.short_put, long_put=self.legs.long_put,
                    short_call=self.legs.short_call, long_call=self.legs.long_call, credit=self.credit,
                    status=self.status, exit_cost=self.exit_cost, pnl=self.pnl,
                    exit_time=self.exit_time.isoformat() if self.exit_time else None)


def resolve_mode(cfg: IronCondorConfig, dry_run: bool) -> str:
    if not cfg.enabled:
        return MODE_OFF
    if dry_run:
        return MODE_PAPER
    logger.warning("iron_condor.enabled is true but the bot is LIVE: IronCondor0DTE only runs as "
                   "paper signals in dry-run. Refusing to run; no orders will be placed.")
    return MODE_REFUSED


# ---------------------------------------------------------------------------
# Black-Scholes helpers (used only when the chain has no deltas)
# ---------------------------------------------------------------------------

def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs(S, K, T, sig, call):
    if T <= 0 or sig <= 0:
        return max(S - K, 0.0) if call else max(K - S, 0.0)
    d1 = (math.log(S / K) + 0.5 * sig * sig * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    return S * _ncdf(d1) - K * _ncdf(d2) if call else K * _ncdf(-d2) - S * _ncdf(-d1)


def implied_delta(S: float, K: float, price: float, minutes_left: float, call: bool) -> Optional[float]:
    """Delta from an option mid via implied vol (bisection). None if not solvable."""
    T = max(minutes_left, 1.0) / TRADING_MINUTES_PER_YEAR
    intrinsic = max(S - K, 0.0) if call else max(K - S, 0.0)
    if price <= intrinsic or price <= 0:
        return None
    lo, hi = 1e-4, 5.0
    if _bs(S, K, T, hi, call) < price:
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if _bs(S, K, T, mid, call) < price:
            lo = mid
        else:
            hi = mid
    sig = 0.5 * (lo + hi)
    d1 = (math.log(S / K) + 0.5 * sig * sig * T) / (sig * math.sqrt(T))
    return _ncdf(d1) if call else _ncdf(d1) - 1.0


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

class IronCondor0DTE:
    """Paper-only 0DTE iron condor. Feed it chains; it returns paper events."""

    def __init__(self, config: Optional[IronCondorConfig] = None, dry_run: bool = True):
        self.cfg = config or IronCondorConfig()
        self.mode = resolve_mode(self.cfg, dry_run)
        self.position: Optional[PaperCondor] = None
        self.history: List[PaperCondor] = []
        self._entered_on: Optional[date] = None

    @classmethod
    def from_params(cls, params: Optional[dict], dry_run: bool) -> "IronCondor0DTE":
        return cls(IronCondorConfig.from_params(params), dry_run=dry_run)

    @property
    def active(self) -> bool:
        return self.mode == MODE_PAPER

    # ---- strike selection --------------------------------------------------
    def _delta(self, chain, strike, side, spx, minutes_left):
        q = chain.get(strike) or {}
        d = q.get(f"{side}_delta")
        if d:
            return abs(float(d))
        bid, ask = q.get(f"{side}_bid") or 0, q.get(f"{side}_ask") or 0
        if bid <= 0 or ask <= 0:
            return None
        d = implied_delta(spx, strike, 0.5 * (bid + ask), minutes_left, side == "call")
        return None if d is None else abs(d)

    def select_strikes(self, chain: Dict[float, Dict], spx: float, minutes_left: float) -> Optional[CondorLegs]:
        strikes = sorted(chain)
        target = self.cfg.short_delta

        def pick(side, candidates):
            best = None
            for k in candidates:
                d = self._delta(chain, k, side, spx, minutes_left)
                if d is None:
                    continue
                if best is None or abs(d - target) < abs(best[1] - target):
                    best = (k, d)
            return best[0] if best else None

        sp = pick("put", [k for k in strikes if k < spx])
        sc = pick("call", [k for k in strikes if k > spx])
        if sp is None or sc is None:
            return None
        lp, lc = sp - self.cfg.wing_width, sc + self.cfg.wing_width
        if lp not in chain or lc not in chain:
            return None
        return CondorLegs(short_put=sp, long_put=lp, short_call=sc, long_call=lc)

    @staticmethod
    def natural_credit(chain, legs: CondorLegs) -> Optional[float]:
        try:
            parts = (chain[legs.short_put]["put_bid"], chain[legs.long_put]["put_ask"],
                     chain[legs.short_call]["call_bid"], chain[legs.long_call]["call_ask"])
        except KeyError:
            return None
        if any(p is None for p in parts) or parts[0] <= 0 or parts[2] <= 0:
            return None
        return round(parts[0] - parts[1] + parts[2] - parts[3], 2)

    @staticmethod
    def cost_to_close(chain, legs: CondorLegs) -> Optional[float]:
        """Buy back shorts at the ask, sell longs at the bid."""
        try:
            sp_ask = chain[legs.short_put]["put_ask"]; lp_bid = chain[legs.long_put]["put_bid"]
            sc_ask = chain[legs.short_call]["call_ask"]; lc_bid = chain[legs.long_call]["call_bid"]
        except KeyError:
            return None
        if not sp_ask or not sc_ask:
            return None
        return round(sp_ask - (lp_bid or 0) + sc_ask - (lc_bid or 0), 2)

    # ---- lifecycle -----------------------------------------------------------
    def on_tick(self, now: datetime, spx: float, chain: Optional[Dict[float, Dict]],
                close_time: time = time(16, 0)) -> Optional[dict]:
        """Advance the paper position. Returns an event dict (entry/stop) or None.

        Never places orders. Does nothing unless mode == 'paper'.
        """
        if not self.active or not chain or spx <= 0:
            return None
        t = now.time()
        minutes_left = (datetime.combine(now.date(), close_time) - datetime.combine(now.date(), t)).total_seconds() / 60

        if self.position is None:
            if self._entered_on == now.date() or not (self.cfg.entry_time <= t <= self.cfg.entry_deadline):
                return None
            legs = self.select_strikes(chain, spx, minutes_left)
            if legs is None:
                return None
            credit = self.natural_credit(chain, legs)
            if credit is None or credit < self.cfg.min_credit:
                return None
            self.position = PaperCondor(trade_date=now.date(), entry_time=now, spx_at_entry=spx,
                                        legs=legs, credit=credit)
            self._entered_on = now.date()
            ev = dict(event="iron_condor_paper_entry", **self.position.to_dict())
            logger.info(f"[PAPER] Iron condor entry {legs.long_put}/{legs.short_put}P "
                        f"{legs.short_call}/{legs.long_call}C credit ${credit:.2f} (no order placed)")
            return ev

        pos = self.position
        if pos.status != "open" or self.cfg.stop_mult is None:
            return None
        cost = self.cost_to_close(chain, pos.legs)
        if cost is not None and cost >= self.cfg.stop_mult * pos.credit:
            cost = min(cost, self.cfg.wing_width)
            pos.status, pos.exit_time, pos.exit_cost = "stopped", now, cost
            pos.pnl = round((pos.credit - cost) * 100 - 8 * self.cfg.commission_per_leg, 2)
            self._close()
            logger.info(f"[PAPER] Iron condor stop at ${cost:.2f}: P&L ${pos.pnl:+.2f}/contract (no order placed)")
            return dict(event="iron_condor_paper_stop", **pos.to_dict())
        return None

    def settle(self, spx_close: float, now: Optional[datetime] = None) -> Optional[dict]:
        """Cash-settle an open paper position at the official close."""
        pos = self.position
        if pos is None or pos.status != "open":
            return None
        L = pos.legs
        value = (max(L.short_put - spx_close, 0) - max(L.long_put - spx_close, 0)
                 + max(spx_close - L.short_call, 0) - max(spx_close - L.long_call, 0))
        pos.status, pos.exit_time, pos.exit_cost = "settled", now, round(value, 2)
        pos.pnl = round((pos.credit - value) * 100 - 4 * self.cfg.commission_per_leg, 2)
        self._close()
        logger.info(f"[PAPER] Iron condor settled at {spx_close:.2f}: P&L ${pos.pnl:+.2f}/contract")
        return dict(event="iron_condor_paper_settle", **pos.to_dict())

    def _close(self):
        self.history.append(self.position)
        self.position = None
