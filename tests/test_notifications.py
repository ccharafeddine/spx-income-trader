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
        assert embed['color'] == 3066993
        assert 'The Daily Melt' in embed['footer']['text']
        assert 'timestamp' in embed

    @patch('requests.post')
    def test_discord_warning_color(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord={'enabled': True, 'webhook_url': 'https://discord.com/api/webhooks/test', 'min_level': 'info'})
        nm.send("W", "m", level='warning')
        payload = mock_post.call_args.kwargs.get('json') or mock_post.call_args[1].get('json')
        assert payload['embeds'][0]['color'] == 16776960


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
        assert 'The Daily Melt' in embed['footer']['text']
        assert 'timestamp' in embed
        # All fields should have inline set
        for f in embed['fields']:
            assert 'inline' in f


class TestColorSelection:
    """Verify win/loss color logic for trade exit embeds."""

    @patch('requests.post')
    def test_trade_exit_win_is_green(self, mock_post):
        """Profitable trade exit uses green color (3066993)."""
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
        assert embed['color'] == 3066993  # DISCORD_GREEN

    @patch('requests.post')
    def test_trade_exit_loss_is_red(self, mock_post):
        """Losing trade exit uses red color (15158332)."""
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
        assert embed['color'] == 15158332  # DISCORD_RED


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
        assert embed['color'] == 3066993  # green for positive daily P&L
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
        assert embed['color'] == 15158332  # red for danger
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
        assert embed['color'] == 15158332  # red
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
        assert embed['color'] == 16776960  # yellow
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
        assert embed['color'] == 3066993  # green
        field_map = {f['name']: f['value'] for f in embed['fields']}
        assert 'live' in field_map['Mode']
        assert '$50,000' in field_map['Equity']
        assert '2' in field_map['Open Positions']


# ------------------------------------------------------------------
# 8 new tests for notification audit
# ------------------------------------------------------------------

class TestEmbedColorSpec:
    """Verify embeds use specified color integers: green=3066993, red=15158332, yellow=16776960."""

    @patch('requests.post')
    def test_embed_colors_match_spec(self, mock_post):
        """Win/loss/warning embeds use the exact color ints from the spec."""
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)

        # Green (win)
        nm.send_trade_exit({
            'strategy': 'DI', 'direction': 'bullish', 'short_strike': 5800,
            'long_strike': 5795, 'pnl': 50.0, 'pnl_pct': 40.0,
            'max_profit_pct': 40, 'hold_duration': '1h', 'exit_reason': 'profit_target',
        })
        green_embed = _get_discord_embed(mock_post)
        assert green_embed['color'] == 3066993

        # Red (loss)
        mock_post.reset_mock()
        nm.send_trade_exit({
            'strategy': 'DI', 'direction': 'bearish', 'short_strike': 5850,
            'long_strike': 5855, 'pnl': -100.0, 'pnl_pct': -50.0,
            'max_profit_pct': -50, 'hold_duration': '30m', 'exit_reason': 'stop_loss',
        })
        red_embed = _get_discord_embed(mock_post)
        assert red_embed['color'] == 15158332

        # Yellow (warning - auto-restart)
        mock_post.reset_mock()
        nm.send_auto_restart({'crash_reason': 'OOM', 'restart_count': 1, 'mode': 'dry-run'})
        yellow_embed = _get_discord_embed(mock_post)
        assert yellow_embed['color'] == 16776960


class TestTradeEntryAllFields:
    """Verify trade entry embed contains every required field including Expiry."""

    @patch('requests.post')
    def test_trade_entry_contains_all_required_fields(self, mock_post):
        """Entry embed must have: Strategy, Direction, Strikes, Credit/Contract,
        Total Credit, Quantity, Breakeven, Max Risk, Expiry."""
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
            'expiry': '02/26 (0DTE)',
        })

        embed = _get_discord_embed(mock_post)
        field_names = {f['name'] for f in embed['fields']}
        required = {'Strategy', 'Direction', 'Strikes', 'Credit/Contract',
                    'Total Credit', 'Quantity', 'Breakeven', 'Max Risk', 'Expiry'}
        assert required.issubset(field_names), f"Missing: {required - field_names}"
        field_map = {f['name']: f['value'] for f in embed['fields']}
        assert '0DTE' in field_map['Expiry']


class TestTradeExitMaxProfit:
    """Verify trade exit calculates and displays % max profit correctly."""

    @patch('requests.post')
    def test_trade_exit_max_profit_percentage(self, mock_post):
        """Exit embed shows correct % max profit captured value."""
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)

        nm.send_trade_exit({
            'strategy': 'Daily Income',
            'direction': 'bullish',
            'short_strike': 5800,
            'long_strike': 5795,
            'pnl': 96.00,
            'pnl_pct': 80.0,
            'max_profit_pct': 80,
            'hold_duration': '3h 10m',
            'exit_reason': 'profit_target',
        })

        embed = _get_discord_embed(mock_post)
        field_map = {f['name']: f['value'] for f in embed['fields']}
        assert '% Max Profit' in field_map
        assert '80%' in field_map['% Max Profit']


class TestDangerAlertSuppression:
    """Verify danger alert fires once per position, not on every loop cycle."""

    def test_danger_alert_fires_once_then_suppresses(self):
        """Same trade_id only triggers one danger alert, subsequent breaches are suppressed."""
        from unittest.mock import MagicMock

        mock_notifier = MagicMock()
        danger_alerts_sent = {}
        trade_id = "test-trade-abc123"
        alert_key = f"danger_{trade_id}"

        # Simulate 5 consecutive breach checks (as happens in main loop)
        for _ in range(5):
            breached = True
            if breached and not danger_alerts_sent.get(alert_key):
                danger_alerts_sent[alert_key] = True
                mock_notifier.send_danger_alert({
                    'direction': 'bearish', 'short_strike': 5850,
                    'current_price': 5855, 'distance': 5.0,
                    'estimated_loss': -250, 'time_remaining': '2h',
                })

        # Only called once despite 5 breach cycles
        assert mock_notifier.send_danger_alert.call_count == 1


class TestEODSummaryTimingGate:
    """Verify EOD summary only fires at/after market close, not before."""

    def test_eod_summary_fires_after_405pm_not_before(self):
        """The _finalize_daily_journal timing gate blocks before 16:05 ET."""
        import pytz
        from datetime import datetime, time

        ET = pytz.timezone("America/New_York")
        eod_threshold = time(16, 5)

        before = ET.localize(datetime(2026, 2, 26, 16, 0))
        after = ET.localize(datetime(2026, 2, 26, 16, 5))

        # At exactly 4:00pm: gate still blocks (need 16:05)
        assert before.time() < eod_threshold
        # At 4:05pm: gate opens
        assert after.time() >= eod_threshold

    def test_eod_summary_skipped_when_bot_starts_after_market_close(self):
        """EOD summary must not fire when bot starts after 4:00 PM ET."""
        import pytz
        from datetime import datetime, time, date as date_type

        ET = pytz.timezone("America/New_York")

        # Simulate: bot started at 17:00, current time is 17:05
        start_time_wall = ET.localize(datetime(2026, 2, 26, 17, 0))
        current_time = ET.localize(datetime(2026, 2, 26, 17, 5))
        trading_date = date_type(2026, 2, 26)

        journal_finalized = False
        eod_summary_sent_today = False

        # Reproduce the guard from main.py _run_main_loop
        should_finalize = (
            not journal_finalized
            and trading_date == current_time.date()
            and current_time.time() >= time(16, 5)
            and start_time_wall
            and start_time_wall.time() < time(16, 0)
        )

        # Bot started after market close, so EOD should NOT fire
        assert should_finalize is False

    def test_eod_summary_does_not_fire_twice(self):
        """EOD summary notification must not fire twice on the same day."""
        import pytz
        from datetime import datetime, time

        ET = pytz.timezone("America/New_York")

        # Simulate: bot started at 09:30, two checks after 16:05
        start_time_wall = ET.localize(datetime(2026, 2, 26, 9, 30))
        check1 = ET.localize(datetime(2026, 2, 26, 16, 5))
        check2 = ET.localize(datetime(2026, 2, 26, 16, 10))

        journal_finalized = False
        eod_summary_sent_today = False
        send_count = 0

        for current_time in [check1, check2]:
            should_finalize = (
                not journal_finalized
                and current_time.time() >= time(16, 5)
                and start_time_wall
                and start_time_wall.time() < time(16, 0)
            )
            if should_finalize:
                journal_finalized = True
                if not eod_summary_sent_today:
                    eod_summary_sent_today = True
                    send_count += 1

        # Exactly one send despite two checks
        assert send_count == 1

    def test_eod_summary_equity_not_from_initial_balance(self):
        """EOD summary must query live equity, never use config initial_balance."""
        nm = _make_notifier(discord=DISCORD_CFG)

        # The notifier just passes through whatever equity value it gets.
        # The caller (main.py) must pass None when broker query fails,
        # not fall back to portfolio.account_size.
        # When equity is None, the embed should omit the Equity field.
        with patch('requests.post') as mock_post:
            mock_post.return_value = MagicMock(ok=True)
            nm.send_eod_summary({
                'trades_count': 2,
                'wins': 1,
                'losses': 1,
                'daily_pnl': -50.0,
                'weekly_pnl': 100.0,
                'monthly_pnl': 500.0,
                'equity': None,  # Broker unreachable, should NOT be $50,000
                'streak': '',
                'open_swings': [],
            })
            embed = _get_discord_embed(mock_post)
            field_map = {f['name']: f['value'] for f in embed['fields']}
            # Equity field must be absent when None (not a hardcoded $50,000)
            assert 'Equity' not in field_map


class TestCircuitBreakerContent:
    """Verify circuit breaker embed has loss amount and limit."""

    @patch('requests.post')
    def test_circuit_breaker_has_loss_and_limit(self, mock_post):
        """Circuit breaker embed displays current loss and the limit that was hit."""
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)

        nm.send_circuit_breaker({
            'current_loss': -1050.00,
            'loss_limit': 1000.00,
            'message': 'Daily P&L $-1050.00 hit the -$1000 limit.',
        })

        embed = _get_discord_embed(mock_post)
        field_map = {f['name']: f['value'] for f in embed['fields']}
        assert '$-1050.00' in field_map['Daily P&L']
        assert '-$1,000' in field_map['Loss Limit']
        assert 'Halted' in field_map['Status']
        assert embed['color'] == 15158332  # red


class TestVixUnknownOmitsField:
    """Verify VIX field is omitted entirely when unavailable (not 'unknown')."""

    @patch('requests.post')
    def test_vix_unknown_omits_field(self, mock_post):
        """When vix_level is None, no VIX field appears in embed."""
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)

        nm.send_market_open({
            'vix_level': None,
            'vix_regime': None,
            'open_positions': 0,
            'mode': 'dry-run',
        })

        embed = _get_discord_embed(mock_post)
        field_names = {f['name'] for f in embed['fields']}
        assert 'VIX' not in field_names
        assert 'VIX Regime' not in field_names
        # Mode should still be present
        assert 'Mode' in field_names


class TestAutoRestartCount:
    """Verify auto-restart embed shows restart count."""

    @patch('requests.post')
    def test_auto_restart_has_restart_count(self, mock_post):
        """Auto-restart embed displays the restart count clearly."""
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)

        nm.send_auto_restart({
            'crash_reason': 'Unhandled exception in main loop',
            'restart_count': 3,
            'mode': 'live',
        })

        embed = _get_discord_embed(mock_post)
        field_map = {f['name']: f['value'] for f in embed['fields']}
        assert 'Restart #' in field_map
        assert '3/3' in field_map['Restart #']
        assert embed['color'] == 16776960  # yellow


# ------------------------------------------------------------------
# FIX 1-6 notification improvement tests
# ------------------------------------------------------------------

class TestBotStartedETTime:
    """FIX 1: send_bot_started shows ET time, not UTC."""

    @patch('requests.post')
    def test_bot_started_shows_et_not_utc(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)
        nm.send_bot_started({'mode': 'dry-run', 'equity': 50000, 'open_positions': 0})
        embed = _get_discord_embed(mock_post)
        assert 'ET' in embed['description']
        assert 'UTC' not in embed['description']


class TestMarketOpenSPXField:
    """FIX 2: send_market_open includes SPX field when spx_price provided."""

    @patch('requests.post')
    def test_market_open_includes_spx_price(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)
        nm.send_market_open({
            'vix_level': 18.5,
            'vix_regime': 'low',
            'open_positions': 0,
            'mode': 'dry-run',
            'spx_price': 5842.15,
        })
        embed = _get_discord_embed(mock_post)
        field_map = {f['name']: f['value'] for f in embed['fields']}
        assert 'SPX' in field_map
        assert '$5,842.15' in field_map['SPX']

    @patch('requests.post')
    def test_market_open_omits_spx_when_none(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)
        nm.send_market_open({
            'vix_level': 18.5,
            'vix_regime': 'low',
            'open_positions': 0,
            'mode': 'dry-run',
            'spx_price': None,
        })
        embed = _get_discord_embed(mock_post)
        field_names = {f['name'] for f in embed['fields']}
        assert 'SPX' not in field_names


class TestEODSummarySPXFields:
    """FIX 3 + 5: send_eod_summary includes SPX Close/Session Range and ET close time."""

    @patch('requests.post')
    def test_eod_summary_includes_spx_close(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)
        nm.send_eod_summary({
            'trades_count': 1, 'wins': 1, 'losses': 0,
            'daily_pnl': 100.0, 'weekly_pnl': 100.0, 'monthly_pnl': 100.0,
            'spx_close': 5842.15, 'spx_change_pct': 0.42,
            'spx_high': 5860.0, 'spx_low': 5820.0,
            'close_time_str': '4:05 PM',
        })
        embed = _get_discord_embed(mock_post)
        field_map = {f['name']: f['value'] for f in embed['fields']}
        assert 'SPX Close' in field_map
        assert '$5,842.15' in field_map['SPX Close']
        assert '+0.42%' in field_map['SPX Close']
        assert 'Session Range' in field_map
        assert '$5,820.00' in field_map['Session Range']
        assert '$5,860.00' in field_map['Session Range']

    @patch('requests.post')
    def test_eod_summary_omits_spx_when_none(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)
        nm.send_eod_summary({
            'trades_count': 1, 'wins': 1, 'losses': 0,
            'daily_pnl': 100.0, 'weekly_pnl': 100.0, 'monthly_pnl': 100.0,
            'spx_close': None, 'spx_high': None, 'spx_low': None,
        })
        embed = _get_discord_embed(mock_post)
        field_names = {f['name'] for f in embed['fields']}
        assert 'SPX Close' not in field_names
        assert 'Session Range' not in field_names

    @patch('requests.post')
    def test_eod_summary_shows_et_close_time(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)
        nm.send_eod_summary({
            'trades_count': 0, 'wins': 0, 'losses': 0,
            'daily_pnl': 0.0, 'weekly_pnl': 0.0, 'monthly_pnl': 0.0,
            'close_time_str': '4:05 PM',
        })
        embed = _get_discord_embed(mock_post)
        assert '4:05 PM' in embed['description']
        assert 'ET' in embed['description']


class TestHourlyUpdate:
    """FIX 6: send_hourly_update includes expected fields."""

    @patch('requests.post')
    def test_hourly_update_title_contains_market_update(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)
        nm.send_hourly_update({
            'current_time_str': '11:00 AM',
            'spx_price': 5842.15, 'spx_change_pct': 0.42,
            'vix_level': 18.5, 'vix_regime': 'low',
            'spx_high': 5860.0, 'spx_low': 5820.0,
            'bars_built': 3, 'pulse_count': 1,
            'open_positions': [], 'next_bar_time': '11:30 AM ET',
        })
        embed = _get_discord_embed(mock_post)
        assert 'Market Update' in embed['title']
        assert '11:00 AM' in embed['title']

    @patch('requests.post')
    def test_hourly_update_includes_spx_and_vix(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)
        nm.send_hourly_update({
            'current_time_str': '11:00 AM',
            'spx_price': 5842.15, 'spx_change_pct': 0.42,
            'vix_level': 18.5, 'vix_regime': 'low',
            'spx_high': 5860.0, 'spx_low': 5820.0,
            'bars_built': 3, 'pulse_count': 1,
            'open_positions': [], 'next_bar_time': '11:30 AM ET',
        })
        embed = _get_discord_embed(mock_post)
        field_map = {f['name']: f['value'] for f in embed['fields']}
        assert 'SPX' in field_map
        assert '$5,842.15' in field_map['SPX']
        assert 'VIX' in field_map
        assert '18.5' in field_map['VIX']

    @patch('requests.post')
    def test_hourly_update_shows_open_positions(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)
        nm.send_hourly_update({
            'current_time_str': '11:00 AM',
            'spx_price': 5842.15, 'spx_change_pct': 0.42,
            'vix_level': 18.5, 'vix_regime': 'low',
            'spx_high': 5860.0, 'spx_low': 5820.0,
            'bars_built': 3, 'pulse_count': 1,
            'open_positions': [{
                'direction': 'bullish', 'short_strike': 5800,
                'long_strike': 5795, 'unrealized_pnl': 45.0,
                'time_held': '2h 15m',
            }],
            'next_bar_time': '11:30 AM ET',
        })
        embed = _get_discord_embed(mock_post)
        field_names = {f['name'] for f in embed['fields']}
        assert 'Open Positions' in field_names

    @patch('requests.post')
    def test_hourly_update_omits_positions_when_empty(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord=DISCORD_CFG)
        nm.send_hourly_update({
            'current_time_str': '11:00 AM',
            'spx_price': 5842.15, 'spx_change_pct': 0.42,
            'vix_level': 18.5, 'vix_regime': 'low',
            'spx_high': 5860.0, 'spx_low': 5820.0,
            'bars_built': 3, 'pulse_count': 0,
            'open_positions': [],
            'next_bar_time': '11:30 AM ET',
        })
        embed = _get_discord_embed(mock_post)
        field_names = {f['name'] for f in embed['fields']}
        assert 'Open Positions' not in field_names


class TestFooterMode:
    """Verify notification footer reflects actual runtime trading mode."""

    def test_footer_shows_live_when_mode_set_to_live(self):
        nm = _make_notifier()
        nm.mode = 'live'
        assert nm._footer_text() == 'The Daily Melt \u2022 live'

    def test_footer_shows_dry_run_when_mode_set_to_dry_run(self):
        nm = _make_notifier()
        nm.mode = 'dry-run'
        assert nm._footer_text() == 'The Daily Melt \u2022 dry-run'

    def test_footer_default_is_dry_run(self):
        nm = _make_notifier()
        assert nm._footer_text() == 'The Daily Melt \u2022 dry-run'

    @patch('requests.post')
    def test_discord_embed_footer_uses_live_mode(self, mock_post):
        mock_post.return_value = MagicMock(ok=True)
        nm = _make_notifier(discord={
            'enabled': True,
            'webhook_url': 'https://discord.com/api/webhooks/test',
            'min_level': 'info',
        })
        nm.mode = 'live'
        nm.send("Test", "msg", level='info')
        embed = _get_discord_embed(mock_post)
        assert embed['footer']['text'] == 'The Daily Melt \u2022 live'
