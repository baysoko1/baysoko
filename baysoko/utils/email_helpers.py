import os
import logging
import threading
from django.core.mail import get_connection, EmailMultiAlternatives, send_mail
from django.conf import settings
from django.template.loader import render_to_string
import requests
from django.utils.html import strip_tags
from django.utils.html import escape
from django.contrib.auth import get_user_model
from django.conf import settings
from baysoko.utils.sms import send_sms_brevo

logger = logging.getLogger(__name__)


def _brevo_sender_email():
    return (
        getattr(settings, 'BREVO_SENDER_EMAIL', None)
        or getattr(settings, 'DEFAULT_FROM_EMAIL', None)
        or os.environ.get('BREVO_SENDER_EMAIL')
        or os.environ.get('SMTP_ENVELOPE_FROM')
        or os.environ.get('EMAIL_HOST_USER')
        or '00peteromondi@10654604.brevosend.com'
    )


def _brevo_sender_name():
    return os.environ.get('EMAIL_FROM_NAME') or getattr(settings, 'EMAIL_FROM_NAME', 'Baysoko')


def _has_brevo_smtp_config():
    smtp_host = (getattr(settings, 'EMAIL_HOST', '') or os.environ.get('EMAIL_HOST', '')).lower()
    smtp_user = getattr(settings, 'EMAIL_HOST_USER', '') or os.environ.get('EMAIL_HOST_USER', '')
    smtp_pass = getattr(settings, 'EMAIL_HOST_PASSWORD', '') or os.environ.get('EMAIL_HOST_PASSWORD', '')
    return ('brevo' in smtp_host or 'sendinblue' in smtp_host) and bool(smtp_user and smtp_pass)


def send_email_brevo(subject, plain_message, html_message, to_emails):
    """
    Send an email using Brevo API if available, otherwise fall back to Django SMTP.
    Runs synchronously – intended to be called from a background thread.
    """
    # Always attempt Brevo API first if an explicit API key is configured.
    # In development (`DEBUG=True`) we still attempt the Brevo API/SMTP so
    # behavior matches production.
    if getattr(settings, 'DEBUG', False):
        logger.info('DEBUG mode detected — attempting Brevo API/SMTP for email sending')

    # Prefer using a real Brevo/API key when provided. Do NOT attempt to
    # reuse the SMTP password as an API key — that produces 401 errors.
    # Prefer the value exposed on Django settings (populated by python-decouple)
    # since `.env` is read by settings via `decouple.config`. Fall back to
    # environment variables if not present on `settings`.
    brevo_key = (
        getattr(settings, 'BREVO_API_KEY', None) or
        os.environ.get('BREVO_API_KEY') or
        os.environ.get('SENDINBLUE_API_KEY') or
        os.environ.get('SIB_API_KEY')
    )

    # Prepare sender info used by the Brevo API request
    sender = {
        'name': _brevo_sender_name(),
        'email': _brevo_sender_email(),
    }

    if not brevo_key:
        if _has_brevo_smtp_config():
            logger.info('BREVO_API_KEY not configured; using Brevo SMTP relay fallback for email send.')
        else:
            logger.error('Brevo email is not configured: missing BREVO_API_KEY and usable Brevo SMTP credentials.')
            raise RuntimeError('Brevo email is not configured. Set BREVO_API_KEY or Brevo SMTP credentials.')

    # Only call the Brevo HTTP API if an explicit API key is configured.
    if brevo_key:
        headers = {
            'accept': 'application/json',
            'api-key': brevo_key,
            'content-type': 'application/json',
        }
        try:
            masked = (brevo_key[:8] + '...' + brevo_key[-8:]) if brevo_key and len(brevo_key) > 16 else brevo_key
        except Exception:
            masked = 'REDACTED'
        logger.debug('Attempting Brevo API send; api-key=%s, from=%s, to=%s', masked, sender.get('email'), to_emails)
        to = [{'email': email} for email in to_emails]
        # Ensure htmlContent is present for Brevo API: fall back to escaped plain text wrapped in minimal HTML
        safe_html = html_message if (html_message and str(html_message).strip()) else f"<pre>{escape(plain_message or '')}</pre>"
        safe_text = plain_message or strip_tags(safe_html) or ''
        payload = {
            'sender': sender,
            'to': to,
            'subject': subject,
            'textContent': safe_text,
            'htmlContent': safe_html,
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
                logger.debug('Brevo API responded status=%s', getattr(resp, 'status_code', None))
                if 200 <= resp.status_code < 300:
                    # Log Brevo response body (contains messageId) for delivery tracing
                    try:
                        j = resp.json()
                    except Exception:
                        j = getattr(resp, 'text', None)
                    logger.info('Email sent via Brevo API to %s (attempt %s) response=%s', to_emails, attempt, j)
                    return
                # For 4xx, don't retry; for 5xx, try again
                if 400 <= resp.status_code < 500:
                    # Client errors often include a JSON body with details
                    try:
                        err = resp.json()
                    except Exception:
                        err = resp.text
                    if resp.status_code == 401:
                        logger.warning('Brevo API unauthorized (401): %s. Falling back to configured email backend.', err)
                    else:
                        logger.warning('Brevo API returned client error %s: %s', resp.status_code, err)
                    break
                try:
                    err = resp.json()
                except Exception:
                    err = resp.text
                logger.warning('Brevo API returned server error %s (attempt %s): %s', resp.status_code, attempt, err)
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
        logger.warning('Brevo API send failed after %s attempts, falling back to SMTP relay/backend', attempts)

    # Fallback to configured email backend. Use the Brevo sender address
    # as envelope-from when available to keep provider headers consistent.
    envelope_from = os.environ.get('SMTP_ENVELOPE_FROM') or _brevo_sender_email()
    logger.debug('Using envelope_from=%s for fallback send', envelope_from)
    try:
        # Use default connection so Django picks up `settings.EMAIL_BACKEND`.
        connection = get_connection()
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
        logger.exception('Email send via default backend failed, trying send_mail fallback')
        try:
            # Final fallback: use Django's send_mail with default connection
            final_conn = get_connection()
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
            # Also attempt to send an SMS to users who have phone numbers configured
            try:
                User = get_user_model()
                # Short SMS-friendly message
                sms_body = strip_tags(html_message or plain_message or subject)
                if sms_body and len(sms_body) > 320:
                    sms_body = sms_body[:317] + '...'
                for email in (to_emails or []):
                    try:
                        user = User.objects.filter(email__iexact=email).first()
                        if user and getattr(settings, 'BREVO_SMS_ENABLED', False) and getattr(user, 'phone_number', None):
                            # Only send SMS when phone is present; do not require phone_verified for backward compatibility
                            send_sms_brevo(user.phone_number, f"{subject}: {sms_body}")
                    except Exception:
                        logger.exception('Failed sending SMS notification for %s', email)
            except Exception:
                logger.exception('Unexpected error when attempting SMS sends after email')
        except Exception:
            logger.exception('Background email send failed')

    # If running in DEBUG or using the console email backend, send synchronously so output
    # appears in the current process (useful for short-lived manage.py shell runs).
    try:
        email_backend = getattr(settings, 'EMAIL_BACKEND', '') or ''
        if getattr(settings, 'DEBUG', False) or 'console' in email_backend:
            _send()
            return
    except Exception:
        # If settings are not available for any reason, fall back to threaded send
        pass

    t = threading.Thread(target=_send, daemon=True)
    t.start()


def render_and_send(template_html, template_txt, context, subject, to_emails):
    html = render_to_string(template_html, context) if template_html else ''
    plain = render_to_string(template_txt, context) if template_txt else ''
    try:
        send_email_brevo(subject, plain, html, to_emails)
        return
    except Exception:
        logger.exception('Brevo email send failed from render_and_send')
        raise


def check_brevo_credentials(timeout=5):
    """Validate Brevo credentials.

    Returns a dict with 'api' and 'smtp' status info. Does not raise on failure.
    """
    results = {'api': {'available': False, 'status': None, 'detail': None},
               'smtp': {'available': False, 'status': None, 'detail': None}}
    # Check API key
    api_key = getattr(settings, 'BREVO_API_KEY', None) or os.environ.get('BREVO_API_KEY') or os.environ.get('SENDINBLUE_API_KEY') or os.environ.get('SIB_API_KEY')
    if api_key:
        try:
            resp = requests.get('https://api.brevo.com/v3/account', headers={'api-key': api_key}, timeout=timeout)
            results['api']['status'] = resp.status_code
            if 200 <= resp.status_code < 300:
                results['api']['available'] = True
                try:
                    results['api']['detail'] = resp.json()
                except Exception:
                    results['api']['detail'] = resp.text
            else:
                try:
                    results['api']['detail'] = resp.json()
                except Exception:
                    results['api']['detail'] = resp.text
        except Exception as e:
            results['api']['detail'] = str(e)

    # Check SMTP
    smtp_host = getattr(settings, 'EMAIL_HOST', os.environ.get('EMAIL_HOST'))
    smtp_port = int(getattr(settings, 'EMAIL_PORT', os.environ.get('EMAIL_PORT') or 587))
    smtp_user = getattr(settings, 'EMAIL_HOST_USER', os.environ.get('EMAIL_HOST_USER'))
    smtp_pass = getattr(settings, 'EMAIL_HOST_PASSWORD', os.environ.get('EMAIL_HOST_PASSWORD') or os.environ.get('EMAIL_HOST_PASSWORD'))
    if smtp_host and smtp_user and smtp_pass:
        import smtplib
        try:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=timeout)
            server.ehlo()
            if smtp_port == 587:
                server.starttls()
                server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.quit()
            results['smtp']['available'] = True
            results['smtp']['status'] = 'ok'
        except Exception as e:
            results['smtp']['detail'] = str(e)
    else:
        results['smtp']['detail'] = 'SMTP config incomplete'

    return results
