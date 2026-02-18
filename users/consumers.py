import json
import logging
from channels.generic.websocket import JsonWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth import authenticate, login as django_login
from django.contrib.sessions.models import Session
from django.utils import timezone
from django.core.exceptions import ValidationError
from .models import User
from .forms import CustomUserCreationForm
from .views import send_verification_email, send_welcome_email, send_password_reset_code
from .utils import verify_email_logic
import random
import string

logger = logging.getLogger(__name__)

class AuthConsumer(JsonWebsocketConsumer):
    def connect(self):
        self.accept()
        logger.info(f"WebSocket connected: {self.channel_name}")

    def disconnect(self, close_code):
        logger.info(f"WebSocket disconnected: {self.channel_name}")

    def receive_json(self, content):
        """Handle incoming JSON messages."""
        msg_type = content.get("type")
        if not msg_type:
            self.send_error("Missing message type")
            return

        # Dispatch based on type
        handler = getattr(self, f"handle_{msg_type}", None)
        if handler:
            handler(content)
        else:
            self.send_error(f"Unknown message type: {msg_type}")

    def send_error(self, message, close=False):
        self.send_json({"type": "error", "error": message})
        if close:
            self.close()

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------


    def validate_csrf(self, token):
        """Compare provided token with the session's CSRF token."""
        expected = self.scope["session"].get("_csrftoken")
        if not expected or token != expected:
            return False
        return True

    def handle_login(self, content):
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
        csrf_token = content.get("csrfmiddlewaretoken")

        if not username or not password:
            self.send_json({
                "type": "login_response",
                "success": False,
                "errors": {"__all__": ["Username and password required."]}
            })
            return

        # Authenticate
        user = authenticate(username=username, password=password)
        if user is None:
            self.send_json({
                "type": "login_response",
                "success": False,
                "errors": {"__all__": ["Invalid username or password."]}
            })
            return

        # Check if user is active
        if not user.is_active:
            self.send_json({
                "type": "login_response",
                "success": False,
                "errors": {"__all__": ["This account is inactive."]}
            })
            return

        # Log the user in (attach session)
        # We need to simulate a request object. Channels provides a session via the scope.
        # We can use the session from the scope and call django_login.
        # However, login() requires a request. We'll create a minimal request-like object.
        from django.contrib.auth import login
        from asgiref.sync import sync_to_async
        import threading

        # Because this is a synchronous consumer, we can call synchronous Django functions directly.
        # We'll need to attach the session to the scope first (already done by AuthMiddlewareStack).
        # Then call login() with a fake request.

        # Create a dummy request that holds the session and user
        class DummyRequest:
            def __init__(self, session, user):
                self.session = session
                self.user = user
                # Add other attributes if needed by login()

        # Get the session from the scope
        session_key = self.scope["session"].session_key
        if not session_key:
            # If no session exists, create one
            self.scope["session"].save()
            session_key = self.scope["session"].session_key

        # Create dummy request
        dummy_request = DummyRequest(self.scope["session"], user)

        # Call login
        login(dummy_request, user)

        # Set session expiry based on remember
        if not remember:
            self.scope["session"].set_expiry(0)  # session expires on browser close
        else:
            self.scope["session"].set_expiry(1209600)  # 2 weeks

        self.send_json({
            "type": "login_response",
            "success": True,
            "redirect": "/"  # or wherever you want
        })

    def handle_register(self, content):
        csrf_token = content.get("csrfmiddlewaretoken")
        if not self.validate_csrf(csrf_token):
            self.send_json({
                "type": "register_response",
                "success": False,
                "errors": {"__all__": ["Invalid CSRF token."]}
            })
            return
        # Reconstruct form data
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

        from .forms import CustomUserCreationForm
        form = CustomUserCreationForm(form_data)

        if form.is_valid():
            try:
                # Save user (but don't commit yet)
                user = form.save(commit=False)
                # Set verification code
                code = ''.join(random.choices(string.digits, k=7))
                user.email_verification_code = code
                user.email_verification_sent_at = timezone.now()
                user.verification_attempts_today = 0
                user.last_verification_attempt_date = timezone.now().date()
                user.save()

                # Send verification email
                send_verification_email(user)

                # Send welcome email in background
                send_welcome_email(user)

                # Log the user in (similar to login handler)
                from django.contrib.auth import login
                class DummyRequest:
                    def __init__(self, session, user):
                        self.session = session
                        self.user = user
                login(DummyRequest(self.scope["session"], user), user)

                self.send_json({
                    "type": "register_response",
                    "success": True,
                    "redirect": "/verify/"  # verification_required URL
                })
            except Exception as e:
                logger.exception("Registration error")
                self.send_json({
                    "type": "register_response",
                    "success": False,
                    "errors": {"__all__": [str(e)]}
                })
        else:
            # Return form errors in the same structure as AJAX endpoint
            errors = {}
            for field, err_list in form.errors.items():
                errors[field] = [str(e) for e in err_list]
            self.send_json({
                "type": "register_response",
                "success": False,
                "errors": errors
            })

    def handle_verify(self, content):
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
            # Log the user in via the session (if needed)
            if not self.scope["user"].is_authenticated:
                from django.contrib.auth import login
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

        # Check cooldown
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

        # Generate new code
        code = ''.join(random.choices(string.digits, k=7))
        user.email_verification_code = code
        user.email_verification_sent_at = now
        user.save()

        # Send email
        send_verification_email(user)

        self.send_json({
            "type": "resend_response",
            "success": True,
            "message": "Code resent."
        })