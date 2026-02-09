from typing import Optional, Tuple, Dict
from datetime import datetime, time
from datetime import time as dt_time
import logging
import pytz

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from config.settings import STRATEGY_PARAMS

from ..models.bar import Bar, BarType
from ..models.spread import CreditSpread, OptionLeg, TradeDirection
from ..models.trade import Trade
from .pulse_detector import PulseBarDetector

logger = logging.getLogger(__name__)


class SPXIncomeStrategy:
    """
    Main strategy engine for SPX Daily Income System
    
    Implements Production Line Trading rules:
    - Pulse bar detection (10% bars)
    - ATM credit spread construction
    - 80% profit target or hold to expiration
    """
    
    def __init__(
        self,
        pulse_threshold: float = 10.0,
        spread_width: float = 5.0,
        profit_target_pct: float = 80.0,
        min_credit: float = 1.00
    ):
        self.pulse_detector = PulseBarDetector(pulse_threshold)
        self.spread_width = spread_width
        self.profit_target = profit_target_pct / 100.0

        # Hard floor for credit received
        execution_params = STRATEGY_PARAMS.get('execution', {})
        self.min_credit = execution_params.get('min_credit', min_credit)

        self.tz = pytz.timezone("America/New_York")

        # Expected credit ranges based on moneyness
        self.credit_expectations = {
            "OTM": (2.40, 2.60),
            "ATM": (2.50, 2.80),
            "ITM": (2.80, 3.00)
        }

        # 1pm in-trade management settings (fully automated based on market conditions)
        monitoring = STRATEGY_PARAMS.get('monitoring', {})
        self.enable_1pm_check = monitoring.get('enable_1pm_check', True)
        self.auto_1pm_close = monitoring.get('auto_1pm_close', True)
        self.trending_threshold = monitoring.get('trending_threshold', 15.0)

        logger.info(f"SPXIncomeStrategy initialized: "
                   f"pulse={pulse_threshold}%, spread=${spread_width}, "
                   f"target={profit_target_pct}%, min_credit=${self.min_credit:.2f}")
        logger.info(f"1pm management: enabled={self.enable_1pm_check}, "
                   f"auto_close={self.auto_1pm_close}, "
                   f"trending_threshold=${self.trending_threshold}")
    
    def evaluate_setup(
        self,
        bar: Bar,
        current_price: float
    ) -> Optional[TradeDirection]:
        """
        Evaluate if bar creates a valid trading setup

        Args:
            bar: Completed 30-minute bar
            current_price: Current SPX price

        Returns:
            TradeDirection if valid setup, None otherwise
        """
        # NOTE: Window gating is handled by TradingBot._is_setup_window()
        # which supports both morning (9:30-11:30) and afternoon windows.
        # No redundant time filter here.

        # Analyze bar for pulse pattern
        bar_type = self.pulse_detector.analyze_bar(bar)
        
        if bar_type == BarType.BULLISH_PULSE:
            logger.info(f"BULLISH setup detected at {bar.timestamp.strftime('%H:%M')}")
            return TradeDirection.BULLISH
        elif bar_type == BarType.BEARISH_PULSE:
            logger.info(f"BEARISH setup detected at {bar.timestamp.strftime('%H:%M')}")
            return TradeDirection.BEARISH
        else:
            return None
    
    def _is_setup_window(self, dt: datetime) -> bool:
        """Check if time is within setup window (9:30-11:30 EST)"""
        morning_start = time(9, 30)
        morning_end = time(11, 30)
        
        if dt.tzinfo is None:
            dt = self.tz.localize(dt)
        else:
            dt = dt.astimezone(self.tz)
        
        current_time = dt.time()
        return morning_start <= current_time <= morning_end
    
    def construct_spread(
        self,
        current_price: float,
        direction: TradeDirection,
        options_chain: Dict[float, Dict]
    ) -> Optional[CreditSpread]:
        """
        Construct credit spread based on direction
        
        Args:
            current_price: Current SPX price
            direction: BULLISH (put spread) or BEARISH (call spread)
            options_chain: Options data {strike: {'call_bid': x, ...}}
            
        Returns:
            CreditSpread object if successful, None otherwise
        """
        try:
            # Select strikes (ATM)
            short_strike, long_strike = self._select_strikes(
                current_price, direction
            )
            
            logger.info(f"Selected strikes: ${short_strike} / ${long_strike}")
            
            # Verify strikes exist in chain
            if short_strike not in options_chain or long_strike not in options_chain:
                logger.error("Selected strikes not in options chain")
                return None
            
            # Get option prices
            if direction == TradeDirection.BULLISH:
                # Put credit spread
                option_type = 'put'
                short_price = options_chain[short_strike]['put_bid']
                long_price = options_chain[long_strike]['put_ask']
            else:
                # Call credit spread
                option_type = 'call'
                short_price = options_chain[short_strike]['call_bid']
                long_price = options_chain[long_strike]['call_ask']
            
            # Calculate net credit
            credit = short_price - long_price

            logger.info(f"Credit: ${credit:.2f} (short=${short_price:.2f}, long=${long_price:.2f})")

            # Hard floor check - reject spreads with insufficient credit
            if credit < self.min_credit:
                logger.warning(
                    f"Credit ${credit:.2f} is below minimum ${self.min_credit:.2f} - REJECTING spread"
                )
                return None

            # Validate credit against expectations
            if not self._validate_credit(credit, current_price, short_strike, direction):
                logger.warning(f"Credit ${credit:.2f} below expectations")

            # Create spread
            short_leg = OptionLeg(
                strike=short_strike,
                option_type=option_type,
                action='sell',
                price=short_price
            )
            
            long_leg = OptionLeg(
                strike=long_strike,
                option_type=option_type,
                action='buy',
                price=long_price
            )
            
            now = datetime.now(self.tz)
            expiration = now.replace(hour=16, minute=0, second=0, microsecond=0)
            
            spread = CreditSpread(
                direction=direction,
                short_leg=short_leg,
                long_leg=long_leg,
                credit_received=credit,
                entry_time=now,
                expiration=expiration,
                underlying_price_at_entry=current_price
            )
            
            logger.info(f"Spread constructed: {spread}")
            logger.info(f"  Max Profit: ${spread.max_profit:.2f}")
            logger.info(f"  Max Risk: ${spread.max_risk:.2f}")
            logger.info(f"  Breakeven: ${spread.breakeven:.2f}")
            
            return spread
            
        except Exception as e:
            logger.error(f"Failed to construct spread: {e}", exc_info=True)
            return None
    
    def _select_strikes(
        self,
        current_price: float,
        direction: TradeDirection
    ) -> Tuple[float, float]:
        """
        Select ATM credit spread strikes
        
        Returns: (short_strike, long_strike)
        """
        # Round to nearest $5 strike
        atm_strike = round(current_price / 5) * 5
        
        if direction == TradeDirection.BULLISH:
            # Put credit spread
            short_strike = atm_strike
            long_strike = short_strike - self.spread_width
        else:
            # Call credit spread
            short_strike = atm_strike
            long_strike = short_strike + self.spread_width
        
        return short_strike, long_strike
    
    def _validate_credit(
        self,
        credit: float,
        current_price: float,
        short_strike: float,
        direction: TradeDirection
    ) -> bool:
        """Validate credit meets expectations"""
        moneyness = self._get_moneyness(current_price, short_strike, direction)
        min_credit, max_credit = self.credit_expectations[moneyness]
        return credit >= (min_credit * 0.9)
    
    def _get_moneyness(
        self,
        current_price: float,
        short_strike: float,
        direction: TradeDirection
    ) -> str:
        """Determine if short strike is ITM, ATM, or OTM"""
        if direction == TradeDirection.BULLISH:
            if current_price < short_strike:
                return "ITM"
            elif abs(current_price - short_strike) <= 5:
                return "ATM"
            else:
                return "OTM"
        else:
            if current_price > short_strike:
                return "ITM"
            elif abs(current_price - short_strike) <= 5:
                return "ATM"
            else:
                return "OTM"
    
    def check_1pm_management(self, trade, current_price: float, current_time) -> Tuple[bool, str, dict]:
        """
        1pm ET in-trade management check per Production Line Trading strategy (p.19-20).

        Fully automated based on market conditions (not trader preference).

        At 1pm, evaluates:
        1. Is the day trending? (|move| >= threshold, typically $15)
        2. Is the move favorable to our position direction?
        3. What is our current P&L position?

        Decision rules:
        - RULE 1: Trending day (any direction) -> HOLD to expiration
        - RULE 2: Non-trending + favorable + profitable (>40%) -> Close for gain
        - RULE 3: Non-trending + unfavorable + losing (>10% loss) -> Close to limit loss
        - Otherwise: HOLD

        Returns (should_close, reason, assessment_dict)
        """
        check_start = dt_time(13, 0)
        check_end = dt_time(14, 0)
        current_time_only = current_time.time()

        if not (check_start <= current_time_only < check_end):
            return False, "", {}

        if not self.enable_1pm_check:
            return False, "", {}

        entry_price = trade.spread.underlying_price_at_entry
        if not entry_price or entry_price == 0:
            logger.warning("1pm check: No entry price available, skipping")
            return False, "", {}

        move = current_price - entry_price
        abs_move = abs(move)

        # Favorable = move in our trade's direction
        direction = trade.spread.direction
        if direction.value == 'bullish':
            favorable = move >= 0  # Put spread = want SPX up/flat
        else:
            favorable = move <= 0  # Call spread = want SPX down/flat

        is_trending = abs_move >= self.trending_threshold

        # Calculate current P&L directly from spread intrinsic value at current SPX price.
        # This is self-sufficient and does not depend on trade.pnl being set by monitor_positions().
        qty = trade.quantity or 1
        current_pnl = trade.spread.profit_at_price(current_price) * qty
        max_profit = trade.spread.max_profit * qty
        pnl_pct = (current_pnl / max_profit * 100) if max_profit > 0 else 0

        assessment = {
            'entry_price': round(entry_price, 2),
            'current_price': round(current_price, 2),
            'move': round(move, 2),
            'abs_move': round(abs_move, 2),
            'is_trending': is_trending,
            'is_favorable': favorable,
            'direction': direction.value,
            'pnl_pct': round(pnl_pct, 1),
            'auto_1pm_close': self.auto_1pm_close,
            'trending_threshold': self.trending_threshold,
        }

        should_close = False
        reason = ""

        # RULE 1: Trending day - always hold to expiration
        if is_trending:
            trend_dir = "UP" if move > 0 else "DOWN"
            fav_str = 'FAVORABLE' if favorable else 'UNFAVORABLE'
            reason = (
                f"1PM CHECK: TRENDING {trend_dir} {fav_str} "
                f"(SPX moved ${move:+.2f}, >${self.trending_threshold} threshold). "
                f"Action: HOLD TO EXPIRATION"
            )
        else:
            # Non-trending day - evaluate based on P&L
            if favorable:
                # Non-trending but position is profitable
                if pnl_pct >= 40:
                    # Sitting at 40%+ profit with no momentum - close for gain
                    if self.auto_1pm_close:
                        should_close = True
                        reason = (
                            f"1PM CHECK: NON-TRENDING FAVORABLE "
                            f"(SPX moved ${move:+.2f}, P&L {pnl_pct:.1f}%). "
                            f"Action: CLOSE for {pnl_pct:.1f}% profit (no momentum to reach target)"
                        )
                    else:
                        reason = (
                            f"1PM CHECK: NON-TRENDING FAVORABLE "
                            f"(SPX moved ${move:+.2f}, P&L {pnl_pct:.1f}%). "
                            f"Recommendation: Close for partial gain (auto_close=off)"
                        )
                else:
                    reason = (
                        f"1PM CHECK: NON-TRENDING FAVORABLE "
                        f"(SPX moved ${move:+.2f}, P&L {pnl_pct:.1f}%). "
                        f"Action: HOLD - profit not yet at 40% threshold"
                    )
            else:
                # Non-trending and unfavorable - position moving against us
                if pnl_pct < -10:
                    # Losing more than 10% - close to limit loss
                    if self.auto_1pm_close:
                        should_close = True
                        reason = (
                            f"1PM CHECK: NON-TRENDING UNFAVORABLE "
                            f"(SPX moved ${move:+.2f}, P&L {pnl_pct:.1f}%). "
                            f"Action: CLOSE to limit loss"
                        )
                    else:
                        reason = (
                            f"1PM CHECK: NON-TRENDING UNFAVORABLE "
                            f"(SPX moved ${move:+.2f}, P&L {pnl_pct:.1f}%). "
                            f"Recommendation: Close to limit loss (auto_close=off)"
                        )
                else:
                    reason = (
                        f"1PM CHECK: NON-TRENDING UNFAVORABLE "
                        f"(SPX moved ${move:+.2f}, P&L {pnl_pct:.1f}%). "
                        f"Action: HOLD - loss still manageable"
                    )

        logger.info(reason)
        assessment['decision'] = 'CLOSE_EARLY' if should_close else 'HOLD'
        assessment['reason'] = reason

        return should_close, reason, assessment

    def classify_credit_quality(self, credit_received: float, current_price: float, short_strike: float, direction) -> dict:
        """
        Classify credit quality per strategy expectations (page 20).
        OTM ~$2.40, ITM $2.50-$2.80, 2xITM $2.80-$3.00.
        """
        moneyness = self._get_moneyness(current_price, short_strike, direction)
        expected_range = self.credit_expectations.get(moneyness, (2.40, 2.80))
        expected_min, expected_max = expected_range

        if credit_received >= expected_min:
            quality = "GOOD" if credit_received <= expected_max * 1.1 else "ABOVE_EXPECTED"
        elif credit_received >= expected_min * 0.8:
            quality = "ACCEPTABLE"
        else:
            quality = "BELOW_EXPECTED"

        return {
            'quality': quality,
            'credit_received': round(credit_received, 2),
            'moneyness': moneyness,
            'expected_range': f"${expected_min:.2f}-${expected_max:.2f}",
            'expected_min': expected_min,
            'expected_max': expected_max,
            'pct_of_expected_min': round((credit_received / expected_min) * 100, 1) if expected_min > 0 else 0,
            'is_simulated_concern': credit_received < 1.50,
        }

    def should_exit(self, trade, current_spread_price: float, current_time) -> Tuple[bool, str]:
        """
        Determine if position should be exited.
        Checks: 1) 80% profit target, 2) 1pm trend management, 3) expiration.
        """
        # 1. Profit target
        max_profit = trade.spread.max_profit * trade.quantity
        current_profit = (trade.entry_price - current_spread_price) * 100 * trade.quantity
        profit_target = max_profit * self.profit_target

        if current_profit >= profit_target:
            return True, f"Profit target reached: ${current_profit:.2f} (target: ${profit_target:.2f})"

        # 2. 1pm management check (run once per trade per day)
        if self.enable_1pm_check and not getattr(trade, '_1pm_checked', False):
            current_spx = getattr(trade, '_current_spx_price', 0)
            if current_spx > 0:
                should_close, reason, assessment = self.check_1pm_management(
                    trade, current_spx, current_time
                )
                if assessment:  # Check was performed (in 1pm-2pm window)
                    trade._1pm_checked = True
                    trade._1pm_assessment = assessment
                    if should_close:
                        return True, reason

        # 3. Expiration
        if current_time >= trade.spread.expiration:
            return True, "Expiration reached (4:00 PM EST)"

        return False, ""