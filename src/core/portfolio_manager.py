"""
Portfolio Risk Manager

Coordinates all parallel strategies to prevent over-allocation
and manage total account risk across Daily Income, Tag 'n Turn, B&B, and ORB.

NOTE ON CIRCUIT BREAKER DESIGN:

The circuit breaker triggers on REALIZED losses only, not unrealized.

For 0DTE credit spreads, intraday drawdown doesn't necessarily mean
the trade will lose. Price can move against you mid-day but if it
comes back by expiration, you still profit. Cutting positions based
on unrealized P&L would lock in losses unnecessarily.

The circuit breaker's purpose is to prevent compounding losses by
stopping NEW entries after you've already realized losses today.
This prevents "revenge trading" or doubling down after a bad trade.

The percentage-based limit scales with account size:
- $50k account at 2% = $1000 max daily loss
- $100k account at 2% = $2000 max daily loss
"""

import json
import logging
import math
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class PositionSizingMethod(Enum):
    """Position sizing calculation methods"""
    PERCENT_RISK = "percent_risk"
    FIXED_CONTRACTS = "fixed_contracts"
    KELLY = "kelly"


class StrategyType(Enum):
    """Strategy identifiers"""
    DAILY_INCOME = "daily_income"
    TAG_N_TURN = "tag_n_turn"
    BNB = "bnb"
    ORB = "orb"


@dataclass
class PositionSlot:
    """Tracks a position slot for a strategy"""
    strategy: StrategyType
    position_id: str
    direction: str
    entry_time: str
    contracts: int
    max_risk: float
    is_0dte: bool = True

    def to_dict(self) -> dict:
        return {
            'strategy': self.strategy.value,
            'position_id': self.position_id,
            'direction': self.direction,
            'entry_time': self.entry_time,
            'contracts': self.contracts,
            'max_risk': self.max_risk,
            'is_0dte': self.is_0dte,
        }


class PortfolioManager:
    """
    Manages risk across all parallel strategies.

    Enforces:
    - Maximum total positions across all strategies
    - Maximum 0DTE positions (same-day expiration)
    - Maximum daily capital at risk
    - Strategy priority when conflicts arise
    - Daily P&L circuit breaker
    """

    def __init__(
        self,
        account_size: float = 50000.0,
        max_total_positions: int = 3,
        max_0dte_positions: int = 2,
        max_daily_risk_pct: float = 5.0,
        max_daily_loss_pct: float = 2.0,
        strategy_priority: Optional[List[str]] = None,
        persistence_path: Optional[Path] = None,
        # Position sizing parameters
        sizing_method: str = "percent_risk",
        risk_per_trade_pct: float = 2.0,
        min_contracts: int = 1,
        max_contracts: int = 20,
    ):
        self.account_size = account_size
        self.max_total_positions = max_total_positions
        self.max_0dte_positions = max_0dte_positions
        self.max_daily_risk_pct = max_daily_risk_pct
        self.max_daily_loss_pct = max_daily_loss_pct

        # Position sizing configuration
        try:
            self.sizing_method = PositionSizingMethod(sizing_method)
        except ValueError:
            logger.warning(f"Unknown sizing method '{sizing_method}', defaulting to percent_risk")
            self.sizing_method = PositionSizingMethod.PERCENT_RISK
        self.risk_per_trade_pct = risk_per_trade_pct
        self.min_contracts = min_contracts
        self.max_contracts = max_contracts

        # Calculate actual dollar limit from percentage
        self.max_daily_loss = self.account_size * (self.max_daily_loss_pct / 100)

        # Priority order (index 0 = highest priority)
        default_priority = [
            StrategyType.BNB,          # Honor overnight commitments first
            StrategyType.DAILY_INCOME, # Core strategy
            StrategyType.ORB,          # Secondary 0DTE
            StrategyType.TAG_N_TURN,   # Swing can wait
        ]
        if strategy_priority:
            self.strategy_priority = [StrategyType(s) for s in strategy_priority]
        else:
            self.strategy_priority = default_priority

        # State - tracks REALIZED P&L only (not unrealized)
        self.active_positions: Dict[str, PositionSlot] = {}
        self.daily_realized_pnl: float = 0.0
        self.daily_risk_used: float = 0.0
        self.trades_today: Dict[str, int] = {s.value: 0 for s in StrategyType}
        self.circuit_breaker_triggered: bool = False

        # Persistence
        if persistence_path:
            self.persistence_path = persistence_path
        else:
            self.persistence_path = Path(__file__).parent.parent.parent / 'database' / 'portfolio_state.json'

        # Load persisted state (for positions that survive restarts)
        self._load_state()

        logger.info(
            f"PortfolioManager initialized: "
            f"max_positions={max_total_positions}, "
            f"max_0dte={max_0dte_positions}, "
            f"max_risk={max_daily_risk_pct}%, "
            f"max_loss={max_daily_loss_pct}% (${self.max_daily_loss:.0f})"
        )
        logger.info(
            f"Position sizing: method={self.sizing_method.value}, "
            f"risk_per_trade={self.risk_per_trade_pct}%, "
            f"contracts={self.min_contracts}-{self.max_contracts}"
        )

    def _load_state(self):
        """Load persisted state from disk"""
        try:
            if not self.persistence_path.exists():
                return

            data = json.loads(self.persistence_path.read_text())

            # Only load swing positions (Tag 'n Turn) - 0DTE positions expire daily
            for pos_id, pos_data in data.get('active_positions', {}).items():
                if not pos_data.get('is_0dte', True):
                    self.active_positions[pos_id] = PositionSlot(
                        strategy=StrategyType(pos_data['strategy']),
                        position_id=pos_data['position_id'],
                        direction=pos_data['direction'],
                        entry_time=pos_data['entry_time'],
                        contracts=pos_data['contracts'],
                        max_risk=pos_data['max_risk'],
                        is_0dte=False,
                    )
                    self.daily_risk_used += pos_data['max_risk']
                    logger.info(f"Portfolio: Loaded persisted swing position: {pos_id}")

        except Exception as e:
            logger.error(f"Failed to load portfolio state: {e}")

    def _save_state(self):
        """Persist state to disk"""
        try:
            data = {
                'active_positions': {k: v.to_dict() for k, v in self.active_positions.items()},
                'daily_realized_pnl': self.daily_realized_pnl,
                'daily_risk_used': self.daily_risk_used,
                'trades_today': self.trades_today,
                'circuit_breaker_triggered': self.circuit_breaker_triggered,
                'last_updated': datetime.now().isoformat(),
            }
            self.persistence_path.parent.mkdir(exist_ok=True)
            self.persistence_path.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.error(f"Failed to save portfolio state: {e}")

    def can_enter_position(
        self,
        strategy: StrategyType,
        contracts: int,
        max_risk_per_contract: float,
        is_0dte: bool = True,
    ) -> Tuple[bool, str]:
        """
        Check if a new position is allowed given current portfolio state.

        Args:
            strategy: Which strategy is requesting entry
            contracts: Number of contracts
            max_risk_per_contract: Max risk per contract in dollars
            is_0dte: Whether this is a same-day expiration trade

        Returns:
            (allowed: bool, reason: str)
        """
        total_risk = contracts * max_risk_per_contract

        # Circuit breaker check
        if self.circuit_breaker_triggered:
            return False, "Circuit breaker active - max daily realized loss reached"

        # Check daily loss limit (REALIZED losses only)
        if self.daily_realized_pnl <= -self.max_daily_loss:
            self.circuit_breaker_triggered = True
            logger.warning(
                f"CIRCUIT BREAKER: Realized loss ${abs(self.daily_realized_pnl):.2f} "
                f"exceeds {self.max_daily_loss_pct}% limit (${self.max_daily_loss:.0f})"
            )
            self._save_state()
            return False, f"Daily realized loss limit ({self.max_daily_loss_pct}%) reached"

        # Check total position count
        if len(self.active_positions) >= self.max_total_positions:
            return False, f"Max total positions ({self.max_total_positions}) reached"

        # Check 0DTE limit
        if is_0dte:
            current_0dte = sum(1 for p in self.active_positions.values() if p.is_0dte)
            if current_0dte >= self.max_0dte_positions:
                return False, f"Max 0DTE positions ({self.max_0dte_positions}) reached"

        # Check daily risk limit
        max_daily_risk = self.account_size * (self.max_daily_risk_pct / 100)
        if self.daily_risk_used + total_risk > max_daily_risk:
            return False, f"Would exceed daily risk limit (${max_daily_risk:.0f})"

        return True, "Approved"

    def register_position(
        self,
        position_id: str,
        strategy: StrategyType,
        direction: str,
        contracts: int,
        max_risk: float,
        is_0dte: bool = True,
    ) -> bool:
        """
        Register a new position after entry is confirmed.

        Returns True if registered successfully.
        """
        if position_id in self.active_positions:
            logger.warning(f"Portfolio: Position {position_id} already registered")
            return False

        self.active_positions[position_id] = PositionSlot(
            strategy=strategy,
            position_id=position_id,
            direction=direction,
            entry_time=datetime.now().isoformat(),
            contracts=contracts,
            max_risk=max_risk,
            is_0dte=is_0dte,
        )

        self.daily_risk_used += max_risk
        self.trades_today[strategy.value] = self.trades_today.get(strategy.value, 0) + 1

        logger.info(
            f"Portfolio: Registered {strategy.value} position {position_id}, "
            f"direction={direction}, contracts={contracts}, "
            f"risk=${max_risk:.0f}, total_risk=${self.daily_risk_used:.0f}"
        )

        self._save_state()
        return True

    def close_position(self, position_id: str, realized_pnl: float) -> bool:
        """
        Close a position and update REALIZED P&L tracking.

        The circuit breaker only triggers on realized losses, not unrealized.
        This prevents cutting positions that might recover by EOD.

        Returns True if position was found and closed.
        """
        if position_id not in self.active_positions:
            logger.debug(f"Portfolio: Position {position_id} not found (may be external)")
            return False

        pos = self.active_positions.pop(position_id)
        self.daily_realized_pnl += realized_pnl

        logger.info(
            f"Portfolio: Closed {pos.strategy.value} position {position_id}, "
            f"Realized P&L=${realized_pnl:.2f}, "
            f"Daily realized=${self.daily_realized_pnl:.2f}"
        )

        # Circuit breaker on REALIZED losses only
        if self.daily_realized_pnl <= -self.max_daily_loss:
            self.circuit_breaker_triggered = True
            logger.warning(
                f"CIRCUIT BREAKER TRIGGERED: "
                f"Realized loss ${abs(self.daily_realized_pnl):.2f} "
                f"exceeds {self.max_daily_loss_pct}% limit (${self.max_daily_loss:.0f})"
            )

        self._save_state()
        return True

    def update_realized_pnl(self, pnl_change: float):
        """Update realized P&L from external source (position manager)"""
        self.daily_realized_pnl += pnl_change

        if self.daily_realized_pnl <= -self.max_daily_loss and not self.circuit_breaker_triggered:
            self.circuit_breaker_triggered = True
            logger.warning(
                f"CIRCUIT BREAKER TRIGGERED: "
                f"Realized loss ${abs(self.daily_realized_pnl):.2f} "
                f"exceeds {self.max_daily_loss_pct}% limit (${self.max_daily_loss:.0f})"
            )

        self._save_state()

    def get_positions_by_strategy(self, strategy: StrategyType) -> List[PositionSlot]:
        """Get all positions for a specific strategy"""
        return [p for p in self.active_positions.values() if p.strategy == strategy]

    def get_0dte_count(self) -> int:
        """Get count of current 0DTE positions"""
        return sum(1 for p in self.active_positions.values() if p.is_0dte)

    def get_swing_count(self) -> int:
        """Get count of current swing positions (non-0DTE)"""
        return sum(1 for p in self.active_positions.values() if not p.is_0dte)

    def has_position_for_strategy(self, strategy: StrategyType) -> bool:
        """Check if strategy already has an open position"""
        return any(p.strategy == strategy for p in self.active_positions.values())

    def reset_daily(self):
        """
        Reset daily counters at market open.

        - Clears daily realized P&L and risk tracking
        - Removes expired 0DTE positions (they should already be closed)
        - Keeps swing positions (Tag 'n Turn)
        - Resets circuit breaker
        """
        self.daily_realized_pnl = 0.0
        self.daily_risk_used = 0.0
        self.trades_today = {s.value: 0 for s in StrategyType}
        self.circuit_breaker_triggered = False

        # Remove any stale 0DTE positions (shouldn't exist if bot ran correctly)
        stale = [k for k, v in self.active_positions.items() if v.is_0dte]
        for pos_id in stale:
            logger.warning(f"Portfolio: Removing stale 0DTE position: {pos_id}")
            self.active_positions.pop(pos_id)

        # Recalculate risk from remaining swing positions
        for pos in self.active_positions.values():
            self.daily_risk_used += pos.max_risk

        self._save_state()
        logger.info(
            f"Portfolio: Daily reset complete. "
            f"Swing positions carried: {len(self.active_positions)}, "
            f"risk=${self.daily_risk_used:.0f}"
        )

    def get_status(self) -> dict:
        """Return status for dashboard/API"""
        max_daily_risk = self.account_size * (self.max_daily_risk_pct / 100)
        return {
            'active_positions': len(self.active_positions),
            'max_total_positions': self.max_total_positions,
            '0dte_positions': self.get_0dte_count(),
            'max_0dte_positions': self.max_0dte_positions,
            'swing_positions': self.get_swing_count(),
            'daily_risk_used': round(self.daily_risk_used, 2),
            'max_daily_risk': round(max_daily_risk, 2),
            'daily_realized_pnl': round(self.daily_realized_pnl, 2),
            'max_daily_loss_pct': self.max_daily_loss_pct,
            'max_daily_loss_dollars': round(self.max_daily_loss, 2),
            'circuit_breaker': self.circuit_breaker_triggered,
            'trades_today': self.trades_today.copy(),
            'positions': {k: v.to_dict() for k, v in self.active_positions.items()},
            'position_sizing': self.get_position_sizing_summary(),
        }

    def update_account_size(self, new_size: float):
        """Update account size and recalculate dollar limits"""
        self.account_size = new_size
        self.max_daily_loss = self.account_size * (self.max_daily_loss_pct / 100)
        logger.info(
            f"Account size updated to ${new_size:.2f}, "
            f"max_daily_loss now ${self.max_daily_loss:.0f}"
        )

    # =========================================================================
    # DYNAMIC POSITION SIZING
    # =========================================================================

    def calculate_position_size(
        self,
        strategy: StrategyType,
        max_risk_per_contract: float,
        fixed_contracts: Optional[int] = None,
    ) -> int:
        """
        Calculate position size based on configured method.

        Args:
            strategy: Strategy requesting position sizing
            max_risk_per_contract: Maximum risk per contract in dollars
            fixed_contracts: Override for fixed_contracts method (from strategy config)

        Returns:
            Number of contracts to trade (clamped to min/max)
        """
        if max_risk_per_contract <= 0:
            logger.warning("Invalid max_risk_per_contract, using minimum contracts")
            return self.min_contracts

        if self.sizing_method == PositionSizingMethod.PERCENT_RISK:
            contracts = self._calculate_percent_risk_size(max_risk_per_contract)
        elif self.sizing_method == PositionSizingMethod.KELLY:
            contracts = self._calculate_kelly_size(strategy, max_risk_per_contract)
        elif self.sizing_method == PositionSizingMethod.FIXED_CONTRACTS:
            contracts = self._get_fixed_contracts(strategy, fixed_contracts)
        else:
            contracts = self.min_contracts

        # Clamp to configured bounds
        contracts = max(self.min_contracts, min(contracts, self.max_contracts))

        logger.info(
            f"Position sizing ({self.sizing_method.value}): "
            f"{contracts} contracts for {strategy.value}, "
            f"risk_per_contract=${max_risk_per_contract:.2f}, "
            f"total_risk=${contracts * max_risk_per_contract:.2f}"
        )

        return contracts

    def _calculate_percent_risk_size(self, max_risk_per_contract: float) -> int:
        """
        Calculate contracts based on risking X% of account per trade.

        Formula: contracts = (account_size * risk_pct) / max_risk_per_contract

        Example:
            $50k account, 2% risk = $1000 risk budget
            $200 risk per contract = 5 contracts
        """
        risk_budget = self.account_size * (self.risk_per_trade_pct / 100)
        contracts = risk_budget / max_risk_per_contract
        return int(math.floor(contracts))

    def _calculate_kelly_size(
        self,
        strategy: StrategyType,
        max_risk_per_contract: float,
    ) -> int:
        """
        Calculate contracts using Kelly Criterion for optimal sizing.

        Kelly Formula: f* = (bp - q) / b
        Where:
            b = odds (avg_win / avg_loss)
            p = probability of winning (win_rate)
            q = probability of losing (1 - win_rate)

        We use half-Kelly for safety (divide result by 2).
        Falls back to percent_risk if insufficient trade history.
        """
        win_rate, avg_win, avg_loss, trade_count = self._get_historical_stats(strategy)

        # Need at least 10 trades for meaningful Kelly calculation
        if trade_count < 10:
            logger.debug(
                f"Kelly: Insufficient history ({trade_count} trades), "
                f"falling back to percent_risk"
            )
            return self._calculate_percent_risk_size(max_risk_per_contract)

        # Avoid division by zero
        if avg_loss <= 0 or avg_win <= 0:
            return self._calculate_percent_risk_size(max_risk_per_contract)

        # Kelly calculation
        b = avg_win / avg_loss  # Odds ratio
        p = win_rate
        q = 1 - win_rate

        kelly_fraction = (b * p - q) / b

        # Use half-Kelly for safety
        kelly_fraction = kelly_fraction / 2

        # Kelly can be negative (don't trade) or very high (too risky)
        if kelly_fraction <= 0:
            logger.warning(f"Kelly suggests no trade (negative edge)")
            return self.min_contracts

        # Cap Kelly at risk_per_trade_pct
        kelly_fraction = min(kelly_fraction, self.risk_per_trade_pct / 100)

        risk_budget = self.account_size * kelly_fraction
        contracts = risk_budget / max_risk_per_contract

        logger.debug(
            f"Kelly sizing: win_rate={win_rate:.1%}, "
            f"avg_win=${avg_win:.2f}, avg_loss=${avg_loss:.2f}, "
            f"half_kelly={kelly_fraction:.2%}, contracts={int(contracts)}"
        )

        return int(math.floor(contracts))

    def _get_fixed_contracts(
        self,
        strategy: StrategyType,
        fixed_contracts: Optional[int],
    ) -> int:
        """
        Return fixed contract count from strategy config.

        Falls back to min_contracts if not specified.
        """
        if fixed_contracts is not None and fixed_contracts > 0:
            return fixed_contracts
        return self.min_contracts

    def _get_historical_stats(
        self,
        strategy: StrategyType,
    ) -> Tuple[float, float, float, int]:
        """
        Get historical trading statistics for Kelly calculation.

        Returns:
            (win_rate, avg_win, avg_loss, trade_count)
        """
        try:
            # Import here to avoid circular imports
            from database.db_manager import DatabaseManager

            db_path = Path(__file__).parent.parent.parent / 'database' / 'trades.db'
            db = DatabaseManager(str(db_path))
            stats = db.get_strategy_stats(strategy.value)

            if not stats or stats.get('total_trades', 0) == 0:
                return (0.5, 0.0, 0.0, 0)

            win_rate = stats.get('win_rate', 0.5)
            avg_win = stats.get('avg_win', 0.0)
            avg_loss = abs(stats.get('avg_loss', 0.0))
            trade_count = stats.get('total_trades', 0)

            return (win_rate, avg_win, avg_loss, trade_count)

        except Exception as e:
            logger.debug(f"Could not load historical stats: {e}")
            return (0.5, 0.0, 0.0, 0)

    def get_position_sizing_summary(self) -> dict:
        """Return position sizing configuration for dashboard/API"""
        return {
            'method': self.sizing_method.value,
            'risk_per_trade_pct': self.risk_per_trade_pct,
            'min_contracts': self.min_contracts,
            'max_contracts': self.max_contracts,
            'account_size': self.account_size,
            'risk_budget_per_trade': round(
                self.account_size * (self.risk_per_trade_pct / 100), 2
            ),
        }
