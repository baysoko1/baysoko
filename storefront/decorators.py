# storefront/decorators.py
from functools import wraps
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from django.urls import reverse
from .models import Store
from .utils.plan_permissions import PlanPermissions


def store_owner_required(permission=None):
    """
    Decorator factory to ensure the requesting user owns the store.

    Can be used as either:
      @store_owner_required
    or
      @store_owner_required('inventory')
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            # Get store slug from kwargs
            store_slug = kwargs.get('slug') or kwargs.get('store_slug')

            if not store_slug:
                # If no store slug in URL, user must own at least one store
                if not Store.objects.filter(owner=getattr(request, 'user', None)).exists():
                    raise PermissionDenied("You don't own any stores.")
                return view_func(request, *args, **kwargs)

            # Get the store
            store = get_object_or_404(Store, slug=store_slug)

            # Check if user owns the store or is staff
            user = getattr(request, 'user', None)
            if not user or (store.owner != user and not getattr(user, 'is_staff', False)):
                raise PermissionDenied("You don't have permission to access this store.")

            return view_func(request, *args, **kwargs)

        return _wrapped_view

    # Support being used without parentheses
    if callable(permission):
        return decorator(permission)

    return decorator


def staff_required(permission=None):
    """
    Decorator factory to require a staff role/permission for a view.

    Usage:
      @staff_required('inventory')
    or
      @staff_required
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            user = getattr(request, 'user', None)
            if not user or not user.is_authenticated:
                raise PermissionDenied("Authentication required.")

            # Strict policy: only the store creator (owner) may satisfy staff_required for store-scoped views.
            # Exception: superusers have access to all stores
            store_slug = kwargs.get('slug') or kwargs.get('store_slug')
            if not store_slug:
                try:
                    resolver = getattr(request, 'resolver_match', None)
                    if resolver:
                        store_slug = resolver.kwargs.get('slug') or resolver.kwargs.get('store_slug')
                except Exception:
                    store_slug = None

            if not store_slug:
                # No store context -> deny access
                raise PermissionDenied("Staff privileges required.")

            # Allow superusers access to all stores
            if getattr(user, 'is_superuser', False):
                return view_func(request, *args, **kwargs)

            try:
                from .models import Store
                # Simple existence check avoids object identity issues
                if Store.objects.filter(slug=store_slug, owner_id=getattr(user, 'id', None)).exists():
                    return view_func(request, *args, **kwargs)
            except Exception:
                # If Store model unavailable, deny for safety
                raise PermissionDenied("Staff privileges required.")

            raise PermissionDenied("Staff privileges required.")

        return _wrapped_view

    if callable(permission):
        return decorator(permission)

    return decorator


def plan_required(feature, redirect_url='storefront:subscription_manage'):
    """
    Decorator to check if user has access to a specific feature based on their plan
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            # Get store from kwargs if available
            store_slug = kwargs.get('slug')
            store = None
            if store_slug:
                try:
                    store = get_object_or_404(Store, slug=store_slug, owner=request.user)
                except:
                    pass

            if not PlanPermissions.has_feature_access(request.user, feature, store):
                plan_status = PlanPermissions.get_user_plan_status(request.user, store)
                # Build a redirect URL that includes the requested feature so subscription_manage can show tailored messages
                try:
                    if store_slug:
                        target = reverse(redirect_url, kwargs={'slug': store_slug}) + f"?feature={feature}"
                    else:
                        target = reverse(redirect_url) + f"?feature={feature}"
                except Exception:
                    # Fallback to simple redirect
                    target = None

                if plan_status['plan'] == 'free':
                    messages.warning(
                        request,
                        "This feature requires an active subscription. Please upgrade to access premium features."
                    )
                else:
                    messages.warning(
                        request,
                        f"This feature is not available on your {plan_status['plan'].title()} plan. Please upgrade to access it."
                    )

                if target:
                    return redirect(target)
                # If we have a store context, redirect to the store-specific subscription manage URL.
                if store_slug:
                    return redirect('storefront:subscription_manage', slug=store_slug)
                # Otherwise send user to seller dashboard as a safe fallback.
                return redirect('storefront:seller_dashboard')
            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator


def store_limit_check(view_func):
    """
    Decorator to check if user can create additional stores
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not PlanPermissions.can_create_store(request.user):
            limits = PlanPermissions.get_plan_limits(request.user)
            messages.error(
                request,
                f"You've reached the maximum number of stores ({limits['max_stores']}) for your plan. "
                "Please upgrade to create more stores."
            )
            # Try to find a store to redirect to subscription management
            try:
                user_store = Store.objects.filter(owner=request.user).first()
                if user_store and user_store.slug:
                    return redirect('storefront:subscription_manage', slug=user_store.slug)
            except:
                pass
            # Fallback to seller dashboard if no store found
            return redirect('storefront:seller_dashboard')
        return view_func(request, *args, **kwargs)
    return _wrapped_view


def listing_limit_check(view_func):
    """
    Decorator to check if user can create additional listings
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not getattr(request, 'user', None) or not request.user.is_authenticated:
            messages.info(request, "Please log in to create listings.")
            return redirect(f"{reverse('login')}?next={request.path}")
        store_slug = kwargs.get('slug')
        store = None
        if store_slug:
            try:
                store = get_object_or_404(Store, slug=store_slug, owner=request.user)
            except:
                pass

        if not PlanPermissions.can_create_listing(request.user, store):
            limits = PlanPermissions.get_plan_limits(request.user, store)
            messages.warning(
                request,
                f"You've reached the listing limit ({limits['max_products']}) for your plan. "
                "Upgrade to add more listings."
            )
            return redirect('storefront:seller_dashboard')
        return view_func(request, *args, **kwargs)
    return _wrapped_view


def analytics_access_required(level='basic'):
    """
    Decorator to check analytics access level
    """
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            user_level = PlanPermissions.get_analytics_level(request.user)
            level_hierarchy = {'none': 0, 'basic': 1, 'advanced': 2, 'enterprise': 3}
            required_level = level_hierarchy.get(level, 1)
            user_level_num = level_hierarchy.get(user_level, 0)

            if user_level_num < required_level:
                messages.warning(
                    request,
                    f"Advanced analytics requires a Premium or Enterprise plan. You have {user_level.title()} access."
                )
                # Try to redirect to the store-specific subscription management page if a store slug
                # is available in the view kwargs or resolver; otherwise fall back to seller dashboard.
                store_slug = kwargs.get('slug')
                if not store_slug:
                    try:
                        resolver = getattr(request, 'resolver_match', None)
                        if resolver:
                            store_slug = resolver.kwargs.get('slug') or resolver.kwargs.get('store_slug')
                    except Exception:
                        store_slug = None

                if store_slug:
                    return redirect('storefront:subscription_manage', slug=store_slug)
                return redirect('storefront:seller_dashboard')
            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator
