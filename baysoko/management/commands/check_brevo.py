from django.core.management.base import BaseCommand
from baysoko.utils.email_helpers import check_brevo_credentials
import json

class Command(BaseCommand):
    help = 'Check Brevo API and SMTP credentials and report status'

    def handle(self, *args, **options):
        res = check_brevo_credentials()
        self.stdout.write('Brevo credentials check:')
        self.stdout.write(json.dumps(res, indent=2, default=str))
