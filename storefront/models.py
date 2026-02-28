from django.conf import settings
from django.db import models
from django.urls import reverse
from django.core.exceptions import ValidationError
from django.db.models import Sum, Avg, F
from django.utils import timezone
from datetime import timedelta, datetime

class Store(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='stores')
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    
    # Optional logo and cover image for storefronts
    if 'cloudinary' in __import__('django.conf').conf.settings.INSTALLED_APPS and hasattr(__import__('django.conf').conf.settings, 'CLOUDINARY_CLOUD_NAME') and __import__('django.conf').conf.settings.CLOUDINARY_CLOUD_NAME:
        from cloudinary.models import CloudinaryField
        logo = CloudinaryField('logo', folder='baysoko/stores/logos/', null=True, blank=True)
        cover_image = CloudinaryField('cover_image', folder='baysoko/stores/covers/', null=True, blank=True)
    else:
        logo = models.ImageField(upload_to='store_logos/', null=True, blank=True)
        cover_image = models.ImageField(upload_to='store_covers/', null=True, blank=True)
    
    description = models.TextField(blank=True)
    is_premium = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)
    location = models.CharField(max_length=255, blank=True)
    # Payout info for sellers
    payout_phone = models.CharField(max_length=15, blank=True, null=True, help_text="Seller M-Pesa phone for payouts")
    payout_verified = models.BooleanField(default=False)
    payout_verified_at = models.DateTimeField(null=True, blank=True)
    policies = models.TextField(blank=True, help_text="Store policies, return policy, etc.")
    is_featured = models.BooleanField(default=False, help_text="Featured stores get premium placement")
    total_views = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    def get_logo_url(self):
        """Return the logo URL or None; templates can fall back to placeholder."""
        try:
            if self.logo and hasattr(self.logo, 'url'):
                return self.logo.url
        except Exception:
            pass
        return None

    def get_cover_image_url(self):
        try:
            if self.cover_image and hasattr(self.cover_image, 'url'):
                return self.cover_image.url
        except Exception:
            pass
        return None

    def get_absolute_url(self):
        return reverse('storefront:store_detail', kwargs={'slug': self.slug})
    
    def get_sales_count(self):
        """Return total sales count for all listings in this store."""
        from listings.models import OrderItem
        
        # Get the sum of quantities from all order items for this store's listings
        total_quantity = OrderItem.objects.filter(
            listing__store=self
        ).aggregate(
            total_quantity=Sum('quantity')
        )['total_quantity']
        
        return total_quantity or 0

    def can_be_featured(self):
        """Check if store can be featured based on subscription status"""
        from .models import Subscription
        
        # Check for active subscription
        has_active = Subscription.objects.filter(
            store=self, 
            status='active'
        ).exists()
        
        # Check for valid trial
        has_valid_trial = Subscription.objects.filter(
            store=self,
            status='trialing',
            trial_ends_at__gt=timezone.now()
        ).exists()
        
        return has_active or has_valid_trial

    def get_effective_subscription(self, owner=None, create_if_missing=False):
        """Get effective subscription for this store, with free plan fallback"""
        # Try to get subscription for this store
        subscription = self.subscriptions.order_by('-created_at').first()
        
        # If no subscription exists and create_if_missing is True, create a free subscription
        if not subscription and create_if_missing:
            from django.utils import timezone
            subscription = Subscription.objects.create(
                store=self,
                plan='free',
                status='active',
                amount=0,
                started_at=timezone.now(),
                metadata={'auto_created': True, 'plan_type': 'free'}
            )
        
        return subscription
    
    def get_rating(self):
        """
        Return combined average rating for:
        1. Product reviews for all listings in this store
        2. Direct store reviews (if StoreReview model exists)
        """
        from listings.models import Review
        from django.db.models import Avg, Q
        
        all_ratings = []
        
        # Get product reviews for all listings in this store
        product_reviews = Review.objects.filter(listing__store=self)
        if product_reviews.exists():
            product_avg = product_reviews.aggregate(avg_rating=Avg('rating'))['avg_rating']
            if product_avg:
                all_ratings.append(product_avg)
        
        # Get direct store reviews if StoreReview model exists
        try:
            # Check if StoreReview model exists in current app
            from .models import StoreReview
            store_reviews = StoreReview.objects.filter(store=self)
            if store_reviews.exists():
                store_avg = store_reviews.aggregate(avg_rating=Avg('rating'))['avg_rating']
                if store_avg:
                    all_ratings.append(store_avg)
        except (ImportError, AttributeError):
            # StoreReview model not defined yet, skip
            pass
        
        # Calculate weighted average if we have both types of reviews
        if not all_ratings:
            return 0
        
        # Simple average of all ratings
        combined_avg = sum(all_ratings) / len(all_ratings)
        return round(combined_avg, 1)

    def get_review_count(self):
        """Get total number of reviews (product reviews + store reviews)."""
        from listings.models import Review
        total = 0
        
        # Count product reviews
        total += Review.objects.filter(listing__store=self).count()
        
        # Count direct store reviews if StoreReview model exists
        try:
            from .models import StoreReview
            total += StoreReview.objects.filter(store=self).count()
        except (ImportError, AttributeError):
            # StoreReview model not defined yet, skip
            pass
        
        return total
    

    def has_user_reviewed(self, user):
        """Check if user has reviewed this store (either via products or directly)."""
        if not user.is_authenticated:
            return False
        
        from listings.models import Review
        
        # Check if user has reviewed any product in this store
        # FIXED: Changed 'reviewer' to 'user' to match the Review model field
        has_product_review = Review.objects.filter(
            listing__store=self,
            user=user  # Changed from reviewer=user to user=user
        ).exists()
        
        if has_product_review:
            return True
        
        # Check if user has directly reviewed the store
        try:
            from .models import StoreReview
            return StoreReview.objects.filter(store=self, reviewer=user).exists()
        except (ImportError, AttributeError):
            # StoreReview model not defined yet
            return False


    def get_all_reviews(self):
        """Get all reviews for this store (both product and direct store reviews)"""
        from listings.models import Review
        
        all_reviews = []
        
        # Get product reviews for this store's listings
        product_reviews = Review.objects.filter(listing__store=self).select_related(
            'user', 'listing'
        ).order_by('-created_at')
        
        # Get direct store reviews
        try:
            store_reviews = self.reviews.all().select_related('reviewer').order_by('-created_at')
        except (ImportError, AttributeError):
            store_reviews = []
        
        # Combine and sort by date
        for review in product_reviews:
            all_reviews.append({
                'type': 'product',
                'id': review.id,
                'reviewer': review.user,
                'rating': review.rating,
                'comment': review.comment,
                'created_at': review.created_at,
                'listing': review.listing,
                'helpful_count': 0,  # Product reviews don't have helpful count
            })
        
        for review in store_reviews:
            all_reviews.append({
                'type': 'store',
                'id': review.id,
                'reviewer': review.reviewer,
                'rating': review.rating,
                'comment': review.comment,
                'created_at': review.created_at,
                'listing': None,
                'helpful_count': review.helpful_count,
            })
        
        # Sort by created_at, newest first
        all_reviews.sort(key=lambda x: x['created_at'], reverse=True)
        
        return all_reviews

    def get_all_reviews_paginated(self, page=1, per_page=10):
        """Get paginated reviews"""
        all_reviews = self.get_all_reviews()
        
        # Simple pagination for list
        from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
        
        paginator = Paginator(all_reviews, per_page)
        
        try:
            reviews_page = paginator.page(page)
        except PageNotAnInteger:
            reviews_page = paginator.page(1)
        except EmptyPage:
            reviews_page = paginator.page(paginator.num_pages)
        
        return reviews_page
    
    def get_average_store_rating(self):
        """Get average rating from direct store reviews only."""
        try:
            from .models import StoreReview
            store_reviews = StoreReview.objects.filter(store=self)
            if store_reviews.exists():
                return store_reviews.aggregate(avg_rating=Avg('rating'))['avg_rating'] or 0
        except (ImportError, AttributeError):
            pass
        return 0
    
    def get_product_reviews(self):
        """Get product reviews for this store's listings."""
        from listings.models import Review
        return Review.objects.filter(listing__store=self).select_related('user', 'listing').order_by('-created_at')
    
    def increment_views(self, request=None):
        """Increment total views and track unique visitors"""
        # Use update() with F() expression for atomic increment
        Store.objects.filter(pk=self.pk).update(total_views=F('total_views') + 1)
        
        # Track unique view if request is provided
        if request:
            self._track_unique_view(request)
        
        # Refresh to get updated count
        self.refresh_from_db()
        return self.total_views
    # Add this method if you want to track unique views (requires more setup)
    def track_view(self, request):
        """Track store view with session to avoid duplicate counts from same user"""
        session_key = f'store_view_{self.id}'
        
        if not request.session.get(session_key):
            # Mark this session as having viewed the store
            request.session[session_key] = True
            request.session.modified = True
            
            # Increment the view count
            self.total_views = F('total_views') + 1
            self.save(update_fields=['total_views'])
            self.refresh_from_db()
        
    def clean(self):
        """
        Enforce that a user may only create more than one Store if they have a premium subscription
        (i.e., at least one existing Store with is_premium=True or an active Subscription).
        This prevents users from bypassing listing limits by creating additional free stores.
        """
        # Only validate on create (no PK yet) or when owner is changing
        if not self.pk:
            # If the owner is not yet set (e.g., ModelForm validation before view assigns owner), skip here.
            # The view will assign owner on save, and save() calls full_clean() again so validation will run then.
            owner = getattr(self, 'owner', None)
            if owner is None:
                return

            # Count existing stores for owner
            existing = Store.objects.filter(owner=owner)
            if existing.exists():
                # If user already has stores, require that they have at least one premium store
                has_premium_store = existing.filter(is_premium=True).exists()
                # Also allow if there's an active subscription tied to any existing store
                has_active_subscription = Subscription.objects.filter(
                    store__owner=owner, 
                    status='active'
                ).exists()
                has_valid_trial = Subscription.objects.filter(
                    store__owner=owner,
                    status='trialing',
                    trial_ends_at__gt=timezone.now()
                ).exists()
                if not (has_premium_store or has_active_subscription or has_valid_trial):
                    raise ValidationError("You must upgrade to Pro (subscribe) to create additional storefronts.")
        
        # Additional validation for featured stores - REMOVED: is_featured is now set automatically
        # if self.is_featured:
        #     # Check if store can be featured
        #     owner = getattr(self, 'owner', None)
        #     if owner:
        #         has_active = Subscription.objects.filter(
        #             store=self, 
        #             status='active'
        #         ).exists()
        #         has_valid_trial = Subscription.objects.filter(
        #             store=self,
        #             status='trialing',
        #             trial_ends_at__gt=timezone.now()
        #         ).exists()
        #         
        #         if not (has_active or has_valid_trial):
        #             raise ValidationError("Store must have an active subscription or valid trial to be featured.")
    
    def save(self, *args, **kwargs):
        # Run clean validation before saving
        # Prevent changing payout phone after verification
        try:
            if self.pk:
                orig = Store.objects.filter(pk=self.pk).first()
                if orig and orig.payout_verified and orig.payout_phone and self.payout_phone and orig.payout_phone != self.payout_phone:
                    raise ValidationError('Payout phone is locked after verification')
        except Exception:
            pass

        self.full_clean()
        super().save(*args, **kwargs)
    


class StoreReview(models.Model):
    """Review model for stores"""
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='reviews')
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='store_reviews')
    rating = models.PositiveIntegerField(
        choices=[(1, '1'), (2, '2'), (3, '3'), (4, '4'), (5, '5')],
        default=5
    )
    comment = models.TextField(max_length=1000)
    helpful_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ['store', 'reviewer']
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.reviewer.username} - {self.store.name} - {self.rating}★"
    
    def mark_helpful(self, user):
        """Mark review as helpful by a user"""
        if not ReviewHelpful.objects.filter(review=self, user=user).exists():
            ReviewHelpful.objects.create(review=self, user=user)
            self.helpful_count += 1
            self.save()
            return True
        return False


class ReviewHelpful(models.Model):
    """Track which reviews users found helpful"""
    review = models.ForeignKey(StoreReview, on_delete=models.CASCADE, related_name='helpful_votes')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ['review', 'user']


class Subscription(models.Model):
    """Store subscription model - Enhanced"""
    SUBSCRIPTION_STATUS = (
        ('trialing', 'Trialing'),
        ('active', 'Active'),
        ('past_due', 'Past Due'),
        ('canceled', 'Canceled'),
        ('unpaid', 'Unpaid'),
    )
    
    PLAN_CHOICES = (
        ('free', 'Free - KSh 0/month'),
        ('basic', 'Basic - KSh 999/month'),
        ('premium', 'Premium - KSh 1,999/month'),
        ('enterprise', 'Enterprise - KSh 4,999/month'),
    )
    
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='subscriptions')
    plan = models.CharField(max_length=20, choices=PLAN_CHOICES, default='free')
    status = models.CharField(max_length=20, choices=SUBSCRIPTION_STATUS, default='trialing')
    
    # Billing details
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=999.00)
    currency = models.CharField(max_length=3, default='KES')
    
    # Dates
    started_at = models.DateTimeField(auto_now_add=True)
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    
    # Payment method
    mpesa_phone = models.CharField(max_length=15, null=True, blank=True)
    
    # Metadata
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    # Trial tracking
    trial_number = models.PositiveIntegerField(default=0, help_text="Which trial number this is for the user")
    trial_started_at = models.DateTimeField(null=True, blank=True)
    trial_ended_at = models.DateTimeField(null=True, blank=True)
    
    # Additional metadata for trial tracking
    metadata = models.JSONField(default=dict, blank=True)
    
    
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.store.name} - {self.get_plan_display()} ({self.status})"
    
    def is_active(self):
        """Check if subscription is currently active"""
        now = timezone.now()
        if self.status in ['active', 'trialing']:
            if self.trial_ends_at and now > self.trial_ends_at:
                return self.status == 'active'
            return True
        return False
    
    @property
    def is_first_trial(self):
        """Check if this is the user's first trial"""
        return self.trial_number == 1
    
    @property
    def has_exceeded_trial_limit(self):
        """Check if user has exceeded trial limit"""
        user_trial_count = Subscription.objects.filter(
            store__owner=self.store.owner,
            trial_ends_at__isnull=False
        ).count()
        return user_trial_count > settings.TRIAL_LIMIT_PER_USER
    
    def save(self, *args, **kwargs):
        # Auto-set trial number if this is a trial
        if self.status == 'trialing' and not self.trial_number:
            # Count user's previous trials
            previous_trials = Subscription.objects.filter(
                store__owner=self.store.owner,
                trial_ends_at__isnull=False
            ).count()
            self.trial_number = previous_trials + 1
        
        # Record trial start/end times
        if self.status == 'trialing' and not self.trial_started_at:
            self.trial_started_at = timezone.now()
        elif self.status != 'trialing' and self.trial_ended_at is None:
            self.trial_ended_at = timezone.now()
        
        super().save(*args, **kwargs)
        
        # Update featured status for store and listings
        self._update_featured_status()
    
    def _update_featured_status(self):
        """Update featured status for store and its listings based on subscription"""
        from listings.models import Listing
        from django.db.models import Q
        
        # Check if store has any active premium or enterprise subscription
        now = timezone.now()
        has_premium = Subscription.objects.filter(
            store=self.store,
            plan__in=['premium', 'enterprise']
        ).filter(
            Q(status='active') | Q(status='trialing', trial_ends_at__gt=now)
        ).exists()
        
        # Update store
        if self.store.is_featured != has_premium:
            self.store.is_featured = has_premium
            self.store.save(update_fields=['is_featured'])
        
        # Update all listings for this store
        Listing.objects.filter(store=self.store).update(is_featured=has_premium)

    @property
    def expires_at(self):
        """Property to get expiration date for admin display"""
        if self.status == 'trialing' and self.trial_ends_at:
            return self.trial_ends_at
        elif self.current_period_end:
            return self.current_period_end
        elif self.trial_ends_at:
            return self.trial_ends_at
        return None
    
    def cancel(self):
        """Cancel subscription"""
        self.set_status('canceled')
    
    def renew(self, payment=None):
        """Renew subscription after payment"""
        # Use centralized status setter to ensure store sync
        if payment:
            try:
                from .utils.phone import normalize_phone
                self.mpesa_phone = normalize_phone(payment.phone_number)
            except Exception:
                self.mpesa_phone = payment.phone_number

        self.current_period_end = timezone.now() + timezone.timedelta(days=30)
        self.set_status('active')
    def check_trial_expiry(self):
        """Check and handle trial expiration"""
        
        if self.status == 'trialing' and self.trial_ends_at:
            if timezone.now() > self.trial_ends_at:
                # Trial expired -> cancel via centralized setter
                self.set_status('canceled')
                # Ensure featured cleared
                if self.store.is_featured:
                    self.store.is_featured = False
                    self.store.save(update_fields=['is_featured'])
                return True
        return False

    def set_status(self, new_status):
        """Centralized status transition that keeps store flags consistent."""
        old_status = self.status
        self.status = new_status

        now = timezone.now()
        # Cancellation metadata handling
        if new_status == 'canceled':
            self.canceled_at = now
            try:
                self.cancel_at_period_end = False
            except Exception:
                pass
        elif new_status == 'active':
            self.canceled_at = None

        # Persist the status change first
        super(Subscription, self).save()

        # Update store-level premium flags
        from django.db.models import Q
        now = timezone.now()
        has_active = Subscription.objects.filter(
            store=self.store
        ).filter(
            Q(status='active') | Q(status='trialing', trial_ends_at__gt=now)
        ).exists()
        if self.store.is_premium != has_active:
            self.store.is_premium = has_active
            self.store.save(update_fields=['is_premium'])

        # Owner-level downgrade if no active subscriptions
        try:
            owner = self.store.owner
            owner_has_active = Subscription.objects.filter(
                store__owner=owner
            ).filter(
                Q(status='active') | Q(status='trialing', trial_ends_at__gt=now)
            ).exists()
            if not owner_has_active:
                self.store.__class__.objects.filter(owner=owner).update(is_premium=False)
        except Exception:
            logging.getLogger(__name__).exception('Failed to sync owner-level subscription flags')

        # Notify owner about status changes (best-effort)
        if old_status != new_status:
            try:
                from django.template.loader import render_to_string
                from notifications.utils import create_notification, NotificationService
            except Exception:
                create_notification = None
                NotificationService = None

            try:
                from baysoko.utils.email_helpers import send_email_brevo
            except Exception:
                send_email_brevo = None

            owner = getattr(self.store, 'owner', None)
            recipients = [getattr(owner, 'email', None)] if owner and getattr(owner, 'email', None) else []

            def _send_in_app(title, message, action_url=None, action_text='View Subscription'):
                if create_notification and owner:
                    try:
                        create_notification(
                            recipient=owner,
                            notification_type='system',
                            title=title,
                            message=message,
                            related_object_id=self.id,
                            related_content_type='subscription',
                            action_url=action_url or f'/dashboard/store/{self.store.slug}/subscription/',
                            action_text=action_text,
                        )
                    except Exception:
                        logging.getLogger(__name__).exception('Failed to create in-app notification: %s', title)

            def _send_email(subject, txt_template, html_template):
                if not send_email_brevo or not recipients:
                    return
                context = {'store': self.store, 'subscription': self, 'user': owner}
                try:
                    html_message = render_to_string(html_template, context)
                except Exception:
                    html_message = ''
                try:
                    text_message = render_to_string(txt_template, context)
                except Exception:
                    text_message = ''
                try:
                    send_email_brevo(subject, text_message, html_message, recipients)
                except Exception:
                    logging.getLogger(__name__).exception('Failed to send email: %s', subject)

            # State-specific notifications
            if new_status == 'canceled':
                _send_in_app('Subscription Canceled', f'Your subscription for {self.store.name} has been canceled. Premium features have been disabled.')
                _send_email(f'Subscription Canceled - {self.store.name}', 'storefront/emails/subscription_canceled.txt', 'storefront/emails/subscription_canceled.html')

            if new_status == 'active':
                _send_in_app('Subscription Activated', f'Your subscription for {self.store.name} is now active. Enjoy premium features!')
                _send_email(f'Subscription Activated - {self.store.name}', 'storefront/emails/subscription_activated.txt', 'storefront/emails/subscription_activated.html')

            if new_status == 'trialing' and old_status != 'trialing':
                _send_in_app('Trial Started', f'Your trial for {self.store.name} has started. You have limited-time access to premium features.')
                _send_email(f'Your Trial Started - {self.store.name}', 'storefront/emails/subscription_trial_started.txt', 'storefront/emails/subscription_trial_started.html')

            if new_status == 'past_due':
                _send_in_app('Subscription Past Due', f'Your subscription for {self.store.name} is past due. Please update your payment method to avoid service interruption.')
                _send_email(f'Subscription Past Due - {self.store.name}', 'storefront/emails/subscription_past_due.txt', 'storefront/emails/subscription_past_due.html')

            if new_status == 'unpaid':
                _send_in_app('Subscription Unpaid', f'Your subscription for {self.store.name} is unpaid. Please settle outstanding invoices to restore access.')
                _send_email(f'Subscription Unpaid - {self.store.name}', 'storefront/emails/subscription_unpaid.txt', 'storefront/emails/subscription_unpaid.html')

            # Attempt SMS for critical states
            try:
                if NotificationService and owner:
                    phone = getattr(owner, 'phone_number', None) or getattr(self.store, 'phone', None)
                    if phone and new_status in ('past_due', 'unpaid', 'canceled'):
                        try:
                            NotificationService().send_sms(phone, f'Notice: your subscription for {self.store.name} is {new_status}.')
                        except Exception:
                            logging.getLogger(__name__).exception('Failed to send SMS for subscription status change')
            except Exception:
                logging.getLogger(__name__).exception('NotificationService not available')
    @classmethod
    def get_store_subscription(cls, store):
        """Get active subscription for store"""
        subscription = cls.objects.filter(
            store=store
        ).order_by('-created_at').first()
        
        if subscription:
            # Check trial expiry
            subscription.check_trial_expiry()
        
        return subscription
    
    def get_remaining_trial_days(self):
        """Get remaining trial days"""
        from datetime import timedelta
        
        if self.status == 'trialing' and self.trial_ends_at:
            remaining = self.trial_ends_at - timezone.now()
            if remaining.days >= 0:
                return remaining.days
        return 0
    
    def can_access_feature(self, feature_name):
        """Check if subscription can access specific feature"""
        if not self.is_active():
            return False
        
        # Feature matrix by plan
        features = {
            'basic': [
                'featured_placement',
                'basic_analytics',
                'store_customization',
                'up_to_5_stores',
                'up_to_50_products',
            ],
            'premium': [
                'featured_placement',
                'advanced_analytics',
                'bulk_operations',
                'inventory_management',
                'product_bundles',
                'multiple_stores',
                'up_to_200_products',
            ],
            'enterprise': [
                'featured_placement',
                'advanced_analytics',
                'bulk_operations',
                'inventory_management',
                'product_bundles',
                'multiple_stores',
                'unlimited_products',
                'api_access',
                'custom_domain',
                'priority_support',
            ]
        }
        
        plan_features = features.get(self.plan, [])
        return feature_name in plan_features

class MpesaPayment(models.Model):
    """M-Pesa payment records"""
    PAYMENT_STATUS = (
        ('pending', 'Pending'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    )
    
    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name='payments')
    
    # M-Pesa details
    checkout_request_id = models.CharField(max_length=100, unique=True)
    merchant_request_id = models.CharField(max_length=100)
    phone_number = models.CharField(max_length=15)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    
    # Transaction details
    mpesa_receipt_number = models.CharField(max_length=50, null=True, blank=True)
    transaction_date = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=PAYMENT_STATUS, default='pending')
    
    # Metadata
    result_code = models.CharField(max_length=10, null=True, blank=True)
    result_description = models.TextField(null=True, blank=True)
    raw_response = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"MPesa Payment - {self.phone_number} - KSh {self.amount} - {self.status}"
    
    def is_successful(self):
        """Check if payment was successful"""
        return self.status == 'completed'


class PayoutVerification(models.Model):
    """Track one-time payout phone verifications initiated via STK push."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='payout_verifications')
    phone = models.CharField(max_length=15)
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    checkout_request_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"PayoutVerification(store={self.store.slug}, phone={self.phone}, verified={self.verified})"



class WithdrawalRequest(models.Model):
    STATUS = [
        ('pending', 'Pending'),
        ('scheduled', 'Scheduled for Processing'),
        ('processed', 'Processed'),
        ('failed', 'Failed'),
    ]

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='withdrawal_requests')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    requested_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS, default='pending')
    scheduled_for = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    reference = models.CharField(max_length=100, blank=True)
    note = models.TextField(blank=True)

    MIN_WITHDRAWAL = 10000

    def schedule(self):
        """Schedule the withdrawal for the next Thursday if valid.

        Returns True if scheduled, False otherwise.
        """
        from django.utils import timezone
        from datetime import timedelta

        # Enforce minimum
        if self.amount < self.MIN_WITHDRAWAL:
            return False

        # Find next Thursday
        today = timezone.now().date()
        # Python: Monday=0 ... Sunday=6; Thursday=3
        days_ahead = (3 - today.weekday()) % 7
        if days_ahead == 0:
            # If today is Thursday, schedule for today
            target = today
        else:
            target = today + timedelta(days=days_ahead)

        # schedule at start of day
        self.scheduled_for = timezone.make_aware(datetime.combine(target, datetime.min.time()))
        self.status = 'scheduled'
        self.save()
        return True

    def process(self):
        """Process the withdrawal. This should be called by a scheduled task on Thursdays."""
        from django.utils import timezone
        try:
            # Call payout wrapper to perform actual disbursement
            from .payout import payout_to_phone

            if not self.store.payout_phone or not self.store.payout_verified:
                self.status = 'failed'
                self.save()
                return False

            success, provider_ref = payout_to_phone(self.store.payout_phone, self.amount, reference=self.reference)
            if success:
                self.status = 'processed'
                self.processed_at = timezone.now()
                if not self.reference:
                    self.reference = provider_ref or f"WITHDRAW-{self.store.id}-{int(timezone.now().timestamp())}"
                else:
                    # Record provider ref if available
                    self.reference = provider_ref or self.reference
                self.save()
                return True
            else:
                self.status = 'failed'
                self.note = str(provider_ref)
                self.save()
                return False
        except Exception:
            self.status = 'failed'
            self.save()
            return False


# Add to existing models.py
import json
from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator

class InventoryAlert(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='inventory_alerts')
    product = models.ForeignKey('listings.Listing', on_delete=models.CASCADE)
    threshold = models.IntegerField(default=5, validators=[MinValueValidator(1)])
    alert_type = models.CharField(max_length=20, choices=[
        ('low_stock', 'Low Stock'),
        ('out_of_stock', 'Out of Stock'),
        ('expiring', 'Expiring Soon')
    ], default='low_stock')
    is_active = models.BooleanField(default=True)
    last_triggered = models.DateTimeField(null=True, blank=True)
    notification_method = models.JSONField(default=list)  # ['email', 'sms', 'dashboard']
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ['store', 'product', 'alert_type']
    
    def __str__(self):
        return f"{self.product.title} - {self.get_alert_type_display()}"
    
    def check_condition(self):
        """Check if alert condition is met"""
        current_stock = self.product.stock
        if self.alert_type == 'low_stock':
            return current_stock <= self.threshold and current_stock > 0
        elif self.alert_type == 'out_of_stock':
            return current_stock == 0
        return False

class ProductVariant(models.Model):
    listing = models.ForeignKey('listings.Listing', on_delete=models.CASCADE, related_name='variants')
    name = models.CharField(max_length=100)  # e.g., "Color", "Size"
    value = models.CharField(max_length=100)  # e.g., "Red", "Large"
    sku = models.CharField(max_length=100, unique=True, blank=True)
    price_adjustment = models.DecimalField(
        max_digits=10, 
        decimal_places=2, 
        default=0,
        help_text="Positive for increase, negative for decrease"
    )
    stock = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    weight = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)  # in grams
    dimensions = models.CharField(max_length=100, blank=True)  # "LxWxH in cm"
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ['listing', 'name', 'value']
        ordering = ['name', 'value']
    
    def __str__(self):
        return f"{self.listing.title} - {self.name}: {self.value}"
    
    @property
    def final_price(self):
        """Calculate final price with adjustment"""
        base_price = self.listing.price
        return max(0, base_price + self.price_adjustment)
    
    @property
    def is_in_stock(self):
        """Check if variant is in stock"""
        return self.stock > 0

class StockMovement(models.Model):
    MOVEMENT_TYPES = [
        ('purchase', 'Purchase'),
        ('sale', 'Sale'),
        ('return', 'Return'),
        ('adjustment', 'Adjustment'),
        ('transfer', 'Transfer'),
        ('damage', 'Damage'),
        ('expired', 'Expired'),
    ]
    
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='stock_movements')
    product = models.ForeignKey('listings.Listing', on_delete=models.CASCADE)
    variant = models.ForeignKey(ProductVariant, on_delete=models.SET_NULL, null=True, blank=True)
    movement_type = models.CharField(max_length=20, choices=MOVEMENT_TYPES)
    quantity = models.IntegerField()
    previous_stock = models.IntegerField()
    new_stock = models.IntegerField()
    reference = models.CharField(max_length=100, blank=True)  # Order ID, Transfer ID, etc.
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.get_movement_type_display()} - {self.product.title} ({self.quantity})"

class InventoryAudit(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='inventory_audits')
    audit_date = models.DateTimeField()
    auditor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    total_items = models.IntegerField(default=0)
    items_counted = models.IntegerField(default=0)
    discrepancies = models.IntegerField(default=0)
    status = models.CharField(max_length=20, choices=[
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('adjusted', 'Adjusted')
    ], default='pending')
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-audit_date']
    
    def __str__(self):
        return f"Audit {self.audit_date.strftime('%Y-%m-%d')} - {self.store.name}"

# Import bulk models to ensure they're discovered by Django
from . import models_bulk

# models.py - Add these analytics models
from django.db import models
from django.db.models import Sum, Count, Avg
from django.utils import timezone

class SellerAnalytics(models.Model):
    """Track seller analytics data"""
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='analytics')
    date = models.DateField(default=timezone.now)
    total_revenue = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_orders = models.IntegerField(default=0)
    active_stores = models.IntegerField(default=0)
    active_listings = models.IntegerField(default=0)
    revenue_trend = models.FloatField(default=0)  # Percentage change
    orders_trend = models.FloatField(default=0)   # Percentage change
    
    class Meta:
        unique_together = ['seller', 'date']
        ordering = ['-date']

class StoreAnalytics(models.Model):
    """Track individual store analytics"""
    store = models.ForeignKey('storefront.Store', on_delete=models.CASCADE, related_name='analytics')
    date = models.DateField(default=timezone.now)
    revenue = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    orders_count = models.IntegerField(default=0)
    active_listings = models.IntegerField(default=0)
    avg_order_value = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    views = models.IntegerField(default=0)
    conversion_rate = models.FloatField(default=0)
    
    class Meta:
        unique_together = ['store', 'date']
        ordering = ['-date']

