# users/views.py - Fixed with auth_views import
from django.shortcuts import render, redirect
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib.auth.views import PasswordChangeView, LoginView, LogoutView
from django.contrib.auth import views as auth_views
from django.views.generic import DetailView, UpdateView
from django.urls import reverse_lazy, reverse
from django.contrib import messages
from django import forms
from .models import User
from .forms import CustomUserCreationForm, CustomUserChangeForm, CustomAuthenticationForm
from listings.models import Listing
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404
from django.db import models, transaction
from django.db import IntegrityError
from django.contrib.admin.views.decorators import staff_member_required
from django.conf import settings
from allauth.socialaccount.models import SocialApp, SocialAccount
from django.contrib.sites.models import Site
import os
import logging
from urllib.parse import urlencode
import secrets
import requests
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
import io
import contextlib
import smtplib
import traceback
from email.message import EmailMessage
import random
import string
from datetime import timedelta
from django.utils import timezone
from django.template.loader import render_to_string
from django.core.mail import send_mail, get_connection, EmailMessage as DjangoEmailMessage
from django.http import JsonResponse, HttpResponse
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.messages import get_messages
import threading

logger = logging.getLogger(__name__)

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

                # Attempt save inside a transaction and handle unique constraint gracefully
                try:
                    with transaction.atomic():
                        user.save()
                except IntegrityError as ie:
                    # Likely a race condition on unique phone/email. Inspect message and attach field-specific errors.
                    msg = 'A user with that phone number or email already exists.'
                    ie_msg = str(ie).lower() if ie else ''
                    logger.warning(f"IntegrityError saving user (likely duplicate): {ie}")
                    # Log any existing users with the same phone/email to aid debugging
                    try:
                        if phone:
                            qs = User.objects.filter(phone_number=phone).values_list('id', 'phone_number')
                            logger.info('Existing users with same phone: %s', list(qs))
                        if user.email:
                            qs2 = User.objects.filter(email__iexact=user.email).values_list('id', 'email')
                            logger.info('Existing users with same email: %s', list(qs2))
                    except Exception:
                        logger.exception('Failed to log existing duplicates')
                    # Decide which field to attach the error to
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

                # Log the user in (user.is_active is True now)
                login(request, user, backend='django.contrib.auth.backends.ModelBackend')
                # mark session so verification page shows one unified message
                request.session['just_registered'] = True
                request.session['just_registered_message'] = 'Registration successful. Please check your email for verification code.'

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

def send_verification_email(user):
    subject = 'Verify your email for Baysoko'
    html_message = render_to_string('users/verification_email.html', {
        'user': user,
        'code': user.email_verification_code,
        'site_name': 'Baysoko',
    })
    plain_message = f'Your verification code is: {user.email_verification_code}'

    # Send email on a background thread to avoid blocking the web worker.
    def _send():
        try:
            # Choose an envelope-from acceptable to the SMTP provider (Brevo).
            # Priority: explicit env override -> EMAIL_HOST_USER (if email-like) -> DEFAULT_FROM_EMAIL
            env_override = (
                os.environ.get('SMTP_ENVELOPE_FROM')
                or os.environ.get('MAIL_ENVELOPE_FROM')
                or os.environ.get('MAILTRAP_ENVELOPE_FROM')
            )

            email_host_user = getattr(settings, 'EMAIL_HOST_USER', None)
            envelope_from = None
            if env_override:
                envelope_from = env_override
            elif email_host_user and '@' in str(email_host_user):
                envelope_from = email_host_user
            else:
                envelope_from = settings.DEFAULT_FROM_EMAIL

            # Use Django's configured email connection (will pick up Brevo settings)
            try:
                connection = get_connection()
                msg = DjangoEmailMessage(
                    subject=subject,
                    body=plain_message,
                    from_email=envelope_from,
                    to=[user.email],
                    connection=connection,
                    headers={'From': settings.DEFAULT_FROM_EMAIL},
                )
                try:
                    msg.attach_alternative(html_message, 'text/html')
                except Exception:
                    # If attaching HTML fails, proceed with plain text only
                    pass

                msg.send(fail_silently=False)
                logger.info('Verification email sent for user id=%s via connection %s using envelope=%s', getattr(user, 'id', None), type(connection).__name__, envelope_from)
                return
            except Exception:
                logger.exception('Direct send via Django connection failed; falling back to send_mail')

            # Final fallback: Django's send_mail (will also use configured EMAIL_BACKEND)
            send_mail(
                subject,
                plain_message,
                settings.DEFAULT_FROM_EMAIL,
                [user.email],
                html_message=html_message,
                fail_silently=False,
            )
            logger.info('Verification email sent for user id=%s via configured EMAIL_BACKEND', getattr(user, 'id', None))
        except Exception as e:
            logger.exception('Failed to send verification email for user id=%s: %s', getattr(user, 'id', None), e)

    t = threading.Thread(target=_send, daemon=True)
    t.start()

# users/views.py (only the relevant change)

@csrf_exempt
def verify_email(request):
    if request.method == 'POST':
        user_id = request.POST.get('user_id')
        code = request.POST.get('code')
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'User not found.'})
        now = timezone.now()
        today = now.date()

        # Reset daily counters if needed
        if user.last_verification_attempt_date != today:
            user.verification_attempts_today = 0
            user.last_verification_attempt_date = today
            user.save()

        # If locked due to too many failed attempts today
        if user.verification_attempts_today >= 3:
            return JsonResponse({'success': False, 'error': 'Maximum verification attempts reached. Try again tomorrow.', 'attempts_left': 0})

        # Validate code and expiry (10 minutes)
        if user.email_verification_code and user.email_verification_code == code and user.email_verification_sent_at:
            if now - user.email_verification_sent_at > timedelta(minutes=10):
                return JsonResponse({'success': False, 'error': 'Code expired. Request a new one.', 'attempts_left': max(0, 3 - user.verification_attempts_today)})
            # Successful verification
            user.email_verified = True
            user.is_active = True
            user.email_verification_code = None
            user.verification_attempts_today = 0
            user.save()
            if not request.user.is_authenticated:
                login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            # If user has no phone number, send them to profile-edit to add one
            if not user.phone_number:
                redirect_url = reverse('profile-edit', kwargs={'pk': user.pk})
            else:
                redirect_url = reverse('home')
            return JsonResponse({'success': True, 'redirect': redirect_url})

        # Invalid code -> increment attempts
        user.verification_attempts_today += 1
        user.last_verification_attempt_date = today
        user.save()
        attempts_left = max(0, 3 - user.verification_attempts_today)
        if attempts_left <= 0:
            return JsonResponse({'success': False, 'error': 'Maximum verification attempts reached. Try again tomorrow.', 'attempts_left': 0})
        return JsonResponse({'success': False, 'error': f'Invalid code. {attempts_left} attempts remaining.', 'attempts_left': attempts_left})
    return JsonResponse({'success': False, 'error': 'Invalid request.'})



@csrf_exempt
def resend_code(request):
    try:
        # Diagnostic logging to help debug 502s on Render (capture headers and sizes)
        try:
            content_type = request.META.get('CONTENT_TYPE') or request.headers.get('content-type')
        except Exception:
            content_type = None
        try:
            content_length = int(request.META.get('CONTENT_LENGTH') or 0)
        except Exception:
            content_length = 0
        logger.info(
            "resend_code called: method=%s content_type=%s content_length=%s remote_addr=%s",
            request.method,
            content_type,
            content_length,
            request.META.get('REMOTE_ADDR') or request.META.get('HTTP_X_FORWARDED_FOR')
        )

        # Accept POST or GET to be resilient behind proxies or when requests are transformed
        if request.method not in ('POST', 'GET'):
            return JsonResponse({'success': False, 'error': 'Invalid request method.'})

        # Try common places for user_id: POST form, GET query, or raw JSON body
        user_id = None
        try:
            user_id = request.POST.get('user_id') or request.GET.get('user_id')
        except Exception as e:
            logger.warning('Could not read request.POST: %s', e)

        if not user_id:
            # Attempt to parse JSON body as fallback
            try:
                import json
                body = request.body.decode('utf-8') if getattr(request, 'body', None) else ''
                if body:
                    data = json.loads(body)
                    user_id = data.get('user_id')
                    logger.info('resend_code parsed JSON body; keys=%s', list(data.keys()))
            except Exception:
                # ignore parse errors; will validate below
                pass

        if not user_id:
            logger.info('resend_code missing user_id; content_type=%s content_length=%s', content_type, content_length)
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
        # Do not increment verification_attempts here (used for failed attempts)
        user.save()

        send_verification_email(user)
        return JsonResponse({'success': True, 'message': 'Code resent.'})
    except Exception as e:
        logger.exception('Error in resend_code')
        return JsonResponse({'success': False, 'error': 'Server error while resending code.'})

@login_required
def verification_required(request):
    # If already verified, go home
    if request.user.email_verified:
        return redirect('home')

    # Consume and clear existing messages to avoid duplicates or stale phone prompts
    list(get_messages(request))  # iterate to clear storage

    # If we just registered, show a single unified message
    if request.session.pop('just_registered', False):
        msg = request.session.pop('just_registered_message', 'Account created. Check your email to verify your account.')
        messages.success(request, msg)

    return render(request, 'users/verify_email.html', {'user': request.user})

def google_login(request):
    try:
        from django.contrib.sites.models import Site
        current_site = Site.objects.get_current()

        if settings.DEBUG:
            redirect_uri = f"http://{request.get_host()}/accounts/google/callback/"
        else:
            redirect_uri = f"https://{current_site.domain}/accounts/google/callback/"

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
    """Initiate Google OAuth to *connect* an existing account.
    Sets session oauth_action='connect' so callback links the social account.
    """
    if not request.user.is_authenticated:
        messages.error(request, 'You must be signed in to connect a Google account.')
        return redirect('login')

    try:
        from django.contrib.sites.models import Site
        current_site = Site.objects.get_current()

        if settings.DEBUG:
            redirect_uri = f"http://{request.get_host()}/accounts/google/callback/"
        else:
            redirect_uri = f"https://{current_site.domain}/accounts/google/callback/"

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
        from django.contrib.sites.models import Site
        current_site = Site.objects.get_current()

        if settings.DEBUG:
            redirect_uri = f"http://{request.get_host()}/accounts/google/callback/"
        else:
            redirect_uri = f"https://{current_site.domain}/accounts/google/callback/"

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
        headers = {'Authorization': f"Bearer {token_data.get('access_token')}"}
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

        # If action is 'connect' and user is authenticated, link the social account
        action = request.session.get('oauth_action')
        if action == 'connect' and request.user.is_authenticated:
            # Ensure the returned email matches the logged-in user
            if email.lower() != request.user.email.lower():
                messages.error(request, 'Google account email does not match your account email.')
                return redirect('profile-edit', pk=request.user.pk)
            # create SocialAccount if missing
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
            counter = 1
            original_username = username
            while User.objects.filter(username=username).exists():
                username = f"{original_username}{counter}"
                counter += 1

            user = User.objects.create(
                email=(email or '').lower(),
                username=username,
                first_name=userinfo.get('given_name', ''),
                last_name=userinfo.get('family_name', ''),
                location='Homabay',
                phone_number=None,
                is_active=True   # <-- changed to True
            )
            user.set_unusable_password()
            code = ''.join(random.choices(string.digits, k=7))
            user.email_verification_code = code
            user.email_verification_sent_at = timezone.now()
            user.email_verified = False
            user.save()

            send_verification_email(user)
            login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            # set session flag instead of immediate message to avoid duplicates
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
            counter = 1
            original_username = username
            while User.objects.filter(username=username).exists():
                username = f"{original_username}{counter}"
                counter += 1

            user = User.objects.create(
                email=(email or '').lower(),
                username=username,
                first_name=userinfo.get('first_name', ''),
                last_name=userinfo.get('last_name', ''),
                location='Homabay',
                phone_number=None,
                is_active=True   # <-- changed to True
            )
            user.set_unusable_password()
            code = ''.join(random.choices(string.digits, k=7))
            user.email_verification_code = code
            user.email_verification_sent_at = timezone.now()
            user.email_verified = False
            user.save()

            send_verification_email(user)
            login(request, user, backend='django.contrib.auth.backends.ModelBackend')
            # set session flag instead of immediate message to avoid duplicates
            request.session['just_registered'] = True
            request.session['just_registered_message'] = 'Account created with Facebook! Check your email to verify your account.'
            return redirect('verification_required')

    except Exception as e:
        logger.error(f"Facebook callback error: {str(e)}")
        messages.error(request, "Error during Facebook login. Please try again.")
        return redirect('register')


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

        # Indicate whether the profile owner can connect Google (has gmail and no google SocialAccount)
        try:
            can_connect = False
            if profile_user.email and profile_user.email.lower().endswith('@gmail.com'):
                # Only allow connect for accounts created via the form (have a usable password)
                if profile_user.has_usable_password() and not SocialAccount.objects.filter(user=profile_user, provider='google').exists():
                    can_connect = True
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
        # For profile edit page, suggest connecting Google if user has gmail and not yet connected
        try:
            user = self.request.user
            can_connect = False
            if user.is_authenticated and user.email and user.email.lower().endswith('@gmail.com'):
                if user.has_usable_password() and not SocialAccount.objects.filter(user=user, provider='google').exists():
                    can_connect = True
        except Exception:
            can_connect = False
        context['can_connect_google'] = can_connect
        return context


class CustomPasswordChangeView(LoginRequiredMixin, PasswordChangeView):
    template_name = 'users/password_change.html'
    success_url = reverse_lazy('password_change_done')

    def form_valid(self, form):
        messages.success(self.request, 'Your password has been changed successfully!')
        return super().form_valid(form)


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
            from django.core.mail import get_connection
            connection = get_connection()
            logger.info(f"Email backend: {connection.__class__.__name__}")
            response = super().form_valid(form)
            if connection.__class__.__name__ == 'ConsoleBackend':
                logger.info(f"Password reset email would be sent to: {email} (printed to console)")
                messages.success(self.request, f'Password reset email has been printed to console for {email}.')
            else:
                logger.info(f"Password reset email sent successfully to: {email}")
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
        'social_providers': settings.SOCIALACCOUNT_PROVIDERS if hasattr(settings, 'SOCIALACCOUNT_PROVIDERS') else {},
    })


@staff_member_required
def debug_send_email(request):
    to_addr = request.GET.get('to') or request.user.email or settings.DEFAULT_FROM_EMAIL
    subject = request.GET.get('subject', 'Baysoko SMTP Debug')
    body = request.GET.get('body', 'This is a test message from Baysoko SMTP debug endpoint.')

    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [to_addr], fail_silently=False)
        logger.info(f"Debug email sent to {to_addr}")
        return JsonResponse({'success': True, 'message': f'Debug email sent to {to_addr}'})
    except Exception as e:
        logger.error(f"Failed to send debug email: {e}", exc_info=True)
        return JsonResponse({'success': False, 'error': str(e)})
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