# users/utils.py
from datetime import timedelta
from django.utils import timezone
from django.urls import reverse

def verify_email_logic(user, code):
    """
    Verify a user's email using a 7‑digit code.
    Returns a tuple: (success, error_message, attempts_left, redirect_url)
    """
    now = timezone.now()
    today = now.date()

    # Reset daily attempts if it's a new day
    if user.last_verification_attempt_date != today:
        user.verification_attempts_today = 0
        user.last_verification_attempt_date = today
        user.save(update_fields=['verification_attempts_today', 'last_verification_attempt_date'])

    # Check if max attempts exceeded
    if user.verification_attempts_today >= 3:
        return False, "Maximum verification attempts reached. Try again tomorrow.", 0, None

    # Validate code presence and expiry (10 minutes)
    if user.email_verification_code == code and user.email_verification_sent_at:
        if now - user.email_verification_sent_at > timedelta(minutes=10):
            attempts_left = max(0, 3 - user.verification_attempts_today)
            return False, "Code expired. Request a new one.", attempts_left, None

        # Success – mark email as verified
        user.email_verified = True
        user.email_verification_code = None
        user.verification_attempts_today = 0
        user.save(update_fields=['email_verified', 'email_verification_code', 'verification_attempts_today'])

        # Determine redirect URL (if user still needs to add phone number)
        if not user.phone_number:
            redirect_url = reverse('profile-edit', kwargs={'pk': user.pk})
        else:
            redirect_url = reverse('home')

        return True, "", 0, redirect_url

    # Invalid code – increment attempts
    user.verification_attempts_today += 1
    user.save(update_fields=['verification_attempts_today'])
    attempts_left = max(0, 3 - user.verification_attempts_today)

    if attempts_left == 0:
        return False, "Maximum verification attempts reached. Try again tomorrow.", 0, None
    else:
        return False, f"Invalid code. {attempts_left} attempts remaining.", attempts_left, None