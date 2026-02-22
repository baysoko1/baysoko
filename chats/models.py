from django.db import models
from django.contrib.auth import get_user_model
from django.utils import timezone
from typing import TYPE_CHECKING
from django.conf import settings
from django.core.files.base import ContentFile
import mimetypes

try:
    from cloudinary.models import CloudinaryField
    _HAS_CLOUDINARY = True
except Exception:
    CloudinaryField = None
    _HAS_CLOUDINARY = False

User = get_user_model()

class Conversation(models.Model):
    participants = models.ManyToManyField(User, related_name='conversations')
    listing = models.ForeignKey('listings.Listing', on_delete=models.CASCADE, related_name='conversations', null=True, blank=True)
    start_date = models.DateTimeField(auto_now_add=True)
    is_archived = models.BooleanField(default=False)
    muted = models.BooleanField(default=False)
   
    created_at = models.DateTimeField(auto_now_add=True)
    archived_by = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='archived_conversations',
        blank=True
    )
    muted_by = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        related_name='muted_conversations',
        blank=True
    )
    updated_at = models.DateTimeField(auto_now=True)

    if TYPE_CHECKING:
        from django.db.models.manager import Manager
        messages: 'Manager'

    class Meta:
        ordering = ['-updated_at']
        indexes = [
            models.Index(fields=['-updated_at']),
        ]

    def __str__(self):
        participant_names = ", ".join([user.username for user in self.participants.all()])
        return f"Conversation between {participant_names}"

    def get_other_participant(self, current_user):
        return self.participants.exclude(id=current_user.id).first()

    def get_unread_count(self, user):
        return self.messages.filter(is_read=False).exclude(sender=user).count()


class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name='messages')
    sender = models.ForeignKey(User, on_delete=models.CASCADE)
    content = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)
    read_at = models.DateTimeField(null=True, blank=True)
    is_deleted = models.BooleanField(default=False)
    reply_to = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL)
    is_pinned = models.BooleanField(default=False)
    delivered =  models.BooleanField(default=False)
    
    class Meta:
        ordering = ['timestamp']
        indexes = [
            models.Index(fields=['conversation', 'timestamp']),
        ]
    
    def __str__(self):
        return f"Message from {self.sender.username} at {self.timestamp}"
    
    def mark_as_read(self):
        if not self.is_read:
            self.is_read = True
            self.read_at = timezone.now()
            self.save(update_fields=['is_read', 'read_at'])
    
    if TYPE_CHECKING:
        from django.db.models.manager import Manager
        attachments: 'Manager'


class MessageAttachment(models.Model):
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name='attachments')
    # Use Cloudinary when available; otherwise fall back to FileField (local storage)
    if _HAS_CLOUDINARY:
        file = CloudinaryField(
            resource_type='auto',
            folder='chat_attachments',
            blank=True,
            null=True,
        )
    else:
        file = models.FileField(upload_to='chat_attachments/', blank=True, null=True)

    filename = models.CharField(max_length=255, blank=True)
    content_type = models.CharField(max_length=100, blank=True)
    size = models.IntegerField(null=True, blank=True)

    @property
    def file_url(self):
        """Return the correct URL for the attachment."""
        if not self.file:
            return None

        if _HAS_CLOUDINARY:
            try:
                # Determine resource type from content_type
                if self.content_type.startswith('image/'):
                    resource_type = 'image'
                elif self.content_type.startswith('video/'):
                    resource_type = 'video'
                else:
                    resource_type = 'raw'

                public_id = self.file.public_id  # e.g. chat_attachments/25/filename
                url, _ = cloudinary_url(
                    public_id,
                    resource_type=resource_type,
                    secure=True,  # forces https://
                )
                return url
            except Exception:
                return str(self.file)
        else:
            return self.file.url

    def save(self, *args, **kwargs):
        # Ensure metadata fields are populated from the uploaded file
        try:
            f = self.file
            # CloudinaryField may store a str reference or a File-like object
            if not f:
                pass
            else:
                # Determine filename
                try:
                    name = getattr(f, 'name', None) or str(f)
                    self.filename = name
                except Exception:
                    pass

                # Size: some storages provide size attribute
                try:
                    size = getattr(f, 'size', None)
                    if size is None and hasattr(f, 'file') and hasattr(f.file, 'size'):
                        size = f.file.size
                    self.size = int(size) if size is not None else None
                except Exception:
                    self.size = None

                try:
                    ct = getattr(f, 'content_type', '') or getattr(f, 'mime_type', '')
                    if not ct and self.filename:
                        ct, _ = mimetypes.guess_type(self.filename)
                    self.content_type = ct or ''
                except Exception:
                    self.content_type = ''
        except Exception:
            # keep save best-effort; don't block on metadata extraction
            pass

        super().save(*args, **kwargs)


class UserOnlineStatus(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='online_status')
    is_online = models.BooleanField(default=False)
    last_seen = models.DateTimeField(default=timezone.now)
    last_active = models.DateTimeField(default=timezone.now)
    
    def __str__(self):
        return f"{self.user.username} - {'Online' if self.is_online else 'Offline'}"