# users/models.py (full file with new fields)

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models.functions import Lower
from django.conf import settings
import os

try:
    from cloudinary.models import CloudinaryField
    CLOUDINARY_AVAILABLE = True
except ImportError:
    CLOUDINARY_AVAILABLE = False
    from django.db.models import ImageField

class User(AbstractUser):
    first_name = models.CharField(max_length=30)
    last_name = models.CharField(max_length=30)
    phone_number = models.CharField(max_length=15, unique=True, null=True, blank=True)
    location = models.CharField(max_length=255, help_text="Your specific area in Homabay, e.g., Ndhiwa, Rodi Kopany")
    date_of_birth = models.DateField(verbose_name='Date of Birth', null=True, blank=True)
    bio = models.TextField(max_length=500, blank=True)
    
    if CLOUDINARY_AVAILABLE and hasattr(settings, 'CLOUDINARY_CLOUD_NAME') and settings.CLOUDINARY_CLOUD_NAME:
        profile_picture = CloudinaryField(
            'image',
            folder='baysoko/profiles/',
            transformation=[{'width': 300, 'height': 300, 'crop': 'fill'}, {'quality': 'auto'}, {'format': 'webp'}],
            null=True, blank=True
        )
        cover_photo = CloudinaryField(
            'image',
            folder='baysoko/covers/',
            transformation=[{'width': 1200, 'height': 400, 'crop': 'fill'}, {'quality': 'auto'}, {'format': 'webp'}],
            null=True, blank=True
        )
    else:
        profile_picture = models.ImageField(upload_to='profile_pics/', null=True, blank=True)
        cover_photo = models.ImageField(upload_to='cover_photos/', null=True, blank=True)
    
    email_verified = models.BooleanField(default=False)
    email_verification_code = models.CharField(max_length=7, blank=True, null=True)
    email_verification_sent_at = models.DateTimeField(blank=True, null=True)
    email_change_count = models.PositiveSmallIntegerField(default=0)
    # Phone verification fields
    phone_verified = models.BooleanField(default=False)
    phone_verification_code = models.CharField(max_length=7, blank=True, null=True)
    phone_verification_sent_at = models.DateTimeField(blank=True, null=True)
    phone_change_count = models.PositiveSmallIntegerField(default=0)
    verification_attempts_today = models.IntegerField(default=0)
    last_verification_attempt_date = models.DateField(blank=True, null=True)

    password_reset_code = models.CharField(max_length=7, blank=True, null=True)
    password_reset_sent_at = models.DateTimeField(blank=True, null=True)
    password_reset_attempts = models.IntegerField(default=0)
    password_reset_last_attempt_date = models.DateField(blank=True, null=True)
    
    is_verified = models.BooleanField(default=False)   # seller verification
    show_contact_info = models.BooleanField(default=True, help_text="Show my contact information to other users")
    date_joined = models.DateTimeField(auto_now_add=True)

    def get_profile_picture_url(self):
        if self.profile_picture:
            try:
                try:
                    if hasattr(self.profile_picture, 'url') and self.profile_picture.url:
                        url = self.profile_picture.url
                        # make sure the url is https to avoid mixed content
                        if url.startswith('http://'):
                            url = 'https://' + url.split('://', 1)[1]
                        return url
                except Exception:
                    pass
                try:
                    from cloudinary.utils import cloudinary_url
                    public_id = None
                    if hasattr(self.profile_picture, 'public_id') and self.profile_picture.public_id:
                        public_id = self.profile_picture.public_id
                    elif hasattr(self.profile_picture, 'name') and self.profile_picture.name:
                        public_id = self.profile_picture.name
                    elif isinstance(self.profile_picture, str):
                        public_id = self.profile_picture
                    if public_id:
                        url, _ = cloudinary_url(public_id, secure=True)
                        return url
                except Exception:
                    pass
                if hasattr(self.profile_picture, 'name'):
                    from django.core.files.storage import default_storage
                    if default_storage.exists(self.profile_picture.name):
                        return default_storage.url(self.profile_picture.name)
            except Exception as e:
                print(f"Error getting profile picture URL: {e}")
                return '/static/images/default_profile_pic.svg'
        return '/static/images/default_profile_pic.svg'
    
    def get_cover_photo_url(self):
        if self.cover_photo:
            try:
                try:
                    if hasattr(self.cover_photo, 'url') and self.cover_photo.url:
                        url = self.cover_photo.url
                        if url.startswith('http://'):
                            url = 'https://' + url.split('://', 1)[1]
                        return url
                except Exception:
                    pass
                try:
                    from cloudinary.utils import cloudinary_url
                    public_id = None
                    if hasattr(self.cover_photo, 'public_id') and self.cover_photo.public_id:
                        public_id = self.cover_photo.public_id
                    elif hasattr(self.cover_photo, 'name') and self.cover_photo.name:
                        public_id = self.cover_photo.name
                    elif isinstance(self.cover_photo, str):
                        public_id = self.cover_photo
                    if public_id:
                        url, _ = cloudinary_url(public_id, secure=True)
                        return url
                except Exception:
                    pass
                if hasattr(self.cover_photo, 'name'):
                    from django.core.files.storage import default_storage
                    if default_storage.exists(self.cover_photo.name):
                        return default_storage.url(self.cover_photo.name)
            except Exception as e:
                print(f"Error getting cover photo URL: {e}")
                return '/static/images/default_cover_photo.jpg'
        return '/static/images/default_cover_photo.jpg'

    def save(self, *args, **kwargs):
        # Normalize phone_number: store empty values as NULL to avoid UNIQUE '' collisions
        try:
            if hasattr(self, 'phone_number'):
                if isinstance(self.phone_number, str):
                    pn = self.phone_number.strip()
                    if pn == '':
                        self.phone_number = None
                    else:
                        self.phone_number = pn
        except Exception:
            # non-fatal: ensure save proceeds
            pass
        # Allow up to two changes for verified email/phone (force re-verification)
        try:
            if self.pk:
                orig = User.objects.filter(pk=self.pk).only(
                    'email', 'phone_number', 'email_verified', 'phone_verified',
                    'email_change_count', 'phone_change_count'
                ).first()
                if orig:
                    if self.email != orig.email and orig.email_verified:
                        if orig.email_change_count >= 2:
                            self.email = orig.email
                        else:
                            self.email_verified = False
                            self.email_change_count = (orig.email_change_count or 0) + 1
                    if self.phone_number != orig.phone_number and orig.phone_verified:
                        if orig.phone_change_count >= 2:
                            self.phone_number = orig.phone_number
                        else:
                            self.phone_verified = False
                            self.phone_change_count = (orig.phone_change_count or 0) + 1
        except Exception:
            pass
        if not CLOUDINARY_AVAILABLE and self.profile_picture:
            os.makedirs(os.path.join(settings.MEDIA_ROOT, 'profile_pics'), exist_ok=True)
        if not self.first_name:
            self.first_name = self.username
        if not self.last_name:
            self.last_name = 'User'
        super().save(*args, **kwargs)

    def __str__(self):
        return self.username

    @property
    def full_name(self):
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        elif self.first_name:
            return self.first_name
        else:
            return self.username

    class Meta:
        constraints = [
            models.UniqueConstraint(
                Lower('email'),
                name='uniq_users_email_ci',
                condition=~models.Q(email__isnull=True) & ~models.Q(email='')
            )
        ]

class AccountDeletionLog(models.Model):
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    username = models.CharField(max_length=150)
    email = models.EmailField()
    reason = models.TextField(blank=True)
    deleted_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.username} - {self.deleted_at}"

# users/models.py (add at the end, before any existing classes)

class UserSettings(models.Model):
    user = models.OneToOneField(
        User, on_delete=models.CASCADE, related_name='settings'
    )
    # Notification preferences
    email_notifications = models.BooleanField(
        default=True,
        help_text="Receive email notifications for messages, orders, etc."
    )
    sms_notifications = models.BooleanField(
        default=True,
        help_text="Receive SMS notifications for important updates."
    )
    marketing_emails = models.BooleanField(
        default=False,
        help_text="Receive promotional emails and offers."
    )
    # Additional settings can be added here

    class Meta:
        verbose_name = "User Settings"
        verbose_name_plural = "User Settings"

    def __str__(self):
        return f"Settings for {self.user.username}"
