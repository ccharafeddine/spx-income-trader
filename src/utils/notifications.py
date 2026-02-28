import logging
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional, Tuple, List, Dict

import pytz
import requests

logger = logging.getLogger(__name__)

LEVEL_PRIORITY = {'info': 0, 'warning': 1, 'critical': 2}

LEVEL_COLORS = {
    'info':     {'slack': '#10b981', 'discord': 3066993},     # Green
    'warning':  {'slack': '#f59e0b', 'discord': 16776960},    # Yellow
    'critical': {'slack': '#ef4444', 'discord': 15158332},    # Red
}

# Semantic colors for trade results (Discord int format)
DISCORD_GREEN = 3066993     # 0x2ECC71
DISCORD_RED = 15158332      # 0xE74C3C
DISCORD_YELLOW = 16776960   # 0xFFFF00
DISCORD_BLUE = 0x3b82f6


class NotificationManager:
    """Manage email, SMS, Slack, Discord, and generic webhook notifications."""

    def __init__(self):
        from config.settings import EMAIL_CONFIG, SMS_CONFIG

        self.email_enabled = EMAIL_CONFIG['enabled']
        self.sms_enabled = SMS_CONFIG['enabled']
        self.email_config = EMAIL_CONFIG
        self.sms_config = SMS_CONFIG
        self.mode = 'dry-run'  # Set by caller (main.py) after creation

        if self.email_enabled:
            logger.info("Email notifications enabled")
        if self.sms_enabled:
            logger.info("SMS notifications enabled")

        # Load webhook configs from strategy params
        self._load_webhook_config()

    def _load_webhook_config(self):
        """Load Slack/Discord/webhook config from strategy params."""
        try:
            from config.settings import load_strategy_params
            params = load_strategy_params()
            notif = params.get('notifications', {})
        except Exception:
            notif = {}

        self.slack_config = notif.get('slack', {})
        self.discord_config = notif.get('discord', {})
        self.webhook_config = notif.get('webhook', {})

        for name, cfg in [('Slack', self.slack_config),
                          ('Discord', self.discord_config),
                          ('Webhook', self.webhook_config)]:
            if cfg.get('enabled'):
                logger.info(f"{name} notifications enabled (min_level={cfg.get('min_level', 'info')})")

    def reload_config(self):
        """Re-read webhook config from strategy params (called on settings change)."""
        self._load_webhook_config()
        logger.info("Notification config reloaded")

    # ------------------------------------------------------------------
    # Core send (backward-compatible plain text)
    # ------------------------------------------------------------------

    def send(self, subject: str, message: str, level: str = 'info'):
        """Send notification via all enabled channels.

        Args:
            subject: Notification subject/title
            message: Notification message body
            level: 'info', 'warning', or 'critical'
        """
        # Email/SMS: no level filtering (backward compat)
        if self.email_enabled:
            self._send_email(subject, message)
        if self.sms_enabled:
            self._send_sms(subject, message)

        # Webhook channels: level-filtered
        if self._should_send(self.slack_config, level):
            self._send_slack(subject, message, level)
        if self._should_send(self.discord_config, level):
            self._send_discord(subject, message, level)
        if self._should_send(self.webhook_config, level):
            self._send_webhook(subject, message, level)

    def _send_rich(self, subject: str, message: str, level: str,
                   fields: Optional[List[Dict]] = None,
                   color: Optional[int] = None):
        """Send notification with rich Discord embeds, plain text fallback for other channels.

        Args:
            subject: Notification title
            message: Description text (shown below title in Discord embed)
            level: 'info', 'warning', or 'critical'
            fields: Optional list of Discord embed fields [{name, value, inline}]
            color: Optional override for Discord embed color (int)
        """
        # Email/SMS: plain text
        if self.email_enabled:
            plain = self._fields_to_plain(message, fields)
            self._send_email(subject, plain)
        if self.sms_enabled:
            plain = self._fields_to_plain(message, fields)
            self._send_sms(subject, plain)

        # Slack: plain text (Slack has its own field format, not worth maintaining)
        if self._should_send(self.slack_config, level):
            plain = self._fields_to_plain(message, fields)
            self._send_slack(subject, plain, level)

        # Discord: rich embed with fields
        if self._should_send(self.discord_config, level):
            self._send_discord_embed(subject, message, level,
                                     fields=fields, color=color)

        # Generic webhook: plain text
        if self._should_send(self.webhook_config, level):
            plain = self._fields_to_plain(message, fields)
            self._send_webhook(subject, plain, level)

    @staticmethod
    def _fields_to_plain(message: str, fields: Optional[List[Dict]]) -> str:
        """Convert embed fields to plain text for non-Discord channels."""
        if not fields:
            return message
        lines = [message] if message else []
        for f in fields:
            lines.append(f"{f['name']}: {f['value']}")
        return "\n".join(lines)

    def _should_send(self, config: dict, level: str) -> bool:
        """Check if a webhook channel should fire for the given level."""
        if not config.get('enabled'):
            return False
        url = config.get('webhook_url') or config.get('url', '')
        if not url:
            return False
        min_level = config.get('min_level', 'info')
        return LEVEL_PRIORITY.get(level, 0) >= LEVEL_PRIORITY.get(min_level, 0)

    # ------------------------------------------------------------------
    # Channel-specific senders
    # ------------------------------------------------------------------

    def _send_slack(self, subject: str, message: str, level: str):
        """Send Slack notification via incoming webhook."""
        url = self.slack_config.get('webhook_url', '')
        color = LEVEL_COLORS.get(level, LEVEL_COLORS['info'])['slack']
        payload = {
            'attachments': [{
                'color': color,
                'text': f"*{subject}*\n{message}",
                'fallback': f"{subject}: {message}",
            }]
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.ok:
                logger.info(f"Slack notification sent: {subject}")
            else:
                logger.warning(f"Slack webhook returned {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Failed to send Slack notification: {e}")

    def _footer_text(self) -> str:
        """Build footer string with mode."""
        return f"The Daily Melt \u2022 {self.mode}"

    def _send_discord(self, subject: str, message: str, level: str):
        """Send Discord notification via webhook (plain embed, no fields)."""
        url = self.discord_config.get('webhook_url', '')
        color = LEVEL_COLORS.get(level, LEVEL_COLORS['info'])['discord']
        payload = {
            'embeds': [{
                'title': subject,
                'description': message,
                'color': color,
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'footer': {'text': self._footer_text()},
            }]
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.ok:
                logger.info(f"Discord notification sent: {subject}")
            else:
                logger.warning(f"Discord webhook returned {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Failed to send Discord notification: {e}")

    def _send_discord_embed(self, subject: str, description: str, level: str,
                            fields: Optional[List[Dict]] = None,
                            color: Optional[int] = None):
        """Send Discord notification with structured embed fields.

        Args:
            subject: Embed title
            description: Embed description (below title)
            level: For level filtering only (color is set via color param)
            fields: List of {name, value, inline} dicts
            color: Discord embed color (int). If None, uses level default.
        """
        url = self.discord_config.get('webhook_url', '')
        if color is None:
            color = LEVEL_COLORS.get(level, LEVEL_COLORS['info'])['discord']

        embed = {
            'title': subject,
            'description': description,
            'color': color,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'footer': {'text': self._footer_text()},
        }
        if fields:
            embed['fields'] = fields

        payload = {'embeds': [embed]}
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.ok:
                logger.info(f"Discord embed sent: {subject}")
            else:
                logger.warning(f"Discord webhook returned {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Failed to send Discord embed: {e}")

    def _send_webhook(self, subject: str, message: str, level: str):
        """Send generic webhook notification."""
        url = self.webhook_config.get('url', '')
        payload = {
            'subject': subject,
            'message': message,
            'level': level,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'source': 'The Daily Melt',
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.ok:
                logger.info(f"Webhook notification sent: {subject}")
            else:
                logger.warning(f"Webhook returned {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.error(f"Failed to send webhook notification: {e}")

    # ------------------------------------------------------------------
    # Specialized rich notifications
    # ------------------------------------------------------------------

    def send_trade_entry(self, data: dict):
        """Send rich trade entry notification.

        Args:
            data: dict with keys: strategy, direction, short_strike, long_strike,
                  credit_per_contract, total_credit, quantity, breakeven, max_risk, expiry
        """
        strategy = data.get('strategy', 'Unknown')
        direction = data.get('direction', '').upper()
        short = data.get('short_strike', 0)
        long = data.get('long_strike', 0)
        credit_pc = data.get('credit_per_contract', 0)
        total_credit = data.get('total_credit', 0)
        qty = data.get('quantity', 0)
        breakeven = data.get('breakeven', 0)
        max_risk = data.get('max_risk', 0)
        expiry = data.get('expiry', '')

        fields = [
            {'name': 'Strategy', 'value': strategy, 'inline': True},
            {'name': 'Direction', 'value': direction, 'inline': True},
            {'name': 'Strikes', 'value': f"${short}/${long}", 'inline': True},
            {'name': 'Credit/Contract', 'value': f"${credit_pc:.2f}", 'inline': True},
            {'name': 'Total Credit', 'value': f"${total_credit:.2f}", 'inline': True},
            {'name': 'Quantity', 'value': str(qty), 'inline': True},
            {'name': 'Breakeven', 'value': f"${breakeven:,.2f}", 'inline': True},
            {'name': 'Max Risk', 'value': f"${max_risk:,.2f}", 'inline': True},
            {'name': 'Expiry', 'value': expiry or '0DTE', 'inline': True},
        ]

        self._send_rich(
            f"Trade Opened: {direction}",
            f"{strategy} credit spread entered",
            level='info',
            fields=fields,
            color=DISCORD_GREEN,
        )

    def send_trade_exit(self, data: dict):
        """Send rich trade exit notification.

        Args:
            data: dict with keys: strategy, direction, short_strike, long_strike,
                  pnl, pnl_pct, max_profit_pct, hold_duration, exit_reason
        """
        strategy = data.get('strategy', 'Unknown')
        direction = data.get('direction', '').upper()
        short = data.get('short_strike', 0)
        long = data.get('long_strike', 0)
        pnl = data.get('pnl', 0)
        pnl_pct = data.get('pnl_pct', 0)
        max_profit_pct = data.get('max_profit_pct', 0)
        duration = data.get('hold_duration', '')
        reason = data.get('exit_reason', '')

        is_win = pnl >= 0
        color = DISCORD_GREEN if is_win else DISCORD_RED

        fields = [
            {'name': 'Strategy', 'value': strategy, 'inline': True},
            {'name': 'Direction', 'value': direction, 'inline': True},
            {'name': 'Strikes', 'value': f"${short}/${long}", 'inline': True},
            {'name': 'P&L', 'value': f"${pnl:+.2f} ({pnl_pct:+.1f}%)", 'inline': True},
            {'name': '% Max Profit', 'value': f"{max_profit_pct:.0f}%", 'inline': True},
            {'name': 'Duration', 'value': duration or 'N/A', 'inline': True},
            {'name': 'Exit Reason', 'value': reason, 'inline': False},
        ]

        self._send_rich(
            f"Trade Closed: {'WIN' if is_win else 'LOSS'}",
            f"{strategy} {direction} ${short}/${long}",
            level='info',
            fields=fields,
            color=color,
        )

    def send_danger_alert(self, data: dict):
        """Send danger alert when short strike is breached.

        Args:
            data: dict with keys: direction, short_strike, long_strike,
                  current_price, distance, estimated_loss, time_remaining
        """
        direction = data.get('direction', '').upper()
        short = data.get('short_strike', 0)
        current_price = data.get('current_price', 0)
        distance = data.get('distance', 0)
        est_loss = data.get('estimated_loss', 0)
        time_remaining = data.get('time_remaining', '')

        fields = [
            {'name': 'Direction', 'value': direction, 'inline': True},
            {'name': 'Short Strike', 'value': f"${short:,.2f}", 'inline': True},
            {'name': 'SPX Price', 'value': f"${current_price:,.2f}", 'inline': True},
            {'name': 'Past Short By', 'value': f"${distance:+.2f}", 'inline': True},
            {'name': 'Est. Loss', 'value': f"${est_loss:,.2f}", 'inline': True},
            {'name': 'Time Left', 'value': time_remaining or 'N/A', 'inline': True},
        ]

        self._send_rich(
            "DANGER: Short Strike Breached",
            f"SPX ${current_price:,.2f} has crossed the ${short:,.2f} short strike",
            level='critical',
            fields=fields,
            color=DISCORD_RED,
        )

    def send_circuit_breaker(self, data: dict):
        """Send circuit breaker notification.

        Args:
            data: dict with keys: current_loss, loss_limit, message
        """
        current_loss = data.get('current_loss', 0)
        loss_limit = data.get('loss_limit', 0)
        message = data.get('message', '')

        fields = [
            {'name': 'Daily P&L', 'value': f"${current_loss:+.2f}", 'inline': True},
            {'name': 'Loss Limit', 'value': f"-${loss_limit:,.0f}", 'inline': True},
            {'name': 'Status', 'value': 'Halted for the day', 'inline': True},
        ]

        self._send_rich(
            "Daily Loss Limit Reached",
            message or "New trades blocked for the remainder of the session.",
            level='critical',
            fields=fields,
            color=DISCORD_RED,
        )

    def send_auto_restart(self, data: dict):
        """Send auto-restart notification from watchdog.

        Args:
            data: dict with keys: crash_reason, restart_count, mode
        """
        crash_reason = data.get('crash_reason', 'Unknown')
        restart_count = data.get('restart_count', 0)
        mode = data.get('mode', 'dry-run')

        fields = [
            {'name': 'Crash Reason', 'value': crash_reason, 'inline': False},
            {'name': 'Restart #', 'value': f"{restart_count}/3 this hour", 'inline': True},
            {'name': 'Mode', 'value': mode, 'inline': True},
        ]

        self._send_rich(
            "Bot Auto-Restarted",
            "The trading bot crashed and was automatically restarted by the watchdog.",
            level='warning',
            fields=fields,
            color=DISCORD_YELLOW,
        )

    def send_market_open(self, data: dict):
        """Send market open notification at 9:30 ET.

        Args:
            data: dict with keys: vix_level, vix_regime, open_positions, mode, spx_price
        """
        vix_level = data.get('vix_level')
        vix_regime = data.get('vix_regime', 'unknown')
        open_positions = data.get('open_positions', 0)
        mode = data.get('mode', 'dry-run')
        spx_price = data.get('spx_price')

        fields = [
            {'name': 'Mode', 'value': mode, 'inline': True},
        ]
        if spx_price is not None:
            fields.append({'name': 'SPX', 'value': f"${spx_price:,.2f}", 'inline': True})
        if vix_level is not None:
            fields.append({'name': 'VIX', 'value': f"{vix_level:.1f} ({vix_regime})", 'inline': True})
        # Omit VIX field entirely when unavailable (don't show "unknown")
        if open_positions > 0:
            fields.append({'name': 'Carry-Over Positions', 'value': str(open_positions), 'inline': True})

        self._send_rich(
            "Market Open",
            "Bot is now watching for trade setups.",
            level='info',
            fields=fields,
            color=DISCORD_BLUE,
        )

    def send_bot_started(self, data: dict):
        """Send enhanced bot started notification.

        Args:
            data: dict with keys: mode, equity, open_positions
        """
        mode = data.get('mode', 'dry-run')
        equity = data.get('equity')
        open_positions = data.get('open_positions', 0)

        fields = [
            {'name': 'Mode', 'value': mode, 'inline': True},
        ]
        if equity is not None:
            fields.append({'name': 'Equity', 'value': f"${equity:,.0f}", 'inline': True})
        fields.append({'name': 'Open Positions', 'value': str(open_positions), 'inline': True})

        et_now = datetime.now(pytz.timezone('America/New_York'))
        et_time_str = et_now.strftime('%I:%M %p ET').lstrip('0')

        self._send_rich(
            "Trading Bot Started",
            f"Started at {et_time_str}",
            level='info',
            fields=fields,
            color=DISCORD_GREEN,
        )

    def send_bot_stopped(self, data: dict):
        """Send enhanced bot stopped notification with shutdown diagnostics.

        Args:
            data: dict with keys: mode, equity, open_positions, trades_today, daily_pnl,
                  streak, best_trade, stop_reason, uptime, last_heartbeat
        """
        mode = data.get('mode', 'dry-run')
        equity = data.get('equity')
        open_positions = data.get('open_positions', 0)
        trades_today = data.get('trades_today', 0)
        daily_pnl = data.get('daily_pnl', 0)
        streak = data.get('streak', '')
        best_trade = data.get('best_trade', '')
        stop_reason = data.get('stop_reason', 'unknown')
        uptime = data.get('uptime', 'N/A')
        last_heartbeat = data.get('last_heartbeat', 'N/A')

        # Human-readable stop reason labels
        reason_labels = {
            'user_stop': 'User-initiated stop',
            'signal': 'OS signal (SIGINT/SIGTERM)',
            'thread_exit': 'Bot thread exited',
            'unknown': 'Unknown',
        }
        reason_display = reason_labels.get(stop_reason, stop_reason)

        # Color: red for crashes/errors, green for clean stops
        is_clean = stop_reason in ('user_stop', 'signal', 'thread_exit', 'unknown')
        color = DISCORD_GREEN if is_clean else DISCORD_RED

        fields = [
            {'name': 'Stop Reason', 'value': reason_display, 'inline': True},
            {'name': 'Uptime', 'value': uptime, 'inline': True},
            {'name': 'Last Heartbeat', 'value': last_heartbeat, 'inline': True},
            {'name': 'Mode', 'value': mode, 'inline': True},
            {'name': 'Trades Today', 'value': str(trades_today), 'inline': True},
            {'name': 'Daily P&L', 'value': f"${daily_pnl:+.2f}", 'inline': True},
        ]
        if streak:
            fields.append({'name': 'Streak', 'value': streak, 'inline': True})
        if best_trade:
            fields.append({'name': 'Best Trade', 'value': best_trade, 'inline': True})
        if equity is not None:
            fields.append({'name': 'Equity', 'value': f"${equity:,.0f}", 'inline': True})
        fields.append({'name': 'Open Positions', 'value': str(open_positions), 'inline': True})

        self._send_rich(
            "Trading Bot Stopped",
            "Bot has been shut down.",
            level='info',
            fields=fields,
            color=color,
        )

    def send_heartbeat_stale(self, data: dict):
        """Alert when bot heartbeat has gone stale (possible freeze).

        Args:
            data: dict with keys: last_heartbeat, stale_seconds, loop_count
        """
        last_hb = data.get('last_heartbeat', 'N/A')
        stale_secs = data.get('stale_seconds', 0)
        loop_count = data.get('loop_count', 0)

        stale_min = int(stale_secs // 60)

        fields = [
            {'name': 'Last Heartbeat', 'value': last_hb, 'inline': True},
            {'name': 'Stale Duration', 'value': f"{stale_min}m", 'inline': True},
            {'name': 'Loop Count', 'value': str(loop_count), 'inline': True},
        ]

        self._send_rich(
            "Heartbeat Stale - Possible Freeze",
            f"Bot heartbeat has not updated in {stale_min} minutes.",
            level='warning',
            fields=fields,
            color=DISCORD_YELLOW,
        )

    def send_eod_summary(self, summary_data: dict):
        """Send end-of-day summary notification with structured fields.

        Args:
            summary_data: dict with keys: trades_count, wins, losses, daily_pnl,
                weekly_pnl, monthly_pnl, win_rate, equity, streak,
                open_swings, no_trade_reason, bnb_signal,
                spx_close, spx_high, spx_low, spx_change_pct, close_time_str
        """
        trades = summary_data.get('trades_count', 0)
        wins = summary_data.get('wins', 0)
        losses = summary_data.get('losses', 0)
        daily_pnl = summary_data.get('daily_pnl', 0.0)
        weekly_pnl = summary_data.get('weekly_pnl', 0.0)
        monthly_pnl = summary_data.get('monthly_pnl', 0.0)
        win_rate = summary_data.get('win_rate')
        equity = summary_data.get('equity')
        streak = summary_data.get('streak', '')
        open_swings = summary_data.get('open_swings', [])
        spx_close = summary_data.get('spx_close')
        spx_high = summary_data.get('spx_high')
        spx_low = summary_data.get('spx_low')
        spx_change_pct = summary_data.get('spx_change_pct')
        close_time_str = summary_data.get('close_time_str')

        color = DISCORD_GREEN if daily_pnl >= 0 else DISCORD_RED

        fields = [
            {'name': 'Trades', 'value': f"{trades} ({wins}W / {losses}L)", 'inline': True},
            {'name': 'Daily P&L', 'value': f"${daily_pnl:+.2f}", 'inline': True},
        ]
        if streak:
            fields.append({'name': 'Streak', 'value': streak, 'inline': True})
        fields.append({'name': 'Weekly P&L', 'value': f"${weekly_pnl:+.2f}", 'inline': True})
        fields.append({'name': 'Monthly P&L', 'value': f"${monthly_pnl:+.2f}", 'inline': True})
        if win_rate is not None:
            fields.append({'name': 'Win Rate', 'value': f"{win_rate:.0f}%", 'inline': True})
        if equity is not None:
            fields.append({'name': 'Equity', 'value': f"${equity:,.0f}", 'inline': True})

        # SPX market data (inserted before no_trade_reason)
        if spx_close is not None:
            spx_val = f"${spx_close:,.2f}"
            if spx_change_pct is not None:
                spx_val += f" ({spx_change_pct:+.2f}%)"
            fields.append({'name': 'SPX Close', 'value': spx_val, 'inline': True})
        if spx_low is not None and spx_high is not None:
            fields.append({'name': 'Session Range', 'value': f"${spx_low:,.2f} \u2013 ${spx_high:,.2f}", 'inline': True})

        if open_swings:
            swing_lines = []
            for s in open_swings:
                swing_lines.append(
                    f"{s.get('direction', '').upper()} ${s.get('short_strike', 0)}/{s.get('long_strike', 0)} "
                    f"P&L: ${s.get('unrealized_pnl', 0):+.2f}"
                )
            fields.append({
                'name': 'Open Swing Positions',
                'value': "\n".join(swing_lines) or 'None',
                'inline': False,
            })

        no_trade = summary_data.get('no_trade_reason')
        if no_trade and trades == 0:
            fields.append({'name': 'No-Trade Reason', 'value': no_trade, 'inline': False})

        bnb = summary_data.get('bnb_signal')
        if bnb:
            fields.append({'name': 'B&B Signal', 'value': bnb, 'inline': True})

        # Build subtitle with ET close time
        if close_time_str:
            subtitle = f"Market closed at {close_time_str} ET - daily results"
        else:
            subtitle = "Market closed - daily results"

        self._send_rich(
            "End of Day Summary",
            subtitle,
            level='info',
            fields=fields,
            color=color,
        )

    def send_hourly_update(self, data: dict):
        """Send hourly market update notification during market hours.

        Args:
            data: dict with keys: current_time_str, spx_price, spx_change_pct,
                vix_level, vix_regime, spx_high, spx_low, bars_built, pulse_count,
                open_positions (list of dicts), next_bar_time
        """
        current_time_str = data.get('current_time_str', '')
        spx_price = data.get('spx_price')
        spx_change_pct = data.get('spx_change_pct')
        vix_level = data.get('vix_level')
        vix_regime = data.get('vix_regime', '')
        spx_high = data.get('spx_high')
        spx_low = data.get('spx_low')
        bars_built = data.get('bars_built', 0)
        pulse_count = data.get('pulse_count', 0)
        open_positions = data.get('open_positions', [])
        next_bar_time = data.get('next_bar_time', '')

        # Color based on SPX change
        if spx_change_pct is not None and spx_change_pct < -1.0:
            color = DISCORD_RED
        elif spx_change_pct is not None and spx_change_pct < -0.5:
            color = DISCORD_YELLOW
        else:
            color = DISCORD_BLUE

        pulse_str = f"{pulse_count} pulse bar{'s' if pulse_count != 1 else ''}" if pulse_count > 0 else "No pulse bars yet"

        fields = []
        if spx_price is not None:
            spx_val = f"${spx_price:,.2f}"
            if spx_change_pct is not None:
                spx_val += f" ({spx_change_pct:+.2f}%)"
            fields.append({'name': 'SPX', 'value': spx_val, 'inline': True})
        if vix_level is not None:
            fields.append({'name': 'VIX', 'value': f"{vix_level:.1f} ({vix_regime})", 'inline': True})
        if spx_low is not None and spx_high is not None:
            fields.append({'name': 'Session Range', 'value': f"${spx_low:,.2f} \u2013 ${spx_high:,.2f}", 'inline': True})
        fields.append({'name': 'Bars Built', 'value': f"{bars_built} bars built", 'inline': True})
        fields.append({'name': 'Pulse Bars', 'value': pulse_str, 'inline': True})
        if next_bar_time:
            fields.append({'name': 'Next Bar', 'value': next_bar_time, 'inline': True})

        if open_positions:
            pos_lines = []
            for p in open_positions:
                pos_lines.append(
                    f"{p.get('direction', '').upper()} {p.get('short_strike', 0)}/{p.get('long_strike', 0)} "
                    f"\u2022 P&L: ${p.get('unrealized_pnl', 0):+.2f} \u2022 {p.get('time_held', 'N/A')}"
                )
            fields.append({
                'name': 'Open Positions',
                'value': "\n".join(pos_lines),
                'inline': False,
            })

        self._send_rich(
            f"Market Update \u2014 {current_time_str} ET",
            f"{bars_built} bars built \u2022 {pulse_str}",
            level='info',
            fields=fields,
            color=color,
        )

    # ------------------------------------------------------------------
    # Utility methods
    # ------------------------------------------------------------------

    def send_test(self, channel: str) -> Tuple[bool, Optional[str]]:
        """Send a test notification to a single channel.

        Returns:
            (success, error_message) tuple
        """
        subject = "Test Notification"
        message = "This is a test from The Daily Melt. If you see this, your webhook is working!"
        level = 'info'

        try:
            if channel == 'slack':
                url = self.slack_config.get('webhook_url', '')
                if not url:
                    return False, "Slack webhook URL is empty"
                self._send_slack(subject, message, level)
            elif channel == 'discord':
                url = self.discord_config.get('webhook_url', '')
                if not url:
                    return False, "Discord webhook URL is empty"
                self._send_discord(subject, message, level)
            elif channel == 'webhook':
                url = self.webhook_config.get('url', '')
                if not url:
                    return False, "Webhook URL is empty"
                self._send_webhook(subject, message, level)
            else:
                return False, f"Unknown channel: {channel}"
            return True, None
        except Exception as e:
            return False, str(e)

    def _send_email(self, subject: str, message: str):
        """Send email notification."""
        try:
            msg = MIMEMultipart()
            msg['From'] = self.email_config['address']
            msg['To'] = self.email_config['address']
            msg['Subject'] = f"[The Daily Melt] {subject}"

            body = MIMEText(message, 'plain')
            msg.attach(body)

            with smtplib.SMTP(self.email_config['smtp_server'], self.email_config['smtp_port']) as server:
                server.starttls()
                server.login(self.email_config['username'], self.email_config['password'])
                server.send_message(msg)

            logger.info(f"Email sent: {subject}")

        except Exception as e:
            logger.error(f"Failed to send email: {e}")

    def _send_sms(self, subject: str, message: str):
        """Send SMS notification via Twilio."""
        try:
            from twilio.rest import Client

            client = Client(
                self.sms_config['twilio_sid'],
                self.sms_config['twilio_token']
            )

            sms_message = client.messages.create(
                body=f"{subject}\n\n{message}",
                from_=self.sms_config['from_number'],
                to=self.sms_config['to_number']
            )

            logger.info(f"SMS sent: {sms_message.sid}")

        except ImportError:
            logger.error("Twilio package not installed. Install with: pip install twilio")
        except Exception as e:
            logger.error(f"Failed to send SMS: {e}")
