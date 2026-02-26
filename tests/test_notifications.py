"""Tests for the webhook notification system (Slack, Discord, generic)."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Project root setup
sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_notifier(slack=None, discord=None, webhook=None):
    """Create a NotificationManager with mocked configs (no real email/SMS)."""
    with patch('config.settings.EMAIL_CONFIG', {'enabled': False}), \
         patch('config.settings.SMS_CONFIG', {'enabled': False}), \
         patch('config.settings.load_strategy_params', return_value={
             'notifications': {
                 'slack': slack or {'enabled': False, 'webhook_url': '', 'min_level': 'info'},
                 'discord': discord or {'enabled': False, 'webhook_url': '', 'min_level': 'info'},
                 'webhook': webhook or {'enabled': False, 'url': '', 'min_level': 'warning'},
             }
         }):
        from src.utils.notifications import NotificationManager
        return NotificationManager()


class TestSlack:
    @patch('requests.post')
    def test_slack_payload_format(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(slack={'enabled': True, 'webhook_url': 'https://hooks.slack.com/test', 'min_level': 'info'})

        nm.send("Test Subject", "Test message", level='info')

        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs.get('json') or mock_post.call_args[1].get('json')
        assert 'attachments' in payload
        att = payload['attachments'][0]
        assert att['color'] == '#10b981'  # info = green
        assert '*Test Subject*' in att['text']
        assert 'Test message' in att['text']

    @patch('requests.post')
    def test_slack_warning_color(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(slack={'enabled': True, 'webhook_url': 'https://hooks.slack.com/test', 'min_level': 'info'})
        nm.send("Warn", "msg", level='warning')
        payload = mock_post.call_args.kwargs.get('json') or mock_post.call_args[1].get('json')
        assert payload['attachments'][0]['color'] == '#f59e0b'

    @patch('requests.post')
    def test_slack_critical_color(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(slack={'enabled': True, 'webhook_url': 'https://hooks.slack.com/test', 'min_level': 'info'})
        nm.send("Crit", "msg", level='critical')
        payload = mock_post.call_args.kwargs.get('json') or mock_post.call_args[1].get('json')
        assert payload['attachments'][0]['color'] == '#ef4444'


class TestDiscord:
    @patch('requests.post')
    def test_discord_payload_format(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord={'enabled': True, 'webhook_url': 'https://discord.com/api/webhooks/test', 'min_level': 'info'})

        nm.send("Test Subject", "Test message", level='info')

        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs.get('json') or mock_post.call_args[1].get('json')
        assert 'embeds' in payload
        embed = payload['embeds'][0]
        assert embed['title'] == 'Test Subject'
        assert embed['description'] == 'Test message'
        assert embed['color'] == 0x10b981
        assert embed['footer']['text'] == 'The Daily Melt'
        assert 'timestamp' in embed

    @patch('requests.post')
    def test_discord_warning_color(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord={'enabled': True, 'webhook_url': 'https://discord.com/api/webhooks/test', 'min_level': 'info'})
        nm.send("W", "m", level='warning')
        payload = mock_post.call_args.kwargs.get('json') or mock_post.call_args[1].get('json')
        assert payload['embeds'][0]['color'] == 0xf59e0b


class TestGenericWebhook:
    @patch('requests.post')
    def test_generic_webhook_payload(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(webhook={'enabled': True, 'url': 'https://example.com/hook', 'min_level': 'info'})

        nm.send("Subj", "Msg", level='info')

        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs.get('json') or mock_post.call_args[1].get('json')
        assert payload['subject'] == 'Subj'
        assert payload['message'] == 'Msg'
        assert payload['level'] == 'info'
        assert payload['source'] == 'The Daily Melt'
        assert 'timestamp' in payload


class TestLevelFiltering:
    @patch('requests.post')
    def test_level_filtering_blocks_low(self, mock_post):
        """min_level=warning should block info messages."""
        nm = _make_notifier(slack={'enabled': True, 'webhook_url': 'https://hooks.slack.com/test', 'min_level': 'warning'})
        nm.send("Info", "msg", level='info')
        mock_post.assert_not_called()

    @patch('requests.post')
    def test_level_filtering_allows_equal(self, mock_post):
        """min_level=warning should allow warning messages."""
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(slack={'enabled': True, 'webhook_url': 'https://hooks.slack.com/test', 'min_level': 'warning'})
        nm.send("Warning", "msg", level='warning')
        mock_post.assert_called_once()

    @patch('requests.post')
    def test_level_filtering_allows_higher(self, mock_post):
        """min_level=warning should allow critical messages."""
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(slack={'enabled': True, 'webhook_url': 'https://hooks.slack.com/test', 'min_level': 'warning'})
        nm.send("Critical", "msg", level='critical')
        mock_post.assert_called_once()


class TestErrorResilience:
    @patch('requests.post')
    def test_connection_error_no_raise(self, mock_post):
        """ConnectionError should be caught, not raised."""
        mock_post.side_effect = ConnectionError("fail")
        nm = _make_notifier(slack={'enabled': True, 'webhook_url': 'https://hooks.slack.com/test', 'min_level': 'info'})
        # Should not raise
        nm.send("Test", "msg", level='info')

    @patch('requests.post')
    def test_timeout_no_raise(self, mock_post):
        """Timeout should be caught, not raised."""
        import requests
        mock_post.side_effect = requests.exceptions.Timeout("timeout")
        nm = _make_notifier(discord={'enabled': True, 'webhook_url': 'https://discord.com/api/webhooks/test', 'min_level': 'info'})
        nm.send("Test", "msg", level='info')


class TestSendTest:
    @patch('requests.post')
    def test_send_test_success(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(slack={'enabled': True, 'webhook_url': 'https://hooks.slack.com/test', 'min_level': 'info'})
        success, error = nm.send_test('slack')
        assert success is True
        assert error is None

    @patch('requests.post')
    def test_send_test_empty_url(self, mock_post):
        nm = _make_notifier(slack={'enabled': True, 'webhook_url': '', 'min_level': 'info'})
        success, error = nm.send_test('slack')
        assert success is False
        assert 'empty' in error.lower()
        mock_post.assert_not_called()


class TestEODSummary:
    @patch('requests.post')
    def test_eod_summary_format(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(slack={'enabled': True, 'webhook_url': 'https://hooks.slack.com/test', 'min_level': 'info'})
        nm.send_eod_summary({
            'trades_count': 2,
            'wins': 1,
            'losses': 1,
            'daily_pnl': 150.50,
            'weekly_pnl': 320.00,
            'monthly_pnl': 800.00,
            'win_rate': 75.0,
            'equity': 51500.0,
        })
        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs.get('json') or mock_post.call_args[1].get('json')
        text = payload['attachments'][0]['text']
        assert 'Trades' in text
        assert '$+150.50' in text
        assert 'Win Rate' in text
        assert '75%' in text
        assert '$51,500' in text


class TestBackwardCompat:
    @patch('requests.post')
    def test_backward_compat_no_level(self, mock_post):
        """send(subj, msg) should work without level param."""
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(slack={'enabled': True, 'webhook_url': 'https://hooks.slack.com/test', 'min_level': 'info'})
        nm.send("Test", "msg")
        mock_post.assert_called_once()

    @patch('requests.post')
    def test_empty_url_skipped(self, mock_post):
        """enabled=True but url="" should not POST."""
        nm = _make_notifier(
            slack={'enabled': True, 'webhook_url': '', 'min_level': 'info'},
            discord={'enabled': True, 'webhook_url': '', 'min_level': 'info'},
            webhook={'enabled': True, 'url': '', 'min_level': 'info'},
        )
        nm.send("Test", "msg")
        mock_post.assert_not_called()


class TestReloadConfig:
    def test_reload_config(self):
        """Config changes should be reflected after reload."""
        nm = _make_notifier()
        assert not nm.slack_config.get('enabled')

        with patch('config.settings.load_strategy_params', return_value={
            'notifications': {
                'slack': {'enabled': True, 'webhook_url': 'https://new-url', 'min_level': 'warning'},
            }
        }):
            nm.reload_config()

        assert nm.slack_config['enabled'] is True
        assert nm.slack_config['webhook_url'] == 'https://new-url'
        assert nm.slack_config['min_level'] == 'warning'


# ------------------------------------------------------------------
# Rich Discord embed tests (8 new tests)
# ------------------------------------------------------------------

DISCORD_CFG = {'enabled': True, 'webhook_url': 'https://discord.com/api/webhooks/test', 'min_level': 'info'}


def _get_discord_embed(mock_post):
    """Extract the first embed from a mocked Discord POST call."""
    payload = mock_post.call_args.kwargs.get('json') or mock_post.call_args[1].get('json')
    return payload['embeds'][0]


class TestRichEmbedFields:
    """Verify Discord embeds include structured fields with name/value/inline."""

    @patch('requests.post')
    def test_trade_entry_embed_has_fields(self, mock_post):
        """send_trade_entry produces an embed with strategy, strikes, and risk fields."""
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)

        nm.send_trade_entry({
            'strategy': 'Daily Income',
            'direction': 'bullish',
            'short_strike': 5800,
            'long_strike': 5795,
            'credit_per_contract': 1.20,
            'total_credit': 120.00,
            'quantity': 1,
            'breakeven': 5798.80,
            'max_risk': 380.00,
        })

        mock_post.assert_called_once()
        embed = _get_discord_embed(mock_post)
        assert 'fields' in embed
        field_names = {f['name'] for f in embed['fields']}
        assert 'Strategy' in field_names
        assert 'Strikes' in field_names
        assert 'Max Risk' in field_names
        assert 'Breakeven' in field_names
        assert embed['footer']['text'] == 'The Daily Melt'
        assert 'timestamp' in embed
        # All fields should have inline set
        for f in embed['fields']:
            assert 'inline' in f


class TestColorSelection:
    """Verify win/loss color logic for trade exit embeds."""

    @patch('requests.post')
    def test_trade_exit_win_is_green(self, mock_post):
        """Profitable trade exit uses green color (0x10b981)."""
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)

        nm.send_trade_exit({
            'strategy': 'Daily Income',
            'direction': 'bullish',
            'short_strike': 5800,
            'long_strike': 5795,
            'pnl': 95.00,
            'pnl_pct': 79.2,
            'max_profit_pct': 79,
            'hold_duration': '2h 15m',
            'exit_reason': 'profit_target',
        })

        embed = _get_discord_embed(mock_post)
        assert embed['color'] == 0x10b981  # DISCORD_GREEN

    @patch('requests.post')
    def test_trade_exit_loss_is_red(self, mock_post):
        """Losing trade exit uses red color (0xef4444)."""
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)

        nm.send_trade_exit({
            'strategy': 'Daily Income',
            'direction': 'bearish',
            'short_strike': 5850,
            'long_strike': 5855,
            'pnl': -280.00,
            'pnl_pct': -73.7,
            'max_profit_pct': -73,
            'hold_duration': '45m',
            'exit_reason': 'stop_loss',
        })

        embed = _get_discord_embed(mock_post)
        assert embed['color'] == 0xef4444  # DISCORD_RED


class TestDailySummaryContent:
    """Verify EOD summary embed includes wins/losses, streak, and open swings."""

    @patch('requests.post')
    def test_eod_summary_discord_embed_content(self, mock_post):
        """EOD summary embed has trades (W/L), streak, and swing position fields."""
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)

        nm.send_eod_summary({
            'trades_count': 3,
            'wins': 2,
            'losses': 1,
            'daily_pnl': 210.00,
            'weekly_pnl': 480.00,
            'monthly_pnl': 1200.00,
            'streak': '2W',
            'equity': 52000.0,
            'open_swings': [
                {'direction': 'bullish', 'short_strike': 5700, 'long_strike': 5690, 'unrealized_pnl': 45.0},
            ],
        })

        mock_post.assert_called_once()
        embed = _get_discord_embed(mock_post)
        assert embed['color'] == 0x10b981  # green for positive daily P&L
        field_map = {f['name']: f['value'] for f in embed['fields']}
        assert '2W / 1L' in field_map['Trades']
        assert '2W' in field_map['Streak']
        assert '$+210.00' in field_map['Daily P&L']
        assert 'Open Swing Positions' in field_map
        assert '5700/5690' in field_map['Open Swing Positions']


class TestDangerAlertFields:
    """Verify danger alert embed contains required threshold fields."""

    @patch('requests.post')
    def test_danger_alert_has_distance_and_loss(self, mock_post):
        """Danger alert includes current price, distance past strike, and estimated loss."""
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)

        nm.send_danger_alert({
            'direction': 'bearish',
            'short_strike': 5850,
            'long_strike': 5855,
            'current_price': 5853.25,
            'distance': 3.25,
            'estimated_loss': -175.00,
            'time_remaining': '1h 30m',
        })

        mock_post.assert_called_once()
        embed = _get_discord_embed(mock_post)
        assert embed['color'] == 0xef4444  # red for danger
        assert 'DANGER' in embed['title']
        field_map = {f['name']: f['value'] for f in embed['fields']}
        assert '$5,853.25' in field_map['SPX Price']
        assert '$+3.25' in field_map['Past Short By']
        assert '$-175.00' in field_map['Est. Loss']
        assert '1h 30m' in field_map['Time Left']


class TestCircuitBreakerMessage:
    """Verify circuit breaker embed includes loss, limit, and halted status."""

    @patch('requests.post')
    def test_circuit_breaker_embed_content(self, mock_post):
        """Circuit breaker embed shows daily P&L, limit, and halt status."""
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)

        nm.send_circuit_breaker({
            'current_loss': -520.00,
            'loss_limit': 500.00,
            'message': 'Daily P&L $-520.00 hit the -$500 limit.',
        })

        mock_post.assert_called_once()
        embed = _get_discord_embed(mock_post)
        assert embed['color'] == 0xef4444  # red
        field_map = {f['name']: f['value'] for f in embed['fields']}
        assert '$-520.00' in field_map['Daily P&L']
        assert '-$500' in field_map['Loss Limit']
        assert 'Halted' in field_map['Status']


class TestAutoRestartEmbed:
    """Verify auto-restart embed includes crash reason and restart count."""

    @patch('requests.post')
    def test_auto_restart_embed_content(self, mock_post):
        """Auto-restart embed contains crash reason, restart count, and mode."""
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)

        nm.send_auto_restart({
            'crash_reason': 'Bot thread died unexpectedly',
            'restart_count': 2,
            'mode': 'live',
        })

        mock_post.assert_called_once()
        embed = _get_discord_embed(mock_post)
        assert embed['color'] == 0xf59e0b  # yellow
        field_map = {f['name']: f['value'] for f in embed['fields']}
        assert 'unexpectedly' in field_map['Crash Reason']
        assert '2/3' in field_map['Restart #']
        assert 'live' in field_map['Mode']


class TestMarketOpenEmbed:
    """Verify market open embed includes VIX regime and carry-over positions."""

    @patch('requests.post')
    def test_market_open_embed_content(self, mock_post):
        """Market open embed has VIX regime and open position count."""
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)

        nm.send_market_open({
            'vix_level': 18.5,
            'vix_regime': 'low',
            'open_positions': 1,
            'mode': 'dry-run',
        })

        mock_post.assert_called_once()
        embed = _get_discord_embed(mock_post)
        assert embed['color'] == 0x3b82f6  # blue
        assert 'Market Open' in embed['title']
        field_map = {f['name']: f['value'] for f in embed['fields']}
        assert '18.5' in field_map['VIX']
        assert 'low' in field_map['VIX']
        assert '1' in field_map['Carry-Over Positions']
        assert 'dry-run' in field_map['Mode']


class TestEnhancedStartStop:
    """Verify enhanced start/stop embeds include equity, mode, and open positions."""

    @patch('requests.post')
    def test_bot_started_embed_content(self, mock_post):
        """Bot started embed includes mode, equity, and open position count."""
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)

        nm.send_bot_started({
            'mode': 'live',
            'equity': 50000.0,
            'open_positions': 2,
        })

        mock_post.assert_called_once()
        embed = _get_discord_embed(mock_post)
        assert embed['color'] == 0x10b981  # green
        field_map = {f['name']: f['value'] for f in embed['fields']}
        assert 'live' in field_map['Mode']
        assert '$50,000' in field_map['Equity']
        assert '2' in field_map['Open Positions']
