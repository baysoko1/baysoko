import logging
from django.dispatch import receiver
from django.db.models.signals import post_save
from .models import User, UserSettings


logger = logging.getLogger(__name__)

try:
    from allauth.account.signals import user_signed_up
    from notifications.utils import create_and_broadcast_notification

    @receiver(user_signed_up)
    def welcome_social_user(request, user, **kwargs):
        """Send welcome notification when a user signs up via social providers."""
        try:
            create_and_broadcast_notification(
                recipient=user,
                notification_type='system',
                title='Welcome to Baysoko',
                message='Your account was created via social login. Welcome to Baysoko!',
                action_url='/',
                action_text='Start Exploring'
            )
        except Exception:
            logger.exception('Failed to create broadcast notification for social signup')

        # Set session toast if request is available
        try:
            if request is not None:
                request.session['welcome_toast'] = {
                    'title': 'Welcome to Baysoko',
                    'message': 'Account created via social login. Welcome!',
                    'variant': 'success',
                    'duration': 8000
                }
        except Exception:
            logger.debug('Could not set welcome_toast in session for social signup')

except Exception:
    logger.debug('allauth signals not available; skipping social signup signal hookup')


# users/signals.py

@receiver(post_save, sender=User)
def create_user_settings(sender, instance, created, **kwargs):
    if created:
        UserSettings.objects.create(user=instance)

@receiver(post_save, sender=User)
def save_user_settings(sender, instance, **kwargs):
    # Ensure `UserSettings` exists for this user, create if missing (avoids RelatedObjectDoesNotExist)
    try:
        settings_obj, created = UserSettings.objects.get_or_create(user=instance)
        # Save the settings instance to trigger any save hooks if necessary
        settings_obj.save()
    except Exception:
        logger.exception('Failed to ensure user settings exist for user %s', getattr(instance, 'pk', None))
