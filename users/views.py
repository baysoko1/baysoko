# users/views.py - Refactored with password reset modal and unified email sending

import os
import re
import json
import random
import string
import secrets
import logging
import threading
import contextlib
import smtplib
import traceback
from datetime import timedelta
# avoid importing stdlib EmailMessage to prevent name collisions with Django's
from urllib.parse import urlencode

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth import login, update_session_auth_hash, authenticate
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.views import PasswordChangeView, LoginView, LogoutView
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib import messages
from django.contrib.messages import get_messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.sites.models import Site
from django.views.generic import DetailView, UpdateView
from django.urls import reverse_lazy, reverse
from django.http import JsonResponse, HttpResponse
from django.core.paginator import Paginator
from django.core.mail import send_mail, get_connection, EmailMessage as DjangoEmailMessage, EmailMultiAlternatives
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import models, transaction, IntegrityError
from django.conf import settings
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_http_methods
from django.template.loader import render_to_string
from django.http import JsonResponse
from django.core.cache import cache
from .ws_token_store import get_token as ws_get_token, delete_token as ws_delete_token
from django.conf import settings
from django.middleware.csrf import get_token
from django.utils.http import url_has_allowed_host_and_scheme
from urllib.parse import unquote

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException, SSLError
from urllib3.util.retry import Retry
from allauth.socialaccount.models import SocialApp, SocialAccount

from .models import User
from .forms import (
    CustomUserCreationForm,
    CustomUserChangeForm,
    CustomAuthenticationForm,
)

from listings.models import Listing

logger = logging.getLogger(__name__)


def _build_google_oauth_session():
    """Create a retrying requests session for Google OAuth endpoints."""
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=0.6,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": "BaysokoOAuth/1.0",
        "Accept": "application/json",
    })
    return session


def _redirect_after_google_error(request, message=None):
    """Preserve current flow while surfacing a friendly OAuth error."""
    action = request.session.get('oauth_action')
    if action == 'connect' and request.user.is_authenticated:
        messages.error(
            request,
            message or "Google account connection could not be completed. Please retry from your profile page."
        )
        return redirect('profile-edit', pk=request.user.pk)

    next_url = _normalize_next_url(request.session.get('post_login_redirect'))
    if _is_delivery_next(next_url):
        request.session['delivery_login_intent'] = True
        messages.error(
            request,
            message or "We could not complete Google sign-in for Delivery right now. Please retry from the Delivery login page."
        )
        return redirect('delivery:login')
    messages.error(
        request,
        message or "We could not complete Google sign-in for Baysoko Marketplace right now. Please retry from the marketplace sign-in page."
    )
    return redirect('register')


def _get_social_callback_base_url(request=None):
    site_url = (getattr(settings, 'SITE_URL', '') or os.environ.get('SITE_URL', '')).strip().rstrip('/')
    if site_url:
        return site_url

    if request is not None:
        scheme = 'http' if settings.DEBUG else 'https'
        return f"{scheme}://{request.get_host()}"

    try:
        current_site = Site.objects.get_current()
        scheme = 'http' if settings.DEBUG else 'https'
        return f"{scheme}://{current_site.domain}"
    except Exception:
        return 'http://localhost:8000' if settings.DEBUG else 'https://baysoko.up.railway.app'


def _get_social_callback_uri(provider, request=None):
    return f"{_get_social_callback_base_url(request)}/accounts/{provider}/callback/"

def _sync_from_delivery_profile(user):
    """Best-effort sync of missing marketplace profile fields from delivery profile."""
    try:
        delivery_profile = getattr(user, 'delivery_profile', None)
        if not delivery_profile:
            return False
        updated = False
        if not user.phone_number and getattr(delivery_profile, 'phone_number', None):
            user.phone_number = delivery_profile.phone_number
            updated = True
        if not user.location:
            loc = getattr(delivery_profile, 'city', '') or getattr(delivery_profile, 'address', '')
            if loc:
                user.location = loc
                updated = True
        if updated:
            user.save(update_fields=['phone_number', 'location'])
        return updated
    except Exception:
        logger.exception('Failed to sync delivery profile for user %s', getattr(user, 'id', None))
        return False

def _normalize_next_url(next_url):
    if not next_url:
        return None
    next_url = str(next_url).strip()
    if not next_url.startswith('/'):
        return None
    if next_url.startswith('//'):
        return None
    return next_url

def _is_delivery_next(next_url):
    return bool(next_url and next_url.startswith('/delivery'))


def _complete_google_identity_login(request, userinfo, action=None):
    email = (userinfo.get('email') or '').strip().lower()
    if not email:
        messages.error(request, "Email not provided by Google")
        return redirect('register')

    action = action or request.session.get('oauth_action')
    if action == 'connect' and request.user.is_authenticated:
        if email != (request.user.email or '').lower():
            messages.error(request, 'Google account email does not match your account email.')
            return redirect('profile-edit', pk=request.user.pk)
        uid = userinfo.get('id') or userinfo.get('sub')
        if uid and not SocialAccount.objects.filter(user=request.user, provider='google', uid=uid).exists():
            SocialAccount.objects.create(user=request.user, provider='google', uid=uid, extra_data=userinfo)
        messages.success(request, 'Google account connected successfully.')
        return redirect('profile-edit', pk=request.user.pk)

    try:
        users = User.objects.filter(email__iexact=email).order_by('date_joined', 'id')
        if users.count() > 1:
            logger.warning("Google sign-in: multiple users found for email=%s; using earliest account.", email)
        user = users.first()
        if not user:
            raise User.DoesNotExist()
        login(request, user, backend='django.contrib.auth.backends.ModelBackend')
        _sync_from_delivery_profile(user)
        next_url = _normalize_next_url(request.session.pop('post_login_redirect', None))
        is_delivery_flow = _is_delivery_next(next_url) or bool(request.session.get('delivery_login_intent'))
        if is_delivery_flow:
            if not getattr(user, 'email_verified', False):
                request.session['post_verify_redirect'] = next_url or reverse('delivery:dashboard')
                request.session['delivery_login_intent'] = True
                return redirect('verification_required')
            request.session['delivery_auth'] = True
            request.session.pop('delivery_login_intent', None)
            return redirect(next_url or reverse('delivery:dashboard'))
        if not user.phone_number:
            messages.info(request, 'Please verify your details and include phone number to continue.')
            return redirect('profile-edit', pk=user.pk)
        if not user.location:
            messages.info(request, 'Please add your location to continue.')
            return redirect('profile-edit', pk=user.pk)
        messages.success(request, f"Welcome back, {user.first_name}!")
        return redirect('home')
    except User.DoesNotExist:
        username = email.split('@')[0]
        original = username
        counter = 1
        while User.objects.filter(username=username).exists():
            username = f"{original}{counter}"
            counter += 1

        pending_location = (request.session.pop('pending_location', '') or '').strip()
        user = User.objects.create(
            email=email,
            username=username,
            first_name=userinfo.get('given_name', ''),
            last_name=userinfo.get('family_name', ''),
            location=pending_location,
            phone_number=None,
            is_active=True
        )
        user.set_unusable_password()
        code = ''.join(random.choices(string.digits, k=7))
        user.email_verification_code = code
        user.email_verification_sent_at = timezone.now()
        user.email_verified = False
        user.save()

        send_verification_email(user)
        try:
            send_welcome_email(user)
        except Exception:
            logger.exception('Failed to queue welcome email for social signup')

        login(request, user, backend='django.contrib.auth.backends.ModelBackend')
        next_url = _normalize_next_url(request.session.pop('post_login_redirect', None))
        is_delivery_flow = _is_delivery_next(next_url) or bool(request.session.get('delivery_login_intent'))
        if is_delivery_flow:
            request.session['post_verify_redirect'] = next_url or reverse('delivery:dashboard')
            request.session['delivery_login_intent'] = True
            messages.success(request, 'Account created. Please verify your email to continue.')
            return redirect('verification_required')
        request.session['just_registered'] = True
        request.session['just_registered_message'] = 'Account created with Google! Check your email to verify your account.'
        if not user.location:
            messages.info(request, 'Please add your location to continue.')
            return redirect('profile-edit', pk=user.pk)
        return redirect('verification_required')

from baysoko.utils.email_helpers import _send_email_threaded, send_email_brevo, render_and_send
from notifications.utils import notify_system_message
from notifications.utils import create_and_broadcast_notification
from baysoko.utils.sms import send_sms_brevo

def send_verification_email(user):
    subject = 'Verify your email for Baysoko'
    # Build an absolute or relative verification link so email buttons can
    # include a one-click verify action that pre-fills the code and triggers
    # automatic verification when clicked.
    site_url = os.environ.get('SITE_URL') or getattr(settings, 'SITE_URL', None) or os.environ.get('SITE_DOMAIN') or ''
    if site_url and not site_url.startswith('http'):
        site_url = f'https://{site_url}'
    # normalize to avoid double slashes when concatenating with paths
    site_url = site_url.rstrip('/')
    verify_path = reverse('verify_email') + f'?user_id={user.id}&code={user.email_verification_code}'
    html_message = render_to_string('users/verification_email.html', {
        'user': user,
        'code': user.email_verification_code,
        'site_name': 'Baysoko',
        'site_url': site_url,
        'verify_path': verify_path,
    })
    plain_message = f'Your verification code is: {user.email_verification_code}'
    _send_email_threaded(subject, plain_message, html_message, [user.email])

def send_welcome_email(user):
    subject = 'Welcome to Baysoko'
    # include a verify link so the welcome email button can perform a one-click verify
    site_url = os.environ.get('SITE_URL') or getattr(settings, 'SITE_URL', None) or os.environ.get('SITE_DOMAIN') or ''
    if site_url and not site_url.startswith('http'):
        site_url = f'https://{site_url}'
    site_url = site_url.rstrip('/')
    verify_path = reverse('verify_email') + f'?user_id={user.id}&code={user.email_verification_code}'
    html_message = render_to_string('users/welcome_email.html', {
        'user': user,
        'site_name': 'Baysoko',
        'site_url': site_url,
        'verify_path': verify_path,
    })
    plain_message = render_to_string('users/welcome_email.txt', {
        'user': user,
        'site_url': getattr(settings, 'SITE_URL', '/')
    })
    _send_email_threaded(subject, plain_message, html_message, [user.email])

def send_password_reset_code(user):
    """Generate and send a 7‑digit code for password reset."""
    code = ''.join(random.choices(string.digits, k=7))
    user.password_reset_code = code
    user.password_reset_sent_at = timezone.now()
    user.password_reset_attempts = 0
    user.password_reset_last_attempt_date = timezone.now().date()
    user.save()

    subject = 'Your Baysoko Password Reset Code'
    html_message = render_to_string('users/password_reset_code_email.html', {
        'user': user,
        'code': code,
        'site_name': 'Baysoko',
    })
    plain_message = f'Your password reset code is: {code}'
    _send_email_threaded(subject, plain_message, html_message, [user.email])


def ws_login_complete(request):
    """Complete a WebSocket-initiated login by setting the session cookie.

    The WebSocket consumer generates a one-time token mapping to the
    server-side session key and returns a URL to this view. When the
    browser visits this URL, the view sets the `sessionid` cookie so
    subsequent HTTP requests carry the authenticated session.
    """
    token = request.GET.get('token')
    next_url = request.GET.get('next') or reverse('home')
    try:
        next_url = unquote(next_url)
    except Exception:
        next_url = reverse('home')

    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        next_url = reverse('home')

    if not token:
        return redirect(next_url)

    cache_key = f"ws_login_{token}"
    # try cache first, then fallback to in-process store
    try:
        session_key = cache.get(cache_key)
    except Exception:
        session_key = None

    if session_key is None:
        session_key = ws_get_token(cache_key)

    if not session_key:
        # token missing or expired
        return redirect(reverse('login'))

    response = redirect(next_url)
    response.set_cookie(
        settings.SESSION_COOKIE_NAME,
        session_key,
        max_age=settings.SESSION_COOKIE_AGE,
        httponly=settings.SESSION_COOKIE_HTTPONLY,
        secure=settings.SESSION_COOKIE_SECURE,
        samesite=getattr(settings, 'SESSION_COOKIE_SAMESITE', 'Lax')
    )

    # Ensure CSRF cookie exists
    get_token(request)

    try:
        cache.delete(cache_key)
    except Exception:
        pass
    try:
        ws_delete_token(cache_key)
    except Exception:
        pass

    return response


@require_http_methods(['POST'])
def clear_welcome_toast(request):
    try:
        request.session.pop('welcome_toast', None)
    except Exception:
        pass
    return JsonResponse({'ok': True})

# ----------------------------------------------------------------------
#  Registration
# ----------------------------------------------------------------------

@ensure_csrf_cookie
def register(request):
    if request.user.is_authenticated:
        return redirect('home')

    if request.method == 'POST':
        form = CustomUserCreationForm(request.POST)
        if form.is_valid():
            try:
                phone = form.cleaned_data.get('phone_number')
                if phone and User.objects.filter(phone_number=phone).exists():
                    form.add_error('phone_number', 'A user with that phone number already exists.')
                    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                        return JsonResponse({'success': False, 'errors': form.errors.get_json_data()})
                    return render(request, 'users/register.html', {'form': form})

                user = form.save(commit=False)
                if not user.location:
                    try:
                        pending_location = (request.session.get('pending_location') or '').strip()
                        if pending_location:
                            user.location = pending_location
                    except Exception:
                        pass
                # ensure empty phone stored as NULL
                if not getattr(user, 'phone_number', None):
                    user.phone_number = None
                # Generate verification code
                code = ''.join(random.choices(string.digits, k=7))
                user.email_verification_code = code
                user.email_verification_sent_at = timezone.now()
                user.verification_attempts_today = 0
                user.last_verification_attempt_date = timezone.now().date()

                try:
                    with transaction.atomic():
                        user.save()
                except IntegrityError as ie:
                    # Handle race condition on unique phone/email
                    msg = 'A user with that phone number or email already exists.'
                    ie_msg = str(ie).lower() if ie else ''
                    logger.warning(f"IntegrityError saving user: {ie}")
                    if 'phone_number' in ie_msg or 'phone' in ie_msg:
                        form.add_error('phone_number', 'A user with that phone number already exists.')
                    elif 'email' in ie_msg:
                        form.add_error('email', 'A user with that email already exists.')
                    else:
                        form.add_error(None, msg)

                    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                        return JsonResponse({'success': False, 'errors': form.errors.get_json_data()})
                    messages.error(request, msg)
                    return render(request, 'users/register.html', {'form': form})

                # Send verification email
                send_verification_email(user)

                # Log the user in
                login(request, user, backend='django.contrib.auth.backends.ModelBackend')
                request.session['just_registered'] = True
                request.session['just_registered_message'] = (
                    'Registration successful. Please check your email for verification code.'
                )
                # Create and broadcast welcome notification
                try:
                    create_and_broadcast_notification(
                        recipient=user,
                        notification_type='system',
                        title='Welcome to Baysoko',
                        message='Welcome to Baysoko! Your account was created successfully. Start exploring now.',
                        action_url='/',
                        action_text='Start Exploring'
                    )
                except Exception:
                    logger.exception('Failed to create/broadcast welcome notification')

                # Request-level toast so client shows immediate welcome message
                try:
                    request.session['welcome_toast'] = {
                        'title': 'Welcome to Baysoko',
                        'message': 'Registration successful. Please check your email for verification code.',
                        'variant': 'success',
                        'duration': 8000
                    }
                except Exception:
                    pass

                # Send welcome email (non‑blocking)
                try:
                    send_welcome_email(user)
                except Exception:
                    logger.exception('Failed to queue welcome email')

                if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                    return JsonResponse({
                        'success': True,
                        'message': 'Registration successful. Please check your email for verification code.',
                        'user_id': user.id
                    })
                return redirect('verification_required')

            except Exception as e:
                logger.error(f"Registration error: {str(e)}", exc_info=True)
                if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                    return JsonResponse({'success': False, 'errors': {'__all__': str(e)}})
                messages.error(request, 'An error occurred during registration.')
        else:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'errors': form.errors.get_json_data()})
            return render(request, 'users/register.html', {'form': form})

    else:
        form = CustomUserCreationForm()

    return render(request, 'users/register.html', {'form': form})

# ----------------------------------------------------------------------
#  Email verification
# ----------------------------------------------------------------------
from .utils import verify_email_logic   # import the helper

@csrf_exempt
def verify_email(request):
    # Support one-click verification via GET (from email button)
    # and AJAX/POST verification from the verification modal.
    redirect_after = False
    if request.method == 'GET' and (request.GET.get('user_id') or request.GET.get('code')):
        user_id = request.GET.get('user_id')
        code = request.GET.get('code')
        redirect_after = True
    elif request.method == 'POST':
        user_id = request.POST.get('user_id')
        code = request.POST.get('code')
    else:
        return JsonResponse({'success': False, 'error': 'Invalid request.'})

    if not user_id or not code:
        if redirect_after:
            context = {
                'message': 'Missing verification parameters.',
                'redirect_to': reverse('verification_required'),
                'countdown': 6
            }
            return render(request, 'users/verify_result.html', context)
        return JsonResponse({'success': False, 'error': 'Missing user_id or code.'})

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        if redirect_after:
            context = {
                'message': 'User not found.',
                'redirect_to': reverse('verification_required'),
                'countdown': 6
            }
            return render(request, 'users/verify_result.html', context)
        return JsonResponse({'success': False, 'error': 'User not found.'})

    # Call the helper function
    success, error_msg, attempts_left, redirect_url = verify_email_logic(user, code)

    if success:
        # Log the user in if not already authenticated (for POST/JSON case)
        if not request.user.is_authenticated:
            login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            try:
                # Create and broadcast welcome notification on verification login
                create_and_broadcast_notification(
                    recipient=user,
                    notification_type='system',
                    title='Welcome to Baysoko',
                    message='Your email has been verified. Welcome to Baysoko!',
                    action_url='/',
                    action_text='Start Exploring'
                )
            except Exception:
                logger.exception('Failed to create/broadcast welcome notification on verify')
            try:
                request.session['welcome_toast'] = {
                    'title': 'Welcome to Baysoko',
                    'message': 'Your email has been verified. Welcome!',
                    'variant': 'success',
                    'duration': 8000
                }
            except Exception:
                pass

        # Delivery flow: honor post-verify redirect and keep delivery session separate
        post_verify_redirect = request.session.pop('post_verify_redirect', None)
        if post_verify_redirect and _is_delivery_next(post_verify_redirect):
            try:
                request.session['delivery_auth'] = True
                request.session.pop('delivery_login_intent', None)
            except Exception:
                pass
            # If user doesn't have a delivery profile, take them to completion first
            try:
                if not hasattr(user, 'delivery_profile') and not hasattr(user, 'delivery_person'):
                    post_verify_redirect = reverse('delivery:profile_complete')
            except Exception:
                pass
            if redirect_after:
                messages.success(request, 'Email verified! Redirecting to delivery...')
                return redirect(post_verify_redirect)
            return JsonResponse({'success': True, 'redirect': post_verify_redirect})

        # If the user has a phone number and it's not yet phone_verified, redirect to phone verification flow
        if getattr(user, 'phone_number', None) and not getattr(user, 'phone_verified', False):
            # build verify phone URL
            phone_verify_url = reverse('verify_phone') + f'?user_id={user.id}'
            if redirect_after:
                messages.success(request, 'Email verified! Redirecting to phone verification...')
                return redirect(phone_verify_url)
            return JsonResponse({'success': True, 'redirect': phone_verify_url})

        if redirect_after:
            messages.success(request, 'Email verified! Redirecting...')
            return redirect(redirect_url)

        return JsonResponse({'success': True, 'redirect': redirect_url})
    else:
        # Verification failed
        if redirect_after:
            context = {
                'message': error_msg,
                'redirect_to': reverse('verification_required'),
                'countdown': 6
            }
            return render(request, 'users/verify_result.html', context)

        return JsonResponse({
            'success': False,
            'error': error_msg,
            'attempts_left': attempts_left
        })

@csrf_exempt
def resend_code(request):
    try:
        # Diagnostic logging
        content_type = request.META.get('CONTENT_TYPE') or request.headers.get('content-type')
        content_length = int(request.META.get('CONTENT_LENGTH') or 0)
        logger.info(
            "resend_code called: method=%s content_type=%s content_length=%s remote_addr=%s",
            request.method,
            content_type,
            content_length,
            request.META.get('REMOTE_ADDR') or request.META.get('HTTP_X_FORWARDED_FOR')
        )

        if request.method not in ('POST', 'GET'):
            return JsonResponse({'success': False, 'error': 'Invalid request method.'})

        user_id = None
        try:
            user_id = request.POST.get('user_id') or request.GET.get('user_id')
        except Exception as e:
            logger.warning('Could not read request.POST: %s', e)

        if not user_id:
            try:
                body = request.body.decode('utf-8') if getattr(request, 'body', None) else ''
                if body:
                    data = json.loads(body)
                    user_id = data.get('user_id')
            except Exception:
                pass

        if not user_id:
            logger.info('resend_code missing user_id')
            return JsonResponse({'success': False, 'error': 'Missing user_id.'})

        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'User not found.'})

        now = timezone.now()
        today = now.date()

        if user.last_verification_attempt_date != today:
            user.verification_attempts_today = 0
            user.last_verification_attempt_date = today

        # Enforce 60s cooldown
        if user.email_verification_sent_at and (now - user.email_verification_sent_at).seconds < 60:
            wait = 60 - (now - user.email_verification_sent_at).seconds
            return JsonResponse({'success': False, 'error': f'Please wait {wait} seconds.', 'wait': wait})

        code = ''.join(random.choices(string.digits, k=7))
        user.email_verification_code = code
        user.email_verification_sent_at = now
        user.save()

        send_verification_email(user)
        return JsonResponse({'success': True, 'message': 'Code resent.'})
    except Exception as e:
        logger.exception('Error in resend_code')
        return JsonResponse({'success': False, 'error': 'Server error while resending code.'})


@login_required
@require_http_methods(["POST"])
def change_verification_email(request):
    if request.user.email_verified:
        return JsonResponse({'success': False, 'error': 'Your email is already verified.'}, status=400)

    new_email = (request.POST.get('email') or '').strip().lower()
    if not new_email:
        return JsonResponse({'success': False, 'error': 'Enter an email address.'}, status=400)

    try:
        validate_email(new_email)
    except ValidationError:
        return JsonResponse({'success': False, 'error': 'Enter a valid email address.'}, status=400)

    if User.objects.filter(email__iexact=new_email).exclude(pk=request.user.pk).exists():
        return JsonResponse({'success': False, 'error': 'That email is already in use.'}, status=400)

    user = request.user
    now = timezone.now()
    if user.email_verification_sent_at and (now - user.email_verification_sent_at).seconds < 45:
        wait = 45 - (now - user.email_verification_sent_at).seconds
        return JsonResponse(
            {'success': False, 'error': f'Please wait {wait} seconds before requesting another email.', 'wait': wait},
            status=429,
        )

    user.email = new_email
    user.email_verified = False
    user.verification_attempts_today = 0
    user.last_verification_attempt_date = now.date()
    user.email_verification_code = ''.join(random.choices(string.digits, k=7))
    user.email_verification_sent_at = now
    user.save(update_fields=[
        'email',
        'email_verified',
        'verification_attempts_today',
        'last_verification_attempt_date',
        'email_verification_code',
        'email_verification_sent_at',
    ])

    send_verification_email(user)
    return JsonResponse({
        'success': True,
        'message': f'Verification code sent to {user.email}.',
        'email': user.email,
    })

@csrf_exempt
def verify_phone(request):
    # Handles both GET (render + send) and POST (verify)
    if request.method == 'GET':
        user_id = request.GET.get('user_id') or request.user.id if request.user.is_authenticated else None
    elif request.method == 'POST':
        user_id = request.POST.get('user_id')
    else:
        return JsonResponse({'success': False, 'error': 'Invalid request method.'})

    if not user_id:
        return JsonResponse({'success': False, 'error': 'Missing user_id.'})

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'User not found.'})

    # POST: verify code
    if request.method == 'POST':
        code = request.POST.get('code')
        if not code:
            return JsonResponse({'success': False, 'error': 'Missing code.'})

        session_key = f'phone_verification_{user.id}'
        stored = request.session.get(session_key)
        if not stored:
            return JsonResponse({'success': False, 'error': 'No verification code found. Please resend.'})

        # Rate limiting and attempts
        attempts = stored.get('attempts', 0)
        if attempts >= 5:
            return JsonResponse({'success': False, 'error': 'Maximum attempts reached.'})

        if stored.get('code') == code:
            # success
            user.phone_verified = True
            user.phone_verification_code = None
            user.phone_verification_sent_at = None
            user.save(update_fields=['phone_verified', 'phone_verification_code', 'phone_verification_sent_at'])
            try:
                request.session.pop(session_key, None)
            except Exception:
                pass
            return JsonResponse({'success': True, 'redirect': reverse('home')})
        else:
            stored['attempts'] = attempts + 1
            request.session[session_key] = stored
            return JsonResponse({'success': False, 'error': 'Invalid code.'})

    # GET: render and send code via SMS
    # generate 6-digit code
    import random
    code = ''.join(random.choices('0123456789', k=6))
    # persist in session and on user model for traceability
    session_key = f'phone_verification_{user.id}'
    request.session[session_key] = {
        'code': code,
        'sent_at': timezone.now().isoformat(),
        'attempts': 0,
        'phone': user.phone_number
    }
    try:
        # save some info on user model too (optional persistence)
        user.phone_verification_code = code
        user.phone_verification_sent_at = timezone.now()
        user.save(update_fields=['phone_verification_code', 'phone_verification_sent_at'])
    except Exception:
        logger.exception('Failed to save phone verification code on user')

    # send SMS (non-blocking would be better but keep simple for now)
    try:
        phone = user.phone_number
        if phone:
            msg = f"Your Baysoko verification code is: {code}"
            send_sms_brevo(phone, msg)
    except Exception:
        logger.exception('Failed to send phone verification SMS')

    # Render page like verify_email
    phone_display = user.phone_number or 'your phone'
    context = {'user': user, 'phone_display': phone_display}
    return render(request, 'users/verify_phone.html', context)


@csrf_exempt
def resend_phone_code(request):
    try:
        if request.method != 'POST':
            return JsonResponse({'success': False, 'error': 'Invalid method.'})
        user_id = request.POST.get('user_id')
        if not user_id:
            return JsonResponse({'success': False, 'error': 'Missing user_id.'})
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'User not found.'})
        session_key = f'phone_verification_{user.id}'
        stored = request.session.get(session_key)
        now = timezone.now()
        # throttle: allow resend every 60 seconds
        if stored and stored.get('sent_at'):
            try:
                last = timezone.datetime.fromisoformat(stored.get('sent_at'))
                delta = (now - last).total_seconds()
                if delta < 60:
                    return JsonResponse({'success': False, 'wait': int(60 - delta)})
            except Exception:
                pass

        import random
        code = ''.join(random.choices('0123456789', k=6))
        request.session[session_key] = {'code': code, 'sent_at': now.isoformat(), 'attempts': 0, 'phone': user.phone_number}
        try:
            user.phone_verification_code = code
            user.phone_verification_sent_at = now
            user.save(update_fields=['phone_verification_code', 'phone_verification_sent_at'])
        except Exception:
            logger.exception('Failed to save phone verification on resend')

        # send SMS
        if user.phone_number:
            send_sms_brevo(user.phone_number, f"Your Baysoko verification code is: {code}")

        return JsonResponse({'success': True})
    except Exception:
        logger.exception('resend_phone_code exception')
        return JsonResponse({'success': False, 'error': 'Server error.'})

@login_required
def verification_required(request):
    if request.user.email_verified:
        return redirect('home')

    list(get_messages(request))  # clear existing messages

    if request.session.pop('just_registered', False):
        msg = request.session.pop(
            'just_registered_message',
            'Account created. Check your email to verify your account.'
        )
        messages.success(request, msg)

    return render(request, 'users/verify_email.html', {'user': request.user})

# ----------------------------------------------------------------------
#  Social authentication (Google, Facebook)
# ----------------------------------------------------------------------

def google_login(request):
    try:
        redirect_uri = _get_social_callback_uri('google', request)

        try:
            app = SocialApp.objects.get(provider='google')
            client_id = app.client_id
        except SocialApp.DoesNotExist:
            client_id = os.environ.get('GOOGLE_OAUTH_CLIENT_ID')
            if not client_id:
                messages.error(request, "Google OAuth is not configured.")
                return redirect('register')

        auth_url = "https://accounts.google.com/o/oauth2/v2/auth"
        params = {
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'response_type': 'code',
            'scope': 'email profile',
            'access_type': 'online',
            'prompt': 'select_account consent',
        }
        state = secrets.token_urlsafe(32)
        request.session['oauth_state'] = state
        request.session['oauth_action'] = 'register'
        params['state'] = state
        next_url = _normalize_next_url(request.GET.get('next'))
        if next_url:
            request.session['post_login_redirect'] = next_url
            if _is_delivery_next(next_url):
                request.session['delivery_login_intent'] = True

        url = f"{auth_url}?{urlencode(params)}"
        logger.info(f"Google OAuth redirect URI: {redirect_uri}")
        return redirect(url)

    except Exception as e:
        logger.error(f"Google login error: {str(e)}", exc_info=True)
        messages.error(request, "Unable to initiate Google login.")
        return redirect('register')

def google_connect(request):
    if not request.user.is_authenticated:
        messages.error(request, 'You must be signed in to connect a Google account.')
        return redirect('login')

    try:
        redirect_uri = _get_social_callback_uri('google', request)

        try:
            app = SocialApp.objects.get(provider='google')
            client_id = app.client_id
        except SocialApp.DoesNotExist:
            client_id = os.environ.get('GOOGLE_OAUTH_CLIENT_ID')
            if not client_id:
                messages.error(request, "Google OAuth is not configured.")
                return redirect('profile-edit', pk=request.user.pk)

        auth_url = "https://accounts.google.com/o/oauth2/v2/auth"
        params = {
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'response_type': 'code',
            'scope': 'email profile',
            'access_type': 'online',
            'prompt': 'select_account consent',
        }
        state = secrets.token_urlsafe(32)
        request.session['oauth_state'] = state
        request.session['oauth_action'] = 'connect'
        params['state'] = state

        url = f"{auth_url}?{urlencode(params)}"
        return redirect(url)

    except Exception as e:
        logger.error(f"Google connect error: {str(e)}", exc_info=True)
        messages.error(request, "Unable to initiate Google connect.")
        return redirect('profile-edit', pk=request.user.pk)

@csrf_exempt
def google_callback(request):
    code = request.GET.get('code')
    error = request.GET.get('error')
    session = _build_google_oauth_session()

    if error:
        messages.error(request, f"Google authorization error: {error}")
        return _redirect_after_google_error(request, f"Google authorization error: {error}")
    if not code:
        return _redirect_after_google_error(request, "Authorization code not received from Google.")

    try:
        app = SocialApp.objects.get(provider='google')
        redirect_uri = _get_social_callback_uri('google', request)

        token_url = 'https://oauth2.googleapis.com/token'
        data = {
            'client_id': app.client_id,
            'client_secret': app.secret,
            'code': code,
            'grant_type': 'authorization_code',
            'redirect_uri': redirect_uri,
        }
        response = session.post(token_url, data=data, timeout=(10, 20))
        if response.status_code != 200:
            logger.error(f"Google token endpoint returned {response.status_code}: {response.text}")
            return _redirect_after_google_error(request)
        token_data = response.json()

        if 'access_token' not in token_data:
            logger.error(f"No access_token in token response: {token_data}")
            return _redirect_after_google_error(request)

        userinfo_url = 'https://www.googleapis.com/oauth2/v2/userinfo'
        headers = {'Authorization': f"Bearer {token_data['access_token']}"}
        userinfo_resp = session.get(userinfo_url, headers=headers, timeout=(10, 20))
        if userinfo_resp.status_code != 200:
            logger.error(f"Google userinfo returned {userinfo_resp.status_code}: {userinfo_resp.text}")
            return _redirect_after_google_error(request)
        userinfo = userinfo_resp.json()

        return _complete_google_identity_login(request, userinfo, action=request.session.get('oauth_action'))

    except SSLError as e:
        logger.warning("Google callback SSL error during OAuth exchange: %s", e, exc_info=True)
        return _redirect_after_google_error(request)
    except RequestException as e:
        logger.warning("Google callback network error during OAuth exchange: %s", e, exc_info=True)
        return _redirect_after_google_error(request)
    except Exception as e:
        logger.error(f"Google callback error: {str(e)}")
        return _redirect_after_google_error(request)


@require_http_methods(["POST"])
def google_native_signin(request):
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except (TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({'success': False, 'message': 'Invalid Google sign-in payload.'}, status=400)

    id_token = (payload.get('id_token') or '').strip()
    action = (payload.get('action') or 'register').strip().lower()
    if action not in {'register', 'connect'}:
        action = 'register'

    if not id_token:
        return JsonResponse({'success': False, 'message': 'Google sign-in token was not provided.'}, status=400)

    if action == 'connect' and not request.user.is_authenticated:
        return JsonResponse({'success': False, 'message': 'You need to sign in before connecting Google.'}, status=403)

    try:
        allowed_audiences = {
            value for value in [
                getattr(settings, 'GOOGLE_ANDROID_CLIENT_ID', ''),
                getattr(settings, 'GOOGLE_OAUTH_CLIENT_ID', ''),
                os.environ.get('GOOGLE_OAUTH_CLIENT_ID', ''),
            ] if value
        }

        session = _build_google_oauth_session()
        tokeninfo_resp = session.get(
            'https://oauth2.googleapis.com/tokeninfo',
            params={'id_token': id_token},
            timeout=(10, 20),
        )
        if tokeninfo_resp.status_code != 200:
            logger.warning("Native Google sign-in tokeninfo returned %s: %s", tokeninfo_resp.status_code, tokeninfo_resp.text)
            return JsonResponse({'success': False, 'message': 'Google could not verify this sign-in request.'}, status=400)

        tokeninfo = tokeninfo_resp.json()
        audience = tokeninfo.get('aud')
        if allowed_audiences and audience not in allowed_audiences:
            logger.warning("Native Google sign-in audience mismatch: %s not in %s", audience, allowed_audiences)
            return JsonResponse({'success': False, 'message': 'This Google sign-in request is not allowed for Baysoko.'}, status=403)

        if tokeninfo.get('email_verified') not in ('true', True):
            return JsonResponse({'success': False, 'message': 'Please use a Google account with a verified email address.'}, status=400)

        request.session['oauth_action'] = action
        userinfo = {
            'id': tokeninfo.get('sub') or tokeninfo.get('user_id'),
            'sub': tokeninfo.get('sub'),
            'email': tokeninfo.get('email'),
            'given_name': tokeninfo.get('given_name', ''),
            'family_name': tokeninfo.get('family_name', ''),
            'picture': tokeninfo.get('picture', ''),
            'name': tokeninfo.get('name', ''),
        }
        redirect_response = _complete_google_identity_login(request, userinfo, action=action)
        redirect_url = getattr(redirect_response, 'url', reverse('home'))
        return JsonResponse({'success': True, 'redirect_url': redirect_url})
    except RequestException as e:
        logger.warning("Native Google sign-in network error: %s", e, exc_info=True)
        return JsonResponse({'success': False, 'message': 'Google sign-in is temporarily unavailable. Please retry.'}, status=503)
    except Exception as e:
        logger.exception("Native Google sign-in failed: %s", e)
        return JsonResponse({'success': False, 'message': 'Google sign-in could not be completed right now.'}, status=500)

def facebook_login(request):
    try:
        app = SocialApp.objects.get(provider='facebook')
        params = {
            'client_id': app.client_id,
            'redirect_uri': request.build_absolute_uri('/accounts/facebook/callback/'),
            'response_type': 'code',
            'scope': 'email,public_profile',
            'auth_type': 'rerequest',
            'display': 'popup',
        }
        auth_url = 'https://www.facebook.com/v13.0/dialog/oauth'
        url = f"{auth_url}?{'&'.join([f'{k}={v}' for k, v in params.items()])}"
        request.session['oauth_action'] = 'register'
        next_url = _normalize_next_url(request.GET.get('next'))
        if next_url:
            request.session['post_login_redirect'] = next_url
            if _is_delivery_next(next_url):
                request.session['delivery_login_intent'] = True
        return redirect(url)

    except SocialApp.DoesNotExist:
        logger.error("Facebook SocialApp not configured")
        messages.error(request, "Facebook login is not configured.")
        return redirect('register')

@csrf_exempt
def facebook_callback(request):
    code = request.GET.get('code')
    error = request.GET.get('error')

    if error:
        messages.error(request, f"Facebook authorization error: {error}")
        return redirect('register')
    if not code:
        messages.error(request, "Authorization code not received")
        return redirect('register')

    try:
        app = SocialApp.objects.get(provider='facebook')

        token_url = 'https://graph.facebook.com/v13.0/oauth/access_token'
        params = {
            'client_id': app.client_id,
            'client_secret': app.secret,
            'code': code,
            'redirect_uri': request.build_absolute_uri('/accounts/facebook/callback/'),
        }
        response = requests.get(token_url, params=params)
        token_data = response.json()

        if 'access_token' not in token_data:
            messages.error(request, "Failed to get access token from Facebook")
            return redirect('register')

        userinfo_url = 'https://graph.facebook.com/v13.0/me'
        params = {
            'access_token': token_data['access_token'],
            'fields': 'id,name,email,first_name,last_name,picture'
        }
        userinfo = requests.get(userinfo_url, params=params).json()

        email = userinfo.get('email')
        if not email:
            email = f"{userinfo.get('id')}@facebook.com"

        try:
            users = User.objects.filter(email__iexact=email).order_by('date_joined', 'id')
            if users.count() > 1:
                logger.warning("Facebook callback: multiple users share email %s; using earliest account.", email)
            user = users.first() if users.exists() else None
            if not user:
                raise User.DoesNotExist()
            login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            _sync_from_delivery_profile(user)
            next_url = _normalize_next_url(request.session.pop('post_login_redirect', None))
            is_delivery_flow = _is_delivery_next(next_url) or bool(request.session.get('delivery_login_intent'))
            if is_delivery_flow:
                if not getattr(user, 'email_verified', False):
                    request.session['post_verify_redirect'] = next_url or reverse('delivery:dashboard')
                    request.session['delivery_login_intent'] = True
                    return redirect('verification_required')
                request.session['delivery_auth'] = True
                request.session.pop('delivery_login_intent', None)
                return redirect(next_url or reverse('delivery:dashboard'))
            if not user.phone_number:
                messages.info(request, 'Please add your phone number to continue.')
                return redirect('profile-edit', pk=user.pk)
            if not user.location:
                messages.info(request, 'Please add your location to continue.')
                return redirect('profile-edit', pk=user.pk)
            messages.success(request, f"Welcome back, {user.first_name}!")
            return redirect('home')
        except User.DoesNotExist:
            username = email.split('@')[0] if '@' in email else userinfo.get('id')
            original = username
            counter = 1
            while User.objects.filter(username=username).exists():
                username = f"{original}{counter}"
                counter += 1

            pending_location = (request.session.pop('pending_location', '') or '').strip()
            user = User.objects.create(
                email=email.lower(),
                username=username,
                first_name=userinfo.get('first_name', ''),
                last_name=userinfo.get('last_name', ''),
                location=pending_location,
                phone_number=None,
                is_active=True
            )
            user.set_unusable_password()
            code = ''.join(random.choices(string.digits, k=7))
            user.email_verification_code = code
            user.email_verification_sent_at = timezone.now()
            user.email_verified = False
            user.save()

            send_verification_email(user)
            try:
                send_welcome_email(user)
            except Exception:
                logger.exception('Failed to queue welcome email for social signup')

            login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            next_url = _normalize_next_url(request.session.pop('post_login_redirect', None))
            is_delivery_flow = _is_delivery_next(next_url) or bool(request.session.get('delivery_login_intent'))
            if is_delivery_flow:
                request.session['post_verify_redirect'] = next_url or reverse('delivery:dashboard')
                request.session['delivery_login_intent'] = True
                messages.success(request, 'Account created. Please verify your email to continue.')
                return redirect('verification_required')
            request.session['just_registered'] = True
            request.session['just_registered_message'] = 'Account created with Facebook! Check your email to verify your account.'
            if not user.location:
                messages.info(request, 'Please add your location to continue.')
                return redirect('profile-edit', pk=user.pk)
            return redirect('verification_required')

    except Exception as e:
        logger.error(f"Facebook callback error: {str(e)}")
        messages.error(request, "Error during Facebook login. Please try again.")
        return redirect('register')

# ----------------------------------------------------------------------
#  Profile views
# ----------------------------------------------------------------------

class ProfileDetailView(DetailView):
    model = User
    template_name = 'users/profile.html'
    context_object_name = 'profile_user'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        profile_user = self.object
        user = self.request.user

        # Get all stores owned by the profile user
        stores = profile_user.stores.all()
        context['stores'] = stores

        # Get all unsold listings from those stores, ordered newest first
        listings_qs = Listing.objects.filter(store__in=stores, is_sold=False).order_by('-date_created')
        paginator = Paginator(listings_qs, 8)  # 8 listings per page
        page_number = self.request.GET.get('page')
        page_obj = paginator.get_page(page_number)
        context['page_obj'] = page_obj
        context['listing_count'] = listings_qs.count()

        # Saved listings (only for the profile owner)
        saved_listings = None
        if user.is_authenticated and user == profile_user:
            saved_listings = Listing.objects.filter(favorites__user=user).order_by('-date_created')
        context['saved_listings'] = saved_listings
        context['saved_count'] = saved_listings.count() if saved_listings is not None else 0

        # Viewer's favorites (for heart icon)
        if user.is_authenticated:
            # Use the user's favorite_listings related manager (assuming a ManyToMany field named 'favorites')
            # Adjust if your relation is different; typical pattern: user.favorite_listings.all()
            context['viewer_favorites'] = set(Listing.objects.filter(favorites__user=user).order_by('-date_created').values_list('id', flat=True))
        else:
            context['viewer_favorites'] = set()

        # Dummy rating – replace with real average rating if available
        context['rating_average'] = 4.5
        context['member_since'] = profile_user.date_joined.strftime("%B %Y")

        # Google connect availability (for profile owner)
        if user == profile_user:
            try:
                from allauth.socialaccount.models import SocialAccount
                can_connect = (
                    profile_user.email and
                    profile_user.email.lower().endswith('@gmail.com') and
                    profile_user.has_usable_password() and
                    not SocialAccount.objects.filter(user=profile_user, provider='google').exists()
                )
            except Exception:
                can_connect = False
            context['can_connect_google'] = can_connect

        return context
    
class ProfileUpdateView(LoginRequiredMixin, UserPassesTestMixin, UpdateView):
    model = User
    form_class = CustomUserChangeForm
    template_name = 'users/profile_edit.html'

    def get_success_url(self):
        return reverse_lazy('profile', kwargs={'pk': self.object.pk})

    def test_func(self):
        return self.request.user == self.get_object()

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.fields['first_name'].initial = self.object.first_name
        form.fields['last_name'].initial = self.object.last_name
        form.fields['username'].initial = self.object.username
        form.fields['email'].initial = self.object.email
        # If user came from delivery app first, try to prefill from delivery profile
        delivery_profile = getattr(self.object, 'delivery_profile', None)
        if not self.object.phone_number and delivery_profile and getattr(delivery_profile, 'phone_number', None):
            form.fields['phone_number'].initial = delivery_profile.phone_number
        else:
            form.fields['phone_number'].initial = self.object.phone_number
        # Location: prefer user.location, otherwise fall back to delivery profile city/address
        try:
            if hasattr(form.fields, 'get') and form.fields.get('location'):
                if self.object.location:
                    form.fields['location'].initial = self.object.location
                elif delivery_profile:
                    form.fields['location'].initial = getattr(delivery_profile, 'city', '') or getattr(delivery_profile, 'address', '')
        except Exception:
            pass
        form.fields['bio'].initial = self.object.bio
        form.fields['show_contact_info'].initial = self.object.show_contact_info
        return form

    def form_valid(self, form):
        # Detect previous phone to determine if verification is needed after save
        try:
            prev_obj = self.get_object()
            prev_phone = getattr(prev_obj, 'phone_number', None)
            prev_email = getattr(prev_obj, 'email', None)
        except Exception:
            prev_phone = None
            prev_email = None

        if 'profile_picture' in self.request.FILES:
            form.instance.profile_picture = self.request.FILES['profile_picture']

        messages.success(self.request, 'Profile updated successfully!')

        # Save the form and update the instance
        response = super().form_valid(form)

        # If email changed, require re-verification (up to change limit enforced at model/form)
        try:
            new_email = getattr(self.object, 'email', None)
            email_verified = getattr(self.object, 'email_verified', False)
            if new_email and prev_email and new_email != prev_email and not email_verified:
                import random
                import string
                code = ''.join(random.choices(string.digits, k=7))
                self.object.email_verification_code = code
                self.object.email_verification_sent_at = timezone.now()
                self.object.save(update_fields=['email_verification_code', 'email_verification_sent_at'])
                send_verification_email(self.object)
                messages.info(self.request, 'Please verify your new email address to continue.')
                return redirect(reverse('verify_email') + f'?user_id={self.object.id}')
        except Exception:
            logger.exception('Failed to send email verification after email change')

        # Sync to delivery profile when user originated from delivery app
        try:
            delivery_profile = getattr(self.object, 'delivery_profile', None)
            if delivery_profile:
                updated = False
                if self.object.phone_number and delivery_profile.phone_number != self.object.phone_number:
                    delivery_profile.phone_number = self.object.phone_number
                    updated = True
                # If delivery profile lacks city/address, use user's location
                if self.object.location:
                    if not delivery_profile.city:
                        delivery_profile.city = self.object.location
                        updated = True
                    if not delivery_profile.address:
                        delivery_profile.address = self.object.location
                        updated = True
                if updated:
                    delivery_profile.save()
        except Exception:
            logger.exception('Failed to sync delivery profile from user profile update')

        # After saving, if a phone number was added/changed or phone is unverified, send verification SMS
        try:
            new_phone = getattr(self.object, 'phone_number', None)
            phone_verified = getattr(self.object, 'phone_verified', False)
            need_send = False
            if new_phone:
                if prev_phone != new_phone:
                    need_send = True
                elif not phone_verified:
                    need_send = True

            if need_send:
                import random
                code = ''.join(random.choices('0123456789', k=6))
                session_key = f'phone_verification_{self.object.id}'
                try:
                    self.request.session[session_key] = {
                        'code': code,
                        'sent_at': timezone.now().isoformat(),
                        'attempts': 0,
                        'phone': new_phone
                    }
                except Exception:
                    pass
                try:
                    self.object.phone_verification_code = code
                    self.object.phone_verification_sent_at = timezone.now()
                    self.object.save(update_fields=['phone_verification_code', 'phone_verification_sent_at'])
                except Exception:
                    logger.exception('Failed to persist phone verification code on profile update')

                # send SMS via Brevo util (best-effort)
                try:
                    if new_phone:
                        send_sms_brevo(new_phone, f"Your Baysoko verification code is: {code}")
                except Exception:
                    logger.exception('Failed to send SMS on profile update')

                # redirect to phone verification page
                try:
                    return redirect(reverse('verify_phone') + f'?user_id={self.object.id}')
                except Exception:
                    pass
        except Exception:
            logger.exception('Error handling phone verification after profile update')

        # If required fields are still missing, keep user on profile edit so they see feedback.
        if not self.object.phone_number or not self.object.location:
            if not self.object.phone_number:
                messages.info(self.request, 'Please add your phone number to complete your profile.')
            if not self.object.location:
                messages.info(self.request, 'Please add your location to complete your profile.')
            return redirect('profile-edit', pk=self.object.pk)

        return response

    def form_invalid(self, form):
        messages.error(self.request, 'Please correct the errors below.')
        return super().form_invalid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['form'] = self.get_form()
        user = self.request.user
        try:
            can_connect = (
                user.email and
                user.email.lower().endswith('@gmail.com') and
                user.has_usable_password() and
                not SocialAccount.objects.filter(user=user, provider='google').exists()
            )
        except Exception:
            can_connect = False
        context['can_connect_google'] = can_connect
        return context

# ----------------------------------------------------------------------
#  Password change (AJAX and regular)
# ----------------------------------------------------------------------

class CustomPasswordChangeView(LoginRequiredMixin, PasswordChangeView):
    template_name = 'users/password_change.html'
    success_url = reverse_lazy('password_change_done')

    def form_valid(self, form):
        messages.success(self.request, 'Your password has been changed successfully!')
        return super().form_valid(form)

def ajax_password_change(request):
    if request.method == 'POST' and request.headers.get('x-requested-with') == 'XMLHttpRequest':
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            return JsonResponse({
                'success': True,
                'message': 'Your password has been changed successfully!'
            })
        else:
            return JsonResponse({
                'success': False,
                'errors': form.errors.get_json_data()
            })
    return JsonResponse({
        'success': False,
        'errors': {'__all__': ['Invalid request']}
    })

# ----------------------------------------------------------------------
#  Password reset (modal with AJAX)
# ----------------------------------------------------------------------

def password_reset_modal(request):
    """Render the single‑page password reset modal."""
    return render(request, 'users/password_reset_modal.html')

@csrf_exempt
@require_http_methods(['POST'])
def password_reset_send_code(request):
    """Step 1: Accept email, send code."""
    try:
        data = json.loads(request.body)
        email = data.get('email', '').strip().lower()
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid request.'})

    if not email:
        return JsonResponse({'success': False, 'error': 'Email is required.'})

    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        # Security: don't reveal non‑existence
        return JsonResponse({'success': True, 'message': 'If an account exists, a code has been sent.'})

    # Rate limit: one request per 60 seconds
    if user.password_reset_sent_at and (timezone.now() - user.password_reset_sent_at).seconds < 60:
        wait = 60 - (timezone.now() - user.password_reset_sent_at).seconds
        return JsonResponse({'success': False, 'error': f'Please wait {wait} seconds.', 'wait': wait})

    send_password_reset_code(user)

    return JsonResponse({
        'success': True,
        'message': 'Code sent. Please check your email.',
        'email': email,
    })

@csrf_exempt
@require_http_methods(['POST'])
def password_reset_verify_code(request):
    """Step 2: Verify the code, allow setting new password."""
    try:
        data = json.loads(request.body)
        email = data.get('email', '').strip().lower()
        code = data.get('code', '').strip()
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid request.'})

    if not email or not code:
        return JsonResponse({'success': False, 'error': 'Email and code are required.'})

    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Invalid request.'})

    now = timezone.now()
    today = now.date()

    # Reset daily attempts if needed
    if user.password_reset_last_attempt_date != today:
        user.password_reset_attempts = 0
        user.password_reset_last_attempt_date = today
        user.save()

    if user.password_reset_attempts >= 3:
        return JsonResponse({'success': False, 'error': 'Too many failed attempts. Try again tomorrow.'})

    # Validate code and expiry (10 minutes)
    if (user.password_reset_code == code and
            user.password_reset_sent_at and
            now - user.password_reset_sent_at <= timedelta(minutes=10)):
        # Success – store verified email in session
        request.session['password_reset_verified_email'] = email
        request.session['password_reset_verified_at'] = now.isoformat()
        return JsonResponse({'success': True})
    else:
        # Failed attempt
        user.password_reset_attempts += 1
        user.save()
        attempts_left = max(0, 3 - user.password_reset_attempts)
        return JsonResponse({
            'success': False,
            'error': f'Invalid code. {attempts_left} attempts remaining.',
            'attempts_left': attempts_left
        })

@csrf_exempt
@require_http_methods(['POST'])
def password_reset_set_password(request):
    """Step 3: Set new password (after code verification)."""
    try:
        data = json.loads(request.body)
        password = data.get('password')
        confirm = data.get('confirm_password')
    except Exception:
        return JsonResponse({'success': False, 'error': 'Invalid request.'})

    email = request.session.get('password_reset_verified_email')
    verified_at = request.session.get('password_reset_verified_at')
    if not email or not verified_at:
        return JsonResponse({'success': False, 'error': 'Verification required.'})

    # Check verification timeout (15 minutes)
    try:
        verified_dt = timezone.datetime.fromisoformat(verified_at)
        if timezone.now() - verified_dt > timedelta(minutes=15):
            del request.session['password_reset_verified_email']
            del request.session['password_reset_verified_at']
            return JsonResponse({'success': False, 'error': 'Verification expired. Please restart.'})
    except Exception:
        pass

    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'User not found.'})

    # Validate password
    errors = []
    if len(password) < 8:
        errors.append('Password must be at least 8 characters.')
    if not re.search(r'\d', password):
        errors.append('Password must contain at least one digit.')
    if not re.search(r'[a-zA-Z]', password):
        errors.append('Password must contain at least one letter.')
    if password != confirm:
        errors.append('Passwords do not match.')

    if errors:
        return JsonResponse({'success': False, 'errors': errors})

    user.set_password(password)
    user.password_reset_code = None
    user.password_reset_attempts = 0
    user.save()

    # Clear session
    del request.session['password_reset_verified_email']
    del request.session['password_reset_verified_at']

    # Send confirmation email and in-app notification
    try:
        ctx = {'user': user, 'site_url': getattr(settings, 'SITE_URL', '')}
        subject = 'Your Baysoko password was changed'
        render_and_send('emails/password_changed.html', 'emails/password_changed.txt', ctx, subject, [user.email])
    except Exception:
        logger.exception('Failed to queue password-reset-complete email')

    try:
        from notifications.utils import notify_system_message
        notify_system_message(user, 'Password Reset Completed', 'Your account password was successfully reset.')
    except Exception:
        logger.exception('Failed to create in-app notification for password reset completion')

    return JsonResponse({'success': True, 'message': 'Password changed successfully.'})

# ----------------------------------------------------------------------
#  Django built‑in password reset overrides (keep for compatibility)
# ----------------------------------------------------------------------

class CustomPasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = 'users/password_reset_confirm.html'
    success_url = '/password-reset-complete/'

    def form_valid(self, form):
        messages.success(self.request, 'Your password has been reset successfully!')
        return super().form_valid(form)

class CustomPasswordResetView(auth_views.PasswordResetView):
    template_name = 'users/password_reset.html'
    email_template_name = 'users/password_reset_email.html'
    subject_template_name = 'users/password_reset_subject.txt'
    success_url = '/password-reset/done/'

    def form_valid(self, form):
        email = form.cleaned_data['email']
        logger.info(f"Password reset requested for email: {email}")
        try:
            response = super().form_valid(form)
            messages.success(self.request, f'Password reset email has been sent to {email}.')
            return response
        except Exception as e:
            logger.error(f"Password reset email failed for {email}: {str(e)}")
            messages.error(self.request, f'Error sending email to {email}. Try again later.')
            return self.form_invalid(form)

class CustomPasswordResetCompleteView(auth_views.PasswordResetCompleteView):
    template_name = 'users/password_reset_complete.html'

    def get(self, request, *args, **kwargs):
        messages.success(self.request, 'Your password has been successfully reset. You can now log in.')
        return super().get(request, *args, **kwargs)

# ----------------------------------------------------------------------
#  OAuth diagnostics and debug email (staff only)
# ----------------------------------------------------------------------

@staff_member_required
def oauth_diagnostics(request):
    site = Site.objects.get_current()
    apps = SocialApp.objects.all()
    env_vars = {
        'GOOGLE_OAUTH_CLIENT_ID': os.environ.get('GOOGLE_OAUTH_CLIENT_ID'),
        'GOOGLE_OAUTH_CLIENT_SECRET': os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET'),
        'FACEBOOK_OAUTH_CLIENT_ID': os.environ.get('FACEBOOK_OAUTH_CLIENT_ID'),
        'FACEBOOK_OAUTH_CLIENT_SECRET': os.environ.get('FACEBOOK_OAUTH_CLIENT_SECRET'),
        'SITE_DOMAIN': os.environ.get('SITE_DOMAIN') or os.environ.get('RENDER_EXTERNAL_HOSTNAME'),
    }
    provider_apps = {app.provider: app for app in apps}
    return render(request, 'users/oauth_diagnostics.html', {
        'site': site,
        'provider_apps': provider_apps,
        'env_vars': env_vars,
        'social_providers': getattr(settings, 'SOCIALACCOUNT_PROVIDERS', {}),
    })

@staff_member_required
def debug_send_email(request):
    to_addr = request.GET.get('to') or request.user.email or settings.DEFAULT_FROM_EMAIL
    subject = request.GET.get('subject', 'Baysoko SMTP Debug')
    body = request.GET.get('body', 'This is a test message from Baysoko SMTP debug endpoint.')

    try:
        # Use centralized threaded sender to ensure provider-first and SMTP fallback
        try:
            from baysoko.utils.email_helpers import _send_email_threaded
            _send_email_threaded(subject, body, None, [to_addr])
            logger.info(f"Debug email queued to {to_addr} via centralized sender")
            return JsonResponse({'success': True, 'message': f'Debug email queued to {to_addr}'})
        except Exception:
            # Fallback to Django send_mail if centralized helper unavailable
            send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [to_addr], fail_silently=False)
            logger.info(f"Debug email sent to {to_addr} via fallback send_mail")
            return JsonResponse({'success': True, 'message': f'Debug email sent to {to_addr}'})
    except Exception as e:
        logger.error(f"Failed to send debug email: {e}", exc_info=True)
        return JsonResponse({'success': False, 'error': str(e)})

# ----------------------------------------------------------------------
#  Login / Logout
# ----------------------------------------------------------------------
@method_decorator(ensure_csrf_cookie, name='dispatch')
class CustomLoginView(LoginView):
    template_name = 'users/login.html'
    authentication_form = CustomAuthenticationForm

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            messages.info(request, 'You are already logged in!')
            return redirect('home')
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        # Ensure main app login doesn't implicitly authenticate delivery session
        try:
            self.request.session.pop('delivery_login_intent', None)
            if 'delivery_auth' in self.request.session:
                self.request.session['delivery_auth'] = False
        except Exception:
            pass
        if self.request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({
                'success': True,
                'redirect': self.get_success_url()
            })
        try:
            messages.success(self.request, f'Welcome back, {self.request.user.first_name}!')
        except Exception:
            messages.success(self.request, 'Login successful!')
        return response

    def form_invalid(self, form):
        if self.request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'errors': form.errors.get_json_data()
            })
        return super().form_invalid(form)

class CustomLogoutView(LogoutView):
    template_name = 'users/logout.html'

    def dispatch(self, request, *args, **kwargs):
        messages.success(request, 'You have been logged out.')
        try:
            if 'delivery_auth' in request.session:
                request.session['delivery_auth'] = False
            request.session.pop('delivery_login_intent', None)
        except Exception:
            pass
        return super().dispatch(request, *args, **kwargs)

# users/views.py

import json
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST
from .models import AccountDeletionLog

@login_required
@require_POST
@ensure_csrf_cookie
def change_password_ajax(request):
    """
    AJAX view to change the logged-in user's password.
    Expects POST with old_password, new_password1, new_password2.
    Returns JSON with success or errors.
    """
    try:
        # Prefer JSON when content type is JSON; otherwise fall back to POST
        content_type = request.META.get('CONTENT_TYPE', '') or request.content_type or ''
        if 'application/json' in content_type:
            try:
                data = json.loads(request.body.decode('utf-8') or '{}')
            except Exception:
                data = request.POST
        else:
            data = request.POST
    except Exception:
        # If the request stream was already consumed elsewhere, fall back
        data = request.POST
    old_password = data.get('old_password')
    new_password1 = data.get('new_password1')
    new_password2 = data.get('new_password2')

    # Basic field presence check
    if not old_password or not new_password1 or not new_password2:
        return JsonResponse({
            'success': False,
            'error': 'All fields are required.'
        }, status=400)

    # Verify old password
    user = authenticate(username=request.user.username, password=old_password)
    if user is None:
        return JsonResponse({
            'success': False,
            'error': 'Your old password was entered incorrectly. Please enter it again.'
        }, status=400)

    # Check that new passwords match
    if new_password1 != new_password2:
        return JsonResponse({
            'success': False,
            'error': 'The two new password fields didn’t match.'
        }, status=400)

    # Validate new password against Django's password validators
    try:
        validate_password(new_password1, user=request.user)
    except ValidationError as e:
        return JsonResponse({
            'success': False,
            'errors': {'new_password1': list(e.messages)}
        }, status=400)

    # All good – set the new password
    request.user.set_password(new_password1)
    request.user.save()

    # Keep the user logged in
    update_session_auth_hash(request, request.user)

    # Send password changed email (non-blocking)
    try:
        ctx = {'user': request.user, 'site_url': getattr(settings, 'SITE_URL', '')}
        subject = 'Your Baysoko password was changed'
        render_and_send('emails/password_changed.html', 'emails/password_changed.txt', ctx, subject, [request.user.email])
    except Exception:
        logger.exception('Failed to queue password-changed email')

    # Create in-app notification
    try:
        notify_system_message(request.user, 'Password Changed', 'Your account password was successfully changed.')
    except Exception:
        logger.exception('Failed to create in-app password-changed notification')

    return JsonResponse({
        'success': True,
        'message': 'Password changed successfully.'
    })


@login_required
@require_POST
@ensure_csrf_cookie
def delete_account_ajax(request):
    """
    AJAX view to permanently delete the logged-in user's account.
    Expects POST with password, reason (optional), reason_other (optional).
    Returns JSON with success and redirect URL.
    """
    try:
        content_type = request.META.get('CONTENT_TYPE', '') or request.content_type or ''
        if 'application/json' in content_type:
            try:
                data = json.loads(request.body.decode('utf-8') or '{}')
            except Exception:
                data = request.POST
        else:
            data = request.POST
    except Exception:
        data = request.POST
    password = data.get('password')
   
    reason = data.get('reason', '')
    reason_other = data.get('reason_other', '')

    # Verify password
    user = authenticate(username=request.user.username, password=password)
    if user is None:
        return JsonResponse({
            'success': False,
            'error': 'Invalid password. Please try again.'
        }, status=400)

    # Optionally log the deletion reason (for analytics or support)
    if reason:
        # You could save this to a model, send an email, or write to a log file
        full_reason = reason_other if reason == 'other' and reason_other else reason
        # Example: log to console (replace with your own logic)
        print(f"User {request.user.username} (ID: {request.user.id}) deleted account. Reason: {full_reason}")

    
    AccountDeletionLog.objects.create(
        user=user,
        username=user.username,
        email=user.email,
        reason=full_reason
    )

    # Log out the user before deletion (optional but recommended)
    logout(request)

    # Delete the user account
    
    user.delete()

    return JsonResponse({
        'success': True,
        'redirect': '/'  # or any other public URL
    })


# users/views.py (add at the end)

from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from .models import UserSettings

@login_required
@require_POST
def toggle_show_contact_info(request):
    """AJAX endpoint to toggle the show_contact_info field."""
    user = request.user
    user.show_contact_info = not user.show_contact_info
    user.save(update_fields=['show_contact_info'])
    return JsonResponse({'success': True, 'show_contact_info': user.show_contact_info})

@login_required
def get_user_settings(request):
    """Return current settings as JSON."""
    settings, _ = UserSettings.objects.get_or_create(user=request.user)
    return JsonResponse({
        'email_notifications': settings.email_notifications,
        'sms_notifications': settings.sms_notifications,
        'marketing_emails': settings.marketing_emails,
        'show_contact_info': request.user.show_contact_info,
    })

@login_required
@require_POST
def update_notification_settings(request):
    """Update one or more notification preferences."""
    try:
        import json
        data = json.loads(request.body)
    except:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)

    settings, _ = UserSettings.objects.get_or_create(user=request.user)
    if 'email_notifications' in data:
        settings.email_notifications = bool(data['email_notifications'])
    if 'sms_notifications' in data:
        settings.sms_notifications = bool(data['sms_notifications'])
    if 'marketing_emails' in data:
        settings.marketing_emails = bool(data['marketing_emails'])
    settings.save()

    return JsonResponse({'success': True})


@require_POST
@ensure_csrf_cookie
def capture_location(request):
    """Capture device location label for social signups (stored in session)."""
    try:
        payload = json.loads(request.body.decode('utf-8') or '{}') if request.body else {}
    except Exception:
        payload = {}
    if not payload:
        payload = request.POST
    label = (payload.get('location') or payload.get('label') or '').strip()
    lat = payload.get('lat')
    lng = payload.get('lng')
    if label:
        request.session['pending_location'] = label
        if lat and lng:
            request.session['pending_location_coords'] = {'lat': lat, 'lng': lng}
    return JsonResponse({'success': True})
