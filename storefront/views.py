from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib import messages
from django.conf import settings
from django.urls import reverse
from django.core.exceptions import ValidationError, PermissionDenied
from django.http import JsonResponse, HttpResponseForbidden, HttpResponseRedirect, HttpResponse
from django.db.models import F, Sum, Count, Avg, Q
from django.utils import timezone
from django.core.mail import send_mail
from django.core.paginator import Paginator
from django.db import transaction
from datetime import timedelta, datetime
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.views.decorators.csrf import csrf_exempt
import json
import logging
from decimal import Decimal
from collections import defaultdict
from .decorators import store_owner_required, analytics_access_required, store_limit_check
from .models import Store, Subscription, MpesaPayment, StockMovement, StoreReview, WithdrawalRequest, StoreVideo
from .models import PayoutVerification
from .mpesa import MpesaGateway
from .forms import StoreForm, UpgradeForm, SubscriptionPlanForm, StoreReviewForm
from .mpesa import MpesaGateway
from .monitoring import PaymentMonitor
from listings.models import Listing, Category, Favorite, ListingImage, Order, OrderItem, Payment
from listings.forms import ListingForm
from reviews.models import Review
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer


def _broadcast_store_reel_created(video):
    try:
        channel_layer = get_channel_layer()
        if not channel_layer:
            return
        store = getattr(video, 'store', None)
        if not store:
            return
        payload = {
            'kind': 'store',
            'video_id': video.id,
            'video_url': video.get_video_url(),
            'likes_count': video.likes_count,
            'comments_count': video.comments_count,
            'shares_count': video.shares_count,
            'views_count': video.views_count,
            'store_name': store.name,
            'listing_url': reverse('storefront:store_detail', args=[store.slug]),
            'url': reverse('storefront:store_detail', args=[store.slug]),
        }
        async_to_sync(channel_layer.group_send)(
            'reels',
            {'type': 'reel_created', 'payload': payload}
        )
    except Exception:
        pass

def _enforce_cloudinary_video_duration(video_obj, max_seconds=45):
    try:
        from cloudinary import api
        cloudinary_field = getattr(video_obj, 'video', None)
        public_id = getattr(cloudinary_field, 'public_id', None)
        if not public_id:
            return True
        resource = api.resource(public_id, resource_type='video')
        duration = resource.get('duration')
        if duration is not None and float(duration) > max_seconds:
            video_obj.delete()
            return False
    except Exception:
        return True
    return True

logger = logging.getLogger(__name__)

def store_list(request):
    stores = Store.objects.filter()
    premium_count = Store.objects.filter(is_premium=True).count()
    from listings.models import Listing as _Listing
    total_products = sum(_Listing.objects.filter(store=store).count() for store in stores)
    
    context = {
        'stores': stores,
        'premium_count': premium_count,
        'total_products': total_products
    }
    
    # Add plan-related context for authenticated users
    if request.user.is_authenticated:
        from .utils.plan_permissions import PlanPermissions
        context['plan_limits'] = PlanPermissions.get_plan_limits(request.user)
        context['can_create_store'] = PlanPermissions.can_create_store(request.user)
    
    return render(request, 'storefront/store_list.html', context)


def store_detail(request, slug):
    store = get_object_or_404(Store, slug=slug) 
    # Increment the view count (using F() to avoid race conditions)
    Store.objects.filter(pk=store.pk).update(total_views=F('total_views') + 1)
    
    # Refresh the store object to get updated view count
    store.refresh_from_db()
    
    # Only show listings associated with this specific store
    products = Listing.objects.filter(store=store, is_active=True)
    # Ensure any legacy listings without slugs get one so product URLs remain valid
    try:
        from django.db.models import Q
        missing_slugs = products.filter(Q(slug__isnull=True) | Q(slug=''))
        if missing_slugs.exists():
            for listing in missing_slugs:
                try:
                    listing.save()
                except Exception:
                    continue
    except Exception:
        pass
    user_favorites = []
    if request.user.is_authenticated:
        user_favorites = Favorite.objects.filter(
            user=request.user,
            listing__in=Listing.objects.filter(store=store)
        ).values_list('listing_id', flat=True)
    
    context = {'store': store, 'products': products, 'user_favorites': user_favorites}
    try:
        recent_store_reviews = StoreReview.objects.filter(store=store).select_related('reviewer').order_by('-created_at')[:10]
        context['store_reel_comments_preview'] = [
            {
                'author': review.reviewer.get_full_name() or review.reviewer.username,
                'comment': review.comment,
                'rating': review.rating,
                'created_at': review.created_at.isoformat() if review.created_at else '',
            }
            for review in recent_store_reviews if getattr(review, 'comment', None)
        ]
    except Exception:
        context['store_reel_comments_preview'] = []
    
    # Add plan-related context for authenticated users
    if request.user.is_authenticated:
        from .utils.plan_permissions import PlanPermissions
        context['plan_limits'] = PlanPermissions.get_plan_limits(request.user, store)
        context['can_create_listing'] = PlanPermissions.can_create_listing(request.user, store)
    
    return render(request, 'storefront/store_detail.html', context)

def product_detail(request, store_slug, slug):
    store = get_object_or_404(Store, slug=store_slug) 
    # Only show products associated with this specific store
    product = get_object_or_404(Listing, store=store, slug=slug, is_active=True) 
    # Ensure `user_favorites` is defined for anonymous users as well
    user_favorites = []
    if request.user.is_authenticated:
        user_favorites = Favorite.objects.filter(
            user=request.user,
            listing__in=Listing.objects.filter(store=store)
        ).values_list('listing_id', flat=True)

    context = {'store': store, 'product': product, 'user_favorites': user_favorites}
    try:
        reviews = product.reviews.select_related('user').all()
        context['product_reel_comments_preview'] = [
            {
                'author': review.user.get_full_name() or review.user.username,
                'comment': review.comment,
                'rating': review.rating,
                'created_at': review.created_at.isoformat() if review.created_at else '',
            }
            for review in reviews if getattr(review, 'comment', None)
        ]
    except Exception:
        context['product_reel_comments_preview'] = []

    return render(request, 'storefront/product_detail.html', context)

@login_required
def seller_dashboard(request):
    from .utils.plan_permissions import PlanPermissions
    
    # Get visible stores based on plan
    stores = PlanPermissions.get_visible_stores(request.user)
    
    # Get visible listings based on plan
    user_listings = PlanPermissions.get_visible_listings(request.user)
    
    # Compute metrics only for visible stores/listings
    total_listings = user_listings.count()
    stores_list = None

    # If `stores` is a plain list, compute metrics directly. If it's a QuerySet,
    # attempt to use DB aggregation, but fall back to converting to a list if the
    # QuerySet has been sliced (filtering a sliced queryset raises TypeError).
    if isinstance(stores, list):
        stores_list = stores
        premium_stores = sum(1 for store in stores_list if store.is_premium)
        store_views_sum = sum(getattr(store, 'total_views', 0) for store in stores_list)
    else:
        try:
            premium_stores = stores.filter(is_premium=True).count()
            store_views_sum = stores.aggregate(total=Sum('total_views'))['total'] or 0
        except TypeError:
            # Likely a sliced QuerySet — convert to list and compute in Python
            stores_list = list(stores)
            premium_stores = sum(1 for store in stores_list if getattr(store, 'is_premium', False))
            store_views_sum = sum(getattr(store, 'total_views', 0) for store in stores_list)
    
    listing_views_sum = user_listings.aggregate(total=Sum('views'))['total'] or 0
    total_views = store_views_sum + listing_views_sum

    # Get plan limits for display
    limits = PlanPermissions.get_plan_limits(request.user)
    # Use the plan's max_products when present; otherwise fall back to global setting (default free limit)
    free_limit = limits.get('max_products')
    if free_limit is None:
        free_limit = getattr(settings, 'STORE_FREE_LISTING_LIMIT', 5)
    try:
        free_limit = int(free_limit)
    except Exception:
        free_limit = getattr(settings, 'STORE_FREE_LISTING_LIMIT', 5)

    remaining = max(free_limit - total_listings, 0)
    percentage_used = (total_listings / free_limit * 100) if free_limit > 0 else 0

    # `get_visible_stores` may return a QuerySet, a sliced QuerySet, or a list.
    # Prefer using the already-converted `stores_list` when available.
    if stores_list is not None:
        store_with_slug = next((s for s in stores_list if getattr(s, 'slug', None)), None)
    else:
        store_with_slug = stores.filter(slug__isnull=False).exclude(slug='').first()

    return render(request, 'storefront/dashboard.html', {
        'stores': stores,
        'total_listings': total_listings,
        'premium_stores': premium_stores,
        'total_views': total_views,
        'free_limit': free_limit,
        'remaining_slots': remaining,
        'percentage_used': min(percentage_used, 100),
        'user_listings': user_listings,
        'store_with_slug': store_with_slug,
        'plan_limits': limits,
        'plan_status': PlanPermissions.get_user_plan_status(request.user)
    })

@login_required
@store_limit_check
def store_create(request):
    """
    Create a new store with enforced subscription-based limits.
    Users can only create multiple stores if they have a premium store or active subscription.
    """
    # Check existing stores and subscription status via PlanPermissions
    existing_stores = Store.objects.filter(owner=request.user)
    from .utils.plan_permissions import PlanPermissions

    # Use centralized plan logic (this respects trials via get_user_plan_status)
    can_create = PlanPermissions.can_create_store(request.user)
    plan_status = PlanPermissions.get_user_plan_status(request.user)
    has_premium = existing_stores.filter(is_premium=True).exists()

    # Enforce store limit for free users / expired trials
    if existing_stores.exists() and not can_create:
        first_store = existing_stores.first()
        if first_store:
            messages.warning(request, 'You must upgrade to create additional storefronts.')
            return redirect('storefront:store_edit', slug=first_store.slug)
        else:
            messages.warning(request, 'You must upgrade to create additional storefronts.')
            return redirect('storefront:seller_dashboard')

    # Show store creation confirmation for users coming from listing creation
    if request.GET.get('from') == 'listing':
        return render(request, 'storefront/confirm_store_create.html')

    if request.method == 'POST':
        form = StoreForm(request.POST, request.FILES, user=request.user)  # Pass user to form
        if form.is_valid():
            store = form.save(commit=False)
            store.owner = request.user
            
            # For new stores, is_featured should always be False initially
            store.is_featured = False
            
            try:
                # This will trigger the clean() method which enforces store limits
                store.full_clean()
                store.save()

                # Process logo and cover image
                if 'logo' in request.FILES:
                    store.logo = request.FILES['logo']
                if 'cover_image' in request.FILES:
                    store.cover_image = request.FILES['cover_image']
                store.save()

                # Process store videos (max 3)
                videos = request.FILES.getlist('store_videos')
                if videos:
                    existing_count = store.videos.count()
                    if existing_count + len(videos) > 3:
                        messages.error(request, "You can upload up to 3 store videos.")
                        return render(request, 'storefront/store_form.html', {
                            'form': form,
                            'creating_store': True,
                            'has_existing_store': existing_stores.exists(),
                            'has_premium': has_premium,
                            'plan_status': plan_status,
                            'can_create_store': can_create,
                            'can_be_featured': False,
                            'is_enterprise': False,
                        })
                    start_order = existing_count
                    for idx, video in enumerate(videos):
                        try:
                            created_video = StoreVideo.objects.create(
                                store=store,
                                video=video,
                                order=start_order + idx
                            )
                            if not _enforce_cloudinary_video_duration(created_video):
                                messages.error(request, "A store video longer than 45 seconds was removed.")
                                continue
                            _broadcast_store_reel_created(created_video)
                        except Exception as e:
                            logger.exception('Failed to save store video: %s', e)
                            messages.error(request, "We couldn't save one of your store videos. Please try again.")

                # Automatically assign Free plan semantics (no DB Subscription required)
                # Inform user via Django messages, create an in-app notification, and send email.
                try:
                    from notifications.utils import create_notification, NotificationService
                    # In-app notification
                    try:
                        create_notification(
                            recipient=request.user,
                            notification_type='store_created',
                            title='Store Created',
                            message=f'Your store "{store.name}" was created and is on the Free plan.',
                            sender=None,
                            related_object_id=store.id,
                            related_content_type='store',
                            action_url=store.get_absolute_url(),
                            action_text='View Store'
                        )
                    except Exception:
                        pass

                    # Email notification via NotificationService for nicer HTML email
                    try:
                        email_context = {
                            'user': request.user,
                            'store': store,
                            'store_url': request.build_absolute_uri(store.get_absolute_url()),
                        }
                        NotificationService.send_email(
                            to_email=request.user.email,
                            subject=f'Your store "{store.name}" has been created',
                            template_name='emails/store_created.html',
                            context=email_context
                        )
                    except Exception:
                        pass
                except Exception:
                    # If notifications app missing or other errors occur, continue gracefully
                    pass

                messages.success(request, 'Store created successfully! Your store is now on the Free plan.')
                return redirect('storefront:seller_dashboard')
                
            except ValidationError as e:
                # Handle all validation errors
                error_message = str(e)
                messages.error(request, error_message)
                # Also add to form errors so they display in the template
                for field, errors in e.message_dict.items():
                    if field == '__all__':  # Non-field errors
                        form.add_error(None, errors[0])
                    else:
                        form.add_error(field, errors[0])
        
        # If form is invalid, add all errors to messages
        for field, errors in form.errors.items():
            if field == '__all__':
                if errors:
                    messages.error(request, errors[0])
            else:
                if errors:
                    label = form.fields.get(field).label if field in form.fields and form.fields.get(field).label else field.replace('_', ' ').title()
                    messages.error(request, f"{label}: {errors[0]}")

    else:
        form = StoreForm(user=request.user)

    context = {
        'form': form,
        'creating_store': True,
        'has_existing_store': existing_stores.exists(),
        'has_premium': has_premium,
        'plan_status': plan_status,
        'can_create_store': can_create,
        'can_be_featured': False,  # New stores cannot be featured
        'is_enterprise': False,     # New stores are not enterprise
    }
    return render(request, 'storefront/store_form.html', context)


@login_required
@store_owner_required
def store_edit(request, slug):
    """
    Edit an existing store with proper form handling and validation.
    """
    store = get_object_or_404(Store, slug=slug)
    
    # Check if store can be featured (has active subscription or valid trial)
    can_be_featured = False
    is_enterprise = False
    
    # Only check if store has a primary key
    if store.pk:
        try:
            has_active = Subscription.objects.filter(
                store=store, 
                status='active'
            ).exists()
            has_valid_trial = Subscription.objects.filter(
                store=store,
                status='trialing',
                trial_ends_at__gt=timezone.now()
            ).exists()
            can_be_featured = has_active or has_valid_trial
            
            # Check if it's enterprise
            if can_be_featured:
                is_enterprise = Subscription.objects.filter(
                    store=store,
                    status='active',
                    plan='enterprise'
                ).exists()
        except Exception as e:
            # If there's any error with subscription check, default to not featured
            logger.error(f"Error checking subscription: {e}")
            can_be_featured = False
            is_enterprise = False
    
    if request.method == 'POST':
        form = StoreForm(request.POST, request.FILES, instance=store, user=request.user)
        
        if form.is_valid():
            try:
                store = form.save()

                if 'delete_store_videos' in request.POST:
                    delete_ids = request.POST.getlist('delete_store_videos')
                    if delete_ids:
                        StoreVideo.objects.filter(id__in=delete_ids, store=store).delete()

                videos = request.FILES.getlist('store_videos')
                if videos:
                    existing_count = store.videos.count()
                    if existing_count + len(videos) > 3:
                        messages.error(request, "You can upload up to 3 store videos.")
                        return render(request, 'storefront/store_form.html', {
                            'form': form,
                            'store': store,
                            'creating_store': False,
                            'can_be_featured': can_be_featured,
                            'is_enterprise': is_enterprise,
                        })
                    start_order = existing_count
                    for idx, video in enumerate(videos):
                        try:
                            created_video = StoreVideo.objects.create(
                                store=store,
                                video=video,
                                order=start_order + idx
                            )
                            if not _enforce_cloudinary_video_duration(created_video):
                                messages.error(request, "A store video longer than 45 seconds was removed.")
                                continue
                            _broadcast_store_reel_created(created_video)
                        except Exception as e:
                            logger.exception('Failed to update store video: %s', e)
                            messages.error(request, "We couldn't save one of the uploaded store videos.")
                
                messages.success(request, "Store updated successfully!")
                return redirect('storefront:store_detail', slug=store.slug)
                
            except ValidationError as e:
                messages.error(request, f"Validation error: {str(e)}")
                return render(request, 'storefront/store_form.html', {
                    'form': form,
                    'store': store,
                    'creating_store': False,
                    'can_be_featured': can_be_featured,
                    'is_enterprise': is_enterprise,
                })
            except Exception as e:
                messages.error(request, f"Error updating store: {str(e)}")
                return render(request, 'storefront/store_form.html', {
                    'form': form,
                    'store': store,
                    'creating_store': False,
                    'can_be_featured': can_be_featured,
                    'is_enterprise': is_enterprise,
                })
        else:
            # Form is invalid, show errors
            messages.error(request, "Please correct the errors below.")
            for field, errors in form.errors.items():
                for error in errors:
                    label = form.fields.get(field).label if field in form.fields and form.fields.get(field).label else field.replace('_', ' ').title()
                    messages.error(request, f"{label}: {error}")
    
    else:
        # GET request - initialize form with instance
        form = StoreForm(instance=store, user=request.user)
    
    context = {
        'form': form,
        'store': store,
        'creating_store': False,
        'can_be_featured': can_be_featured,
        'is_enterprise': is_enterprise,
    }
    
    return render(request, 'storefront/store_form.html', context)

@login_required
@store_owner_required
def product_create(request, store_slug):
    
    FREE_LISTING_LIMIT = getattr(settings, 'STORE_FREE_LISTING_LIMIT', 5)

    # Count all listings created by this user (global per-user limit)
    user_listing_count = Listing.objects.filter(seller=request.user).count()

    # Get or create the user's single storefront
    user_store = Store.objects.filter(owner=request.user).first()  

    # If user reached limit and is not premium, prompt upgrade
    is_premium = user_store.is_premium if user_store else False
    if not is_premium and user_listing_count >= FREE_LISTING_LIMIT:
        store_for_template = user_store or Store(owner=request.user, name=f"{request.user.username}'s Store", slug=request.user.username)
        messages.warning(request, f"You've reached the free listing limit ({FREE_LISTING_LIMIT}). Upgrade to premium to add more listings.")
        return render(request, 'storefront/subscription_manage.html', {
            'store': store_for_template,
            'limit_reached': True,
            'trial_count': user_listing_count,
            'trial_limit': FREE_LISTING_LIMIT,
            'trial_available': user_listing_count < FREE_LISTING_LIMIT,
        })

    # If the user does not have a store, require they create one first instead of auto-creating it.
    if not user_store:
        messages.info(request, 'Please create a storefront before creating products.')
        return redirect(reverse('storefront:store_create') + '?from=listing')

    # Ensure the route matches the user's storefront; if not, redirect
    if store_slug != user_store.slug:
        return redirect('storefront:product_create', store_slug=user_store.slug)

    store = user_store
    user_stores = Store.objects.filter(owner=request.user)

    if request.method == 'POST':
        form = ListingForm(request.POST, request.FILES)
        if form.is_valid():
            listing = form.save(commit=False)
            listing.seller = request.user
            listing.store = store
            
            # Set is_featured automatically based on store's subscription
            from django.utils import timezone
            active_premium_subscription = Subscription.objects.filter(
                store=store,
                plan__in=['premium', 'enterprise']
            ).filter(
                Q(status='active') | Q(status='trialing', trial_ends_at__gt=timezone.now())
            ).exists()
            listing.is_featured = active_premium_subscription
            
            listing.save()
            # Handle multiple uploaded images robustly
            images = request.FILES.getlist('images')
            failed_images = []
            max_size = getattr(settings, 'MAX_IMAGE_UPLOAD_SIZE', 5 * 1024 * 1024)
            for img in images:
                try:
                    # Basic validation: content type and size
                    content_type = getattr(img, 'content_type', '')
                    size = getattr(img, 'size', None)
                    if content_type and not content_type.startswith('image/'):
                        raise ValueError('Invalid file type')
                    if size is not None and size > max_size:
                        raise ValueError('File too large')

                    ListingImage.objects.create(listing=listing, image=img)
                except Exception as e:
                    # Log and track failed image; continue processing
                    failed_images.append({'name': getattr(img, 'name', 'unknown'), 'error': str(e)})

            if failed_images:
                # Keep the listing but inform the user which images failed to upload.
                err_msgs = '; '.join([f"{f['name']}: {f['error']}" for f in failed_images])
                messages.warning(request, f"Listing created but some images failed to upload: {err_msgs}")
            else:
                messages.success(request, 'Listing created successfully')
            return redirect('storefront:store_detail', slug=store.slug)
    else:
        form = ListingForm()

    # Render using the same template as the generic ListingCreateView so users see the identical "Sell Item" form
    categories = Category.objects.filter(is_active=True)
    return render(request, 'listings/listing_form.html', {'form': form, 'store': store, 'categories': categories, 'stores': user_stores})


@login_required
@store_owner_required
def product_edit(request, pk):
    product = get_object_or_404(Listing, pk=pk) 
    # Allow the listing seller, the store owner, or staff to edit
    user = request.user
    store_owner_id = product.store.owner_id if product.store and hasattr(product.store, 'owner_id') else None
    if not (product.seller == user or store_owner_id == getattr(user, 'id', None) or getattr(user, 'is_staff', False)):
        raise PermissionDenied("You don't have permission to edit this listing.")
    if request.method == 'POST':
        # Handle removal of the main listing image via a small separate POST
        if request.POST.get('remove_main_image'):
            # Ensure owner
            if product.seller == request.user:
                if product.image and hasattr(product.image, 'delete'):
                    try:
                        product.image.delete(save=False)
                    except Exception:
                        pass
                    product.image = None
                    product.save()
                    messages.success(request, 'Main image removed successfully.')
                else:
                    messages.info(request, 'No main image to remove.')
            return redirect('storefront:product_edit', pk=product.pk)

        form = ListingForm(request.POST, request.FILES, instance=product)
        if form.is_valid():
            listing = form.save(commit=False)
            
            # Ensure the listing has a store associated
            if not listing.store:
                # Try to get the seller's store
                store = Store.objects.filter(owner=listing.seller).first()
                if store:
                    listing.store = store
                else:
                    # Create a new store for the seller if they don't have one
                    store_name = f"{listing.seller.username}'s Store"
                    store_slug = listing.seller.username.lower()
                    store = Store.objects.create(
                        owner=listing.seller,
                        name=store_name,
                        slug=store_slug
                    )
                    listing.store = store
                    messages.info(request, "A new store was created for your listings.")
            
            # Set is_featured automatically based on store's subscription
            from django.utils import timezone
            if listing.store:
                active_premium_subscription = Subscription.objects.filter(
                    store=listing.store,
                    plan__in=['premium', 'enterprise']
                ).filter(
                    Q(status='active') | Q(status='trialing', trial_ends_at__gt=timezone.now())
                ).exists()
                listing.is_featured = active_premium_subscription
            
            listing.save()
            
            # Handle additional uploaded images
            images = request.FILES.getlist('images')
            failed_images = []
            max_size = getattr(settings, 'MAX_IMAGE_UPLOAD_SIZE', 5 * 1024 * 1024)
            for img in images:
                try:
                    content_type = getattr(img, 'content_type', '')
                    size = getattr(img, 'size', None)
                    if content_type and not content_type.startswith('image/'):
                        raise ValueError('Invalid file type')
                    if size is not None and size > max_size:
                        raise ValueError('File too large')
                    ListingImage.objects.create(listing=listing, image=img)
                except Exception as e:
                    failed_images.append({'name': getattr(img, 'name', 'unknown'), 'error': str(e)})

            if failed_images:
                err_msgs = '; '.join([f"{f['name']}: {f['error']}" for f in failed_images])
                messages.warning(request, f"Some images failed to upload: {err_msgs}")
            else:
                messages.success(request, "Listing updated successfully!")

            # Redirect to store detail if store exists, otherwise to dashboard
            if listing.store:
                return redirect('storefront:store_detail', slug=listing.store.slug)
            return redirect('storefront:seller_dashboard')
        else:
            # Add form-level error if there are any
            non_field_errors = form.non_field_errors()
            if non_field_errors:
                messages.error(request, non_field_errors[0] if len(non_field_errors) > 0 else "Form validation failed")
            # Add field-specific errors
            for field, errors in form.errors.items():
                if errors:
                    messages.error(request, f"{field}: {errors[0]}")
    else:
        form = ListingForm(instance=product)
    
    # Add categories for form and editing flag
    context = {
        'form': form, 
        'product': product,
        'categories': Category.objects.filter(is_active=True),
        'editing': True,
    }
    return render(request, 'listings/listing_form.html', context)


@login_required
@store_owner_required
def product_delete(request, pk):
    product = get_object_or_404(Listing, pk=pk)  
    # Allow seller, store owner, or staff to delete
    user = request.user
    store_slug = request.POST.get('store_slug') or (product.store.slug if product.store else (product.seller.stores.first().slug if hasattr(product.seller, 'stores') and product.seller.stores.exists() else ''))
    if not (product.seller == user or (product.store and product.store.owner == user) or getattr(user, 'is_staff', False)):
        raise PermissionDenied("You don't have permission to delete this listing.")
    if request.method == 'POST':
        product.delete()
        if store_slug:
            return redirect('storefront:store_detail', slug=store_slug)
        return redirect('storefront:seller_dashboard')
    return render(request, 'storefront/product_confirm_delete.html', {'product': product})


@login_required
@store_owner_required
def image_delete(request, pk):
    # Delete a ListingImage
    img = get_object_or_404(ListingImage, pk=pk) 
    # Ensure the requesting user owns the listing or is store owner/staff
    user = request.user
    listing = img.listing
    if not (listing.seller == user or (listing.store and listing.store.owner == user) or getattr(user, 'is_staff', False)):
        raise PermissionDenied("You don't have permission to delete this image.")
    if request.method == 'POST':
        # Allow a "next" parameter to return to a specific URL (e.g., edit page)
        next_url = request.POST.get('next') or request.GET.get('next')
        img.delete()
        if next_url:
            # Only allow relative URLs for safety
            if next_url.startswith('/'):
                return redirect(next_url)
        # Fallback to store detail if available
        store_slug = img.listing.store.slug if img.listing.store else (img.listing.seller.stores.first().slug if hasattr(img.listing.seller, 'stores') and img.listing.seller.stores.exists() else '')
        if store_slug:
            return redirect('storefront:store_detail', slug=store_slug)
        return redirect('storefront:seller_dashboard')
    return render(request, 'storefront/image_confirm_delete.html', {'image': img})

@login_required
@store_owner_required
def delete_logo(request, slug):
    """Delete a store's logo."""
    store = get_object_or_404(Store, slug=slug)
    if request.method == 'POST':
        # Delete the actual file
        if store.logo and hasattr(store.logo, 'delete'):
            store.logo.delete(save=False)
        store.logo = None
        store.save()
        messages.success(request, 'Store logo removed successfully.')
        return redirect('storefront:store_edit', slug=store.slug)
    return redirect('storefront:store_edit', slug=store.slug)

@login_required
@store_owner_required
def delete_cover(request, slug):
    """Delete a store's cover image."""
    store = get_object_or_404(Store, slug=slug)
    if request.method == 'POST':
        # Delete the actual file
        if store.cover_image and hasattr(store.cover_image, 'delete'):
            store.cover_image.delete(save=False)
        store.cover_image = None
        store.save()
        messages.success(request, 'Store cover image removed successfully.')
        return redirect('storefront:store_edit', slug=store.slug)
    return redirect('storefront:store_edit', slug=store.slug)



@login_required
@store_owner_required
def cancel_subscription(request, slug):
    """Cancel subscription"""
    if request.method != 'POST':
        return redirect('storefront:subscription_manage', slug=slug)
        
    store = get_object_or_404(Store, slug=slug)
    subscription = store.subscriptions.order_by('-started_at').first()  # type: "Subscription"
    
    if not subscription or not subscription.is_active():
        messages.error(request, 'No active subscription found.')
        return redirect('storefront:subscription_manage', slug=slug)
    
    try:
        subscription.cancel()
        messages.success(request, 'Subscription cancelled successfully. Premium features will be available until the end of your current billing period.')
    except Exception as e:
        messages.error(request, f'Failed to cancel subscription: {str(e)}')
    
    return redirect('storefront:subscription_manage', slug=slug)

@login_required
def payment_monitor(request):
   
    # Get user's stores
    user_stores = Store.objects.filter(owner=request.user)
    
    # Get primary store for withdrawal context
    store = user_stores.first()

    # Get time period from query params
    period = request.GET.get('period', '30d')
    time_period = None
    if period != 'all':
        days = int(period.rstrip('d'))
        time_period = timezone.now() - timedelta(days=days)

    # ===== SUBSCRIPTION PAYMENTS =====
    subscription_payments = MpesaPayment.objects.filter(
        subscription__store__in=user_stores
    ).select_related('subscription', 'subscription__store')

    if time_period:
        subscription_payments = subscription_payments.filter(created_at__gte=time_period)

    subscription_payments = subscription_payments.order_by('-created_at')[:50]

    # ===== ORDER PAYMENTS (Customer purchases) =====
    order_payments = Payment.objects.filter(
        order__order_items__listing__store__in=user_stores
    ).select_related(
        'order',
        'order__user'
    ).prefetch_related('order__order_items__listing').distinct()

    if time_period:
        order_payments = order_payments.filter(created_at__gte=time_period)

    order_payments = order_payments.order_by('-created_at')[:50]

    # ===== ESCROW RELEASES =====
    escrow_releases = Payment.objects.filter(
        order__order_items__listing__store__in=user_stores,
        status='completed',
        seller_payout_reference__isnull=False
    ).exclude(seller_payout_reference='').select_related(
        'order',
        'order__user'
    ).prefetch_related('order__order_items__listing').distinct()

    if time_period:
        escrow_releases = escrow_releases.filter(actual_release_date__gte=time_period)

    escrow_releases = escrow_releases.order_by('-actual_release_date')[:50]

    # ===== PAYMENT STATISTICS =====
    # Total earnings from completed orders
    total_earnings = Payment.objects.filter(
        order__order_items__listing__store__in=user_stores,
        status='completed'
    ).aggregate(total=Sum('amount'))['total'] or 0

    # Pending escrow funds
    pending_escrow = Payment.objects.filter(
        order__order_items__listing__store__in=user_stores,
        status='completed',
        is_held_in_escrow=True,
        actual_release_date__isnull=True
    ).aggregate(total=Sum('amount'))['total'] or 0

    # Released escrow funds (available for withdrawal)
    released_escrow = Payment.objects.filter(
        order__order_items__listing__store__in=user_stores,
        status='completed',
        is_held_in_escrow=True,
        actual_release_date__isnull=False
    ).aggregate(total=Sum('amount'))['total'] or 0

    # Subscription revenue
    subscription_revenue = MpesaPayment.objects.filter(
        subscription__store__in=user_stores,
        status='completed'
    ).aggregate(total=Sum('amount'))['total'] or 0

    # Recent failed payments (both subscription and order)
    failed_payments = []
    failed_subs = MpesaPayment.objects.filter(
        subscription__store__in=user_stores,
        status='failed'
    ).order_by('-created_at')[:10]
    failed_orders = Payment.objects.filter(
        order__order_items__listing__store__in=user_stores,
        status='failed'
    ).order_by('-created_at')[:10]

    failed_payments = list(failed_subs) + list(failed_orders)
    failed_payments.sort(key=lambda x: x.created_at, reverse=True)
    failed_payments = failed_payments[:20]

    # ===== WITHDRAWAL REQUESTS =====
    from storefront.models import WithdrawalRequest
    withdrawals = WithdrawalRequest.objects.filter(
        store__in=user_stores
    ).select_related('store').order_by('-requested_at')

    if time_period:
        withdrawals = withdrawals.filter(requested_at__gte=time_period)

    withdrawals = withdrawals[:50]

    # ===== CUSTOMER PURCHASES (Orders user bought from other sellers) =====
    customer_purchases = Payment.objects.filter(
        order__user=request.user
    ).select_related(
        'order',
        'order__user'
    ).prefetch_related('order__order_items__listing', 'order__order_items__listing__seller').distinct()

    if time_period:
        customer_purchases = customer_purchases.filter(created_at__gte=time_period)

    customer_purchases = customer_purchases.order_by('-created_at')[:50]

    # ===== AFFILIATE WITHDRAWALS FOR SELLERS =====
    affiliate_available_balance = 0
    affiliate_min_withdrawal = Decimal('5000')
    try:
        from affiliates.models import AffiliateProfile, AffiliateCommission, AffiliateSubscriptionCommission, AffiliatePayout
        profile = AffiliateProfile.objects.filter(user=request.user).first()
        if profile:
            approved_order = AffiliateCommission.objects.filter(affiliate=profile, status='approved').aggregate(total=Sum('amount')).get('total') or 0
            approved_subs = AffiliateSubscriptionCommission.objects.filter(affiliate=profile, status='approved').aggregate(total=Sum('amount')).get('total') or 0
            payouts_total = AffiliatePayout.objects.filter(affiliate=profile, status__in=['pending', 'paid']).aggregate(total=Sum('amount')).get('total') or 0
            affiliate_available_balance = max(Decimal(approved_order + approved_subs) - Decimal(payouts_total), Decimal('0'))
    except Exception:
        affiliate_available_balance = 0

    context = {
        'period': period,
        'user_stores': user_stores,
        'store': store,  # Primary store for withdrawal UI

        # Payment collections
        'subscription_payments': subscription_payments,
        'order_payments': order_payments,
        'escrow_releases': escrow_releases,
        'failed_payments': failed_payments,
        'withdrawals': withdrawals,
        'customer_purchases': customer_purchases,

        # Statistics
        'total_earnings': total_earnings,
        'pending_escrow': pending_escrow,
        'released_escrow': released_escrow,
        'subscription_revenue': subscription_revenue,
        'available_balance': released_escrow,  # Funds available for withdrawal
        'affiliate_available_balance': affiliate_available_balance,
        'affiliate_min_withdrawal': affiliate_min_withdrawal,

        # Counts
        'subscription_count': subscription_payments.count(),
        'order_count': order_payments.count(),
        'escrow_count': escrow_releases.count(),
        'failed_count': len(failed_payments),
        'customer_purchase_count': customer_purchases.count(),
    }

    return render(request, 'storefront/payment_monitor_enhanced.html', context)


@login_required
def start_payout_verification(request, slug):
    
    store = get_object_or_404(Store, slug=slug)
    if store.owner != request.user:
        return HttpResponseForbidden('Not authorized')

    user_phone = getattr(request.user, 'phone_number', None)
    if not user_phone:
        messages.error(request, 'Please add your phone number in your profile before verifying payouts.')
        return redirect('storefront:store_edit', slug=slug)
    if not getattr(request.user, 'phone_verified', False):
        try:
            return redirect(reverse('verify_phone') + f'?user_id={request.user.id}')
        except Exception:
            messages.error(request, 'Please verify your phone number before requesting payouts.')
            return redirect('storefront:store_edit', slug=slug)

    mpesa = MpesaGateway()
    try:
        phone_norm = mpesa._normalize_phone(user_phone)
    except Exception as e:
        messages.error(request, f'Invalid phone: {e}')
        return redirect('storefront:store_edit', slug=slug)

    # Small amount STK push for verification (e.g., KSh 1)
    amount = 1
    account_ref = f'VERIFY{store.id}'
    try:
        resp = mpesa.initiate_stk_push(phone_norm, amount, account_ref)
        # Create a PayoutVerification record
        pv = PayoutVerification.objects.create(store=store, phone=phone_norm, amount=amount, checkout_request_id=resp.get('CheckoutRequestID') or resp.get('checkout_request_id') or '')
        messages.success(request, 'Verification STK push initiated. Complete verification by approving the prompt on your phone.')
    except Exception as e:
        messages.error(request, f'Failed to initiate verification: {e}')

    return redirect('storefront:store_edit', slug=slug)


@csrf_exempt
@require_http_methods(['POST'])
def payout_verification_callback(request):
    """Callback endpoint for verification STK push. Marks PayoutVerification and Store payout_verified.

    This reuses existing mpesa callback structure; expects `CheckoutRequestID` to map.
    """
    data = json.loads(request.body or '{}')
    try:
        checkout = data.get('Body', {}).get('stkCallback', {}).get('CheckoutRequestID')
        result_code = data.get('Body', {}).get('stkCallback', {}).get('ResultCode')
        if not checkout:
            return JsonResponse({'success': False, 'error': 'Missing CheckoutRequestID'}, status=400)

        pv = PayoutVerification.objects.filter(checkout_request_id=checkout).first()
        if not pv:
            return JsonResponse({'success': False, 'error': 'Verification not found'}, status=404)

        if result_code == 0:
            pv.verified = True
            pv.verified_at = timezone.now()
            pv.save()
            # Update store payout phone and mark verified
            store = pv.store
            store.payout_phone = pv.phone
            store.payout_verified = True
            store.payout_verified_at = timezone.now()
            store.save()
            return JsonResponse({'success': True})
        else:
            pv.verified = False
            pv.save()
            return JsonResponse({'success': False, 'error': 'Verification failed'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@csrf_exempt
@require_http_methods(['POST'])
def mpesa_b2c_result(request):
    """Handle M-Pesa B2C payout result callbacks for withdrawals and affiliate payouts."""
    try:
        data = json.loads(request.body or '{}')
        result = data.get('Result', {})
        originator = result.get('OriginatorConversationID') or result.get('ConversationID')
        result_code = result.get('ResultCode')
        result_desc = result.get('ResultDesc')

        status = 'processed' if result_code == 0 else 'failed'

        # Update seller withdrawals
        wr = WithdrawalRequest.objects.filter(
            Q(mpesa_reference=originator) | Q(mpesa_conversation_id=originator)
        ).first()
        if wr:
            wr.mpesa_status = status
            wr.mpesa_response = data
            wr.status = status
            if status == 'processed':
                wr.processed_at = timezone.now()
            wr.save()

        # Update affiliate payouts
        try:
            from affiliates.models import AffiliatePayout
            payout = AffiliatePayout.objects.filter(mpesa_reference=originator).first()
            if payout:
                payout.mpesa_status = status
                payout.mpesa_response = data
                payout.status = 'paid' if status == 'processed' else 'canceled'
                if payout.status == 'paid':
                    payout.paid_at = timezone.now()
                payout.save()
        except Exception:
            pass

        return JsonResponse({'success': True, 'status': status, 'description': result_desc})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@csrf_exempt
@require_http_methods(['POST'])
def mpesa_b2c_timeout(request):
    """Handle M-Pesa B2C timeout callbacks."""
    try:
        data = json.loads(request.body or '{}')
        result = data.get('Result', {})
        originator = result.get('OriginatorConversationID') or result.get('ConversationID')
        wr = WithdrawalRequest.objects.filter(
            Q(mpesa_reference=originator) | Q(mpesa_conversation_id=originator)
        ).first()
        if wr:
            wr.mpesa_status = 'timeout'
            wr.mpesa_response = data
            wr.status = 'failed'
            wr.save()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

# Helper function to serialize Decimal objects
def dumps_with_decimals(data):
    """JSON serializer that handles Decimal objects"""
    def default(obj):
        if isinstance(obj, Decimal):
            return float(obj)
        raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")
    return json.dumps(data, default=default)


@login_required
def seller_analytics(request):
    """Seller analytics dashboard showing aggregated metrics across all stores."""
    # Get all stores owned by the user
    stores = Store.objects.filter(owner=request.user)
    from .utils.plan_permissions import PlanPermissions
    analytics_level = PlanPermissions.get_analytics_level(request.user)
    
    # Get time period from query params
    period = request.GET.get('period', '24h')
    time_period = None
    previous_period = None
    
    # Calculate date ranges
    end_date = timezone.now()
    if period == '24h':
        time_period = end_date - timedelta(hours=24)
        previous_period = end_date - timedelta(hours=48)
        interval = 'hour'
        trend_days = 24
    elif period == '7d':
        time_period = end_date - timedelta(days=7)
        previous_period = end_date - timedelta(days=14)
        interval = 'day'
        trend_days = 7
    elif period == '30d':
        time_period = end_date - timedelta(days=30)
        previous_period = end_date - timedelta(days=60)
        interval = 'day'
        trend_days = 30
    else:
        # All time
        time_period = None
        previous_period = None
        interval = 'month'
        trend_days = 12
    
    # Base queryset for orders across all stores (include all statuses so
    # analytics reflect created orders even if not paid yet)
    orders_qs = OrderItem.objects.filter(
        listing__store__in=stores
    )
    
    # Current period metrics
    if time_period:
        # Use order created time for period filtering
        current_orders = orders_qs.filter(order__created_at__gte=time_period)
        current_revenue = current_orders.aggregate(
            total=Sum(F('price') * F('quantity'), default=0)
        )['total'] or 0
        current_order_count = current_orders.count()
        
        # Previous period for trend calculation
        previous_orders = orders_qs.filter(
            order__created_at__gte=previous_period,
            order__created_at__lt=time_period
        )
        previous_revenue = previous_orders.aggregate(
            total=Sum(F('price') * F('quantity'), default=0)
        )['total'] or 0
        previous_order_count = previous_orders.count()
        
        # Calculate trends
        revenue_trend = (
            ((current_revenue - previous_revenue) / previous_revenue * 100)
            if previous_revenue > 0 else (100 if current_revenue > 0 else 0)
        )
        orders_trend = (
            ((current_order_count - previous_order_count) / previous_order_count * 100)
            if previous_order_count > 0 else (100 if current_order_count > 0 else 0)
        )
    else:
        # All time metrics
        current_revenue = orders_qs.aggregate(
            total=Sum(F('price') * F('quantity'), default=0)
        )['total'] or 0
        current_order_count = orders_qs.count()
        revenue_trend = 0
        orders_trend = 0
    
    # Store metrics
    active_stores = stores.filter(is_active=True).count()
    premium_stores = stores.filter(is_premium=True).count()
    active_listings = Listing.objects.filter(
        store__in=stores,
        is_active=True
    ).count()
    
    # Revenue & Orders trend data
    revenue_data = []
    orders_data = []
    labels = []
    
    for i in range(trend_days):
        if interval == 'hour':
            hour_start = end_date - timedelta(hours=i)
            hour_end = hour_start + timedelta(hours=1)
            day_orders = orders_qs.filter(
                order__created_at__gte=hour_start,
                order__created_at__lt=hour_end
            )
            label = hour_start.strftime('%H:%M')
        else:  # day or month
            day_start = end_date - timedelta(days=i)
            day_end = day_start + timedelta(days=1)
            day_orders = orders_qs.filter(
                order__created_at__gte=day_start,
                order__created_at__lt=day_end
            )
            label = day_start.strftime('%b %d')
        
        revenue = day_orders.aggregate(
            total=Sum(F('price') * F('quantity'), default=0)
        )['total'] or 0
        orders = day_orders.count()
        
        revenue_data.insert(0, revenue)
        orders_data.insert(0, orders)
        labels.insert(0, label)
    
    revenue_orders_trend_data = {
        'labels': labels,
        'datasets': [
            {
                'label': 'Revenue',
                'data': revenue_data,
                'borderColor': '#4CAF50',
                'backgroundColor': 'rgba(76, 175, 80, 0.1)',
                'yAxisID': 'y',
                'tension': 0.4,
            },
            {
                'label': 'Orders',
                'data': orders_data,
                'borderColor': '#2196F3',
                'backgroundColor': 'rgba(33, 150, 243, 0.1)',
                'yAxisID': 'y1',
                'tension': 0.4,
            }
        ]
    }
    
    # Store performance distribution
    store_performance = []
    for store in stores:
        store_revenue = orders_qs.filter(
            listing__store=store
        ).aggregate(total=Sum(F('price') * F('quantity'), default=0))['total'] or 0
        store_performance.append({
            'name': store.name,
            'revenue': store_revenue,
            'store': store
        })
    
    store_performance.sort(key=lambda x: x['revenue'], reverse=True)
    
    store_performance_data = {
        'labels': [s['name'][:15] + '...' if len(s['name']) > 15 else s['name'] 
                   for s in store_performance[:5]],
        'datasets': [{
            'data': [s['revenue'] for s in store_performance[:5]],
            'backgroundColor': [
                '#FF6384', '#36A2EB', '#FFCE56', '#4BC0C0', '#9966FF'
            ]
        }]
    }
    
    # Top performing stores
    top_stores = []
    for store in stores:
        store_orders = orders_qs.filter(listing__store=store)
        store_revenue = store_orders.aggregate(
            total=Sum(F('price') * F('quantity'), default=0)
        )['total'] or 0
        
        # Calculate average rating
        store_ratings = Review.objects.filter(
            seller=store.owner
        ).aggregate(avg_rating=Avg('rating'))
        avg_rating = store_ratings['avg_rating'] or 0
        
        top_stores.append({
            'name': store.name,
            'slug': store.slug,
            'revenue': store_revenue,
            'orders': store_orders.count(),
            'rating': round(avg_rating, 1)
        })
    
    top_stores.sort(key=lambda x: x['revenue'], reverse=True)
    
    # Top categories
    top_categories = []
    categories = Category.objects.filter(
        listing__store__in=stores
    ).distinct()
    
    for category in categories:
        category_orders = orders_qs.filter(
            listing__category=category
        )
        revenue = category_orders.aggregate(
            total=Sum(F('price') * F('quantity'), default=0)
        )['total'] or 0
        
        top_categories.append({
            'name': category.name,
            'revenue': revenue,
            'orders': category_orders.count(),
            'listings': Listing.objects.filter(
                store__in=stores,
                category=category,
                is_active=True
            ).count()
        })
    
    top_categories.sort(key=lambda x: x['revenue'], reverse=True)
    top_categories = top_categories[:5]
    
    # Recent activity across all stores
    recent_activity = []
    
    # Recent orders
    recent_orders = Order.objects.filter(
        order_items__listing__store__in=stores
    ).distinct().order_by('-created_at')[:5]

    for order in recent_orders:
        # `order.items.first()` returns a `Listing` instance (not a wrapper),
        # so access `.store` directly. Guard against missing relationships.
        store_name = "Unknown Store"
        items_qs = getattr(order, 'order_items', None) or getattr(order, 'items', None)
        if items_qs and items_qs.exists():
            first_listing = items_qs.first()
            if first_listing and getattr(first_listing, 'store', None):
                store_name = first_listing.store.name

        recent_activity.append({
            'timestamp': order.created_at,
            'store': store_name,
            'type': 'Order',
            'description': f'New order #{order.id if hasattr(order, "id") else "N/A"} from {order.user.username if order.user else "Unknown"}'
        })
    
    # Recent reviews
    recent_reviews = Review.objects.filter(
        seller=request.user
    ).order_by('-date_created')[:5]
    
    for review in recent_reviews:
        store_name = "Unknown Store"
        if review.seller and hasattr(review.seller, 'stores'):
            if review.seller.stores.exists():
                first_store = review.seller.stores.first()
                if first_store:
                    store_name = first_store.name
        
        recent_activity.append({
            'timestamp': review.created_at,
            'store': store_name,
            'type': 'Review',
            'description': f'{review.rating}★ review by {review.user.username if review.user else "Unknown"}'
        })
    
    # Recent listings
    recent_listings = Listing.objects.filter(
        store__in=stores
    ).order_by('-date_created')[:5]
    
    for listing in recent_listings:
        recent_activity.append({
            'timestamp': listing.date_created,
            'store': listing.store.name if listing.store else "Unknown Store",
            'type': 'Listing',
            'description': f'New listing: {listing.title}'
        })
    
    # Sort by timestamp and limit
    recent_activity.sort(key=lambda x: x['timestamp'], reverse=True)
    recent_activity = recent_activity[:10]
    
    # Customer location data
    customer_locations = orders_qs.values(
        'order__city'
    ).annotate(
        count=Count('id')
    ).order_by('-count')[:5]
    
    customer_map_data = {
        'labels': [loc['order__city'] or 'Unknown' for loc in customer_locations],
        'datasets': [{
            'data': [loc['count'] for loc in customer_locations],
            'backgroundColor': [
                '#FF6384', '#36A2EB', '#FFCE56', '#4BC0C0', '#9966FF'
            ]
        }]
    }
    
    context = {
        'period': period,
        'total_revenue': current_revenue,
        'total_orders': current_order_count,
        'revenue_trend': round(revenue_trend, 1),
        'orders_trend': round(orders_trend, 1),
        'active_stores': active_stores,
        'premium_stores': premium_stores,
        'active_listings': active_listings,
        'revenue_orders_trend_data': dumps_with_decimals(revenue_orders_trend_data),
        'store_performance_data': dumps_with_decimals(store_performance_data),
        'top_stores': top_stores[:5],
        'top_categories': top_categories,
        'recent_activity': recent_activity,
        'customer_map_data': dumps_with_decimals(customer_map_data)
    }
    # Add analytics access level and store list for advanced analytics actions
    context.update({
        'analytics_level': analytics_level,
        'stores': stores,
    })
    
    return render(request, 'storefront/seller_analytics.html', context)


@login_required
@analytics_access_required(level='basic')
@store_owner_required
def store_analytics(request, slug):
    """Store analytics view with comprehensive metrics and visualizations."""
    store = get_object_or_404(Store, slug=slug)
    
    # Get time period from query params
    period = request.GET.get('period', '24h')
    time_period = None
    
    end_date = timezone.now()
    if period == '24h':
        time_period = end_date - timedelta(hours=24)
        interval = 'hour'
        trend_days = 24
    elif period == '7d':
        time_period = end_date - timedelta(days=7)
        interval = 'day'
        trend_days = 7
    elif period == '30d':
        time_period = end_date - timedelta(days=30)
        interval = 'day'
        trend_days = 30
    else:
        time_period = None
        interval = 'month'
        trend_days = 12
    
    # Base queryset for the store's listings
    listings_qs = Listing.objects.filter(store=store)
    orders_qs = OrderItem.objects.filter(
        listing__store=store
    )
    
    if time_period:
        # Use order created time for period filtering to include recent orders
        orders_qs = orders_qs.filter(order__created_at__gte=time_period)
    
    # Basic metrics
    revenue_result = orders_qs.aggregate(
        total=Sum(F('price') * F('quantity'), default=0)
    )
    revenue = revenue_result['total'] or 0
    
    orders_count = orders_qs.count()
    active_listings = listings_qs.filter(is_active=True).count()
    
    avg_order_result = orders_qs.aggregate(
        avg=Avg(F('price') * F('quantity'))
    )
    avg_order_value = avg_order_result['avg'] or 0
    
    # Revenue trend (daily/hourly data points)
    revenue_trend = []
    labels = []
    
    for i in range(trend_days):
        if interval == 'hour':
            hour_start = end_date - timedelta(hours=i)
            hour_end = hour_start + timedelta(hours=1)
            day_orders = orders_qs.filter(
                order__created_at__gte=hour_start,
                order__created_at__lt=hour_end
            )
            label = hour_start.strftime('%H:%M')
        else:  # day or month
            day_start = end_date - timedelta(days=i)
            day_end = day_start + timedelta(days=1)
            day_orders = orders_qs.filter(
                order__created_at__gte=day_start,
                order__created_at__lt=day_end
            )
            label = day_start.strftime('%b %d')
        
        day_revenue = day_orders.aggregate(
            total=Sum(F('price') * F('quantity'), default=0)
        )['total'] or 0
        
        revenue_trend.insert(0, day_revenue)
        labels.insert(0, label)
    
    revenue_trend_data = {
        'labels': labels,
        'datasets': [{
            'label': 'Revenue',
            'data': revenue_trend,
            'borderColor': '#4CAF50',
            'backgroundColor': 'rgba(76, 175, 80, 0.1)',
            'fill': True,
            'tension': 0.4
        }]
    }
    
    # Top categories by sales
    category_sales = orders_qs.values(
        'listing__category__name'
    ).annotate(
        total_sales=Count('id'),
        revenue=Sum(F('price') * F('quantity'))
    ).order_by('-total_sales')[:5]
    
    category_data = {
        'labels': [item['listing__category__name'] or 'Uncategorized' 
                   for item in category_sales],
        'datasets': [{
            'data': [item['total_sales'] for item in category_sales],
            'backgroundColor': [
                '#FF6384', '#36A2EB', '#FFCE56', '#4BC0C0', '#9966FF'
            ]
        }]
    }
    
    # Top performing products
    top_products = orders_qs.values(
        'listing__id',
        'listing__title'
    ).annotate(
        sales_count=Sum('quantity'),
        revenue=Sum(F('price') * F('quantity'))
    ).order_by('-revenue')[:5]
    
    # Recent activity (orders, reviews, listings)
    recent_activity = []
    # Add recent orders for this store
    recent_orders_qs = Order.objects.filter(
        order_items__listing__store=store
    )
    if time_period:
        recent_orders_qs = recent_orders_qs.filter(created_at__gte=time_period)
    recent_orders = recent_orders_qs.distinct().order_by('-created_at')[:5]
    
    for order in recent_orders:
        recent_activity.append({
            'timestamp': order.created_at,
            'type': 'Order',
            'description': f'Order #{order.id if hasattr(order, "id") else "N/A"} - KSh {order.total_price if hasattr(order, "total_price") else 0} from {order.user.username if order.user else "Unknown"}'
        })
    
    # Add recent reviews
    recent_reviews = Review.objects.filter(
        seller=store.owner
    ).order_by('-date_created')[:5]
    
    for review in recent_reviews:
        recent_activity.append({
            'timestamp': review.date_created,
            'type': 'Review',
            'description': f'{review.rating}★ review by {review.user.username if review.user else "Unknown"}'
        })
    
    # Add recent listings
    recent_listings = listings_qs.order_by('-date_created')[:5]
    for listing in recent_listings:
        recent_activity.append({
            'timestamp': listing.date_created,
            'type': 'Listing',
            'description': f'New listing: {listing.title}'
        })
    
    # Sort combined activity by timestamp and limit — keep timestamps as datetimes
    recent_activity.sort(key=lambda x: x['timestamp'], reverse=True)
    recent_activity = recent_activity[:10]
    
    context = {
        'store': store,
        'period': period,
        'revenue': revenue,
        'orders_count': orders_count,
        'active_listings': active_listings,
        'avg_order_value': avg_order_value,
        'revenue_trend_data': dumps_with_decimals(revenue_trend_data),
        'category_data': dumps_with_decimals(category_data),
        'top_products': list(top_products),
        'recent_activity': recent_activity,
    }
    
    return render(request, 'storefront/store_analytics.html', context)


# ============== ANALYTICS API ENDPOINTS ==============

@login_required
@analytics_access_required(level='basic')
@require_GET
def seller_analytics_summary(request):
    """API endpoint for seller analytics summary (JSON)"""
    stores = Store.objects.filter(owner=request.user)
    
    # Get period from query params
    period = request.GET.get('period', '7d')
    time_period = timezone.now() - timedelta(days=7)
    
    if period == '24h':
        time_period = timezone.now() - timedelta(hours=24)
    elif period == '30d':
        time_period = timezone.now() - timedelta(days=30)
    
    # Get metrics
    orders_qs = OrderItem.objects.filter(
        listing__store__in=stores,
        order__status__in=['paid', 'delivered'],
        added_at__gte=time_period
    )
    
    revenue = orders_qs.aggregate(
        total=Sum(F('price') * F('quantity'), default=0)
    )['total'] or 0
    
    order_count = orders_qs.count()
    active_listings = Listing.objects.filter(
        store__in=stores,
        is_active=True
    ).count()
    
    active_stores = stores.filter(is_active=True).count()
    
    # Calculate week-over-week growth
    previous_period = time_period - (timezone.now() - time_period)
    previous_orders = OrderItem.objects.filter(
        listing__store__in=stores,
        order__status__in=['paid', 'delivered'],
        added_at__gte=previous_period,
        added_at__lt=time_period
    )
    
    previous_revenue = previous_orders.aggregate(
        total=Sum(F('price') * F('quantity'), default=0)
    )['total'] or 0
    
    revenue_growth = (
        ((revenue - previous_revenue) / previous_revenue * 100)
        if previous_revenue > 0 else (100 if revenue > 0 else 0)
    )
    
    # Top categories
    top_categories = Category.objects.filter(
        listing__store__in=stores
    ).annotate(
        revenue=Sum('listing__orderitem__price')
    ).order_by('-revenue')[:3]
    
    return JsonResponse({
        'success': True,
        'data': {
            'revenue': float(revenue),
            'order_count': order_count,
            'active_listings': active_listings,
            'active_stores': active_stores,
            'revenue_growth': round(revenue_growth, 1),
            'avg_order_value': float(revenue / order_count) if order_count > 0 else 0,
            'top_categories': [
                {
                    'name': cat.name,
                    'revenue': float(getattr(cat, 'revenue', 0) or 0)
                }
                for cat in top_categories
            ],
            'period': period,
            'last_updated': timezone.now().isoformat()
        }
    })

@login_required
@analytics_access_required(level='basic')
@store_owner_required
@require_GET
def store_analytics_summary(request, slug):
    """API endpoint for store analytics summary (JSON)"""
    store = get_object_or_404(Store, slug=slug)
    
    # Get period from query params
    period = request.GET.get('period', '7d')
    time_period = timezone.now() - timedelta(days=7)
    
    if period == '24h':
        time_period = timezone.now() - timedelta(hours=24)
    elif period == '30d':
        time_period = timezone.now() - timedelta(days=30)
    
    # Get metrics
    orders_qs = OrderItem.objects.filter(
        listing__store=store,
        order__status__in=['paid', 'delivered'],
        added_at__gte=time_period
    )
    
    revenue = orders_qs.aggregate(
        total=Sum(F('price') * F('quantity'), default=0)
    )['total'] or 0
    
    order_count = orders_qs.count()
    active_listings = Listing.objects.filter(
        store=store,
        is_active=True
    ).count()
    
    avg_order_value = revenue / order_count if order_count > 0 else 0
    
    # Views data (if available)
    views = store.total_views or 0
    
    # Conversion rate (orders/views) - SAFE CHECK FOR views > 0
    conversion_rate = (order_count / views * 100) if views > 0 else 0
    
    # Top products
    top_products = orders_qs.values(
        'listing__title',
        'listing__id'
    ).annotate(
        quantity=Sum('quantity'),
        revenue=Sum(F('price') * F('quantity'))
    ).order_by('-revenue')[:5]
    
    return JsonResponse({
        'success': True,
        'data': {
            'store_name': store.name,
            'revenue': float(revenue),
            'order_count': order_count,
            'active_listings': active_listings,
            'avg_order_value': float(avg_order_value),
            'views': views,
            'conversion_rate': round(conversion_rate, 2) if conversion_rate is not None else 0,
            'rating': store.get_rating() if hasattr(store, 'get_rating') else 0,
            'review_count': store.reviews.count() if hasattr(store, 'reviews') else 0,
            'top_products': list(top_products),
            'period': period,
            'last_updated': timezone.now().isoformat()
        }
    })

@login_required
@analytics_access_required(level='basic')
@require_GET
def revenue_trend_data(request):
    """API endpoint for revenue trend data (JSON)"""
    stores = Store.objects.filter(owner=request.user)
    store_slug = request.GET.get('store')
    
    # Filter by specific store if provided
    if store_slug:
        stores = stores.filter(slug=store_slug)
    
    # Get period from query params
    period = request.GET.get('period', '7d')
    
    # Calculate date range and interval
    end_date = timezone.now()
    if period == '24h':
        start_date = end_date - timedelta(hours=24)
        interval = 'hour'
        points = 24
    elif period == '30d':
        start_date = end_date - timedelta(days=30)
        interval = 'day'
        points = 30
    else:  # 7d
        start_date = end_date - timedelta(days=7)
        interval = 'day'
        points = 7
    
    # Generate data points
    labels = []
    revenue_data = []
    orders_data = []
    
    for i in range(points):
        if interval == 'hour':
            point_start = end_date - timedelta(hours=i+1)
            point_end = end_date - timedelta(hours=i)
            label = point_start.strftime('%H:%M')
        else:  # day
            point_start = end_date - timedelta(days=i+1)
            point_end = end_date - timedelta(days=i)
            label = point_start.strftime('%b %d')
        
        # Get data for this time period
        orders = OrderItem.objects.filter(
            listing__store__in=stores,
            order__status__in=['paid', 'delivered'],
            added_at__gte=point_start,
            added_at__lt=point_end
        )
        
        revenue = orders.aggregate(
            total=Sum(F('price') * F('quantity'), default=0)
        )['total'] or 0
        
        orders_count = orders.count()
        
        labels.insert(0, label)
        revenue_data.insert(0, float(revenue))
        orders_data.insert(0, orders_count)
    
    # Calculate totals and averages
    total_revenue = sum(revenue_data)
    total_orders = sum(orders_data)
    avg_daily_revenue = total_revenue / len(revenue_data) if revenue_data else 0
    
    return JsonResponse({
        'success': True,
        'data': {
            'labels': labels,
            'revenue': revenue_data,
            'orders': orders_data,
            'total_revenue': total_revenue,
            'total_orders': total_orders,
            'avg_daily_revenue': avg_daily_revenue,
            'period': period,
            'interval': interval
        }
    })

@login_required
@analytics_access_required(level='basic')
@require_GET
def store_performance_comparison(request):
    """API endpoint for comparing performance across stores"""
    stores = Store.objects.filter(owner=request.user)
    
    store_data = []
    for store in stores:
        orders = OrderItem.objects.filter(
            listing__store=store,
            order__status__in=['paid', 'delivered'],
            added_at__gte=timezone.now() - timedelta(days=30)
        )
        
        revenue = orders.aggregate(
            total=Sum(F('price') * F('quantity'), default=0)
        )['total'] or 0
        
        order_count = orders.count()
        avg_order_value = revenue / order_count if order_count > 0 else 0
        
        # Get reviews
        reviews = Review.objects.filter(seller=store.owner)
        avg_rating = reviews.aggregate(avg=Avg('rating'))['avg'] or 0
        
        store_data.append({
            'name': store.name,
            'slug': store.slug,
            'revenue': float(revenue),
            'orders': order_count,
            'avg_order_value': float(avg_order_value),
            'rating': round(avg_rating, 1),
            'review_count': reviews.count(),
            'listings': Listing.objects.filter(store=store, is_active=True).count(),
            'is_premium': store.is_premium
        })
    
    # Sort by revenue
    store_data.sort(key=lambda x: x['revenue'], reverse=True)
    
    return JsonResponse({
        'success': True,
        'data': {
            'stores': store_data,
            'total_stores': len(store_data),
            'total_revenue': sum(s['revenue'] for s in store_data),
            'total_orders': sum(s['orders'] for s in store_data),
            'last_updated': timezone.now().isoformat()
        }
    })


# Store Review Views

@login_required
def store_review_create(request, slug):
    """
    Create or update a store review
    """
    store = get_object_or_404(Store, slug=slug)
    
    # Check if user owns the store
    if store.owner == request.user:
        messages.error(request, "You cannot review your own store.")
        return redirect('storefront:store_detail', slug=slug)
    
    # Check if user already reviewed
    existing_review = StoreReview.objects.filter(store=store, reviewer=request.user).first()

    # The site has two Review models: reviews.Review (seller-level) and listings.Review (listing-level).
    # Ensure we check product/listing reviews using the listings app Review model.
    try:
        from listings.models import Review as ListingReview
        has_product_review = ListingReview.objects.filter(
            listing__store=store,
            user=request.user
        ).exists()
    except Exception:
        # Fallback: if listings.Review is unavailable, fall back to seller-level reviews
        has_product_review = Review.objects.filter(
            seller=store.owner,
            reviewer=request.user
        ).exists()
    
    if has_product_review and not existing_review:
        messages.info(request, "You've already reviewed products from this store. You can still leave a direct store review.")
    
    if request.method == 'POST':
        form = StoreReviewForm(request.POST, instance=existing_review)
        if form.is_valid():
            review = form.save(commit=False)
            review.store = store
            review.reviewer = request.user
            
            if existing_review:
                messages.success(request, "Your review has been updated.")
            else:
                messages.success(request, "Thank you for your review!")
            
            review.save()
            
            # Redirect back to the product page if coming from there
            next_url = request.GET.get('next')
            if next_url:
                return redirect(next_url)
            return redirect('storefront:store_reviews', slug=slug)
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        form = StoreReviewForm(instance=existing_review)
    
    context = {
        'store': store,
        'form': form,
        'existing_review': existing_review,
        'editing': bool(existing_review),
    }
    
    # If coming from product page, show product-specific template
    if request.GET.get('from') == 'product':
        return render(request, 'storefront/store_review_product.html', context)
    return render(request, 'storefront/store_review_form.html', context)



def store_reviews(request, slug):
    """
    Display all reviews for a store (both product reviews and direct store reviews)
    """
    store = get_object_or_404(Store, slug=slug)
    
    # Get page number from query params
    page = request.GET.get('page', 1)
    
    # Get paginated reviews
    reviews_page = store.get_all_reviews_paginated(page=page, per_page=10)
    
    # Calculate rating distribution for all reviews
    rating_distribution = defaultdict(int)
    all_reviews = store.get_all_reviews()
    
    for review in all_reviews:
        rating_distribution[review['rating']] += 1
    
    # Get average rating
    avg_rating = store.get_rating()
    
    # Check if user has reviewed
    user_has_reviewed = False
    user_review = None
    
    if request.user.is_authenticated:
        user_has_reviewed = store.has_user_reviewed(request.user)
        if user_has_reviewed:
            # Try to get user's direct store review
            user_review = StoreReview.objects.filter(store=store, reviewer=request.user).first()
    
    context = {
        'store': store,
        'reviews': reviews_page,
        'avg_rating': avg_rating,
        'rating_distribution': dict(sorted(rating_distribution.items())),
        'user_has_reviewed': user_has_reviewed,
        'user_review': user_review,
        'total_reviews': len(all_reviews),
    }
    
    return render(request, 'storefront/store_reviews.html', context)

@login_required
def store_review_update(request, slug, review_id):
    """
    Update an existing review
    """
    review = get_object_or_404(StoreReview, id=review_id, reviewer=request.user)
    store = review.store
    
    if request.method == 'POST':
        form = StoreReviewForm(request.POST, instance=review)
        if form.is_valid():
            form.save()
            messages.success(request, "Your review has been updated.")
            return redirect('storefront:store_reviews', slug=slug)
    else:
        form = StoreReviewForm(instance=review)
    
    context = {
        'store': store,
        'form': form,
        'review': review,
        'editing': True,
    }
    
    return render(request, 'storefront/store_review_form.html', context)


@login_required
def store_review_delete(request, slug, review_id):
    """
    Delete a review
    """
    review = get_object_or_404(StoreReview, id=review_id, reviewer=request.user)
    
    if request.method == 'POST':
        review.delete()
        messages.success(request, "Your review has been deleted.")
        return redirect('storefront:store_reviews', slug=slug)
    
    context = {
        'store': review.store,
        'review': review,
    }
    
    return render(request, 'storefront/store_review_confirm_delete.html', context)


@login_required
def mark_review_helpful(request, slug, review_id):
    """
    Mark a review as helpful (AJAX)
    """
    if request.method == 'POST' and request.headers.get('x-requested-with') == 'XMLHttpRequest':
        review = get_object_or_404(StoreReview, id=review_id)
        success = review.mark_helpful(request.user)
        
        return JsonResponse({
            'success': success,
            'helpful_count': review.helpful_count,
        })
    
    return JsonResponse({'success': False}, status=400)



@login_required
@store_owner_required
def subscription_plan_select(request, slug):
    """
    Select subscription plan before payment - FIXED VERSION
    """
    store = get_object_or_404(Store, slug=slug)
    
    # Check if already has active subscription (treat 'trialing' as active only if trial hasn't ended)
    active_subscription = Subscription.objects.filter(store=store).filter(
        Q(status='active') | Q(status='trialing', trial_ends_at__gt=timezone.now())
    ).first()

    if active_subscription and active_subscription.is_active():
        messages.info(request, "You already have an active subscription.")
        return redirect('storefront:subscription_manage', slug=slug)
    
    if request.method == 'POST':
        # Handle direct form submission
        plan_form = SubscriptionPlanForm(request.POST)
        upgrade_form = UpgradeForm(request.POST)
        
        if plan_form.is_valid() and upgrade_form.is_valid():
            # Store plan selection in session
            request.session['selected_plan'] = plan_form.cleaned_data['plan']
            request.session.modified = True  # Ensure session is saved
            
            # Get phone number from form
            phone_number = upgrade_form.cleaned_data['phone_number']
            
            # Redirect to payment page with parameters
            return redirect('storefront:store_upgrade', slug=slug)
        else:
            # Form validation failed
            messages.error(request, "Please correct the errors below.")
    else:
        plan_form = SubscriptionPlanForm()
        upgrade_form = UpgradeForm()
    
    # Plan details - MUST MATCH pricing in store_upgrade view
    plan_details = {
        'basic': {
            'price': 999,
            'features': [
                'Priority Listing',
                'Basic Analytics',
                'Store Customization',
                'Verified Badge',
                'Up to 50 products'
            ]
        },
        'premium': {
            'price': 1999,
            'features': [
                'Everything in Basic',
                'Advanced Analytics',
                'Bulk Product Upload',
                'Marketing Tools',
                'Up to 200 products',
                'Dedicated Support'
            ]
        },
        'enterprise': {
            'price': 4999,
            'features': [
                'Everything in Premium',
                'Custom Integrations',
                'API Access',
                'Unlimited Products',
                'Priority Support',
                'Custom Domain'
            ]
        }
    }
    
    context = {
        'store': store,
        'plan_form': plan_form,
        'upgrade_form': upgrade_form,
        'plan_details': plan_details,
    }
    
    return render(request, 'storefront/subscription_plan_select.html', context)



@login_required
@staff_member_required
def admin_subscription_list(request):
    """
    Admin view to list all subscriptions
    """
    subscriptions = Subscription.objects.all().order_by('-created_at')
    
    # Filter by status
    status_filter = request.GET.get('status')
    if status_filter:
        subscriptions = subscriptions.filter(status=status_filter)
    
    # Search
    search_query = request.GET.get('q')
    if search_query:
        subscriptions = subscriptions.filter(
            Q(store__name__icontains=search_query) |
            Q(store__owner__username__icontains=search_query) |
            Q(mpesa_phone__icontains=search_query)
        )
    
    context = {
        'subscriptions': subscriptions,
        'status_filter': status_filter,
        'search_query': search_query,
    }
    
    return render(request, 'storefront/admin_subscription_list.html', context)


@login_required
@staff_member_required
def admin_subscription_detail(request, subscription_id):
    """
    Admin view for subscription details
    """
    subscription = get_object_or_404(Subscription, id=subscription_id)
    payments = MpesaPayment.objects.filter(subscription=subscription).order_by('-created_at')
    
    context = {
        'subscription': subscription,
        'payments': payments,
    }
    
    return render(request, 'storefront/admin_subscription_detail.html', context)


@login_required
@store_owner_required
@require_GET
def store_views_analytics(request, slug):
    """Get store views analytics data"""
    store = get_object_or_404(Store, slug=slug)
    
    # You can implement more detailed analytics here
    # For example, views over time, comparison with other stores, etc.
    
    data = {
        'total_views': store.total_views,
        'store_name': store.name,
        'avg_daily_views': store.total_views / max((timezone.now() - store.created_at).days, 1),
        'rank': None,  # You can add ranking logic
    }
    
    return JsonResponse(data)

def popular_stores(request):
    """Display stores sorted by popularity (views)"""
    stores = Store.objects.filter(is_active=True).order_by('-total_views')
    
    # Filter by category or other criteria if needed
    category = request.GET.get('category')
    if category:
        stores = stores.filter(listings__category__slug=category).distinct()
    
    context = {
        'stores': stores,
        'title': 'Most Popular Stores',
    }
    
    return render(request, 'storefront/popular_stores.html', context)


@require_POST
@login_required
@store_owner_required
def undo_movement(request, slug, movement_id):
    """Undo a stock movement"""
    try:
        movement = StockMovement.objects.get(
            id=movement_id,
            product__store__slug=slug,
            product__store__owner=request.user
        )
        
        # Reverse the movement
        movement.product.stock = movement.previous_stock
        movement.product.save()
        
        # Create undo movement record
        StockMovement.objects.create(
            product=movement.product,
            movement_type='adjustment',
            quantity=movement.quantity * -1,
            previous_stock=movement.new_stock,
            new_stock=movement.previous_stock,
            created_by=request.user,
            reference=f'Undo of movement #{movement.id}',
            notes=f'Undoing movement from {movement.created_at}'
        )
        
        return JsonResponse({'success': True})
    except StockMovement.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Movement not found'}, status=404)

@require_GET
@login_required
@store_owner_required
def get_movement_details(request, slug, movement_id):
    #"""Get detailed movement information"""
    try:
        movement = StockMovement.objects.get(
            id=movement_id,
            product__store__slug=slug,
            product__store__owner=request.user
        )
        # type: StockMovementModel
        
        # Get movement type display safely
        movement_type_display = ''
        if hasattr(movement, 'get_movement_type_display'):
            movement_type_display = movement.get_movement_type_display()
        
        # Get image URL safely
        product_image = ''
        if movement.product and hasattr(movement.product, 'image'):
            if movement.product.image:
                product_image = movement.product.image.url if hasattr(movement.product.image, 'url') else ''
        
        data = {
            'id': movement.id,
            'product_title': movement.product.title,
            'product_sku': movement.product.sku,
            'product_image': product_image,
            'movement_type': movement.movement_type,
            'movement_type_display': movement_type_display,
            'quantity': movement.quantity,
            'previous_stock': movement.previous_stock,
            'new_stock': movement.new_stock,
            'reference': movement.reference,
            'notes': movement.notes,
            'changed_by': movement.created_by.username if movement.created_by else 'System',
            'created_at_formatted': movement.created_at.strftime('%b %d, %Y %H:%M')
        }
        
        return JsonResponse(data)
    except StockMovement.DoesNotExist:
        return JsonResponse({'error': 'Movement not found'}, status=404)

# Additional analytics API endpoints
@login_required
@analytics_access_required(level='basic')
@require_GET
def category_performance(request):
    """API endpoint for category performance analysis"""
    stores = Store.objects.filter(owner=request.user)
    
    categories = Category.objects.filter(
        listing__store__in=stores
    ).distinct().annotate(
        revenue=Sum('listing__orderitem__price'),
        orders=Count('listing__orderitem'),
        listings=Count('listing', filter=Q(listing__is_active=True))
    ).order_by('-revenue')
    
    category_data = []
    for category in categories:
        revenue = getattr(category, 'revenue', 0) or 0
        if revenue:
            category_data.append({
                'name': category.name,
                'revenue': float(revenue),
                'orders': getattr(category, 'orders', 0) or 0,
                'listings': getattr(category, 'listings', 0) or 0,
                'avg_order_value': float(revenue / getattr(category, 'orders', 1)) if getattr(category, 'orders', 0) else 0,
                'conversion_rate': (getattr(category, 'orders', 0) / getattr(category, 'listings', 1) * 100) if getattr(category, 'listings', 0) else 0
            })
    
    return JsonResponse({
        'success': True,
        'data': {
            'categories': category_data,
            'total_categories': len(category_data),
            'period': '30d'
        }
    })

@login_required
@analytics_access_required(level='enterprise')
@require_GET
def customer_insights(request):
    """API endpoint for customer insights"""
    stores = Store.objects.filter(owner=request.user)
    stores_ids = list(stores.values_list('id', flat=True))
    
    # Customer demographics
    customers = Order.objects.filter(
        order_items__listing__store_id__in=stores_ids,
        status__in=['paid', 'delivered']
    ).values('user__id', 'user__username').distinct()
    
    # Repeat customers
    repeat_customers = Order.objects.filter(
        order_items__listing__store_id__in=stores_ids,
        status__in=['paid', 'delivered']
    ).values('user__id').annotate(
        order_count=Count('id'),
        total_spent=Sum('total_price')
    ).filter(order_count__gt=1)
    
    # Customer locations
    customer_locations = Order.objects.filter(
        order_items__listing__store_id__in=stores_ids,
        status__in=['paid', 'delivered']
    ).exclude(city__isnull=True).values('city').annotate(
        customer_count=Count('user__id', distinct=True),
        order_count=Count('id'),
        total_revenue=Sum('total_price')
    ).order_by('-total_revenue')[:10]
    
    # Prepare serializable data
    total_customers = customers.count()
    repeat_count = repeat_customers.count()
    customer_locations_list = list(customer_locations)
    top_spenders = list(repeat_customers.order_by('-total_spent')[:5])
    avg_customer_value = (sum([c['total_spent'] for c in repeat_customers]) / repeat_count) if repeat_count else 0

    # Prefer JSON by default for API endpoints. Only render HTML when
    # explicitly requested via `format=html` or Accept header contains text/html.
    accept = request.headers.get('accept', '')
    want_html = request.GET.get('format') == 'html' or 'text/html' in accept

    if want_html:
        context = {
            'total_customers': total_customers,
            'repeat_customers': repeat_count,
            'repeat_customer_rate': round((repeat_count / total_customers * 100), 2) if total_customers else 0,
            'customer_locations': customer_locations_list,
            'avg_customer_value': avg_customer_value,
            'top_spenders': top_spenders,
            'period': request.GET.get('period', '30d')
        }
        return render(request, 'storefront/analytics/customer_insights.html', context)

    return JsonResponse({
        'success': True,
        'data': {
            'total_customers': total_customers,
            'repeat_customers': repeat_count,
            'repeat_customer_rate': (repeat_count / total_customers * 100) if total_customers else 0,
            'customer_locations': customer_locations_list,
            'avg_customer_value': avg_customer_value,
            'top_spenders': top_spenders
        }
    })

@login_required
@analytics_access_required(level='enterprise')
@store_owner_required
@require_GET
def product_performance(request, slug):
    """API endpoint for product performance analysis"""
    store = get_object_or_404(Store, slug=slug)
    
    # Get products with performance metrics
    products = Listing.objects.filter(
        store=store,
        is_active=True
    ).annotate(
        revenue=Sum('orderitem__price'),
        orders=Count('orderitem'),
        quantity_sold=Sum('orderitem__quantity')
    ).order_by('-revenue')[:20]
    
    product_data = []
    for product in products:
        revenue = getattr(product, 'revenue', 0) or 0
        if revenue:
            category_name = 'Uncategorized'
            if hasattr(product, 'category') and product.category:
                category_name = getattr(product.category, 'name', 'Uncategorized')
            
            product_data.append({
                'id': product.id,
                'title': product.title,
                'price': float(product.price),
                'stock': product.stock,
                'revenue': float(revenue),
                'orders': getattr(product, 'orders', 0) or 0,
                'quantity_sold': getattr(product, 'quantity_sold', 0) or 0,
                'avg_order_quantity': (getattr(product, 'quantity_sold', 0) / getattr(product, 'orders', 1)) if getattr(product, 'orders', 0) else 0,
                'stock_health': (product.stock / getattr(product, 'quantity_sold', 1) * 100) if getattr(product, 'quantity_sold', 0) else 100,
                'category': category_name
            })
    
    # Calculate inventory metrics
    total_products = Listing.objects.filter(store=store).count()
    out_of_stock = Listing.objects.filter(store=store, stock=0).count()
    low_stock = Listing.objects.filter(store=store, stock__lt=10, stock__gt=0).count()
    
    # Prepare serializable data
    performance_metrics = {
        'total_revenue': sum(p['revenue'] for p in product_data),
        'total_orders': sum(p['orders'] for p in product_data),
        'total_quantity_sold': sum(p['quantity_sold'] for p in product_data),
        'avg_product_revenue': sum(p['revenue'] for p in product_data) / len(product_data) if product_data else 0
    }

    inventory_metrics = {
        'total_products': total_products,
        'out_of_stock': out_of_stock,
        'low_stock': low_stock,
        'in_stock_rate': ((total_products - out_of_stock) / total_products * 100) if total_products else 0
    }

    # Prefer JSON by default for API endpoints. Only render HTML when
    # explicitly requested via `format=html` or Accept header contains text/html.
    accept = request.headers.get('accept', '')
    want_html = request.GET.get('format') == 'html' or 'text/html' in accept

    if want_html:
        context = {
            'store': store,
            'products': product_data,
            'inventory_metrics': inventory_metrics,
            'performance_metrics': performance_metrics,
            'period': request.GET.get('period', '30d')
        }
        return render(request, 'storefront/analytics/product_performance.html', context)

    return JsonResponse({
        'success': True,
        'data': {
            'products': product_data,
            'inventory_metrics': inventory_metrics,
            'performance_metrics': performance_metrics
        }
    })


@login_required
@require_POST
def request_withdrawal(request, slug):
    store = get_object_or_404(Store, slug=slug)
    if store.owner != request.user:
        return HttpResponseForbidden('Not authorized')

    amount_raw = request.POST.get('amount')
    try:
        amount = Decimal(amount_raw)
    except Exception:
        messages.error(request, 'Invalid amount')
        return redirect('storefront:payment_monitor')

    # Enforce payout phone verification
    if not store.payout_verified or not store.payout_phone:
        messages.error(request, 'Please add and verify your payout phone before requesting withdrawals.')
        return redirect('storefront:payment_monitor')

    if amount < WithdrawalRequest.MIN_WITHDRAWAL:
        messages.error(request, f'Withdrawal must be at least KSh {WithdrawalRequest.MIN_WITHDRAWAL}')
        return redirect('storefront:payment_monitor')

    from listings.mpesa_utils import MpesaGateway
    gateway = MpesaGateway()
    mpesa_phone = store.payout_phone
    resp = gateway.initiate_b2c_payout(mpesa_phone, amount, remarks='Seller Withdrawal', occasion=f'STORE-{store.id}')

    wr = WithdrawalRequest.objects.create(
        store=store,
        amount=amount,
        mpesa_phone=mpesa_phone,
        mpesa_reference=resp.get('originator_conversation_id', ''),
        mpesa_conversation_id=resp.get('conversation_id', ''),
        mpesa_status='processed' if resp.get('simulated') else 'initiated',
        mpesa_response=resp,
        status='processed' if resp.get('simulated') else 'pending',
        processed_at=timezone.now() if resp.get('simulated') else None,
    )

    if resp.get('success'):
        messages.success(request, f'Withdrawal request for KSh {amount:,.0f} submitted. M-Pesa transfer is being processed.')
    else:
        wr.status = 'failed'
        wr.save(update_fields=['status'])
        messages.error(request, f'Withdrawal initiation failed: {resp.get("error", "Unknown error")}')

    return redirect('storefront:payment_monitor')
