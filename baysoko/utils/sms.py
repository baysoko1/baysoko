import os
import time
import requests
from requests.exceptions import RequestException, SSLError
from django.conf import settings
import logging
from .phone import normalize_phone_number

logger = logging.getLogger(__name__)

BREVO_API_URL = 'https://api.brevo.com/v3/transactionalSMS/sms'


def send_sms_brevo(to_number: str, message: str) -> dict:
    """Send an SMS via Brevo transactional SMS API.

    Expects `BREVO_API_KEY` and `BREVO_SMS_SENDER` to be set in Django settings or environment.
    Returns the parsed JSON response or raises an exception on HTTP error.
    """
    api_key = getattr(settings, 'BREVO_API_KEY', os.environ.get('BREVO_API_KEY', ''))
    sender = getattr(settings, 'BREVO_SMS_SENDER', os.environ.get('BREVO_SMS_SENDER', 'Baysoko'))
    enabled = getattr(settings, 'BREVO_SMS_ENABLED', os.environ.get('BREVO_SMS_ENABLED', False))

    if not api_key:
        logger.warning('BREVO_API_KEY not configured; skipping SMS send')
        return {'success': False, 'error': 'Brevo API key not configured'}

    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'api-key': api_key,
    }

    # Normalize number to E.164 where possible
    normalized = normalize_phone_number(to_number)
    if not normalized:
        logger.warning('Unable to normalize phone number: %s', to_number)
        return {'success': False, 'error': 'invalid_phone_number', 'original': to_number}

    # Brevo expects a `recipients` array with objects containing an `msisdn` field.
    # Include both singular `recipient` and `recipients` for API compatibility
    payload = {
        'sender': sender,
        'content': message,
        'recipient': normalized,
        'recipients': [
            {'msisdn': normalized},
        ],
    }

    logger.debug('Prepared Brevo payload: %s', payload)

    # Robust send with retries and backoff for transient network/SSL issues
    max_attempts = getattr(settings, 'BREVO_MAX_ATTEMPTS', 3)
    backoff_base = getattr(settings, 'BREVO_BACKOFF_BASE', 1)
    attempt = 0
    last_exc = None
    while attempt < max_attempts:
        attempt += 1
        try:
            resp = requests.post(BREVO_API_URL, json=payload, headers=headers, timeout=10)
            try:
                resp.raise_for_status()
            except requests.HTTPError:
                body = None
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text
                logger.error('Brevo SMS failed (%s): %s', resp.status_code, body)
                return {'success': False, 'status_code': resp.status_code, 'body': body}

            data = resp.json()
            logger.info('Brevo SMS sent to %s: %s', to_number, data)
            return {'success': True, 'response': data}

        except SSLError as sx:
            # SSL handshake issues can be transient; retry a few times
            last_exc = sx
            logger.warning('Brevo SSLError on attempt %d/%d: %s', attempt, max_attempts, sx)
        except RequestException as re:
            last_exc = re
            logger.warning('Brevo RequestException on attempt %d/%d: %s', attempt, max_attempts, re)

        # Backoff before next attempt
        if attempt < max_attempts:
            sleep_for = backoff_base * (2 ** (attempt - 1)) + (0.5 * attempt)
            try:
                time.sleep(sleep_for)
            except Exception:
                pass

    # All attempts failed
    logger.exception('Failed to send Brevo SMS after %d attempts: %s', max_attempts, last_exc)
    return {'success': False, 'error': str(last_exc)}
