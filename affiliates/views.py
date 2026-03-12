from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required
from django.conf import settings
from django.db.models import Sum

from .models import AffiliateProfile, AffiliateClick, AffiliateAttribution, AffiliateCommission, AffiliatePayout


@login_required
def affiliate_dashboard(request):
    profile, _ = AffiliateProfile.objects.get_or_create(user=request.user)
    link_base = getattr(settings, 'SITE_URL', '').rstrip('/')
    affiliate_link = f"{link_base}/?{getattr(settings, 'AFFILIATE_QUERY_PARAM', 'aid')}={profile.code}" if link_base else f"/?aid={profile.code}"

    clicks = AffiliateClick.objects.filter(affiliate=profile)
    attributions = AffiliateAttribution.objects.filter(affiliate=profile)
    commissions = AffiliateCommission.objects.filter(affiliate=profile)

    stats = {
        'clicks': clicks.count(),
        'referrals': attributions.count(),
        'pending_commissions': commissions.filter(status='pending').count(),
        'approved_commissions': commissions.filter(status='approved').count(),
        'paid_commissions': commissions.filter(status='paid').count(),
        'total_commissions': commissions.aggregate(total=Sum('amount')).get('total') or 0,
        'pending_amount': commissions.filter(status='pending').aggregate(total=Sum('amount')).get('total') or 0,
        'paid_amount': commissions.filter(status='paid').aggregate(total=Sum('amount')).get('total') or 0,
    }

    recent_commissions = commissions.select_related('order').order_by('-created_at')[:10]

    context = {
        'affiliate_profile': profile,
        'affiliate_link': affiliate_link,
        'stats': stats,
        'recent_commissions': recent_commissions,
    }
    return render(request, 'affiliates/dashboard.html', context)


@login_required
def affiliate_commissions(request):
    profile, _ = AffiliateProfile.objects.get_or_create(user=request.user)
    commissions = AffiliateCommission.objects.filter(affiliate=profile).select_related('order').order_by('-created_at')
    payouts = AffiliatePayout.objects.filter(affiliate=profile).order_by('-created_at')[:10]
    return render(request, 'affiliates/commissions.html', {
        'affiliate_profile': profile,
        'commissions': commissions,
        'payouts': payouts,
    })


@staff_member_required
def affiliate_admin_dashboard(request):
    stats = {
        'total_affiliates': AffiliateProfile.objects.filter(is_active=True).count(),
        'total_clicks': AffiliateClick.objects.count(),
        'total_referrals': AffiliateAttribution.objects.count(),
        'pending_commissions': AffiliateCommission.objects.filter(status='pending').aggregate(total=Sum('amount')).get('total') or 0,
        'paid_commissions': AffiliateCommission.objects.filter(status='paid').aggregate(total=Sum('amount')).get('total') or 0,
    }
    recent_commissions = AffiliateCommission.objects.select_related('affiliate', 'order').order_by('-created_at')[:12]
    recent_payouts = AffiliatePayout.objects.select_related('affiliate').order_by('-created_at')[:8]
    return render(request, 'affiliates/admin_dashboard.html', {
        'stats': stats,
        'recent_commissions': recent_commissions,
        'recent_payouts': recent_payouts,
    })


def affiliate_terms(request):
    return render(request, 'affiliates/terms.html', {})
