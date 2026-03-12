from django.utils import timezone
from django.conf import settings

from .models import AffiliateProfile, AffiliateClick, AffiliateAttribution


class AffiliateMiddleware:
    """Capture affiliate codes from query params and attach affiliate to request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        affiliate = None
        code = None
        try:
            param = getattr(settings, 'AFFILIATE_QUERY_PARAM', 'aid')
            code = request.GET.get(param) or request.GET.get('ref') or request.GET.get('affiliate')
            if code:
                code = code.strip()
                affiliate = AffiliateProfile.objects.filter(code=code, is_active=True).first()
        except Exception:
            affiliate = None

        # Resolve from session/cookie if not supplied.
        if not affiliate:
            try:
                session_code = request.session.get('affiliate_code')
                cookie_code = request.COOKIES.get(getattr(settings, 'AFFILIATE_COOKIE_NAME', 'affiliate_code'))
                code = session_code or cookie_code
                if code:
                    affiliate = AffiliateProfile.objects.filter(code=code, is_active=True).first()
            except Exception:
                affiliate = None

        request.affiliate = affiliate

        response = self.get_response(request)

        if affiliate:
            try:
                request.session['affiliate_code'] = affiliate.code
                request.session['affiliate_last_touch'] = timezone.now().isoformat()
                max_age = int(getattr(settings, 'AFFILIATE_COOKIE_AGE', 60 * 60 * 24 * 30))
                response.set_cookie(
                    getattr(settings, 'AFFILIATE_COOKIE_NAME', 'affiliate_code'),
                    affiliate.code,
                    max_age=max_age,
                    samesite='Lax',
                )
            except Exception:
                pass

            try:
                # Record click once per session for this affiliate.
                session_key = request.session.session_key or ''
                click_key = f"affiliate_click_logged:{affiliate.code}"
                if not request.session.get(click_key):
                    AffiliateClick.objects.create(
                        affiliate=affiliate,
                        user=request.user if request.user.is_authenticated else None,
                        session_key=session_key,
                        ip_address=self._get_ip(request),
                        user_agent=request.META.get('HTTP_USER_AGENT', ''),
                        path=request.get_full_path(),
                    )
                    request.session[click_key] = True
            except Exception:
                pass

            # Attribute user to affiliate when logged in.
            try:
                if request.user.is_authenticated:
                    attr, created = AffiliateAttribution.objects.get_or_create(
                        user=request.user,
                        defaults={
                            'affiliate': affiliate,
                            'first_touch_at': timezone.now(),
                            'last_touch_at': timezone.now(),
                            'source': 'link',
                        }
                    )
                    if not created and attr.affiliate_id != affiliate.id:
                        attr.affiliate = affiliate
                        attr.last_touch_at = timezone.now()
                        attr.save(update_fields=['affiliate', 'last_touch_at'])
                    elif not created:
                        attr.last_touch_at = timezone.now()
                        attr.save(update_fields=['last_touch_at'])
            except Exception:
                pass

        return response

    def _get_ip(self, request):
        try:
            forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
            if forwarded:
                return forwarded.split(',')[0].strip()
            return request.META.get('REMOTE_ADDR')
        except Exception:
            return None
