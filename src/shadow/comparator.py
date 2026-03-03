"""
Shadow Mode Comparator

Read-only Schwab broker that runs alongside the dry-run broker,
comparing pricing and chain data at trade entry/exit points.
Logs discrepancies to logs/shadow.jsonl without affecting execution.
"""

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

import pytz

logger = logging.getLogger(__name__)


class ShadowComparator:
    """Compares dry-run decisions against live Schwab market data.

    All comparisons run in background daemon threads so they never
    block the main trading loop. Every method is wrapped in try/except
    so shadow failures cannot crash the bot.
    """

    def __init__(self, strategy_params: dict):
        shadow_cfg = strategy_params.get('broker', {}).get('shadow', {})
        self._compare_interval_min = shadow_cfg.get('compare_interval_min', 15)
        self._last_periodic = 0.0  # monotonic timestamp of last periodic check
        self._tz = pytz.timezone('America/New_York')

        # Instantiate a read-only SchwabBroker using the same auth path
        # as broker_factory.py (lines 47-61)
        from src.brokers.schwab_broker import SchwabBroker
        from src.brokers.schwab_auth import SchwabAuth
        from config.settings import get_schwab_credentials

        schwab_cfg = strategy_params.get('broker', {}).get('schwab', {})
        creds = get_schwab_credentials()

        auth = SchwabAuth(
            app_key=creds.get('app_key', ''),
            app_secret=creds.get('app_secret', ''),
            callback_url=creds.get('callback_url', 'https://127.0.0.1'),
            token_path=schwab_cfg.get('token_path'),
        )
        self._schwab = SchwabBroker(config=schwab_cfg, auth=auth)

        # Open log file (append-only JSONL)
        from config.settings import BASE_DIR
        self._log_path = Path(BASE_DIR) / 'logs' / 'shadow.jsonl'
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_lock = threading.Lock()

        logger.info("ShadowComparator initialized (read-only Schwab comparison)")

    # ------------------------------------------------------------------
    # Public hooks (called from main.py)
    # ------------------------------------------------------------------

    def compare_at_entry(self, direction, current_price, dry_run_chain,
                         spread, quantity, expiration):
        """Compare entry data against Schwab. Runs in a background thread."""
        t = threading.Thread(
            target=self._do_entry_comparison,
            args=(direction, current_price, dry_run_chain,
                  spread, quantity, expiration),
            daemon=True,
        )
        t.start()

    def compare_at_exit(self, spread, dry_run_value):
        """Compare exit valuation against Schwab. Runs in a background thread."""
        t = threading.Thread(
            target=self._do_exit_comparison,
            args=(spread, dry_run_value),
            daemon=True,
        )
        t.start()

    def periodic_check(self):
        """Called every heartbeat (~5 min). Only runs at compare_interval_min."""
        now = time.monotonic()
        elapsed_min = (now - self._last_periodic) / 60.0
        if elapsed_min < self._compare_interval_min:
            return
        self._last_periodic = now
        t = threading.Thread(
            target=self._do_periodic_comparison,
            daemon=True,
        )
        t.start()

    # ------------------------------------------------------------------
    # Background comparison workers
    # ------------------------------------------------------------------

    def _do_entry_comparison(self, direction, current_price, dry_run_chain,
                             spread, quantity, expiration):
        try:
            # Normalize expiration to string
            if hasattr(expiration, 'strftime'):
                exp_str = expiration.strftime('%Y-%m-%d')
            else:
                exp_str = str(expiration)

            schwab_price = self._schwab.get_current_price('SPX')
            schwab_chain = self._schwab.get_options_chain('SPX', exp_str)

            # Dry-run side
            dry = {
                'spx_price': current_price,
                'chain_strikes': len(dry_run_chain) if dry_run_chain else 0,
                'short_strike': spread.short_leg.strike,
                'long_strike': spread.long_leg.strike,
                'credit': spread.credit_received,
                'quantity': quantity,
                'direction': direction.value if hasattr(direction, 'value') else str(direction),
            }

            # Schwab side
            short_available = spread.short_leg.strike in schwab_chain if schwab_chain else False
            long_available = spread.long_leg.strike in schwab_chain if schwab_chain else False
            schwab_credit = self._calc_schwab_credit(
                schwab_chain, spread) if (short_available and long_available) else None

            schwab_data = {
                'spx_price': schwab_price,
                'chain_strikes': len(schwab_chain) if schwab_chain else 0,
                'short_strike_available': short_available,
                'long_strike_available': long_available,
                'credit': schwab_credit,
            }

            # Divergence
            price_diff = abs(schwab_price - current_price) if schwab_price else None
            price_pct = (price_diff / current_price * 100) if (price_diff and current_price) else None
            credit_diff = abs(schwab_credit - spread.credit_received) if schwab_credit else None
            credit_pct = (credit_diff / spread.credit_received * 100) if (credit_diff and spread.credit_received) else None

            same_trade_possible = short_available and long_available
            decision_would_differ = (
                not same_trade_possible
                or (credit_pct is not None and credit_pct > 20)
            )

            divergence = {
                'price_pts': round(price_diff, 2) if price_diff is not None else None,
                'price_pct': round(price_pct, 4) if price_pct is not None else None,
                'credit_diff': round(credit_diff, 2) if credit_diff is not None else None,
                'credit_pct': round(credit_pct, 1) if credit_pct is not None else None,
                'same_trade_possible': same_trade_possible,
                'decision_would_differ': decision_would_differ,
            }

            self._write_log('entry_comparison', dry, schwab_data, divergence)

            if decision_would_differ:
                logger.warning(
                    f"Shadow: entry would DIFFER - "
                    f"strikes available={same_trade_possible}, "
                    f"credit divergence={credit_pct:.1f}%" if credit_pct else
                    f"Shadow: entry would DIFFER - strikes not in Schwab chain"
                )
            else:
                logger.info(
                    f"Shadow: entry matches - price diff={price_diff:.2f}pts, "
                    f"credit diff={credit_diff:.2f}" if credit_diff is not None else
                    f"Shadow: entry matches - price diff={price_diff:.2f}pts"
                )

        except Exception as e:
            logger.warning(f"Shadow entry comparison failed: {e}")

    def _do_exit_comparison(self, spread, dry_run_value):
        try:
            schwab_value = self._schwab.get_position_value(spread)

            dry = {
                'short_strike': spread.short_leg.strike,
                'long_strike': spread.long_leg.strike,
                'value': dry_run_value,
            }

            schwab_data = {
                'value': schwab_value,
            }

            value_diff = abs(schwab_value - dry_run_value) if schwab_value else None
            value_pct = (value_diff / dry_run_value * 100) if (value_diff and dry_run_value) else None

            divergence = {
                'value_diff': round(value_diff, 4) if value_diff is not None else None,
                'value_pct': round(value_pct, 1) if value_pct is not None else None,
            }

            self._write_log('exit_comparison', dry, schwab_data, divergence)

            logger.info(
                f"Shadow: exit comparison - dry={dry_run_value:.4f}, "
                f"schwab={schwab_value:.4f}, diff={value_diff:.4f}" if value_diff is not None else
                f"Shadow: exit comparison - dry={dry_run_value:.4f}, schwab unavailable"
            )

        except Exception as e:
            logger.warning(f"Shadow exit comparison failed: {e}")

    def _do_periodic_comparison(self):
        try:
            schwab_price = self._schwab.get_current_price('SPX')

            schwab_balance = None
            try:
                bal = self._schwab.get_account_balance()
                schwab_balance = bal.get('net_account_value')
            except Exception:
                pass

            dry = {
                'check_type': 'periodic',
            }

            schwab_data = {
                'spx_price': schwab_price,
                'account_balance': schwab_balance,
            }

            divergence = {}

            self._write_log('periodic_comparison', dry, schwab_data, divergence)

            logger.info(
                f"Shadow: periodic check - SPX=${schwab_price:,.2f}"
                + (f", balance=${schwab_balance:,.2f}" if schwab_balance else "")
            )

        except Exception as e:
            logger.warning(f"Shadow periodic comparison failed: {e}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _calc_schwab_credit(self, chain, spread):
        """Calculate the mid-price credit from Schwab chain for the same spread."""
        from src.models.spread import TradeDirection

        short_strike = spread.short_leg.strike
        long_strike = spread.long_leg.strike

        if short_strike not in chain or long_strike not in chain:
            return None

        if spread.direction == TradeDirection.BULLISH:
            short_mid = (chain[short_strike]['put_bid'] + chain[short_strike]['put_ask']) / 2
            long_mid = (chain[long_strike]['put_bid'] + chain[long_strike]['put_ask']) / 2
        else:
            short_mid = (chain[short_strike]['call_bid'] + chain[short_strike]['call_ask']) / 2
            long_mid = (chain[long_strike]['call_bid'] + chain[long_strike]['call_ask']) / 2

        # Credit = what we receive selling short - what we pay buying long
        credit = short_mid - long_mid
        return round(credit, 2) if credit > 0 else 0.0

    def _write_log(self, record_type, dry_run_data, schwab_data, divergence):
        """Append a JSONL record to shadow.jsonl (thread-safe)."""
        record = {
            'ts': datetime.now(self._tz).isoformat(),
            'type': record_type,
            'dry_run': dry_run_data,
            'schwab': schwab_data,
            'divergence': divergence,
        }
        line = json.dumps(record, default=str) + '\n'
        with self._log_lock:
            with open(self._log_path, 'a') as f:
                f.write(line)
