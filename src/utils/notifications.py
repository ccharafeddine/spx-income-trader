import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

logger = logging.getLogger(__name__)


class NotificationManager:
    """
    Manage email and SMS notifications
    """
    
    def __init__(self):
        # Import settings here to avoid circular imports
        from config.settings import EMAIL_CONFIG, SMS_CONFIG
        
        self.email_enabled = EMAIL_CONFIG['enabled']
        self.sms_enabled = SMS_CONFIG['enabled']
        self.email_config = EMAIL_CONFIG
        self.sms_config = SMS_CONFIG
        
        if self.email_enabled:
            logger.info("Email notifications enabled")
        if self.sms_enabled:
            logger.info("SMS notifications enabled")
    
    def send(self, subject: str, message: str):
        """
        Send notification via all enabled channels
        
        Args:
            subject: Notification subject/title
            message: Notification message body
        """
        if self.email_enabled:
            self._send_email(subject, message)
        
        if self.sms_enabled:
            self._send_sms(subject, message)
    
    def _send_email(self, subject: str, message: str):
        """Send email notification"""
        try:
            msg = MIMEMultipart()
            msg['From'] = self.email_config['address']
            msg['To'] = self.email_config['address']
            msg['Subject'] = f"[SPX Trader] {subject}"
            
            body = MIMEText(message, 'plain')
            msg.attach(body)
            
            # Connect to SMTP server
            with smtplib.SMTP(self.email_config['smtp_server'], self.email_config['smtp_port']) as server:
                server.starttls()
                server.login(self.email_config['username'], self.email_config['password'])
                server.send_message(msg)
            
            logger.info(f"Email sent: {subject}")
            
        except Exception as e:
            logger.error(f"Failed to send email: {e}")
    
    def _send_sms(self, subject: str, message: str):
        """Send SMS notification via Twilio"""
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