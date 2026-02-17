from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.conf import settings
import logging

logger = logging.getLogger(__name__)

try:
    from baysoko.utils.email_helpers import render_and_send
    from notifications.utils import notify_system_message, create_notification
except Exception:
    render_and_send = None

from .models import Store, StoreReview
from .models import Subscription, MpesaPayment
from django.db.models.signals import pre_save
from django.utils import timezone
import datetime


@receiver(post_save, sender=Store)
def store_saved(sender, instance, created, **kwargs):
    try:
        if not render_and_send:
            return
        ctx = {'store': instance, 'user': instance.owner, 'site_url': getattr(settings, 'SITE_URL', '')}
        if created:
            subject = f'Your store "{instance.name}" has been created'
            recipients = [e for e in [getattr(instance.owner, 'email', None)] if e]
            if recipients:
                render_and_send('emails/store_created.html', 'emails/store_created.txt', ctx, subject, recipients)
            # Create in-app notification for the owner
            try:
                notify_system_message(instance.owner, 'Store Created', f'Your store "{instance.name}" has been created.')
            except Exception:
                logger.exception('Failed to create in-app notification for store created')
        else:
            subject = f'Your store "{instance.name}" was updated'
            recipients = [e for e in [getattr(instance.owner, 'email', None)] if e]
            if recipients:
                render_and_send('emails/store_edited.html', 'emails/store_edited.txt', ctx, subject, recipients)
            try:
                notify_system_message(instance.owner, 'Store Updated', f'Your store "{instance.name}" was updated.')
            except Exception:
                logger.exception('Failed to create in-app notification for store updated')
    except Exception:
        logger.exception('Error sending store saved email')


@receiver(post_delete, sender=Store)
def store_deleted(sender, instance, **kwargs):
    try:
        if not render_and_send:
            return
        ctx = {'store': instance, 'user': instance.owner, 'site_url': getattr(settings, 'SITE_URL', '')}
        subject = f'Your store "{instance.name}" was deleted'
        recipients = [e for e in [getattr(instance.owner, 'email', None)] if e]
        if recipients:
            render_and_send('emails/store_deleted.html', 'emails/store_deleted.txt', ctx, subject, recipients)
        try:
            notify_system_message(instance.owner, 'Store Deleted', f'Your store "{instance.name}" was deleted.')
        except Exception:
            logger.exception('Failed to create in-app notification for store deleted')
    except Exception:
        logger.exception('Error sending store deleted email')


@receiver(post_save, sender=StoreReview)
def store_reviewed(sender, instance, created, **kwargs):
    if not created:
        return
    try:
        if not render_and_send:
            return
        store = instance.store
        owner = store.owner if store else None
        if owner and owner.email:
            subject = f'New review for your store "{store.name}"'
            ctx = {'review': instance, 'store': store, 'user': owner, 'site_url': getattr(settings, 'SITE_URL', '')}
            render_and_send('emails/store_reviewed.html', 'emails/store_reviewed.txt', ctx, subject, [owner.email])
            try:
                notify_system_message(owner, 'New Review', f'You received a new review for {store.name}.')
            except Exception:
                logger.exception('Failed to create in-app notification for store review')
    except Exception:
        logger.exception('Error sending store review notification')



@receiver(pre_save, sender=Subscription)
def subscription_pre_save(sender, instance, **kwargs):
    # capture previous status for change detection
    try:
        if instance.pk:
            orig = Subscription.objects.filter(pk=instance.pk).first()
            if orig:
                instance._previous_status = orig.status
    except Exception:
        pass


@receiver(post_save, sender=Subscription)
def subscription_changed(sender, instance, created, **kwargs):
    try:
        if not render_and_send:
            return

        owner_email = instance.store.owner.email if instance.store and instance.store.owner else None
        ctx = {
            'subscription': instance,
            'store': instance.store,
            'user': instance.store.owner if instance.store else None,
            'site_url': getattr(settings, 'SITE_URL', ''),
        }

        # Created: notify trial start or activation
        if created:
            recipients = [e for e in [owner_email] if e]
            if instance.status == 'trialing':
                subject = f'Your trial for {instance.store.name} has started'
                if recipients:
                    render_and_send('emails/subscription_trial_started.html', 'emails/subscription_trial_started.txt', ctx, subject, recipients)
                # Create in-app notification only if owner exists
                try:
                    if instance.store and getattr(instance.store, 'owner', None):
                        notify_system_message(instance.store.owner, 'Trial Started', f'Your trial for {instance.store.name} has started.')
                except Exception:
                    logger.exception('Failed to create in-app notification for subscription trial start')
            elif instance.status == 'active':
                subject = f'Your subscription for {instance.store.name} is active'
                if recipients:
                    render_and_send('emails/subscription_activated.html', 'emails/subscription_activated.txt', ctx, subject, recipients)
                try:
                    notify_system_message(instance.store.owner, 'Subscription Active', f'Your subscription for {instance.store.name} is active.')
                except Exception:
                    logger.exception('Failed to create in-app notification for subscription activated')
            return

        prev = getattr(instance, '_previous_status', None)

        # Status transitions
        if prev != instance.status:
            # Reactivated
            if instance.status == 'active' and prev in ('canceled', 'past_due', 'unpaid'):
                recipients = [e for e in [owner_email] if e]
                subject = f'Your subscription for {instance.store.name} has been reactivated'
                if recipients:
                    render_and_send('emails/subscription_reactivated.html', 'emails/subscription_reactivated.txt', ctx, subject, recipients)
                try:
                    notify_system_message(instance.store.owner, 'Subscription Reactivated', f'Your subscription for {instance.store.name} has been reactivated.')
                except Exception:
                    logger.exception('Failed to create in-app notification for subscription reactivated')

            if instance.status == 'past_due':
                recipients = [e for e in [owner_email] if e]
                subject = f'Payment past due for {instance.store.name}'
                if recipients:
                    render_and_send('emails/subscription_past_due.html', 'emails/subscription_past_due.txt', ctx, subject, recipients)
                try:
                    notify_system_message(instance.store.owner, 'Subscription Past Due', f'Payment for {instance.store.name} is past due.')
                except Exception:
                    logger.exception('Failed to create in-app notification for subscription past due')

            if instance.status == 'canceled':
                recipients = [e for e in [owner_email] if e]
                subject = f'Your subscription for {instance.store.name} was cancelled'
                if recipients:
                    render_and_send('emails/subscription_cancelled.html', 'emails/subscription_cancelled.txt', ctx, subject, recipients)
                try:
                    notify_system_message(instance.store.owner, 'Subscription Cancelled', f'Your subscription for {instance.store.name} was cancelled.')
                except Exception:
                    logger.exception('Failed to create in-app notification for subscription cancelled')

        # Almost due reminder: if current_period_end exists and within 3 days, and not previously notified
        try:
            if instance.current_period_end:
                now = timezone.now()
                days_left = (instance.current_period_end - now).days
                meta = instance.metadata or {}
                if days_left <= 3 and not meta.get('almost_due_notified'):
                    recipients = [e for e in [owner_email] if e]
                    subject = f'Subscription for {instance.store.name} is almost due'
                    if recipients:
                        render_and_send('emails/subscription_almost_due.html', 'emails/subscription_almost_due.txt', ctx, subject, recipients)
                    try:
                        notify_system_message(instance.store.owner, 'Subscription Almost Due', f'Subscription for {instance.store.name} is almost due in {days_left} days.')
                    except Exception:
                        logger.exception('Failed to create in-app notification for subscription almost due')
                    meta['almost_due_notified'] = True
                    instance.metadata = meta
                    # avoid recursive save loops by updating silently
                    Subscription.objects.filter(pk=instance.pk).update(metadata=meta)
        except Exception:
            pass


@receiver(post_save, sender=MpesaPayment)
def subscription_payment_event(sender, instance, created, **kwargs):
    try:
        if not render_and_send:
            return
        sub = instance.subscription
        owner_email = sub.store.owner.email if sub and sub.store and sub.store.owner else None
        ctx = {'payment': instance, 'subscription': sub, 'store': sub.store if sub else None, 'site_url': getattr(settings, 'SITE_URL', '')}

        # Notify on payment status
        if instance.status == 'completed':
            subject = f'Payment received for your subscription {sub.store.name}'
            recipients = [e for e in [owner_email] if e]
            if recipients:
                render_and_send('emails/subscription_payment_success.html', 'emails/subscription_payment_success.txt', ctx, subject, recipients)
        elif instance.status in ('failed', 'cancelled'):
            subject = f'Payment {instance.status} for subscription {sub.store.name}'
            recipients = [e for e in [owner_email] if e]
            if recipients:
                render_and_send('emails/subscription_payment_failed.html', 'emails/subscription_payment_failed.txt', ctx, subject, recipients)
    except Exception:
        logger.exception('Error sending subscription payment notification')
