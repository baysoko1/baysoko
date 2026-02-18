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
from django.db import models, transaction, IntegrityError
from django.conf import settings
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.template.loader import render_to_string

import requests
from allauth.socialaccount.models import SocialApp, SocialAccount

from .models import User
from .forms import (
    CustomUserCreationForm,
    CustomUserChangeForm,
    CustomAuthenticationForm,
)

from listings.models import Listing

logger = logging.getLogger(__name__)

from baysoko.utils.email_helpers import _send_email_threaded, send_email_brevo, render_and_send
from notifications.utils import notify_system_message

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

# ----------------------------------------------------------------------
#  Registration
# ----------------------------------------------------------------------

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
                    user.location = 'Homabay'
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
        current_site = Site.objects.get_current()
        redirect_uri = (
            f"http://{request.get_host()}/accounts/google/callback/"
            if settings.DEBUG
            else f"https://{current_site.domain}/accounts/google/callback/"
        )

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
            'prompt': 'consent',
        }
        state = secrets.token_urlsafe(32)
        request.session['oauth_state'] = state
        request.session['oauth_action'] = 'register'
        params['state'] = state

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
        current_site = Site.objects.get_current()
        redirect_uri = (
            f"http://{request.get_host()}/accounts/google/callback/"
            if settings.DEBUG
            else f"https://{current_site.domain}/accounts/google/callback/"
        )

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
            'prompt': 'consent',
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

    if error:
        messages.error(request, f"Google authorization error: {error}")
        return redirect('register')
    if not code:
        messages.error(request, "Authorization code not received")
        return redirect('register')

    try:
        app = SocialApp.objects.get(provider='google')
        current_site = Site.objects.get_current()
        redirect_uri = (
            f"http://{request.get_host()}/accounts/google/callback/"
            if settings.DEBUG
            else f"https://{current_site.domain}/accounts/google/callback/"
        )

        token_url = 'https://oauth2.googleapis.com/token'
        data = {
            'client_id': app.client_id,
            'client_secret': app.secret,
            'code': code,
            'grant_type': 'authorization_code',
            'redirect_uri': redirect_uri,
        }
        response = requests.post(token_url, data=data, timeout=10)
        if response.status_code != 200:
            logger.error(f"Google token endpoint returned {response.status_code}: {response.text}")
            messages.error(request, "Failed to get access token from Google")
            return redirect('register')
        token_data = response.json()

        if 'access_token' not in token_data:
            logger.error(f"No access_token in token response: {token_data}")
            messages.error(request, "Failed to get access token from Google")
            return redirect('register')

        userinfo_url = 'https://www.googleapis.com/oauth2/v2/userinfo'
        headers = {'Authorization': f"Bearer {token_data['access_token']}"}
        userinfo_resp = requests.get(userinfo_url, headers=headers, timeout=10)
        if userinfo_resp.status_code != 200:
            logger.error(f"Google userinfo returned {userinfo_resp.status_code}: {userinfo_resp.text}")
            messages.error(request, "Failed to retrieve profile information from Google")
            return redirect('register')
        userinfo = userinfo_resp.json()

        email = userinfo.get('email')
        if not email:
            messages.error(request, "Email not provided by Google")
            return redirect('register')

        action = request.session.get('oauth_action')
        if action == 'connect' and request.user.is_authenticated:
            if email.lower() != request.user.email.lower():
                messages.error(request, 'Google account email does not match your account email.')
                return redirect('profile-edit', pk=request.user.pk)
            uid = userinfo.get('id')
            if not SocialAccount.objects.filter(user=request.user, provider='google', uid=uid).exists():
                SocialAccount.objects.create(user=request.user, provider='google', uid=uid, extra_data=userinfo)
            messages.success(request, 'Google account connected successfully.')
            return redirect('profile-edit', pk=request.user.pk)

        try:
            user = User.objects.get(email=email)
            login(request, user)
            if not user.phone_number:
                messages.info(request, 'Please verify your details and include phone number to continue.')
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

            user = User.objects.create(
                email=email.lower(),
                username=username,
                first_name=userinfo.get('given_name', ''),
                last_name=userinfo.get('family_name', ''),
                location='Homabay',
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
            request.session['just_registered'] = True
            request.session['just_registered_message'] = 'Account created with Google! Check your email to verify your account.'
            return redirect('verification_required')

    except Exception as e:
        logger.error(f"Google callback error: {str(e)}")
        messages.error(request, "Error during Google login. Please try again.")
        return redirect('register')

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
            user = User.objects.get(email=email)
            login(request, user)
            if not user.phone_number:
                messages.info(request, 'Please add your phone number to continue.')
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

            user = User.objects.create(
                email=email.lower(),
                username=username,
                first_name=userinfo.get('first_name', ''),
                last_name=userinfo.get('last_name', ''),
                location='Homabay',
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
            request.session['just_registered'] = True
            request.session['just_registered_message'] = 'Account created with Facebook! Check your email to verify your account.'
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

        stores = profile_user.stores.all()
        listings_qs = Listing.objects.filter(store__in=stores, is_sold=False).order_by('-date_created')
        paginator = Paginator(listings_qs, 8)
        page_number = self.request.GET.get('page')
        page_obj = paginator.get_page(page_number)
        context['page_obj'] = page_obj
        context['stores'] = stores

        saved_listings = None
        if user.is_authenticated and user == profile_user:
            saved_listings = Listing.objects.filter(favorites__user=user).order_by('-date_created')
        context['saved_listings'] = saved_listings
        context['listing_count'] = listings_qs.count()
        context['saved_count'] = saved_listings.count() if saved_listings is not None else 0
        context['rating_average'] = 4.5
        context['member_since'] = profile_user.date_joined.strftime("%B %Y")

        # Check if profile owner can connect Google
        try:
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
        form.fields['phone_number'].initial = self.object.phone_number
        form.fields['bio'].initial = self.object.bio
        form.fields['show_contact_info'].initial = self.object.show_contact_info
        return form

    def form_valid(self, form):
        if 'profile_picture' in self.request.FILES:
            form.instance.profile_picture = self.request.FILES['profile_picture']
        messages.success(self.request, 'Profile updated successfully!')
        return super().form_valid(form)

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


