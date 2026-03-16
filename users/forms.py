# users/forms.py (updated)

from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import ValidationError
import re
from .models import User
import os
import logging
import threading
import requests
from django.template.loader import render_to_string
from django.core.mail import send_mail, get_connection, EmailMessage as DjangoEmailMessage
from django.conf import settings

logger = logging.getLogger(__name__)

from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth import authenticate
from django.utils.translation import gettext_lazy as _

class CustomUserCreationForm(forms.ModelForm):
    password1 = forms.CharField(
        label='Password',
        widget=forms.PasswordInput(attrs={'class': 'input-modern', 'placeholder': 'Create a strong password', 'autocomplete': 'new-password'}),
        help_text='Password must be at least 8 characters with letters and numbers.'
    )
    password2 = forms.CharField(
        label='Confirm Password',
        widget=forms.PasswordInput(attrs={'class': 'input-modern', 'placeholder': 'Confirm your password', 'autocomplete': 'new-password'})
    )
    terms = forms.BooleanField(
        required=True,
        error_messages={'required': 'You must agree to the Terms of Service and Privacy Policy.'}
    )

    class Meta:
        model = User
        fields = ('username', 'email', 'first_name', 'last_name', 'phone_number', 'location')
        widgets = {
            'username': forms.TextInput(attrs={'class': 'input-modern', 'placeholder': 'Choose a username'}),
            'email': forms.EmailInput(attrs={'class': 'input-modern', 'placeholder': 'your.email@example.com'}),
            'first_name': forms.TextInput(attrs={'class': 'input-modern', 'placeholder': 'First name'}),
            'last_name': forms.TextInput(attrs={'class': 'input-modern', 'placeholder': 'Last name'}),
            'phone_number': forms.TextInput(attrs={'class': 'input-modern', 'placeholder': '+254 712 345 678'}),
            'location': forms.TextInput(attrs={'class': 'input-modern', 'placeholder': 'Your area in Homabay'}),
        }
        error_messages = {
            'username': {
                'required': 'Username is required.',
                'unique': 'This username is already taken.',
                'max_length': 'Username is too long.',
            },
            'email': {
                'required': 'Email address is required.',
                'unique': 'This email is already registered.',
                'invalid': 'Please enter a valid email address.',
            },
            'first_name': {'required': 'First name is required.', 'max_length': 'First name is too long.'},
            'last_name': {'required': 'Last name is required.', 'max_length': 'Last name is too long.'},
            'phone_number': {'required': 'Phone number is required.', 'max_length': 'Phone number is too long.'},
            'location': {'required': 'Location is required.', 'max_length': 'Location is too long.'},
        }

    def clean_password1(self):
        password1 = self.cleaned_data.get('password1')
        if len(password1) < 8:
            raise ValidationError("Password must be at least 8 characters long.")
        if not re.search(r'\d', password1):
            raise ValidationError("Password must contain at least one digit.")
        if not re.search(r'[a-zA-Z]', password1):
            raise ValidationError("Password must contain at least one letter.")
        return password1

    def clean_email(self):
        email = self.cleaned_data.get('email')
        if email:
            email = email.lower().strip()
            if User.objects.filter(email__iexact=email).exists():
                raise ValidationError("This email is already registered.")
        return email

    def clean_username(self):
        username = self.cleaned_data.get('username')
        if username:
            username = username.strip()
            if User.objects.filter(username__iexact=username).exists():
                raise ValidationError("This username is already taken.")
            if len(username) < 3:
                raise ValidationError("Username must be at least 3 characters.")
            if not re.match(r'^[\w.@+-]+$', username):
                raise ValidationError("Username can only contain letters, numbers, and @/./+/-/_ characters.")
        return username

    def clean_phone_number(self):
        phone_number = self.cleaned_data.get('phone_number')
        if phone_number:
            phone_number = phone_number.strip()
            leading_plus = phone_number.startswith('+')
            digits = re.sub(r'[^0-9]', '', phone_number)
            phone_number = f"+{digits}" if leading_plus else digits
            if not re.match(r'^\+?[0-9]+$', phone_number):
                raise ValidationError("Please enter a valid phone number.")
            if User.objects.filter(phone_number=phone_number).exists():
                raise ValidationError("This phone number is already registered.")
        else:
            phone_number = None
        return phone_number

    def clean(self):
        cleaned_data = super().clean()
        password1 = cleaned_data.get('password1')
        password2 = cleaned_data.get('password2')
        if password1 and password2 and password1 != password2:
            self.add_error('password2', "Passwords do not match.")
        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        user.is_active = True   # User can log in but middleware will enforce email verification
        phone = self.cleaned_data.get('phone_number')
        user.phone_number = phone if phone else None
        if commit:
            user.save()
        return user

class EmailVerificationForm(forms.Form):
    code = forms.CharField(
        max_length=7,
        min_length=7,
        widget=forms.TextInput(attrs={
            'class': 'verification-input',
            'placeholder': '• • • • • • •',
            'autocomplete': 'off',
            'inputmode': 'numeric',
            'pattern': '[0-9]*'
        })
    )
    
class CustomUserChangeForm(forms.ModelForm):
    class Meta:
        model = User
        fields = [
            'first_name', 
            'last_name', 
            'username', 
            'email', 
            'phone_number', 
            'location',
            'bio', 
            'profile_picture',
            'cover_photo',
            'show_contact_info'
        ]
        widgets = {
            'bio': forms.Textarea(attrs={'rows': 4, 'maxlength': 500}),
            'profile_picture': forms.FileInput(attrs={'accept': 'image/*'}),
            'cover_photo': forms.FileInput(attrs={'accept': 'image/*'}),
            'location': forms.TextInput(attrs={'class': 'input-modern', 'placeholder': 'Your area in Homabay'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Enforce required fields for profile completion
        if 'phone_number' in self.fields:
            self.fields['phone_number'].required = True
        if 'location' in self.fields:
            self.fields['location'].required = True

        # Lock verified contact fields only after the change limit is reached
        try:
            if getattr(self.instance, 'email_verified', False) and getattr(self.instance, 'email_change_count', 0) >= 2:
                self.fields['email'].disabled = True
            if getattr(self.instance, 'phone_verified', False) and getattr(self.instance, 'phone_change_count', 0) >= 2:
                self.fields['phone_number'].disabled = True
        except Exception:
            pass

    def clean_username(self):
        username = self.cleaned_data.get('username')
        if User.objects.filter(username=username).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError('This username is already taken.')
        return username
    
    def clean_email(self):
        email = self.cleaned_data.get('email')
        # Prevent changing verified email
        try:
            if getattr(self.instance, 'email_verified', False) and email and email != self.instance.email:
                if getattr(self.instance, 'email_change_count', 0) >= 2:
                    raise forms.ValidationError('Email can only be changed twice after verification.')
        except Exception:
            pass
        if User.objects.filter(email=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError('This email is already registered.')
        return email

    def clean_phone_number(self):
        phone_number = self.cleaned_data.get('phone_number')
        # Prevent changing verified phone number
        try:
            if getattr(self.instance, 'phone_verified', False) and phone_number and phone_number != self.instance.phone_number:
                if getattr(self.instance, 'phone_change_count', 0) >= 2:
                    raise forms.ValidationError('Phone number can only be changed twice after verification.')
        except Exception:
            pass
        if phone_number:
            phone_number = phone_number.strip()
            leading_plus = phone_number.startswith('+')
            digits = re.sub(r'[^0-9]', '', phone_number)
            phone_number = f"+{digits}" if leading_plus else digits
            if not re.match(r'^\+?[0-9]+$', phone_number):
                raise forms.ValidationError("Please enter a valid phone number.")
            if User.objects.filter(phone_number=phone_number).exclude(pk=self.instance.pk).exists():
                raise forms.ValidationError("This phone number is already registered.")
            return phone_number
        raise forms.ValidationError("Phone number is required.")

    def clean_location(self):
        location = (self.cleaned_data.get('location') or '').strip()
        if not location:
            raise forms.ValidationError("Location is required.")
        return location


class CustomAuthenticationForm(AuthenticationForm):
    username = forms.CharField(
        max_length=254,
        widget=forms.TextInput(attrs={'autofocus': True, 'class': 'input-modern'}),
        label=_('Email or Username')
    )

    def clean(self):
        username = self.cleaned_data.get('username')
        password = self.cleaned_data.get('password')
        if username and password:
            user_qs = User.objects.none()
            if '@' in username:
                user_qs = User.objects.filter(email__iexact=username)
            if not user_qs.exists():
                user_qs = User.objects.filter(username__iexact=username)

            if user_qs.exists():
                user_obj = user_qs.first()
                user = authenticate(self.request, username=user_obj.username, password=password)
            else:
                user = authenticate(self.request, username=username, password=password)

            if user is None:
                raise forms.ValidationError(self.error_messages['invalid_login'], code='invalid_login')
            else:
                self.confirm_login_allowed(user)
                self.user_cache = user
        return self.cleaned_data


from django.contrib.auth.forms import PasswordResetForm


class CustomPasswordResetForm(PasswordResetForm):
    """Override PasswordResetForm.send_mail to route through Brevo API first
    and fall back to SMTP/Django backends. Sending happens on a background
    thread to avoid blocking request handling.
    """
    def send_mail(self, subject_template_name, email_template_name,
                  context, from_email, to_email, html_email_template_name=None):
        subject = render_to_string(subject_template_name, context)
        # Subject may contain newlines — collapse to single line
        subject = ''.join(subject.splitlines())
        plain_message = render_to_string(email_template_name, context)
        html_message = None
        if html_email_template_name:
            html_message = render_to_string(html_email_template_name, context)

        def _send():
            try:
                brevo_key = (
                    os.environ.get('BREVO_API_KEY') or os.environ.get('SENDINBLUE_API_KEY') or os.environ.get('SIB_API_KEY')
                )
                if not brevo_key:
                    email_host_val = getattr(settings, 'EMAIL_HOST', os.environ.get('EMAIL_HOST', '')).lower()
                    if 'brevo' in email_host_val or 'sendinblue' in email_host_val:
                        brevo_key = os.environ.get('EMAIL_HOST_PASSWORD') or getattr(settings, 'EMAIL_HOST_PASSWORD', None)

                if brevo_key:
                    try:
                        headers = {'accept': 'application/json', 'api-key': brevo_key, 'content-type': 'application/json'}
                        sender = {'name': os.environ.get('EMAIL_FROM_NAME', 'Baysoko'), 'email': settings.DEFAULT_FROM_EMAIL}
                        to_list = [{'email': e, 'name': ''} for e in to_email]
                        payload = {'sender': sender, 'to': to_list, 'subject': subject, 'textContent': plain_message, 'htmlContent': html_message or plain_message}
                        resp = requests.post('https://api.brevo.com/v3/smtp/email', json=payload, headers=headers, timeout=10)
                        if 200 <= getattr(resp, 'status_code', 0) < 300:
                            logger.info('Password reset email sent via Brevo API to %s; status=%s', to_email, resp.status_code)
                            return
                        logger.warning('Brevo API password reset send failed status=%s body=%s', getattr(resp, 'status_code', None), getattr(resp, 'text', None))
                    except Exception:
                        logger.exception('Brevo API password reset attempt failed; will fallback to SMTP/Django backend')

                envelope_from = os.environ.get('SMTP_ENVELOPE_FROM') or settings.DEFAULT_FROM_EMAIL
                try:
                    connection = get_connection()
                    msg = DjangoEmailMessage(subject=subject, body=plain_message, from_email=envelope_from, to=to_email, connection=connection, headers={'From': settings.DEFAULT_FROM_EMAIL})
                    if html_message:
                        try:
                            msg.attach_alternative(html_message, 'text/html')
                        except Exception:
                            pass
                    msg.send(fail_silently=False)
                    logger.info('Password reset email sent for %s via SMTP connection using envelope=%s', to_email, envelope_from)
                    return
                except Exception:
                    logger.exception('Direct send via Django connection failed; falling back to send_mail')

                # Final fallback: use Django send_mail
                try:
                    send_mail(subject, plain_message, settings.DEFAULT_FROM_EMAIL, to_email, html_message=html_message, fail_silently=False)
                    logger.info('Password reset email sent for %s via configured EMAIL_BACKEND', to_email)
                except Exception:
                    logger.exception('Final fallback send_mail failed for password reset to %s', to_email)
            except Exception:
                logger.exception('Unexpected error while sending password reset email to %s', to_email)

        threading.Thread(target=_send, daemon=True).start()
