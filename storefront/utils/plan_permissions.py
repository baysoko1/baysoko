# storefront/utils/plan_permissions.py
from ..subscription_service import SubscriptionService
from django.utils import timezone
from django.db.models import Q
from storefront.models import Subscription
from storefront.models import Store


class PlanPermissions:
    """Centralized plan-based permissions and feature access control"""

    # Feature permissions by plan
    FEATURE_PERMISSIONS = {
        'free': {  # No active subscription
            'analytics': False,
            'inventory': False,
            'bulk_operations': False,
            'multiple_stores': False,
            'advanced_analytics': False,
            'api_access': False,
            'custom_domain': False,
            'white_label': False,
        },
        'basic': {
            'analytics': True,  # Basic analytics only
            'inventory': False,
            'bulk_operations': False,
            'multiple_stores': True,
            'advanced_analytics': False,
            'api_access': False,
            'custom_domain': False,
            'white_label': False,
        },
        'premium': {
            'analytics': True,
            'inventory': True,
            'bulk_operations': True,
            'multiple_stores': True,
            'advanced_analytics': False,
            'api_access': False,
            'custom_domain': False,
            'white_label': False,
        },
        'enterprise': {
            'analytics': True,
            'inventory': True,
            'bulk_operations': True,
            'multiple_stores': True,
            'advanced_analytics': True,
            'api_access': True,
            'custom_domain': True,
            'white_label': True,
        }
    }

    @classmethod
    def get_user_plan_status(cls, user, store=None):
        """Get user's current plan and status"""
        if not user or not getattr(user, 'is_authenticated', False):
            return {
                'plan': 'free',
                'status': 'inactive',
                'subscription': None,
                'is_active': False,
                'is_trialing': False,
            }
        
        # Get active subscription
        subscription = None
        if store:
            subscription = Subscription.objects.filter(
                store=store,
                status__in=['active', 'trialing']
            ).order_by('-created_at').first()
        else:
            # Check any active subscription for the user
            subscription = Subscription.objects.filter(
                store__owner=user,
                status__in=['active', 'trialing']
            ).order_by('-created_at').first()

        if not subscription:
            return {
                'plan': 'free',
                'status': 'inactive',
                'subscription': None,
                'is_active': False,
                'is_trialing': False,
            }

        # Check if trial is expired
        if subscription.status == 'trialing' and subscription.trial_ends_at:
            if timezone.now() > subscription.trial_ends_at:
                return {
                    'plan': 'free',
                    'status': 'trial_expired',
                    'subscription': subscription,
                    'is_active': False,
                    'is_trialing': False,
                }

        return {
            'plan': subscription.plan,
            'status': subscription.status,
            'subscription': subscription,
            'is_active': subscription.is_active(),
            'is_trialing': subscription.status == 'trialing',
        }

    @classmethod
    def has_feature_access(cls, user, feature, store=None):
        """Check if user has access to a specific feature"""
        # If user is currently in an active trial for the store (or any store when store is None),
        # grant access to all features until the trial ends.
        plan_status = cls.get_user_plan_status(user, store)
        if plan_status.get('is_trialing'):
            return True

        plan = plan_status['plan']
        # Fall back to configured feature permissions per plan (free disallows analytics)
        return cls.FEATURE_PERMISSIONS.get(plan, {}).get(feature, False)

    @classmethod
    def get_plan_limits(cls, user, store=None):
        """Get plan limits for the user"""
        plan_status = cls.get_user_plan_status(user, store)
        plan = plan_status['plan']
        # Use centralized plan details from SubscriptionService to determine limits
        plan_details = SubscriptionService.PLAN_DETAILS.get(plan, {})

        raw_max_stores = plan_details.get('max_stores', 1)
        raw_max_products = plan_details.get('max_products', 5)

        # Convert large sentinels or None to actual None (meaning unlimited)
        max_stores = None if (raw_max_stores is None or (isinstance(raw_max_stores, int) and raw_max_stores >= 100)) else int(raw_max_stores)
        max_products = None if (raw_max_products is None or (isinstance(raw_max_products, int) and raw_max_products >= 500)) else int(raw_max_products)

        return {
            'max_stores': max_stores,
            'max_products': max_products,
            'unlimited_stores': max_stores is None,
            'unlimited_products': max_products is None,
            'features': cls.FEATURE_PERMISSIONS.get(plan, {})
        }

    @classmethod
    def can_create_store(cls, user):
        """Check if user can create additional stores"""
        limits = cls.get_plan_limits(user)
        
        current_stores = Store.objects.filter(owner=user).count()
        max_stores = limits.get('max_stores')
        # None means unlimited
        if max_stores is None:
            return True
        try:
            return current_stores < int(max_stores)
        except Exception:
            return current_stores < 1

    @classmethod
    def can_create_listing(cls, user, store=None):
        """Check if user can create additional listings"""
        limits = cls.get_plan_limits(user, store)
        from listings.models import Listing
        if store:
            current_listings = Listing.objects.filter(seller=user, store=store).count()
        else:
            current_listings = Listing.objects.filter(seller=user).count()
        max_products = limits.get('max_products')
        # None indicates unlimited products
        if max_products is None:
            return True
        try:
            return current_listings < int(max_products)
        except Exception:
            # If invalid limit, be conservative and allow up to global free limit of 5
            return current_listings < 5

    @classmethod
    def get_visible_stores(cls, user):
        """Get stores that should be visible to the user based on plan"""
        
        limits = cls.get_plan_limits(user)
        stores = Store.objects.filter(owner=user, is_active=True).order_by('-created_at')

        max_stores = limits.get('max_stores')
        if max_stores is not None:
            try:
                max_stores_int = int(max_stores)
                # Use [:n] to limit but return as list-like queryset by requerying
                # Actually, just apply limit via queryset - don't slice, use count check for UI
                # The filtering will happen on the full queryset before any slicing for aggregation
                stores = stores[:max_stores_int]  # This returns a list, not a queryset
            except Exception:
                pass

        return stores

    @classmethod
    def get_visible_listings(cls, user, store=None):
        """Get listings that should be visible to the user based on plan"""
        from listings.models import Listing
        limits = cls.get_plan_limits(user, store)

        if store:
            listings = Listing.objects.filter(seller=user, store=store, is_active=True).order_by('-created_at')
        else:
            listings = Listing.objects.filter(seller=user, is_active=True).order_by('-date_created')

        max_products = limits.get('max_products')
        if max_products is None:
            return listings
        try:
            if listings.count() > int(max_products):
                return listings[:int(max_products)]
        except Exception:
            pass

        return listings

    @classmethod
    def get_analytics_level(cls, user, store=None):
        """Get the analytics level user has access to"""
        plan_status = cls.get_user_plan_status(user, store)
        plan = plan_status['plan']
        # During an active trial, grant full analytics access
        if plan_status.get('is_trialing'):
            return 'enterprise'

        if plan == 'free':
            return 'none'
        elif plan == 'basic':
            return 'basic'
        elif plan == 'premium':
            return 'basic'
        elif plan == 'enterprise':
            return 'enterprise'
        else:
            return 'none'
