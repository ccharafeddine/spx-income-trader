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
            'daily_pnl': 150.50,
            'weekly_pnl': 320.00,
            'monthly_pnl': 800.00,
            'win_rate': 75.0,
            'equity': 51500.0,
        })
        mock_post.assert_called_once()
        payload = mock_post.call_args.kwargs.get('json') or mock_post.call_args[1].get('json')
        text = payload['attachments'][0]['text']
        assert 'Trades: 2' in text
        assert '$+150.50' in text
        assert 'Win Rate: 75%' in text
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
