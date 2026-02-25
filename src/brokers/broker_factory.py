"""
Broker Factory

Creates the appropriate broker implementation based on configuration.
Centralizes broker instantiation logic so main.py and other entry points
don't need to know about individual broker classes.
"""

import logging

from .base import BrokerInterface

logger = logging.getLogger(__name__)


def get_broker(config: dict) -> BrokerInterface:
    """Factory function to create the appropriate broker based on config.

    Args:
        config: Full strategy_params config dict. Reads:
            - broker.active: 'dry_run', 'etrade', 'schwab', or 'ibkr'
            - broker.schwab.*: Schwab-specific config
            - portfolio.account_size: For DryRunBroker

    Returns:
        A BrokerInterface implementation.

    Raises:
        ValueError: If broker.active is an unknown value.
    """
    broker_cfg = config.get('broker', {})
    active = broker_cfg.get('active', 'dry_run')

    if active == 'dry_run':
        from .dry_run_broker import DryRunBroker
        account_size = config.get('portfolio', {}).get('account_size', 50000.0)
        logger.info(f"Creating DryRunBroker (account_size=${account_size:,.2f})")
        return DryRunBroker(initial_balance=account_size)

    elif active == 'etrade':
        from .etrade_broker import ETradeBroker
        from .etrade_auth import ETradeAuth
        logger.info("Creating ETradeBroker")
        auth = ETradeAuth()
        return ETradeBroker(auth=auth)

    elif active == 'schwab':
        from .schwab_broker import SchwabBroker
        from .schwab_auth import SchwabAuth
        from config.settings import get_schwab_credentials
        schwab_cfg = broker_cfg.get('schwab', {})
        # Use get_schwab_credentials() which checks keyring first, then YAML
        schwab_creds = get_schwab_credentials()
        logger.info("Creating SchwabBroker")
        auth = SchwabAuth(
            app_key=schwab_creds.get('app_key', ''),
            app_secret=schwab_creds.get('app_secret', ''),
            callback_url=schwab_creds.get('callback_url', 'https://127.0.0.1'),
            token_path=schwab_cfg.get('token_path'),
        )
        return SchwabBroker(config=schwab_cfg, auth=auth)

    elif active == 'ibkr':
        from .ibkr_broker import IBKRBroker
        ibkr_cfg = broker_cfg.get('ibkr', {})
        logger.info("Creating IBKRBroker")
        broker = IBKRBroker(
            host=ibkr_cfg.get('host', '127.0.0.1'),
            port=ibkr_cfg.get('port', 7496),
            client_id=ibkr_cfg.get('client_id', 1),
            paper_trading=ibkr_cfg.get('paper_trading', False),
        )
        broker.connect()
        return broker

    else:
        raise ValueError(f"Unknown broker: {active}")
