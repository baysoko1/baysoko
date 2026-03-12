from decimal import Decimal, ROUND_HALF_UP
from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
from django.utils import timezone
from django.conf import settings
from django.contrib.auth import get_user_model

from listings.models import Order
from .models import AffiliateProfile, AffiliateCommission, AffiliateAttribution


@receiver(pre_save, sender=AffiliateProfile)
def _affiliate_profile_presave(sender, instance, **kwargs):
    instance.ensure_code()


@receiver(post_save, sender=get_user_model())
def _ensure_affiliate_profile(sender, instance, created, **kwargs):
    if not created:
        return
    try:
        AffiliateProfile.objects.get_or_create(user=instance)
    except Exception:
        return


@receiver(post_save, sender=Order)
def _affiliate_commission_on_paid(sender, instance, created, **kwargs):
    try:
        if instance.status != 'paid':
            return
        if AffiliateCommission.objects.filter(order=instance).exists():
            return

        attribution = AffiliateAttribution.objects.filter(user=instance.user).select_related('affiliate').first()
        if not attribution or not attribution.affiliate or not attribution.affiliate.is_active:
            return
        # Avoid self-referral
        if attribution.affiliate.user_id == instance.user_id:
            return

        rate = getattr(attribution.affiliate, 'default_rate', None)
        if rate is None:
            rate = getattr(settings, 'AFFILIATE_DEFAULT_RATE', 0.05)
        total = instance.total_price
        try:
            amount = (total * Decimal(rate)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        except Exception:
            amount = total * rate

        AffiliateCommission.objects.create(
            affiliate=attribution.affiliate,
            order=instance,
            amount=amount,
            rate=rate,
            status='pending',
        )
    except Exception:
        return
