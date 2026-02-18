# users/consumers.py
import json
import logging
import random
import string

from channels.generic.websocket import JsonWebsocketConsumer
from django.utils import timezone
from django.core.exceptions import ValidationError

logger = logging.getLogger(__name__)


class AuthConsumer(JsonWebsocketConsumer):
    def connect(self):
        self.accept()
        logger.info(f"WebSocket connected: {self.channel_name}")

    def disconnect(self, close_code):
        logger.info(f"WebSocket disconnected: {self.channel_name}")

    def receive_json(self, content):
        msg_type = content.get("type")
        if not msg_type:
            self.send_error("Missing message type")
            return

        handler = getattr(self, f"handle_{msg_type}", None)
        if handler:
            handler(content)
        else:
            self.send_error(f"Unknown message type: {msg_type}")

    def send_error(self, message, close=False):
        self.send_json({"type": "error", "error": message})
        if close:
            self.close()

    def validate_csrf(self, token):
        """Compare provided token with the session's CSRF token."""
        expected = self.scope["session"].get("_csrftoken")
        if not expected or token != expected:
            return False
        return True

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def handle_login(self, content):
        # Validate CSRF token first
        csrf_token = content.get("csrfmiddlewaretoken")
        if not self.validate_csrf(csrf_token):
            self.send_json({
                "type": "login_response",
                "success": False,
                "errors": {"__all__": ["Invalid CSRF token."]}
            })
            return

        username = content.get("username")
        password = content.get("password")
        remember = content.get("remember", False)

        if not username or not password:
            self.send_json({
                "type": "login_response",
                "success": False,
                "errors": {"__all__": ["Username and password required."]}
            })
            return

        # Import authentication functions locally
        from django.contrib.auth import authenticate, login

        user = authenticate(username=username, password=password)
        if user is None:
            self.send_json({
                "type": "login_response",
                "success": False,
                "errors": {"__all__": ["Invalid username or password."]}
            })
            return

        if not user.is_active:
            self.send_json({
                "type": "login_response",
                "success": False,
                "errors": {"__all__": ["This account is inactive."]}
            })
            return

        # Create a dummy request for login()
        class DummyRequest:
            def __init__(self, session, user):
                self.session = session
                self.user = user

        # Ensure session exists
        if not self.scope["session"].session_key:
            self.scope["session"].save()

        dummy_request = DummyRequest(self.scope["session"], user)
        login(dummy_request, user)

        # Set session expiry based on "remember"
        if remember:
            self.scope["session"].set_expiry(1209600)   # 2 weeks
        else:
            self.scope["session"].set_expiry(0)         # browser close

        self.send_json({
            "type": "login_response",
            "success": True,
            "redirect": "/"
        })

    def handle_register(self, content):
        # Validate CSRF token
        csrf_token = content.get("csrfmiddlewaretoken")
        if not self.validate_csrf(csrf_token):
            self.send_json({
                "type": "register_response",
                "success": False,
                "errors": {"__all__": ["Invalid CSRF token."]}
            })
            return

        # Import forms and models locally
        from .forms import CustomUserCreationForm
        from .models import User
        from django.contrib.auth import login
        from .views import send_verification_email, send_welcome_email

        form_data = {
            "first_name": content.get("first_name"),
            "last_name": content.get("last_name"),
            "username": content.get("username"),
            "email": content.get("email"),
            "password1": content.get("password1"),
            "password2": content.get("password2"),
            "phone_number": content.get("phone_number"),
            "location": content.get("location"),
            "terms": content.get("terms", False),
        }

        form = CustomUserCreationForm(form_data)

        if form.is_valid():
            try:
                user = form.save(commit=False)
                # Generate verification code
                code = ''.join(random.choices(string.digits, k=7))
                user.email_verification_code = code
                user.email_verification_sent_at = timezone.now()
                user.verification_attempts_today = 0
                user.last_verification_attempt_date = timezone.now().date()
                user.save()

                # Send emails
                send_verification_email(user)
                send_welcome_email(user)

                # Log the user in
                class DummyRequest:
                    def __init__(self, session, user):
                        self.session = session
                        self.user = user
                login(DummyRequest(self.scope["session"], user), user)

                self.send_json({
                    "type": "register_response",
                    "success": True,
                    "redirect": "/verify/"
                })
            except Exception as e:
                logger.exception("Registration error")
                self.send_json({
                    "type": "register_response",
                    "success": False,
                    "errors": {"__all__": [str(e)]}
                })
        else:
            errors = {}
            for field, err_list in form.errors.items():
                errors[field] = [str(e) for e in err_list]
            self.send_json({
                "type": "register_response",
                "success": False,
                "errors": errors
            })

    def handle_verify(self, content):
        from .models import User
        from .utils import verify_email_logic
        from django.contrib.auth import login

        user_id = content.get("user_id")
        code = content.get("code")
        if not user_id or not code:
            self.send_json({
                "type": "verify_response",
                "success": False,
                "error": "Missing user_id or code."
            })
            return

        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            self.send_json({
                "type": "verify_response",
                "success": False,
                "error": "User not found."
            })
            return

        success, error_msg, attempts_left, redirect_url = verify_email_logic(user, code)

        if success:
            if not self.scope["user"].is_authenticated:
                class DummyRequest:
                    def __init__(self, session, user):
                        self.session = session
                        self.user = user
                login(DummyRequest(self.scope["session"], user), user)

            self.send_json({
                "type": "verify_response",
                "success": True,
                "redirect": redirect_url
            })
        else:
            self.send_json({
                "type": "verify_response",
                "success": False,
                "error": error_msg,
                "attempts_left": attempts_left
            })

    def handle_resend(self, content):
        from .models import User
        from .views import send_verification_email

        user_id = content.get("user_id")
        if not user_id:
            self.send_json({
                "type": "resend_response",
                "success": False,
                "error": "Missing user_id."
            })
            return

        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            self.send_json({
                "type": "resend_response",
                "success": False,
                "error": "User not found."
            })
            return

        now = timezone.now()
        if user.email_verification_sent_at and (now - user.email_verification_sent_at).seconds < 60:
            wait = 60 - (now - user.email_verification_sent_at).seconds
            self.send_json({
                "type": "resend_response",
                "success": False,
                "error": f"Please wait {wait} seconds.",
                "wait": wait
            })
            return

        code = ''.join(random.choices(string.digits, k=7))
        user.email_verification_code = code
        user.email_verification_sent_at = now
        user.save()

        send_verification_email(user)

        self.send_json({
            "type": "resend_response",
            "success": True,
            "message": "Code resent."
        })