from django.core.management.base import BaseCommand
from django.conf import settings
from baysoko.utils.email_helpers import send_email_brevo


class Command(BaseCommand):
    help = 'Send a test email via Brevo API/SMTP to verify delivery'

    def add_arguments(self, parser):
        parser.add_argument('--to', '-t', help='Recipient email address', required=True)

    def handle(self, *args, **options):
        to = options.get('to')
        subject = 'Baysoko Test Email (Brevo)'
        html = f'<p>This is a test email sent at server time.</p>'
        plain = 'This is a test email sent at server time.'
        from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', None) or getattr(settings, 'EMAIL_HOST_USER', None)
        self.stdout.write(f'Sending test email to {to} via Brevo API/SMTP...')
        try:
            send_email_brevo(subject, plain, html, [to])
            self.stdout.write(self.style.SUCCESS('send_email_brevo invoked — check Brevo dashboard or inbox for delivery.'))
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error invoking send_email_brevo: {e}'))
