from django.core.management.base import BaseCommand
from django.conf import settings
import requests
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Verify configured BREVO_SENDER_EMAIL exists in Brevo account (best-effort).'

    def handle(self, *args, **options):
        api_key = getattr(settings, 'BREVO_API_KEY', None)
        sender = getattr(settings, 'BREVO_SENDER_EMAIL', None) or None

        if not api_key:
            self.stderr.write('BREVO_API_KEY not configured in settings or environment.')
            return

        if not sender:
            self.stdout.write('No BREVO_SENDER_EMAIL configured. Set BREVO_SENDER_EMAIL in .env to the validated sender address.')

        endpoints = [
            'https://api.brevo.com/v3/smtp/senders',
            'https://api.brevo.com/v3/senders',
        ]

        headers = {'api-key': api_key}
        found = False
        for url in endpoints:
            try:
                resp = requests.get(url, headers=headers, timeout=10)
                if resp.status_code == 200:
                    try:
                        body = resp.json()
                    except Exception:
                        body = None
                    # Try to locate sender email in returned body
                    emails = []
                    if isinstance(body, dict):
                        # common shapes: {'senders': [...]} or list-like under other keys
                        if 'senders' in body and isinstance(body['senders'], list):
                            for s in body['senders']:
                                e = s.get('email') or s.get('sender')
                                if e:
                                    emails.append(e)
                        else:
                            # flatten values that look like senders
                            for v in body.values():
                                if isinstance(v, list):
                                    for s in v:
                                        if isinstance(s, dict):
                                            e = s.get('email') or s.get('sender')
                                            if e:
                                                emails.append(e)
                    elif isinstance(body, list):
                        for s in body:
                            if isinstance(s, dict):
                                e = s.get('email') or s.get('sender')
                                if e:
                                    emails.append(e)

                    if sender and sender in emails:
                        self.stdout.write(self.style.SUCCESS(f'Configured BREVO_SENDER_EMAIL {sender} found in Brevo account via {url}'))
                        found = True
                        break
                    else:
                        # Show a sample of returned senders for manual comparison
                        sample = ', '.join(emails[:10]) or str(body)
                        self.stdout.write(f'Checked {url} — did not find configured sender. Sample senders/response: {sample}')
                else:
                    self.stdout.write(f'Brevo returned status {resp.status_code} for {url} — body: {getattr(resp, "text", "")[:200]}')
            except Exception as e:
                logger.exception('Error calling Brevo endpoint %s', url)
                self.stdout.write(f'Error calling {url}: {e}')

        if not found:
            self.stderr.write('Brevo sender not verified by API. Confirm the sender in the Brevo dashboard (Senders / Sender Domains) and set BREVO_SENDER_EMAIL to the validated address.')
