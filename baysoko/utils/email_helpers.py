import os
import logging
import threading
from django.core.mail import get_connection, EmailMultiAlternatives, send_mail
from django.conf import settings
from django.template.loader import render_to_string
import requests

logger = logging.getLogger(__name__)


def send_email_brevo(subject, plain_message, html_message, to_emails):
    """
    Send an email using Brevo API if available, otherwise fall back to Django SMTP.
    Runs synchronously – intended to be called from a background thread.
    """
    brevo_key = (
        os.environ.get('BREVO_API_KEY') or
        os.environ.get('SENDINBLUE_API_KEY') or
        os.environ.get('SIB_API_KEY')
    )
    if not brevo_key:
        email_host = getattr(settings, 'EMAIL_HOST', '').lower()
        if 'brevo' in email_host or 'sendinblue' in email_host:
            brevo_key = os.environ.get('EMAIL_HOST_PASSWORD') or getattr(settings, 'EMAIL_HOST_PASSWORD', None)

    if brevo_key:
        headers = {
            'accept': 'application/json',
            'api-key': brevo_key,
            'content-type': 'application/json',
        }
        sender = {
            'name': os.environ.get('EMAIL_FROM_NAME', 'Baysoko'),
            'email': os.environ.get('SMTP_ENVELOPE_FROM') or settings.EMAIL_HOST_USER or settings.DEFAULT_FROM_EMAIL
        }
        to = [{'email': email} for email in to_emails]
        payload = {
            'sender': sender,
            'to': to,
            'subject': subject,
            'textContent': plain_message,
            'htmlContent': html_message,
        }

        # Try a few times for transient network issues
        attempts = 3
        for attempt in range(1, attempts + 1):
            try:
                resp = requests.post(
                    'https://api.brevo.com/v3/smtp/email',
                    json=payload,
                    headers=headers,
                    timeout=10
                )
                if 200 <= resp.status_code < 300:
                    logger.info('Email sent via Brevo API to %s (attempt %s)', to_emails, attempt)
                    return
                # For 4xx, don't retry; for 5xx, try again
                if 400 <= resp.status_code < 500:
                    logger.warning('Brevo API returned client error %s: %s', resp.status_code, resp.text)
                    break
                logger.warning('Brevo API returned server error %s (attempt %s): %s', resp.status_code, attempt, resp.text)
            except requests.exceptions.SSLError as e:
                logger.warning('Brevo API SSL error (attempt %s), will not retry: %s', attempt, e)
                break
            except requests.exceptions.RequestException as e:
                logger.warning('Brevo API request exception (attempt %s): %s', attempt, e)
                # brief backoff for next attempt
                import time
                time.sleep(0.8 * attempt)
            except Exception:
                logger.exception('Unexpected exception when calling Brevo API (attempt %s)', attempt)
                import time
                time.sleep(0.8 * attempt)
        logger.warning('Brevo API send failed after %s attempts, falling back to SMTP', attempts)

    # Fallback to SMTP. Force use of SMTP backend (avoid console backend in DEBUG).
    envelope_from = os.environ.get('SMTP_ENVELOPE_FROM') or settings.EMAIL_HOST_USER or settings.DEFAULT_FROM_EMAIL
    try:
        # Force the SMTP backend to ensure an actual SMTP connection is used even in DEBUG
        connection = get_connection(backend='django.core.mail.backends.smtp.EmailBackend')
        msg = EmailMultiAlternatives(
            subject=subject,
            body=plain_message,
            from_email=envelope_from,
            to=to_emails,
            connection=connection,
        )
        if html_message:
            msg.attach_alternative(html_message, 'text/html')
        msg.send(fail_silently=False)
        logger.info('Email sent via SMTP to %s', to_emails)
    except Exception:
        logger.exception('SMTP sending failed, trying send_mail as last resort')
        try:
            # Ensure we use a real SMTP connection for the final fallback as well
            final_conn = get_connection(backend='django.core.mail.backends.smtp.EmailBackend')
            send_mail(
                subject,
                plain_message,
                envelope_from,
                to_emails,
                html_message=html_message,
                connection=final_conn,
                fail_silently=False
            )
        except Exception:
            logger.exception('Final send_mail fallback also failed')


def _send_email_threaded(subject, plain_message, html_message, to_emails):
    """Run send_email_brevo in a background thread."""
    def _send():
        try:
            send_email_brevo(subject, plain_message, html_message, to_emails)
        except Exception:
            logger.exception('Background email send failed')
    t = threading.Thread(target=_send, daemon=True)
    t.start()


def render_and_send(template_html, template_txt, context, subject, to_emails):
    html = render_to_string(template_html, context) if template_html else ''
    plain = render_to_string(template_txt, context) if template_txt else ''
    _send_email_threaded(subject, plain, html, to_emails)
