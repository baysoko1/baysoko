from django.core.management.base import BaseCommand
from django.utils import timezone
from storefront.models import Subscription
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Mark subscriptions with expired trials as canceled (run manually or via cron)'

    def handle(self, *args, **options):
        now = timezone.now()
        expired = Subscription.objects.filter(status='trialing', trial_ends_at__lt=now)
        count = 0
        for sub in expired:
            try:
                sub.set_status('canceled')
                # Ensure trial_ended_at recorded
                if not sub.trial_ended_at:
                    sub.trial_ended_at = now
                    sub.save(update_fields=['trial_ended_at'])
                logger.info('Expired trial canceled: subscription=%s store=%s', sub.id, sub.store_id)
                count += 1
            except Exception:
                logger.exception('Failed to cancel expired subscription %s', sub.id)

        self.stdout.write(self.style.SUCCESS(f'Processed {count} expired trials'))
