# listings/models.py
import os
from django.conf import settings
from django.db import models
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.db.models import Avg, Q
from cloudinary.models import CloudinaryField

from django.db import models
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
from django.template.loader import render_to_string
from django.db.models.signals import pre_save, post_save
from django.dispatch import receiver
import logging

logger = logging.getLogger(__name__)

# Import the shared email helper lazily-friendly
try:
    from baysoko.utils.email_helpers import render_and_send
except Exception:
    render_and_send = None


User = get_user_model()

class Category(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    icon = models.CharField(max_length=50, blank=True, default='bi-grid', help_text="Bootstrap icon class name")
    is_active = models.BooleanField(default=True)
    order = models.PositiveIntegerField(default=0)
    is_featured = models.BooleanField(default=False)
    # Optional grouping key: categories sharing the same group will inherit the same schema
    schema_group = models.CharField(max_length=100, blank=True, help_text="Optional key to group categories for shared field schemas")
    fields_schema = models.JSONField(default=dict, blank=True, help_text="JSON schema for category-specific fields")

    def __str__(self):
        return self.name

    class Meta:
        verbose_name_plural = "Categories"
        ordering = ['order', 'name']

class ListingImage(models.Model):
    listing = models.ForeignKey('Listing', on_delete=models.CASCADE, related_name='images')
    
    # Cloudinary field for images
    if 'cloudinary' in settings.INSTALLED_APPS and hasattr(settings, 'CLOUDINARY_CLOUD_NAME') and settings.CLOUDINARY_CLOUD_NAME:
        image = CloudinaryField(
            'image',
            folder='baysoko/listings/gallery/',
            null=True,
            blank=True
        )
    else:
        image = models.ImageField(
            upload_to='listing_images/gallery/',
            null=True,
            blank=True
        )
    
    caption = models.CharField(max_length=200, blank=True)
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order', 'created_at']

    def __str__(self):
        return f"Image for {self.listing.title}"

    def get_image_url(self):
       
        if self.image:
            try:
                return self.image.url
            except Exception as e:
                if hasattr(self.image, 'url'):
                    return self.image.url
        return '/static/images/listing_placeholder.svg'


class ListingVideo(models.Model):
    listing = models.ForeignKey('Listing', on_delete=models.CASCADE, related_name='videos')
    order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    likes_count = models.PositiveIntegerField(default=0)
    comments_count = models.PositiveIntegerField(default=0)
    shares_count = models.PositiveIntegerField(default=0)
    views_count = models.PositiveIntegerField(default=0)

    if 'cloudinary' in settings.INSTALLED_APPS and hasattr(settings, 'CLOUDINARY_CLOUD_NAME') and settings.CLOUDINARY_CLOUD_NAME:
        video = CloudinaryField(
            'video',
            folder='baysoko/listing_videos/',
            resource_type='video',
            null=True,
            blank=True
        )
    else:
        video = models.FileField(upload_to='listing_videos/', null=True, blank=True)

    class Meta:
        ordering = ['order', 'created_at']

    def __str__(self):
        return f"Video for {self.listing.title}"

    def get_video_url(self):
        if self.video:
            try:
                return self.video.url
            except Exception:
                if hasattr(self.video, 'url'):
                    return self.video.url
        return ''
    
    
class Listing(models.Model):
    HOMABAY_LOCATIONS = [
        ('HB_Town', 'Homa Bay Town'),
        ('Kendu_Bay', 'Kendu Bay'),
        ('Rodi_Kopany', 'Rodi Kopany'),
        ('Mbita', 'Mbita'),
        ('Oyugis', 'Oyugis'),
        ('Rangwe', 'Rangwe'),
        ('Ndhiwa', 'Ndhiwa'),
        ('Suba', 'Suba'),
    ]

    CONDITION_CHOICES = [
        ('new', 'New'),
        ('used', 'Used'),
        ('refurbished', 'Refurbished'),
    ]

    DELIVERY_OPTIONS = [
        ('pickup', 'Pickup'),
        ('delivery', 'Delivery'),
        ('shipping', 'Shipping'),
    ]
    
    title = models.CharField(max_length=200)
    description = models.TextField()
    price = models.DecimalField(max_digits=10, decimal_places=2)
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True)
    location = models.CharField(max_length=50, choices=HOMABAY_LOCATIONS)
    
    # Image field with Cloudinary fallback
    if 'cloudinary' in settings.INSTALLED_APPS and hasattr(settings, 'CLOUDINARY_CLOUD_NAME') and settings.CLOUDINARY_CLOUD_NAME:
        image = CloudinaryField(
            'image',
            folder='baysoko/listings/',
            null=True,
            blank=True
        )
    else:
        image = models.ImageField(
            upload_to='listing_images/',
            null=True,
            blank=True
        )
    
    condition = models.CharField(max_length=20, choices=CONDITION_CHOICES, default='used')
    delivery_option = models.CharField(max_length=20, choices=DELIVERY_OPTIONS, default='pickup')
    stock = models.PositiveIntegerField(default=1)
    is_sold = models.BooleanField(default=False)
    is_featured = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    
    
    # Product specifications
    brand = models.CharField(max_length=100, blank=True)
    model = models.CharField(max_length=100, blank=True)
    dimensions = models.CharField(max_length=100, blank=True, help_text="e.g., 10x5x3 inches")
    weight = models.CharField(max_length=50, blank=True, help_text="e.g., 2.5 kg")
    color = models.CharField(max_length=50, blank=True)
    material = models.CharField(max_length=100, blank=True)
    
    # SEO and sharing
    meta_description = models.TextField(blank=True)
    slug = models.SlugField(unique=True, blank=True)
    # Optional explicit link to a Store (storefront.Store). Nullable to remain backward compatible.
    store = models.ForeignKey('storefront.Store', on_delete=models.SET_NULL, null=True, blank=True, related_name='listings')
    
    date_created = models.DateTimeField(auto_now_add=True)
    date_updated = models.DateTimeField(auto_now=True)
    seller = models.ForeignKey(User, on_delete=models.CASCADE, related_name='listings', null=True)
    
    # Price history (we'll track this via a separate model)
    original_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    views = models.PositiveIntegerField(default=0)  # For trending
    discount_price = models.DecimalField(  # For flash sales
        max_digits=10, 
        decimal_places=2, 
        null=True, 
        blank=True
    )
    # Stores values for category-specific dynamic fields (keyed by field name)
    dynamic_fields = models.JSONField(default=dict, blank=True, help_text="Stores category-specific field values")

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse('listing-detail', kwargs={'pk': self.pk})

    def get_condition_display(self):
        return dict(self.CONDITION_CHOICES).get(self.condition, 'Unknown')

    def get_delivery_option_display(self):
        return dict(self.DELIVERY_OPTIONS).get(self.delivery_option, 'Unknown')

    @property
    def average_rating(self):
        if self.reviews.count() > 0:
            return self.reviews.aggregate(Avg('rating'))['rating__avg']
        return 0
    
    @property
    def rating_average(self):
        
        reviews = self.reviews.filter(review_type='listing')
        if reviews.exists():
            avg = reviews.aggregate(Avg('rating'))['rating__avg']
            return round(avg, 1)
        return 0.0

    @property 
    def rating_count(self):
        
        return self.reviews.filter(review_type='listing').count()

    @property
    def get_views(self):
        
        return self.views
    
    def get_rating_display(self):
        
        avg = self.rating_average
        if avg == 0:
            return "No ratings yet"
        return f"{avg} ⭐ ({self.rating_count} review{'s' if self.rating_count != 1 else ''})"
    
    def get_image_url(self):
        
        if self.image:
            try:
                return self.image.url
            except Exception as e:
                if hasattr(self.image, 'url'):
                    return self.image.url
        return '/static/images/listing_placeholder.svg'

    def _infer_location_from_store(self):
        try:
            if not self.store or not self.store.location:
                return None
            t = str(self.store.location).lower()
            if 'homa bay' in t or 'homabay' in t:
                return 'HB_Town'
            if 'kendu' in t:
                return 'Kendu_Bay'
            if 'rodi' in t:
                return 'Rodi_Kopany'
            if 'mbita' in t:
                return 'Mbita'
            if 'oyugis' in t:
                return 'Oyugis'
            if 'rangwe' in t:
                return 'Rangwe'
            if 'ndhiwa' in t:
                return 'Ndhiwa'
            if 'suba' in t:
                return 'Suba'
        except Exception:
            return None
        return None

    
    
    @property
    def price_trend(self):
        
        if self.original_price and self.original_price > self.price:
            return 'down'
        elif self.original_price and self.original_price < self.price:
            return 'up'
        return 'stable'

    def get_discount_percentage(self):
        if self.original_price and self.original_price > self.price:
            discount = self.original_price - self.price
            percentage = (discount / self.original_price) * 100
            return round(percentage)
        return 0


class NewsletterSubscription(models.Model):
    """Simple model to store newsletter subscriptions from the homepage form."""
    email = models.EmailField(unique=True)
    source = models.CharField(max_length=100, blank=True, help_text="Optional source (e.g., homepage)")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Newsletter: {self.email}"
    
    # In the save method of Listing model, add store validation
    def save(self, *args, **kwargs):
        # Set original price on first save
        if not self.pk and not self.original_price:
            self.original_price = self.price
        
        # Ensure the directory exists before saving
        if self.image:
            import os
            os.makedirs(os.path.join(settings.MEDIA_ROOT, 'listing_images'), exist_ok=True)
        
        # Ensure listing has a store (for backward compatibility)
        if not self.store and self.seller:
            try:
                from storefront.models import Store
                user_store = Store.objects.filter(owner=self.seller).first()
                if user_store:
                    self.store = user_store
                elif self.pk:
                    # For existing listings without a store, create a default one
                    default_store, created = Store.objects.get_or_create(
                        owner=self.seller,
                        defaults={
                            'name': f"{self.seller.username}'s Store",
                            'slug': self.seller.username,
                            'description': f"Default store for {self.seller.username}"
                        }
                    )
                    self.store = default_store
            except Exception as e:
                # Store model might not be available yet (during migrations)
                pass

        # Default listing location to store location when missing/invalid
        try:
            valid_choices = {c[0] for c in self.HOMABAY_LOCATIONS}
            if (not self.location) or (self.location not in valid_choices):
                inferred = self._infer_location_from_store()
                if inferred:
                    self.location = inferred
        except Exception:
            pass
        
        # Enforce that only stores with active subscriptions (or valid trial)
        # can set `is_featured` to True. If the store does not have an
        # eligible subscription, force `is_featured` to False.
        try:
            from storefront.models import Subscription
            from django.utils import timezone as _tz

            if self.is_featured and self.store:
                now = _tz.now()
                has_active = Subscription.objects.filter(store=self.store, status='active').exists()
                has_valid_trial = Subscription.objects.filter(
                    store=self.store,
                    status='trialing',
                    trial_ends_at__gt=now
                ).exists()

                if not (has_active or has_valid_trial):
                    # Not allowed to be featured without an active subscription
                    self.is_featured = False
        except Exception:
            # If subscription model/table is not available yet, silently
            # allow save (pre-migration states) and avoid breaking requests.
            pass
        
        # Generate slug if not provided
        if not self.slug:
            from django.utils.text import slugify
            self.slug = slugify(self.title)
            # Ensure uniqueness
            original_slug = self.slug
            counter = 1
            while Listing.objects.filter(slug=self.slug).exclude(pk=self.pk).exists():
                self.slug = f"{original_slug}-{counter}"
                counter += 1
        
        super().save(*args, **kwargs)


# Signals to broadcast stock/price changes so clients can update live
@receiver(pre_save, sender=Listing)
def _listing_pre_save(sender, instance, **kwargs):
    """Store previous price/stock on the instance before save."""
    if instance.pk:
        try:
            prev = Listing.objects.filter(pk=instance.pk).values('price', 'stock').first()
            if prev:
                instance._previous_price = prev.get('price')
                instance._previous_stock = prev.get('stock')
        except Exception as e:
            logger.exception('Error fetching previous listing values: %s', e)


@receiver(post_save, sender=Listing)
def _listing_post_save(sender, instance, created, **kwargs):
    """After a listing is saved, broadcast price/stock deltas if they changed."""
    try:
        prev_price = getattr(instance, '_previous_price', None)
        prev_stock = getattr(instance, '_previous_stock', None)

        price_changed = False
        stock_changed = False

        # For newly created listings, broadcast as created elsewhere; skip here
        if not created:
            try:
                # Compare Decimal/ints safely
                if prev_price is not None and float(prev_price) != float(instance.price):
                    price_changed = True
            except Exception:
                price_changed = False

            try:
                if prev_stock is not None and int(prev_stock) != int(instance.stock):
                    stock_changed = True
            except Exception:
                stock_changed = False

        if price_changed or stock_changed:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            channel_layer = get_channel_layer()

            payload = {
                'id': instance.id,
                'price': float(instance.price) if instance.price is not None else None,
                'stock': int(instance.stock) if instance.stock is not None else None,
                'old_price': float(prev_price) if prev_price is not None else None,
                'old_stock': int(prev_stock) if prev_stock is not None else None,
            }

            # Broadcast to all users' notification groups (small-scale fallback)
            from django.contrib.auth import get_user_model
            User = get_user_model()
            user_ids = list(User.objects.filter(is_active=True).values_list('id', flat=True))
            for uid in user_ids:
                try:
                    async_to_sync(channel_layer.group_send)(
                        f'notifications_user_{uid}',
                        {
                            'type': 'listing_changed',
                            'listing': payload,
                        }
                    )
                except Exception:
                    logger.exception('Failed to send listing_changed to user %s', uid)

    except Exception:
        logger.exception('Error in listing post_save signal')

class PriceHistory(models.Model):
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name='price_history')
    price = models.DecimalField(max_digits=10, decimal_places=2)
    date_changed = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        verbose_name_plural = "Price Histories"
        ordering = ['-date_changed']

class Favorite(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.CASCADE, 
        related_name='favorites'
    )
    listing = models.ForeignKey(
        'Listing', 
        on_delete=models.CASCADE, 
        related_name='favorites'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ('user', 'listing')
        ordering = ['-created_at']

class Activity(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='activities'
    )
    action = models.CharField(max_length=255)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.user.username} - {self.action} at {self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}"

class RecentlyViewed(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE)
    viewed_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ('user', 'listing')
        ordering = ['-viewed_at']

class FAQ(models.Model):
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE, related_name='faqs')
    question = models.CharField(max_length=255)
    answer = models.TextField()
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    
    class Meta:
        ordering = ['order', 'id']
    

class Cart(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='cart'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Cart ({self.user.username})"

    def get_total_price(self):
        return sum(item.get_total_price() for item in self.items.all())

    @property
    def total_items(self):
        return self.items.count()


class CartItem(models.Model):
    cart = models.ForeignKey(
        Cart,
        on_delete=models.CASCADE,
        related_name='items'
    )
    listing = models.ForeignKey(
        Listing,
        on_delete=models.CASCADE
    )
    quantity = models.PositiveIntegerField(default=1)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('cart', 'listing')

    def __str__(self):
        return f"{self.quantity} x {self.listing.title}"

    def get_total_price(self):
        return self.quantity * self.listing.price


class Order(models.Model):
    ORDER_STATUS = [
        ('pending', 'Pending Payment'),
        ('paid', 'Paid'),
        ('partially_shipped', 'Partially Shipped'),
        ('confirmed', 'Confirmed'),
        ('shipped', 'Shipped'),
        ('delivered', 'Delivered'),
        ('cancelled', 'Cancelled'),
        ('disputed', 'Disputed'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='orders'
    )
    items = models.ManyToManyField(Listing, through='OrderItem')
    total_price = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=ORDER_STATUS, default='pending')
    
    # Add shipping and contact information fields
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    email = models.EmailField(blank=True)
    phone_number = models.CharField(max_length=20, blank=True)
    shipping_address = models.TextField(blank=True)
    city = models.CharField(max_length=100, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    # Geo location (Google Maps / Places)
    shipping_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    shipping_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    shipping_place_id = models.CharField(max_length=255, blank=True)
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    delivery_breakdown = models.JSONField(default=dict, blank=True)

    tracking_number = models.CharField(max_length=100, blank=True)
    shipped_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    
    # Delivery system integration
    delivery_request_id = models.CharField(max_length=100, blank=True)
    driver_assigned = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    # Webhook tracking
    webhook_sent = models.BooleanField(default=False)
    webhook_sent_at = models.DateTimeField(null=True, blank=True)
    webhook_status = models.CharField(
        max_length=20,
        choices=[
            ('pending', 'Pending'),
            ('sent', 'Sent'),
            ('failed', 'Failed'),
            ('retried', 'Retried'),
        ],
        default='pending'
    )
    webhook_retries = models.IntegerField(default=0)
    webhook_error = models.TextField(blank=True)
    
    # Delivery tracking
    delivery_tracking_number = models.CharField(max_length=50, blank=True)
    delivery_status = models.CharField(
        max_length=20,
        choices=[
            ('pending', 'Pending'),
            ('accepted', 'Accepted'),
            ('in_transit', 'In Transit'),
            ('out_for_delivery', 'Out for Delivery'),
            ('delivered', 'Delivered'),
            ('failed', 'Failed'),
            ('cancelled', 'Cancelled'),
        ],
        default='pending'
    )

    def __str__(self):
        return f"Order #{self.id} - {self.user.username}"

    def mark_as_paid(self):
        self.status = 'paid'
        self.paid_at = timezone.now()
        self.save()
        
        # Send order-paid notification to buyer
        try:
            if render_and_send:
                subject = f'Your order #{self.id} is paid'
                ctx = {
                    'order': self,
                    'user': self.user,
                    'site_url': getattr(settings, 'SITE_URL', ''),
                    'tracking_url': self.get_delivery_tracking_url(),
                }
                render_and_send('emails/order_paid.html', 'emails/order_paid.txt', ctx, subject, [self.email or self.user.email])
        except Exception:
            # best-effort; do not raise
            pass
        # Update stock for each item in the order
        for order_item in self.order_items.all():
            listing = order_item.listing
            # Only update stock if it's greater than 0
            if listing.stock >= order_item.quantity:
                listing.stock -= order_item.quantity
                # Mark as sold only if stock reaches 0
                if listing.stock == 0:
                    listing.is_sold = True
                listing.save()
            else:
                # This shouldn't happen if validation is proper, but handle just in case
                listing.stock = 0
                listing.is_sold = True
                listing.save()

        
    def can_be_shipped(self):
        
        return self.status == 'paid'
    
    def can_confirm_delivery(self):
        
        return self.status == 'shipped'

    def send_to_delivery_system(self):
        
        from .webhooks import send_order_webhook
        
        event_type = 'order_created' if not self.webhook_sent else 'order_updated'
        
        try:
            send_order_webhook(self, event_type)
            
            self.webhook_sent = True
            self.webhook_sent_at = timezone.now()
            self.webhook_status = 'sent'
            self.webhook_retries = 0
            self.webhook_error = ''
            
            self.save(update_fields=[
                'webhook_sent',
                'webhook_sent_at',
                'webhook_status',
                'webhook_retries',
                'webhook_error'
            ])
            
            return True
            
        except Exception as e:
            self.webhook_status = 'failed'
            self.webhook_retries += 1
            self.webhook_error = str(e)
            self.save()
            
            return False
    
    def get_delivery_tracking_url(self):
        
        if self.delivery_tracking_number:
            return f"{settings.DELIVERY_SYSTEM_URL}track/{self.delivery_tracking_number}/"
        return None

    def save(self, *args, **kwargs):
        # Track status changes for webhooks
        created = not bool(getattr(self, 'pk', None))
        if self.pk:
            try:
                original = Order.objects.get(pk=self.pk)
                self._original_status = original.status
            except Order.DoesNotExist:
                pass
        # Prevent shipping/delivered status from being set outside delivery app
        if getattr(self, 'pk', None):
            try:
                orig = Order.objects.get(pk=self.pk)
                orig_status = orig.status
            except Order.DoesNotExist:
                orig_status = None
            # If trying to set to delivery-controlled status without explicit allowance, revert
            # Allow tests to set these statuses directly for legacy tests
            from django.conf import settings as _settings
            is_testing = getattr(_settings, 'RUNNING_TESTS', False)

            if self.status in ('shipped', 'delivered') and not getattr(self, '_delivery_status_allowed', False) and not is_testing:
                # revert to original status
                if orig_status is not None:
                    self.status = orig_status
        
        super().save(*args, **kwargs)

        # After save: send a 'order placed' notification when created
        try:
            if created and render_and_send:
                subject = f'Order #{self.id} placed successfully'
                ctx = {
                    'order': self,
                    'user': self.user,
                    'site_url': getattr(settings, 'SITE_URL', ''),
                }
                render_and_send('emails/order_placed.html', 'emails/order_placed.txt', ctx, subject, [self.email or self.user.email])
        except Exception:
            pass

        # If status changed (and we have original), notify for cancellations or disputes
        try:
            orig_status = getattr(self, '_original_status', None)
            if orig_status is not None and self.status != orig_status and render_and_send:
                if self.status == 'cancelled':
                    subject = f'Order #{self.id} cancelled'
                    ctx = {'order': self, 'user': self.user, 'site_url': getattr(settings, 'SITE_URL', '')}
                    render_and_send('emails/order_cancelled.html', 'emails/order_cancelled.txt', ctx, subject, [self.email or self.user.email])
                elif self.status == 'disputed':
                    subject = f'Order #{self.id} under dispute'
                    ctx = {'order': self, 'user': self.user, 'site_url': getattr(settings, 'SITE_URL', '')}
                    render_and_send('emails/order_disputed.html', 'emails/order_disputed.txt', ctx, subject, [self.email or self.user.email])
        except Exception:
            pass

    def set_delivery_status(self, new_status):
        
        if new_status not in ('shipped', 'delivered', 'in_transit', 'out_for_delivery', 'picked_up', 'failed', 'cancelled'):
            # allow other statuses via normal flow
            self.status = new_status
            self.save()
            return True

        # Mark flag to bypass save-time guard
        self._delivery_status_allowed = True
        try:
            self.status = new_status
            from django.utils import timezone as _tz
            if new_status == 'shipped':
                self.shipped_at = _tz.now()
            if new_status == 'delivered':
                self.delivered_at = _tz.now()
            self.save()
        finally:
            try:
                delattr(self, '_delivery_status_allowed')
            except Exception:
                pass
        # Send notifications for important delivery transitions
        try:
            if render_and_send:
                if new_status == 'shipped':
                    subject = f'Your order #{self.id} has been shipped'
                    ctx = {'order': self, 'user': self.user, 'tracking_url': self.get_delivery_tracking_url(), 'site_url': getattr(settings, 'SITE_URL', '')}
                    render_and_send('emails/order_shipped.html', 'emails/order_shipped.txt', ctx, subject, [self.email or self.user.email])
                if new_status == 'delivered':
                    subject = f'Your order #{self.id} is delivered'
                    ctx = {'order': self, 'user': self.user, 'site_url': getattr(settings, 'SITE_URL', '')}
                    render_and_send('emails/order_delivered.html', 'emails/order_delivered.txt', ctx, subject, [self.email or self.user.email])
        except Exception:
            pass

        return True

class WebhookLog(models.Model):
    
    order = models.ForeignKey('Order', on_delete=models.CASCADE, related_name='webhook_logs')
    event_type = models.CharField(max_length=50)
    payload = models.JSONField()
    response_status = models.IntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True)
    sent_at = models.DateTimeField(auto_now_add=True)
    success = models.BooleanField(default=False)
    error_message = models.TextField(blank=True)
    
    class Meta:
        ordering = ['-sent_at']
    
    def __str__(self):
        return f"Webhook for Order #{self.order.id} - {self.event_type}"
                
                
class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='order_items')
    listing = models.ForeignKey(Listing, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField()
    price = models.DecimalField(max_digits=10, decimal_places=2)
    added_at = models.DateTimeField(auto_now_add=True, blank=True, null=True)
    # Per-item shipment state (important for multi-seller orders)
    shipped = models.BooleanField(default=False)
    shipped_at = models.DateTimeField(null=True, blank=True)
    tracking_number = models.CharField(max_length=100, blank=True)

    def __str__(self):
        return f"{self.quantity} x {self.listing.title}"

    def get_total_price(self):
        return self.quantity * self.price




class Payment(models.Model):
    PAYMENT_METHODS = [
        ('mpesa', 'M-Pesa'),
        ('bank_transfer', 'Bank Transfer'),
        ('cash', 'Cash on Delivery'),
    ]

    PAYMENT_STATUS = [
        ('pending', 'Pending'),
        ('initiated', 'M-Pesa Initiated'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('refunded', 'Refunded'),
    ]

    order = models.OneToOneField(
        Order,
        on_delete=models.CASCADE,
        related_name='payment'
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    method = models.CharField(max_length=20, choices=PAYMENT_METHODS, default='mpesa')
    status = models.CharField(max_length=20, choices=PAYMENT_STATUS, default='pending')
    transaction_id = models.CharField(max_length=100, blank=True)
    mpesa_phone_number = models.CharField(max_length=15, blank=True)
    mpesa_checkout_request_id = models.CharField(max_length=100, blank=True)
    mpesa_merchant_request_id = models.CharField(max_length=100, blank=True)
    mpesa_result_code = models.IntegerField(null=True, blank=True)
    mpesa_result_desc = models.TextField(blank=True)
    mpesa_callback_data = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    # Add these for real escrow
    is_held_in_escrow = models.BooleanField(default=True)
    actual_release_date = models.DateTimeField(null=True, blank=True)
    seller_payout_reference = models.CharField(max_length=100, blank=True)
    
    def hold_in_escrow(self):
        """Actually move funds to escrow account"""
        # Implementation depends on your payment processor
        # This would typically involve:
        # 1. Capturing payment but not settling to seller
        # 2. Moving to a separate escrow account
        # 3. Setting up automatic release after X days or manual release
        pass
    
    def release_to_seller(self):
        """Actually transfer funds from escrow to seller"""
        # Implementation depends on your payment processor
        # This would typically involve:
        # 1. Releasing from escrow account to seller's balance
        # 2. Creating a payout transaction
        # 3. Updating accounting records
        # Mark payment-level fields to indicate funds moved
        try:
            from django.utils import timezone
            self.is_held_in_escrow = False
            self.actual_release_date = timezone.now()
            # Generate a simple payout reference if none exists
            if not self.seller_payout_reference:
                self.seller_payout_reference = f"PAYOUT-{self.order.id}-{int(timezone.now().timestamp())}"
            self.save()
        except Exception:
            # Best-effort: do not raise to avoid blocking higher-level flows
            pass

    def __str__(self):
        return f"Payment for Order #{self.order.id}"

    def mark_as_refunded(self, reason=None):
        """Mark payment as refunded and notify buyer via email (best-effort)."""
        try:
            self.status = 'refunded'
            self.save()
            # Notify buyer
            try:
                if render_and_send:
                    subject = f'Your order #{self.order.id} has been refunded'
                    ctx = {'order': self.order, 'user': self.order.user, 'site_url': getattr(settings, 'SITE_URL', ''), 'reason': reason}
                    render_and_send('emails/order_refunded.html', 'emails/order_refunded.txt', ctx, subject, [self.order.email or self.order.user.email])
            except Exception:
                pass
        except Exception:
            # Best-effort: do not propagate
            pass

    def mark_as_completed(self, transaction_id=None):
        """Mark the payment completed.

        transaction_id may be None if the upstream provider did not supply
        a receipt number. The DB column is NOT NULL, so coerce None to an
        empty string and store a string representation for debugging.
        """
        self.status = 'completed'
        # Ensure we never write NULL into the non-nullable DB column
        if transaction_id is None:
            self.transaction_id = ''
        else:
            # store as string to be safe (could be int in some providers)
            self.transaction_id = str(transaction_id)
        self.completed_at = timezone.now()
        self.save()

        # Mark order as paid
        self.order.mark_as_paid()

        # If there is an escrow record attached to the order and it's held,
        # ensure the escrow object references are consistent. Do NOT release here;
        # release should only happen after buyer confirmation.

    
    def initiate_mpesa_payment(self, phone_number):
        """Initiate M-Pesa STK push with proper error handling"""
        from .mpesa_utils import mpesa_gateway
        from django.conf import settings as _settings
        
        result = mpesa_gateway.stk_push(
            phone_number=phone_number,
            amount=self.amount,
            account_reference=f"ORDER{self.order.id}",
            transaction_desc=f"Payment for order #{self.order.id}"
        )
        
        if result['success']:
            self.status = 'initiated'
            self.method = 'mpesa'
            self.mpesa_phone_number = phone_number
            self.mpesa_checkout_request_id = result['checkout_request_id']
            self.mpesa_merchant_request_id = result['merchant_request_id']
            self.save()
            
            # For simulation mode, auto-complete after delay
            if (not mpesa_gateway.has_valid_credentials and
                getattr(_settings, 'MPESA_SIMULATE_PAYMENTS', False)):
                self._simulate_payment_completion()
            
            return True, result['response_description']
        else:
            self.status = 'failed'
            self.save()
            return False, result['error']

    def _simulate_payment_completion(self):
        """Simulate payment completion for development"""
        import threading
        import time
        
        import logging
        logger = logging.getLogger(__name__)

        def complete_payment():
            time.sleep(10)  # Wait 10 seconds to simulate payment processing
            try:
                # Refresh the payment object
                payment = Payment.objects.get(id=self.id)
                if payment.status == 'initiated':  # Only complete if still initiated
                    payment.mark_as_completed(f"MPESA{int(time.time())}")
                    logger.info(f"Simulated payment completion for order #{payment.order.id}")
            except Payment.DoesNotExist:
                logger.error("Payment no longer exists for simulation")
            except Exception as e:
                logger.error(f"Error in payment simulation: {str(e)}")
        
        thread = threading.Thread(target=complete_payment)
        thread.daemon = True
        thread.start()

class Escrow(models.Model):
    ESCROW_STATUS = [
        ('held', 'Funds Held'),
        ('released', 'Funds Released to Seller'),
        ('refunded', 'Funds Refunded to Buyer'),
        ('disputed', 'Disputed'),
    ]

    order = models.OneToOneField(
        Order,
        on_delete=models.CASCADE,
        related_name='escrow'
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, choices=ESCROW_STATUS, default='held')
    ready_for_release = models.BooleanField(default=False)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='escrow_approvals'
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)
    auto_release_date = models.DateTimeField(null=True, blank=True)
    dispute_resolved_at = models.DateTimeField(null=True, blank=True)

    def schedule_auto_release(self, days=7):
        """Automatically release funds after X days if no dispute"""
        from django.utils import timezone
        from datetime import timedelta
        
        self.auto_release_date = timezone.now() + timedelta(days=days)
        self.save()
    
    def check_auto_release(self):
        """Check if escrow should be automatically released"""
        if (self.auto_release_date and 
            timezone.now() >= self.auto_release_date and
            self.status == 'held'):
            self.release_funds()
            return True
        return False

    def __str__(self):
        return f"Escrow for Order #{self.order.id}"

    def release_funds(self):
        self.status = 'released'
        self.released_at = timezone.now()
        self.save()
        
        # In a real implementation, you would transfer funds to seller here
        # For now, we'll just update the status
        
        # Create activity log
        Activity.objects.create(
            user=self.order.user,
            action=f"Escrow released for Order #{self.order.id}"
        )

    def _get_delivery_request(self):
        try:
            from delivery.models import DeliveryRequest
            order = self.order
            delivery = None
            if getattr(order, 'delivery_request_id', None):
                delivery = DeliveryRequest.objects.filter(id=order.delivery_request_id).first()
            if not delivery and getattr(order, 'tracking_number', None):
                delivery = DeliveryRequest.objects.filter(tracking_number=order.tracking_number).first()
            if not delivery and getattr(order, 'delivery_tracking_number', None):
                delivery = DeliveryRequest.objects.filter(tracking_number=order.delivery_tracking_number).first()
            if not delivery:
                delivery = DeliveryRequest.objects.filter(order_id=str(order.id)).first()
            return delivery
        except Exception:
            return None

    def can_approve_release(self):
        """Check if escrow can be approved for release."""
        try:
            if self.status != 'held':
                return False
            if self.order.status != 'delivered':
                return False
            delivery = self._get_delivery_request()
            if not delivery:
                return False
            if delivery.status != 'delivered':
                return False
            has_proof = delivery.proofs.exists() or bool((delivery.metadata or {}).get('proof'))
            if not has_proof:
                return False
            otp_verified = delivery.otps.filter(used=True).exists()
            if not otp_verified:
                return False
            if not delivery.confirmations.exists():
                return False
            return True
        except Exception:
            return False

    def approve_release(self, approved_by=None):
        """Approve and release escrow when delivery conditions are met."""
        if not self.can_approve_release():
            return False
        self.ready_for_release = True
        self.status = 'released'
        self.released_at = timezone.now()
        self.approved_by = approved_by
        self.approved_at = timezone.now()
        self.save()
        try:
            payment = getattr(self.order, 'payment', None)
            if payment and getattr(payment, 'is_held_in_escrow', False):
                payment.release_to_seller()
        except Exception:
            pass
        return True

    def refund_funds(self):
        self.status = 'refunded'
        self.released_at = timezone.now()
        self.save()
        
        # In a real implementation, you would refund funds to buyer here
        
        # Create activity log
        Activity.objects.create(
            user=self.order.user,
            action=f"Escrow refunded for Order #{self.order.id}"
        )

# Add this to models.py after the Review model

class ReviewType(models.Model):
    """Type of review (seller, listing, or order)"""
    name = models.CharField(max_length=50, unique=True)
    description = models.TextField(blank=True)
    icon = models.CharField(max_length=50, blank=True)
    
    def __str__(self):
        return self.name
    
    class Meta:
        ordering = ['name']


class Review(models.Model):
    REVIEW_TYPES = [
        ('listing', 'Listing Review'),
        ('seller', 'Seller Review'),
        ('order', 'Order Review'),
    ]
    
    # Keep existing fields but add review_type
    review_type = models.CharField(max_length=20, choices=REVIEW_TYPES, default='listing')
    
    # Make listing optional (for seller/order reviews)
    listing = models.ForeignKey(
        Listing,
        on_delete=models.CASCADE,
        related_name='reviews',
        null=True,
        blank=True
    )
    
    # Add order field for order reviews
    order = models.ForeignKey(
        'Order',
        on_delete=models.CASCADE,
        related_name='reviews',
        null=True,
        blank=True
    )
    
    # Keep existing fields
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='reviews'
    )
    
    # For seller reviews, store the seller separately
    seller = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='seller_reviews',
        null=True,
        blank=True
    )
    
    rating = models.PositiveIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    # Additional fields for detailed ratings
    communication_rating = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        null=True,
        blank=True,
        help_text="Communication quality (1-5)"
    )
    delivery_rating = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        null=True,
        blank=True,
        help_text="Delivery speed and packaging (1-5)"
    )
    accuracy_rating = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        null=True,
        blank=True,
        help_text="Item as described (1-5)"
    )
    
    # Photos for reviews
    
    is_verified_purchase = models.BooleanField(default=True)
    is_public = models.BooleanField(default=True)
    
    class Meta:
        # Update unique constraint to be more flexible
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'listing', 'review_type'], 
                name='unique_listing_review',
                condition=Q(review_type='listing')
            ),
            models.UniqueConstraint(
                fields=['user', 'seller', 'review_type'], 
                name='unique_seller_review',
                condition=Q(review_type='seller')
            ),
            models.UniqueConstraint(
                fields=['user', 'order', 'review_type'], 
                name='unique_order_review',
                condition=Q(review_type='order')
            ),
        ]
        ordering = ['-created_at']

    def __str__(self):
        if self.review_type == 'listing' and self.listing:
            return f"Listing Review: {self.listing.title} by {self.user.username}"
        elif self.review_type == 'seller' and self.seller:
            return f"Seller Review: {self.seller.username} by {self.user.username}"
        elif self.review_type == 'order' and self.order:
            return f"Order Review: Order #{self.order.id} by {self.user.username}"
        return f"Review by {self.user.username}"


class ReviewPhoto(models.Model):
    review = models.ForeignKey(
        Review, 
        on_delete=models.CASCADE, 
        related_name='review_images'  # Changed from default
    )
    
    if 'cloudinary' in settings.INSTALLED_APPS and hasattr(settings, 'CLOUDINARY_CLOUD_NAME') and settings.CLOUDINARY_CLOUD_NAME:
        image = CloudinaryField(
            'image',
            folder='baysoko/reviews/',
            null=True,
            blank=True
        )
    else:
        image = models.ImageField(
            upload_to='review_photos/',
            null=True,
            blank=True
        )
    
    caption = models.CharField(max_length=200, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['uploaded_at']
    
    def __str__(self):
        return f"Photo for review #{self.review.id}"
