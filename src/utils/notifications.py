import logging
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)

LEVEL_PRIORITY = {'info': 0, 'warning': 1, 'critical': 2}

LEVEL_COLORS = {
    'info':     {'slack': '#10b981', 'discord': 0x10b981},
    'warning':  {'slack': '#f59e0b', 'discord': 0xf59e0b},
    'critical': {'slack': '#ef4444', 'discord': 0xef4444},
}


class NotificationManager:
    """Manage email, SMS, Slack, Discord, and generic webhook notifications."""

    def __init__(self):
        from config.settings import EMAIL_CONFIG, SMS_CONFIG

        self.email_enabled = EMAIL_CONFIG['enabled']
        self.sms_enabled = SMS_CONFIG['enabled']
        self.email_config = EMAIL_CONFIG
        self.sms_config = SMS_CONFIG

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

    def _should_send(self, config: dict, level: str) -> bool:
        """Check if a webhook channel should fire for the given level."""
        if not config.get('enabled'):
            return False
        url = config.get('webhook_url') or config.get('url', '')
        if not url:
            return False
        min_level = config.get('min_level', 'info')
        return LEVEL_PRIORITY.get(level, 0) >= LEVEL_PRIORITY.get(min_level, 0)

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

    def _send_discord(self, subject: str, message: str, level: str):
        """Send Discord notification via webhook."""
        url = self.discord_config.get('webhook_url', '')
        color = LEVEL_COLORS.get(level, LEVEL_COLORS['info'])['discord']
        payload = {
            'embeds': [{
                'title': subject,
                'description': message,
                'color': color,
                'timestamp': datetime.now(timezone.utc).isoformat(),
                'footer': {'text': 'The Daily Melt'},
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

    def send_eod_summary(self, summary_data: dict):
        """Send end-of-day summary notification.

        Args:
            summary_data: dict with keys like trades_count, daily_pnl,
                weekly_pnl, monthly_pnl, win_rate, equity,
                no_trade_reason, bnb_signal
        """
        trades = summary_data.get('trades_count', 0)
        daily_pnl = summary_data.get('daily_pnl', 0.0)
        weekly_pnl = summary_data.get('weekly_pnl', 0.0)
        monthly_pnl = summary_data.get('monthly_pnl', 0.0)
        win_rate = summary_data.get('win_rate')
        equity = summary_data.get('equity')

        lines = [f"Trades: {trades}"]
        lines.append(f"Daily P&L: ${daily_pnl:+.2f}")
        lines.append(f"Weekly P&L: ${weekly_pnl:+.2f}")
        lines.append(f"Monthly P&L: ${monthly_pnl:+.2f}")
        if win_rate is not None:
            lines.append(f"Win Rate: {win_rate:.0f}%")
        if equity is not None:
            lines.append(f"Equity: ${equity:,.0f}")

        no_trade = summary_data.get('no_trade_reason')
        if no_trade and trades == 0:
            lines.append(f"No-trade reason: {no_trade}")

        bnb = summary_data.get('bnb_signal')
        if bnb:
            lines.append(f"B&B signal: {bnb}")

        self.send("End of Day Summary", "\n".join(lines), level='info')

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
