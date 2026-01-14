import logging
from typing import Dict, List, Optional
from datetime import datetime
import random
import uuid
import pytz

from .base import BrokerInterface
from ..models.spread import CreditSpread, TradeDirection

logger = logging.getLogger(__name__)


class PaperBroker(BrokerInterface):
    """
    Paper trading broker for testing without real money
    
    Simulates realistic bid-ask spreads, slippage, and order fills
    """
    
    def __init__(
        self,
        initial_balance: float = 50000.0,
        slippage: float = 0.05,
        commission_per_contract: float = 0.65
    ):
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.slippage = slippage
        self.commission = commission_per_contract
        
        # Track orders and positions
        self.orders = {}
        self.positions = {}
        self.order_counter = 0
        
        # Simulated market data
        self.current_spx_price = 5000.0
        self.last_price_update = datetime.now(pytz.timezone("America/New_York"))
        
        logger.info(f"PaperBroker initialized with ${initial_balance:,.2f}")
        logger.info(f"Slippage: ${slippage:.2f}, Commission: ${self.commission:.2f}/contract")
    
    def get_current_price(self, symbol: str) -> float:
        """Get current simulated SPX price"""
        if symbol in ["SPX", "$SPX.X"]:
            self._update_simulated_price()
            return self.current_spx_price
        else:
            raise ValueError(f"Unsupported symbol: {symbol}")
    
    def _update_simulated_price(self):
        """Update simulated SPX price with random walk"""
        now = datetime.now(pytz.timezone("America/New_York"))
        time_diff = (now - self.last_price_update).total_seconds() / 60
        
        if time_diff > 0:
            # Random walk: ~$1-3 per minute on average
            drift = random.uniform(-2, 2) * time_diff
            self.current_spx_price += drift
            self.last_price_update = now
    
    def get_options_chain(
        self,
        symbol: str,
        expiration: str
    ) -> Dict[float, Dict]:
        """Generate simulated options chain"""
        if symbol in ["SPX", "SPXW"]:
            current_price = self.get_current_price("SPX")
            
            # Generate strikes around current price
            strikes = []
            base_strike = round(current_price / 5) * 5
            for i in range(-20, 21):
                strikes.append(base_strike + (i * 5))
            
            options_chain = {}
            
            for strike in strikes:
                # Calculate theoretical option prices
                moneyness = abs(current_price - strike)
                time_value = 0.5 + (moneyness * 0.02)
                
                intrinsic_call = max(0, current_price - strike)
                intrinsic_put = max(0, strike - current_price)
                
                call_price = intrinsic_call + time_value
                put_price = intrinsic_put + time_value
                
                # Add bid-ask spread
                spread = 0.15
                
                options_chain[strike] = {
                    'call_bid': max(0.05, call_price - spread/2),
                    'call_ask': call_price + spread/2,
                    'put_bid': max(0.05, put_price - spread/2),
                    'put_ask': put_price + spread/2,
                    'call_volume': random.randint(100, 5000),
                    'put_volume': random.randint(100, 5000)
                }
            
            logger.debug(f"Generated options chain with {len(options_chain)} strikes")
            return options_chain
        
        else:
            raise ValueError(f"Unsupported symbol: {symbol}")
    
    def place_spread_order(
        self,
        spread: CreditSpread,
        quantity: int,
        limit_price: Optional[float] = None
    ) -> str:
        """Simulate placing a spread order"""
        self.order_counter += 1
        order_id = f"PAPER_{self.order_counter}_{uuid.uuid4().hex[:8]}"
        
        if limit_price is None:
            limit_price = spread.credit_received
        
        # Apply slippage
        slippage_amount = random.uniform(0, self.slippage)
        fill_price = max(0.05, limit_price - slippage_amount)
        
        # Calculate commission
        total_commission = self.commission * quantity * 2  # 2 legs
        
        # Credit received
        credit_received = (fill_price * 100 * quantity) - total_commission
        
        # Update balance
        self.balance += credit_received
        
        # Store order
        order = {
            'order_id': order_id,
            'type': 'spread',
            'direction': spread.direction.value,
            'short_strike': spread.short_leg.strike,
            'long_strike': spread.long_leg.strike,
            'quantity': quantity,
            'limit_price': limit_price,
            'fill_price': fill_price,
            'status': 'filled',
            'timestamp': datetime.now(pytz.timezone("America/New_York")),
            'commission': total_commission
        }
        
        self.orders[order_id] = order
        self.positions[order_id] = order.copy()
        
        logger.info(f"Paper order filled: {order_id}")
        logger.info(f"  Fill price: ${fill_price:.2f} (slippage: ${slippage_amount:.2f})")
        logger.info(f"  Credit received: ${credit_received:.2f}")
        logger.info(f"  New balance: ${self.balance:,.2f}")
        
        return order_id
    
    def get_order_status(self, order_id: str) -> Dict:
        """Get order status"""
        if order_id not in self.orders:
            return {
                'order_id': order_id,
                'status': 'not_found',
                'fill_price': 0,
                'filled_quantity': 0
            }
        
        order = self.orders[order_id]
        
        return {
            'order_id': order_id,
            'status': order['status'],
            'fill_price': order['fill_price'],
            'filled_quantity': order['quantity'],
            'timestamp': order['timestamp']
        }
    
    def close_spread(
        self,
        spread: CreditSpread,
        quantity: int,
        limit_price: float = 0.20
    ) -> str:
        """Simulate closing a spread"""
        self.order_counter += 1
        order_id = f"PAPER_CLOSE_{self.order_counter}_{uuid.uuid4().hex[:8]}"
        
        # Apply slippage
        slippage_amount = random.uniform(0, self.slippage * 1.5)
        fill_price = limit_price + slippage_amount
        
        # Calculate commission
        total_commission = self.commission * quantity * 2
        
        # Debit paid
        debit_paid = (fill_price * 100 * quantity) + total_commission
        
        # Update balance
        self.balance -= debit_paid
        
        # Store order
        order = {
            'order_id': order_id,
            'type': 'spread_close',
            'direction': spread.direction.value,
            'short_strike': spread.short_leg.strike,
            'long_strike': spread.long_leg.strike,
            'quantity': quantity,
            'limit_price': limit_price,
            'fill_price': fill_price,
            'status': 'filled',
            'timestamp': datetime.now(pytz.timezone("America/New_York")),
            'commission': total_commission
        }
        
        self.orders[order_id] = order
        
        # Remove from positions
        for pos_id, pos in list(self.positions.items()):
            if (pos['short_strike'] == spread.short_leg.strike and
                pos['long_strike'] == spread.long_leg.strike):
                del self.positions[pos_id]
                break
        
        logger.info(f"Paper close order filled: {order_id}")
        logger.info(f"  Fill price: ${fill_price:.2f}")
        logger.info(f"  Debit paid: ${debit_paid:.2f}")
        logger.info(f"  New balance: ${self.balance:,.2f}")
        
        return order_id
    
    def get_position_value(self, spread: CreditSpread) -> float:
        """Calculate current value of spread"""
        current_price = self.get_current_price("SPX")
        expiration = spread.expiration.strftime("%Y%m%d")
        
        try:
            chain = self.get_options_chain("SPX", expiration)
            
            if spread.direction == TradeDirection.BULLISH:
                short_price = chain[spread.short_leg.strike]['put_ask']
                long_price = chain[spread.long_leg.strike]['put_bid']
            else:
                short_price = chain[spread.short_leg.strike]['call_ask']
                long_price = chain[spread.long_leg.strike]['call_bid']
            
            spread_value = short_price - long_price
            return max(0.01, spread_value)
            
        except Exception as e:
            logger.error(f"Error calculating position value: {e}")
            return 0.20
    
    def get_account_balance(self) -> Dict:
        """Get account balance"""
        open_pnl = 0.0
        for pos_id, pos in self.positions.items():
            open_pnl += pos['fill_price'] * 100 * pos['quantity']
        
        return {
            'net_account_value': self.balance + open_pnl,
            'cash_available': self.balance,
            'buying_power': self.balance * 4,
            'open_positions': len(self.positions),
            'initial_balance': self.initial_balance,
            'total_pnl': (self.balance - self.initial_balance)
        }
    
    def get_positions(self) -> List[Dict]:
        """Get all open positions"""
        return list(self.positions.values())
    
    def reset(self):
        """Reset paper trading account"""
        self.balance = self.initial_balance
        self.orders = {}
        self.positions = {}
        self.order_counter = 0
        logger.info("Paper trading account reset")