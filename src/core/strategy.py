from typing import Optional, Tuple, Dict
from datetime import datetime, time
import logging
import pytz

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
        profit_target_pct: float = 80.0
    ):
        self.pulse_detector = PulseBarDetector(pulse_threshold)
        self.spread_width = spread_width
        self.profit_target = profit_target_pct / 100.0
        
        self.tz = pytz.timezone("America/New_York")
        
        # Expected credit ranges based on moneyness
        self.credit_expectations = {
            "OTM": (2.40, 2.60),
            "ATM": (2.50, 2.80),
            "ITM": (2.80, 3.00)
        }
        
        logger.info(f"SPXIncomeStrategy initialized: "
                   f"pulse={pulse_threshold}%, spread=${spread_width}, "
                   f"target={profit_target_pct}%")
    
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
        # Check time window (9:30 - 11:30 EST)
        if not self._is_setup_window(bar.timestamp):
            logger.debug("Outside setup window")
            return None
        
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
            
            # Validate credit
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
    
    def should_exit(
        self,
        trade: Trade,
        current_spread_price: float,
        current_time: datetime
    ) -> Tuple[bool, str]:
        """
        Determine if position should be exited
        
        Returns:
            (should_exit, reason)
        """
        # Check profit target (80% of max profit)
        max_profit = trade.spread.max_profit * trade.quantity
        current_profit = (trade.entry_price - current_spread_price) * 100 * trade.quantity
        profit_target = max_profit * self.profit_target
        
        if current_profit >= profit_target:
            return True, f"Profit target reached: ${current_profit:.2f} (target: ${profit_target:.2f})"
        
        # Check expiration
        if current_time >= trade.spread.expiration:
            return True, "Expiration reached (4:00 PM EST)"
        
        return False, ""