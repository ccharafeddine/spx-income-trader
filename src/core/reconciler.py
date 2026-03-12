"""
P&L Reconciliation - Compare DB trades against broker order data.

Detects price discrepancies, quantity mismatches, and missing trades
by comparing local trade records with broker fill information.
"""

import logging
from datetime import date, datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class TradeReconciler:
    """Reconciles local trade DB against broker order fills."""

    PRICE_TOLERANCE = 0.05  # $0.05 per share

    def __init__(self, broker, db_manager):
        self.broker = broker
        self.db = db_manager

    def reconcile(self, trade_date: date) -> dict:
        """
        Compare DB trades for a date against broker order data.

        Returns:
            {
                date, trades_in_db, trades_at_broker, matched,
                discrepancies: [{trade_id, direction, entry_time, field,
                                db_value, broker_value, delta, status}],
                total_db_pnl, total_broker_pnl, pnl_delta,
                broker_supports_order_list, run_timestamp
            }
        """
        discrepancies: List[Dict] = []
        matched = 0
        trades_checked = 0

        trades = self.db.get_trades_by_date(trade_date)
        trades_in_db = len(trades)

        for trade in trades:
            trade_id = trade.get('id', '')
            direction = trade.get('direction', '')
            entry_time = trade.get('entry_time', '')
            entry_order_id = trade.get('entry_order_id')
            exit_order_id = trade.get('exit_order_id')
            trade_has_discrepancy = False

            # Skip trades without order IDs (manual or legacy)
            if not entry_order_id:
                continue

            trades_checked += 1

            # Check entry order
            try:
                entry_status = self.broker.get_order_status(entry_order_id)
            except Exception as e:
                discrepancies.append({
                    'trade_id': trade_id,
                    'direction': direction,
                    'entry_time': str(entry_time),
                    'field': 'entry_order',
                    'db_value': entry_order_id,
                    'broker_value': f'error: {e}',
                    'delta': None,
                    'status': 'broker_error',
                })
                continue

            broker_entry_status = entry_status.get('status', '')

            if broker_entry_status == 'not_found':
                discrepancies.append({
                    'trade_id': trade_id,
                    'direction': direction,
                    'entry_time': str(entry_time),
                    'field': 'entry_order',
                    'db_value': entry_order_id,
                    'broker_value': 'not found',
                    'delta': None,
                    'status': 'missing_from_broker',
                })
                trade_has_discrepancy = True
            else:
                # Compare entry price
                db_entry_price = trade.get('entry_price', 0) or 0
                broker_fill_price = entry_status.get('fill_price', 0) or 0
                price_delta = abs(db_entry_price - broker_fill_price)

                if price_delta > self.PRICE_TOLERANCE:
                    discrepancies.append({
                        'trade_id': trade_id,
                        'direction': direction,
                        'entry_time': str(entry_time),
                        'field': 'entry_price',
                        'db_value': db_entry_price,
                        'broker_value': broker_fill_price,
                        'delta': round(price_delta, 4),
                        'status': 'price_discrepancy',
                    })
                    trade_has_discrepancy = True

                # Compare entry quantity
                db_quantity = trade.get('quantity', 0) or 0
                broker_quantity = entry_status.get('filled_quantity', 0) or 0
                if broker_quantity and db_quantity != broker_quantity:
                    discrepancies.append({
                        'trade_id': trade_id,
                        'direction': direction,
                        'entry_time': str(entry_time),
                        'field': 'quantity',
                        'db_value': db_quantity,
                        'broker_value': broker_quantity,
                        'delta': db_quantity - broker_quantity,
                        'status': 'quantity_mismatch',
                    })
                    trade_has_discrepancy = True

            # Check exit order (only for closed trades)
            if exit_order_id and trade.get('status', '').lower() == 'closed':
                try:
                    exit_status = self.broker.get_order_status(exit_order_id)
                except Exception as e:
                    discrepancies.append({
                        'trade_id': trade_id,
                        'direction': direction,
                        'entry_time': str(entry_time),
                        'field': 'exit_order',
                        'db_value': exit_order_id,
                        'broker_value': f'error: {e}',
                        'delta': None,
                        'status': 'broker_error',
                    })
                    continue

                broker_exit_status = exit_status.get('status', '')

                if broker_exit_status == 'not_found':
                    discrepancies.append({
                        'trade_id': trade_id,
                        'direction': direction,
                        'entry_time': str(entry_time),
                        'field': 'exit_order',
                        'db_value': exit_order_id,
                        'broker_value': 'not found',
                        'delta': None,
                        'status': 'missing_from_broker',
                    })
                    trade_has_discrepancy = True
                else:
                    db_exit_price = trade.get('exit_price', 0) or 0
                    broker_exit_fill = exit_status.get('fill_price', 0) or 0
                    exit_delta = abs(db_exit_price - broker_exit_fill)

                    if exit_delta > self.PRICE_TOLERANCE:
                        discrepancies.append({
                            'trade_id': trade_id,
                            'direction': direction,
                            'entry_time': str(entry_time),
                            'field': 'exit_price',
                            'db_value': db_exit_price,
                            'broker_value': broker_exit_fill,
                            'delta': round(exit_delta, 4),
                            'status': 'price_discrepancy',
                        })
                        trade_has_discrepancy = True

            if not trade_has_discrepancy:
                matched += 1

        # Check for trades at broker not in DB (only if broker supports order listing)
        broker_supports_order_list = hasattr(self.broker, 'get_orders')
        trades_at_broker = trades_checked  # Default: assume 1:1

        if broker_supports_order_list:
            try:
                broker_orders = self.broker.get_orders(status='EXECUTED')
                # Build set of known order IDs from DB
                db_order_ids = set()
                for t in trades:
                    if t.get('entry_order_id'):
                        db_order_ids.add(t['entry_order_id'])
                    if t.get('exit_order_id'):
                        db_order_ids.add(t['exit_order_id'])

                # Filter broker orders to today's date
                for order in broker_orders:
                    order_id = str(order.get('orderId', order.get('order_id', '')))
                    if order_id and order_id not in db_order_ids:
                        discrepancies.append({
                            'trade_id': None,
                            'direction': None,
                            'entry_time': None,
                            'field': 'order',
                            'db_value': 'not in DB',
                            'broker_value': order_id,
                            'delta': None,
                            'status': 'missing_from_db',
                        })

                trades_at_broker = len(broker_orders)
            except Exception as e:
                logger.warning(f"Failed to fetch broker order list: {e}")

        # Compute P&L totals
        closed_trades = [
            t for t in trades
            if t.get('status', '').lower() == 'closed' and t.get('pnl') is not None
        ]
        total_db_pnl = round(sum(t['pnl'] for t in closed_trades), 2)
        total_commissions = round(
            sum(t.get('commissions', 0) or 0 for t in closed_trades), 2
        )
        total_net_pnl = round(total_db_pnl - total_commissions, 2)

        # Compare bot P&L against actual account equity change to surface fees/slippage
        account_pnl = None
        fee_slippage_delta = None
        starting_equity = None
        ending_equity = None
        try:
            balance = self.broker.get_account_balance()
            ending_equity = balance.get('net_account_value')
            if ending_equity is not None:
                # Try to read starting equity from previous day's post-settlement file
                from pathlib import Path
                balance_file = Path(self.db.db_path).parent / 'post_settlement_balance.json'
                if balance_file.exists():
                    import json
                    with open(balance_file) as f:
                        prev = json.load(f)
                    prev_date = prev.get('date', '')
                    prev_nav = prev.get('net_account_value')
                    # Only use if it's from a previous day (not today's post-settlement)
                    if prev_nav is not None and prev_date != str(trade_date):
                        starting_equity = prev_nav
                        account_pnl = round(ending_equity - starting_equity, 2)
                        # Use net P&L (gross - commissions) for comparison
                        fee_slippage_delta = round(total_net_pnl - account_pnl, 2)
                        logger.info(
                            f"Fee/slippage analysis: Gross P&L=${total_db_pnl:+.2f}, "
                            f"Commissions=${total_commissions:.2f}, "
                            f"Net P&L=${total_net_pnl:+.2f}, "
                            f"Account P&L=${account_pnl:+.2f}, "
                            f"Residual=${fee_slippage_delta:+.2f}"
                        )
        except Exception as e:
            logger.debug(f"Could not compute fee/slippage delta: {e}")

        return {
            'date': str(trade_date),
            'trades_in_db': trades_in_db,
            'trades_at_broker': trades_at_broker,
            'trades_checked': trades_checked,
            'matched': matched,
            'discrepancies': discrepancies,
            'total_db_pnl': total_db_pnl,
            'total_commissions': total_commissions,
            'total_net_pnl': total_net_pnl,
            'total_broker_pnl': None,  # Would require reconstructing from fills
            'pnl_delta': None,
            'account_pnl': account_pnl,
            'fee_slippage_delta': fee_slippage_delta,
            'starting_equity': starting_equity,
            'ending_equity': ending_equity,
            'broker_supports_order_list': broker_supports_order_list,
            'run_timestamp': datetime.now().isoformat(),
        }
