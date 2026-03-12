from django.contrib import admin
from django.utils import timezone
from django.db.models import Sum
from .models import AffiliateProfile, AffiliateClick, AffiliateAttribution, AffiliateCommission, AffiliatePayout


@admin.register(AffiliateProfile)
class AffiliateProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'code', 'is_active', 'default_rate', 'created_at')
    search_fields = ('user__username', 'code')
    list_filter = ('is_active',)


@admin.register(AffiliateClick)
class AffiliateClickAdmin(admin.ModelAdmin):
    list_display = ('affiliate', 'user', 'ip_address', 'created_at')
    search_fields = ('affiliate__user__username', 'user__username', 'ip_address')
    list_filter = ('created_at',)


@admin.register(AffiliateAttribution)
class AffiliateAttributionAdmin(admin.ModelAdmin):
    list_display = ('user', 'affiliate', 'first_touch_at', 'last_touch_at', 'source')
    search_fields = ('user__username', 'affiliate__user__username')


@admin.register(AffiliateCommission)
class AffiliateCommissionAdmin(admin.ModelAdmin):
    list_display = ('affiliate', 'order', 'amount', 'rate', 'status', 'created_at')
    search_fields = ('affiliate__user__username', 'order__id')
    list_filter = ('status', 'created_at')
    actions = ['mark_commissions_paid']

    def mark_commissions_paid(self, request, queryset):
        unpaid = queryset.filter(status__in=['pending', 'approved'])
        for affiliate_id in unpaid.values_list('affiliate_id', flat=True).distinct():
            aff_qs = unpaid.filter(affiliate_id=affiliate_id)
            total = aff_qs.aggregate(total=Sum('amount')).get('total') or 0
            if total <= 0:
                continue
            payout = AffiliatePayout.objects.create(
                affiliate_id=affiliate_id,
                amount=total,
                status='paid',
                paid_at=timezone.now(),
                reference=f"PAYOUT-{affiliate_id}-{int(timezone.now().timestamp())}",
            )
            aff_qs.update(status='paid', paid_at=timezone.now())
        self.message_user(request, "Selected commissions marked as paid and payout records created.")
    mark_commissions_paid.short_description = "Mark selected commissions as paid"


@admin.register(AffiliatePayout)
class AffiliatePayoutAdmin(admin.ModelAdmin):
    list_display = ('affiliate', 'amount', 'status', 'reference', 'created_at', 'paid_at')
    search_fields = ('affiliate__user__username', 'reference')
    list_filter = ('status', 'created_at')
