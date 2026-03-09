# listings/views.py
from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.views.generic import ListView, DetailView, CreateView, UpdateView, DeleteView
from django.db.models import Q, Count, Avg, F
from django.db import utils as db_utils
from django.db import IntegrityError, OperationalError
from django.conf import settings
from .models import Listing, Category, Favorite, Activity, RecentlyViewed, Review, Order, OrderItem, Cart, CartItem, Payment, Escrow, ListingImage
from chats.models import Message
from .forms import ListingForm, AIListingForm
from storefront.models import Store
from django.contrib.auth import get_user_model
from django.contrib import messages
from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.core.paginator import Paginator
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.db.models import Q, Case, When, Value
from blog.models import BlogPost
from django.http import JsonResponse, HttpResponse
import json
from .decorators import ajax_required
from storefront.models import Store, Subscription
from django.utils import timezone
from django.db.models import Subquery, OuterRef
from django.db.models.functions import Coalesce
from django.utils.decorators import method_decorator
from storefront.decorators import listing_limit_check
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from .models import NewsletterSubscription


from notifications.utils import (
    notify_order_created, notify_order_shipped, notify_order_delivered,
    notify_payment_received, notify_listing_favorited, notify_new_review,
    notify_delivery_assigned, notify_delivery_confirmed, notify_delivery_status
)


User = get_user_model()

import logging

logger = logging.getLogger(__name__)

# In your views.py - Update the ListingListView
from django.db.models import Count, Q
from django.utils import timezone
from datetime import timedelta


# Add this helper function near the top of views.py
def _assign_store_to_listing(listing, user):
    """Helper function to assign a store to a listing"""
    from storefront.models import Store
    
    if listing.store:
        return listing.store
    
    # Try to get user's stores
    user_stores = Store.objects.filter(owner=user)
    
    if user_stores.exists():
        # Use the first store or a specific one based on business logic
        return user_stores.first()
    
    # Create a default store if none exists
    default_store = Store.objects.create(
        owner=user,
        name=f"{user.username}'s Store",
        slug=user.username,
        description=f"Default store for {user.username}"
    )
    
    return default_store

# In your listings/views.py - Updated ListingListView class
class ListingListView(ListView):
    model = Listing
    template_name = 'listings/home.html'
    context_object_name = 'listings'
    paginate_by = 12

    def get(self, request, *args, **kwargs):
        """Override get() to intercept AJAX requests early and return JSON."""
        # Check if this is an AJAX request for filters
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return self._handle_ajax_request(request, *args, **kwargs)
        
        # Normal HTML request
        return super().get(request, *args, **kwargs)

    def _handle_ajax_request(self, request, *args, **kwargs):
        """Handle AJAX filter requests and return JSON."""
        try:
            # Get the queryset using normal logic
            queryset = self.get_queryset()
            
            # Get paginator and page
            paginator = Paginator(queryset, self.paginate_by)
            page_num = request.GET.get('page', 1)
            page_obj = paginator.get_page(page_num)
            
            listings_data = []
            for listing in page_obj.object_list:
                try:
                    img_url = ''
                    if hasattr(listing, 'get_image_url') and callable(listing.get_image_url):
                        img_url = listing.get_image_url()
                    elif hasattr(listing, 'image') and listing.image:
                        img_url = listing.image.url
                    
                    loc_display = ''
                    if hasattr(listing, 'get_location_display') and callable(listing.get_location_display):
                        loc_display = listing.get_location_display()
                    else:
                        loc_display = listing.location
                    
                    listings_data.append({
                        'id': listing.id,
                        'title': listing.title,
                        'price': float(listing.price) if listing.price is not None else 0,
                        'image_url': img_url,
                        'category_id': listing.category.id if listing.category else None,
                        'category': listing.category.name if listing.category else None,
                        'category_icon': getattr(listing.category, 'icon', '') if listing.category else '',
                        'location': listing.location,
                        'location_name': loc_display,
                        'stock': listing.stock,
                        'is_featured': listing.is_featured,
                        'is_recent': getattr(listing, 'is_recent', False),
                        'is_sold': listing.is_sold,
                        'seller_id': listing.seller_id if hasattr(listing, 'seller_id') else (listing.seller.id if listing.seller else None),
                        'url': reverse('listing-detail', args=[listing.pk]) if listing.pk else '',
                        'is_favorited': getattr(listing, 'is_favorited', False),
                        'total_favorites': getattr(listing, 'total_favorites', 0),
                        'cart_quantity': getattr(listing, 'cart_quantity', 0),
                    })
                except Exception as e:
                    logger.warning(f"Error serializing listing {listing.id}: {e}")
                    continue

            pagination = {
                'total_count': paginator.count,
                'current_page': page_obj.number,
                'num_pages': paginator.num_pages,
                'has_next': page_obj.has_next(),
                'has_previous': page_obj.has_previous(),
                'next_page': page_obj.next_page_number() if page_obj.has_next() else None,
                'previous_page': page_obj.previous_page_number() if page_obj.has_previous() else None,
            }

            user_fav_count = 0
            if request.user.is_authenticated:
                try:
                    user_fav_count = Favorite.objects.filter(user=request.user).count()
                except Exception as e:
                    logger.warning(f"Error getting user favorite count: {e}")

            # Get cart info if authenticated
            cart_total = 0
            cart_item_count = 0
            if request.user.is_authenticated:
                try:
                    cart, _ = Cart.objects.get_or_create(user=request.user)
                    cart_total = cart.get_total_price()
                    cart_item_count = cart.items.count()
                except Exception as e:
                    logger.warning(f"Error getting cart: {e}")

            data = {
                'success': True,
                'listings': listings_data,
                'pagination': pagination,
                'is_authenticated': request.user.is_authenticated,
                'user_id': request.user.id if request.user.is_authenticated else None,
                'user_favorite_count': user_fav_count,
                'cart_total': cart_total,
                'cart_item_count': cart_item_count,
            }

            return JsonResponse(data)
        except Exception as e:
            logger.error(f"Error in _handle_ajax_request: {e}", exc_info=True)
            return JsonResponse({'success': False, 'error': str(e)}, status=500)

    def get_queryset(self):
        queryset = Listing.objects.filter(is_active=True).order_by('-date_created')
        
        # Search functionality
        query = self.request.GET.get('q')
        if query:
            queryset = queryset.filter(
                Q(title__icontains=query) | 
                Q(description__icontains=query) |
                Q(brand__icontains=query) |
                Q(model__icontains=query)
            )
        
        # Filter by location
        location = self.request.GET.get('location')
        if location:
            queryset = queryset.filter(location=location)
        
        # Filter by category
        category_id = self.request.GET.get('category')
        if category_id:
            queryset = queryset.filter(category__id=category_id)
        
        return queryset

    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # Essential counts for stats - UPDATED
        context['total_users'] = User.objects.count()
        context['total_listings'] = Listing.objects.filter(is_active=True).count()
        context['total_orders'] = Order.objects.count()  # All orders, not just delivered
        context['total_stores'] = Store.objects.filter(is_active=True).count()
        
        # Categories data - UPDATED
        context['categories'] = Category.objects.filter(is_active=True)[:34]

        stores = Store.objects.filter(is_active=True)

        context['featured_stores'] = []
        for store in stores:
            if store.can_be_featured():
                # Add calculated fields to store instance
                store.listing_count = store.listings.filter(is_active=True).count()
                store.average_rating = store.get_average_store_rating()
                store.product_count = store.listing_count  # Alias for template
                context['featured_stores'].append(store)
        
        # Sort by listing count and limit to 8
        context['featured_stores'].sort(key=lambda x: x.listing_count, reverse=True)
        context['featured_stores'] = context['featured_stores'][:8]
        
        # Featured categories (if your Category model has is_featured field)
        # If not, you can use categories with most listings as featured
        try:
            context['featured_categories'] = Category.objects.filter(
                is_active=True
            ).annotate(
                listing_count=Count('listing', filter=Q(listing__is_active=True))
            ).order_by('-listing_count')[:3]
        except:
            # Fallback if is_featured field doesn't exist
            context['featured_categories'] = Category.objects.filter(is_active=True)[:3]
        
        # Categories with listings count
        context['categories_with_listings'] = Category.objects.annotate(
            listing_count=Count('listing', filter=Q(listing__is_active=True))
        ).filter(listing_count__gt=0)[:6]
        
        # Listings data - ENHANCED with all the home function data
        # Featured listings (already existed)
        context['featured_listings'] = Listing.objects.filter(
            is_featured=True, 
            is_active=True,
            is_sold=False
        ).select_related('category', 'seller').order_by('-date_created')[:8]
        
        # Trending listings (based on favorite count as proxy for popularity)
        context['trending_listings'] = Listing.objects.filter(
            is_active=True,
            is_sold=False
        ).annotate(
            favorite_count=Count('favorites')
        ).order_by('-favorite_count', '-date_created')[:8]
        
        # New arrivals (similar to existing but properly limited)
        context['new_arrivals'] = Listing.objects.filter(
            is_active=True,
            is_sold=False
        ).order_by('-date_created')[:8]
        
        # Top rated listings
        context['top_rated_listings'] = Listing.objects.filter(
            is_active=True,
            is_sold=False
        ).annotate(
            avg_rating=Avg('reviews__rating')
        ).filter(avg_rating__gte=4.0).order_by('-avg_rating')[:8]
        
        # Flash sale listings (listings with price discounts)
        context['flash_sale_listings'] = Listing.objects.filter(
            is_active=True,
            is_sold=False
        ).exclude(original_price__isnull=True).filter(
            original_price__gt=F('price')
        ).order_by('-date_created')[:4]

        context['my_orders']= Order.objects.filter(user=self.request.user) if self.request.user.is_authenticated else None
        cart_items = {}
        cart_total = 0
        cart_item_count = 0
        if self.request.user.is_authenticated:
            try:
                cart, created = Cart.objects.get_or_create(user=self.request.user)
                cart_items = {str(item.listing.id): item.quantity for item in cart.items.all()}
                cart_total = cart.get_total_price()
                cart_item_count = cart.items.count()
            except Exception as e:
                print(f"Cart error in home view: {str(e)}")
                cart_total = 0
                cart_item_count = 0
        
        context['cart_items'] = cart_items
        context['cart_total'] = cart_total
        context['cart_item_count'] = cart_item_count
        
        # Add favorite count annotation to listings - NEW
        featured_listings_with_favorites = Listing.objects.filter(
            is_featured=True, 
            is_active=True,
            is_sold=False
        ).annotate(
            total_favorites=Count('favorites', distinct=True)
        ).order_by('-date_created')[:8]
        
        trending_listings_with_favorites = Listing.objects.filter(
            is_active=True,
            is_sold=False
        ).annotate(
            favorite_count=Count('favorites'),
            total_favorites=Count('favorites', distinct=True)
        ).order_by('-favorite_count', '-date_created')[:8]
        
        context['featured_listings'] = featured_listings_with_favorites
        context['trending_listings'] = trending_listings_with_favorites
        
        # New arrivals with favorites
        context['new_arrivals'] = Listing.objects.filter(
            is_active=True,
            is_sold=False
        ).annotate(
            total_favorites=Count('favorites', distinct=True)
        ).order_by('-date_created')[:8]
        
        # Get user favorites
        user_favorites = set()
        if self.request.user.is_authenticated:
            try:
                user_favorites = set(Favorite.objects.filter(
                    user=self.request.user
                ).values_list('listing_id', flat=True))
            except Exception as e:
                print(f"Favorites error: {str(e)}")
                user_favorites = set()
        
        context['user_favorites'] = user_favorites
        
        # Get user favorite count if authenticated
        user_favorite_count = 0
        if self.request.user.is_authenticated:
            user_favorite_count = Favorite.objects.filter(user=self.request.user).count()
        
        context['user_favorite_count'] = user_favorite_count
        
       # Replace this section in the ListingListView.get_context_data method:
        user_favorites = set()
        try:
            if self.request.user.is_authenticated:
                # Use the Favorite model directly
                qs = Favorite.objects.filter(user=self.request.user).values_list('listing__pk', flat=True)
                user_favorites = set(int(pk) for pk in qs if pk is not None)
        except (db_utils.OperationalError, db_utils.ProgrammingError, Exception) as exc:
            # Don't raise — log and continue with empty favorites.
            logger.warning("Could not load user favorites: %s", exc)
            user_favorites = set()

        context['user_favorites'] = user_favorites
        # Existing functionality that should be preserved
        context['locations'] = Listing.HOMABAY_LOCATIONS
        
        # Popular categories with counts
        context['popular_categories'] = Category.objects.filter(
            is_active=True
        ).annotate(
            listing_count=Count('listing', filter=Q(listing__is_active=True))
        ).filter(listing_count__gt=0).order_by('-listing_count')[:8]
        
        # Add plan-related context for authenticated users
        if self.request.user.is_authenticated:
            from storefront.utils.plan_permissions import PlanPermissions
            context['plan_limits'] = PlanPermissions.get_plan_limits(self.request.user)
            context['plan_status'] = PlanPermissions.get_user_plan_status(self.request.user)
            context['can_create_store'] = PlanPermissions.can_create_store(self.request.user)
            context['can_create_listing'] = PlanPermissions.can_create_listing(self.request.user)
            # Add user's stores for subscription management URLs
            context['user_stores'] = Store.objects.filter(owner=self.request.user)[:1]  # Get first store if any
        
        # Recently viewed listings for authenticated users
        if self.request.user.is_authenticated:
            recently_viewed = RecentlyViewed.objects.filter(
                user=self.request.user
            ).select_related('listing').order_by('-viewed_at')[:6]
            context['recently_viewed'] = [rv.listing for rv in recently_viewed]

        # Brands for the homepage (distinct non-empty brand names)
        try:
            brands_qs = Listing.objects.filter(brand__isnull=False).exclude(brand__exact='').values_list('brand', flat=True).distinct()
            context['brands'] = list(brands_qs[:24])
        except Exception:
            context['brands'] = []

        # Recommended listings: personalized for authenticated users using recently viewed categories, fallback to trending
        try:
            recommended = Listing.objects.filter(is_active=True, is_sold=False)
            if self.request.user.is_authenticated:
                recent_cats = RecentlyViewed.objects.filter(user=self.request.user).values_list('listing__category', flat=True)[:3]
                recent_cats = [c for c in recent_cats if c]
                if recent_cats:
                    recommended = recommended.filter(category__in=recent_cats)
            # Exclude items already in recently viewed list for clarity
            if self.request.user.is_authenticated:
                rv_ids = RecentlyViewed.objects.filter(user=self.request.user).values_list('listing__id', flat=True)
                recommended = recommended.exclude(id__in=rv_ids)
            recommended = recommended.order_by('-date_created')[:8]
            if not recommended.exists():
                # Fallback to trending
                recommended = Listing.objects.filter(is_active=True, is_sold=False).annotate(favorite_count=Count('favorites')).order_by('-favorite_count')[:8]
            context['recommended_listings'] = recommended
        except Exception:
            context['recommended_listings'] = context.get('trending_listings', [])
        
        # Featured users
        context['featured_users'] = User.objects.annotate(
            listing_count=Count('listings', filter=Q(listings__is_active=True))
        ).filter(listing_count__gt=0).order_by('-listing_count')[:3]

        # Seller ratings (average and count) for each listing in the page
        seller_ratings = {}
        seller_reviews_count = {}
        for listing in context['listings']:
            seller = listing.seller
            reviews = Review.objects.filter(listing__seller=seller)
            avg_rating = reviews.aggregate(avg_rating=Avg('rating'))['avg_rating']
            seller_ratings[seller.id] = round(avg_rating, 1) if avg_rating else 0
            seller_reviews_count[seller.id] = reviews.count()
        context['seller_ratings'] = seller_ratings
        context['seller_reviews_count'] = seller_reviews_count

        
        context['latest_reviews'] = Review.objects.filter(
            is_public=True,
            comment__isnull=False,
            comment__gt=''
        ).select_related(
            'user', 'listing', 'seller'
        ).annotate(
            # Get reviewer's first name or username
            reviewer_name=Coalesce(
                Subquery(
                    User.objects.filter(pk=OuterRef('user_id')).values('first_name')[:1]
                ),
                Subquery(
                    User.objects.filter(pk=OuterRef('user_id')).values('username')[:1]
                ),
                Value('Anonymous')
            ),
            # Get review type display
            type_display=Case(
                When(review_type='listing', then=Value('Product')),
                When(review_type='seller', then=Value('Seller')),
                When(review_type='order', then=Value('Order')),
                default=Value('Review')
            )
        ).order_by('-created_at')[:3]

        # Prepare testimonial data
        testimonials = []
        for review in context['latest_reviews']:
            # Get user's profile picture or use default
            try:
                avatar_url = review.user.profile.avatar.url if hasattr(review.user, 'profile') else ''
            except:
                avatar_url = ''
            
            # Default avatar if none exists
            if not avatar_url:
                avatar_url = "https://ui-avatars.com/api/?name=" + review.reviewer_name + "&background=random&color=fff"
            
            # Prepare testimonial text
            if review.listing:
                context_text = f" about '{review.listing.title}'"
            elif review.seller:
                context_text = f" about {review.seller.username}"
            else:
                context_text = ""
            
            testimonials.append({
                'content': review.comment,
                'rating': review.rating,
                'author_name': review.reviewer_name,
                'author_title': f"Verified Buyer{context_text}",
                'avatar_url': avatar_url,
                'date': review.created_at.strftime('%B %Y'),
                'stars': '⭐' * int(review.rating),
                'type': review.type_display,
            })

        context['testimonials'] = testimonials

        # Also add review stats for the empty state
        context['total_reviews'] = Review.objects.filter(is_public=True).count()
        context['avg_rating'] = Review.objects.filter(is_public=True).aggregate(Avg('rating'))['rating__avg'] or 0
        context['has_reviews'] = context['total_reviews'] > 0

        
        # Delivered orders for review button
        if self.request.user.is_authenticated:
            context['delivered_orders'] = Order.objects.filter(user=self.request.user, status='delivered')
        else:
            context['delivered_orders'] = Order.objects.none()
        
        # Blog posts
        try:
            context['blog_posts'] = BlogPost.objects.filter(status="published").order_by('-published_at')[:3]
        except:
            context['blog_posts'] = []

        return context


@require_POST
@ajax_required
def newsletter_subscribe(request):
    """Simple AJAX endpoint to accept email subscriptions from the homepage form.

    Expects JSON payload: {email: 'user@example.com'} or form-encoded `email`.
    Returns JSON: {success: True, message: '...'}
    """
    data = {}
    try:
        if request.content_type == 'application/json':
            payload = json.loads(request.body.decode('utf-8') or '{}')
            email = payload.get('email')
        else:
            email = request.POST.get('email') or request.POST.get('newsletter_email')

        if not email:
            return JsonResponse({'success': False, 'error': 'Email is required.'}, status=400)

        # Basic validation
        try:
            validate_email(email)
        except ValidationError:
            return JsonResponse({'success': False, 'error': 'Invalid email address.'}, status=400)

        # Create or update subscription
        sub, created = NewsletterSubscription.objects.get_or_create(email=email, defaults={'source': 'homepage'})
        if created:
            message = 'Subscribed successfully.'
        else:
            message = 'Email already subscribed.'

        return JsonResponse({'success': True, 'message': message})
    except Exception as exc:
        logger.exception('Error in newsletter_subscribe: %s', exc)
        return JsonResponse({'success': False, 'error': 'Server error'}, status=500)

class ListingDetailView(DetailView):
    model = Listing
    template_name = 'listings/listing_detail.html'
    context_object_name = 'listing'

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)

        # Track recently viewed for authenticated users (avoid update_or_create to reduce DB locking on SQLite)
        if self.request.user.is_authenticated:
            try:
                # Try to update existing record first (non-blocking)
                updated = RecentlyViewed.objects.filter(user=self.request.user, listing=obj).update(viewed_at=timezone.now())
                if not updated:
                    # If no rows updated, try to create; handle race with IntegrityError
                    try:
                        RecentlyViewed.objects.create(user=self.request.user, listing=obj, viewed_at=timezone.now())
                    except IntegrityError:
                        # Another process created it concurrently — update the timestamp
                        RecentlyViewed.objects.filter(user=self.request.user, listing=obj).update(viewed_at=timezone.now())
            except OperationalError as e:
                # DB locked or similar issue — log and continue without failing the view
                logger.warning('Could not record RecentlyViewed (DB issue): %s', e)

        return obj

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        listing = self.get_object()
        user = self.request.user

        # Get all reviews for the listing
        reviews = listing.reviews.select_related('user').all()
        context['reviews'] = reviews

        # Calculate average rating
        avg_rating = listing.reviews.aggregate(
            avg_rating=Avg('rating')
        )['avg_rating']
        context['avg_rating'] = round(avg_rating, 1) if avg_rating else 0

        # Calculate rating distribution
        rating_counts = {5: 0, 4: 0, 3: 0, 2: 0, 1: 0}
        for review in reviews:
            if 1 <= review.rating <= 5:
                rating_counts[review.rating] += 1

        total_reviews = reviews.count()
        rating_distribution = []
        for rating in [5, 4, 3, 2, 1]:
            count = rating_counts[rating]
            percentage = (count / total_reviews * 100) if total_reviews > 0 else 0
            rating_distribution.append({
                'rating': rating,
                'count': count,
                'percentage': percentage
            })

        context['rating_distribution'] = rating_distribution

        # Check if the current user has favorited this listing
        if user.is_authenticated:
            context['is_favorited'] = Favorite.objects.filter(
                user=user,
                listing=listing
            ).exists()
        else:
            context['is_favorited'] = False

        # Get similar listings
        context['similar_listings'] = Listing.objects.filter(
            category=listing.category,
            is_active=True,
            is_sold=False
        ).exclude(id=listing.id)[:6]

        # Get seller's other listings
        context['seller_other_listings'] = Listing.objects.filter(
            seller=listing.seller,
            is_active=True,
            is_sold=False
        ).exclude(id=listing.id)[:4]

        # Get seller statistics
        seller = listing.seller
        seller_listings = Listing.objects.filter(seller=seller, is_active=True)
        seller_reviews = Review.objects.filter(listing__seller=seller)

        context['seller_reviews_count'] = seller_reviews.count()
        seller_avg_rating = seller_reviews.aggregate(
            avg_rating=Avg('rating')
        )['avg_rating']
        context['seller_avg_rating'] = round(seller_avg_rating, 1) if seller_avg_rating else 0

        # Get FAQs for this listing
        context['faqs'] = listing.faqs.filter(is_active=True).order_by('order')

        # Get recently viewed for sidebar
        if user.is_authenticated:
            recently_viewed = RecentlyViewed.objects.filter(
                user=user
            ).exclude(listing=listing).select_related('listing').order_by('-viewed_at')[:4]
            context['recently_viewed_sidebar'] = [rv.listing for rv in recently_viewed]

        # Get price history
        context['price_history'] = listing.price_history.all()[:10]

        # Calculate price change and percentage for template
        if listing.original_price and listing.original_price != listing.price:
            price_change = listing.price - listing.original_price
            percentage = abs(price_change * 100 / listing.original_price)
            context['price_change'] = price_change
            context['price_change_percentage'] = round(percentage, 1)
        else:
            context['price_change'] = 0
            context['price_change_percentage'] = 0

        # Prepare dynamic fields display using category schema (label -> value), with group fallback
        try:
            dynamic_display = []
            schema = getattr(listing.category, 'fields_schema', None) or {}
            # if empty schema but category belongs to a group, try to pick group's schema
            if (not schema or schema == {}) and getattr(listing.category, 'schema_group', None):
                group = listing.category.schema_group
                # find another category in group with a schema
                fallback = Category.objects.filter(schema_group=group).exclude(fields_schema={}).first()
                if fallback and fallback.fields_schema:
                    schema = fallback.fields_schema
            fields = schema.get('fields', []) if isinstance(schema, dict) else []
            for fd in fields:
                name = fd.get('name')
                label = fd.get('label') or name
                val = None
                try:
                    val = listing.dynamic_fields.get(name)
                except Exception:
                    val = None
                if val is not None and val != '':
                    dynamic_display.append({'name': name, 'label': label, 'value': val, 'type': fd.get('type')})
            context['dynamic_fields_display'] = dynamic_display
        except Exception:
            context['dynamic_fields_display'] = []

        return context

    
@method_decorator(listing_limit_check, name='dispatch')
class ListingCreateView(LoginRequiredMixin, CreateView):
    model = Listing
    form_class = AIListingForm

    def dispatch(self, request, *args, **kwargs):
        # If the user is not authenticated, defer to LoginRequiredMixin's handling
        # (calling super() will let the mixin redirect to login).
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)

        # Check if user has any stores before allowing listing creation
        if not Store.objects.filter(owner=request.user).exists():
            messages.info(request, "You need to create a store first before you can list items for sale.")
            return redirect(reverse('storefront:store_create') + '?from=listing')

        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        user_store = Store.objects.filter(owner=self.request.user)
        try:
            context = super().get_context_data(**kwargs)
        except AttributeError:
            # Some mixin chains may call DetailView.get_context_data which expects
            # `self.object` to exist. For CreateView there is no `object` yet —
            # fall back to an empty context and populate what we need.
            context = {}
        # Add categories to context for the form
        context['categories'] = Category.objects.filter(is_active=True)
        # Provide category schemas for dynamic form rendering (id -> schema), with group fallback
        try:
            cats = Category.objects.filter(is_active=True).only('id', 'fields_schema', 'schema_group')
            group_map = {}
            for c in cats:
                if c.schema_group and c.fields_schema:
                    group_map[c.schema_group] = c.fields_schema
            category_schemas = {}
            for c in cats:
                if c.fields_schema:
                    category_schemas[str(c.id)] = c.fields_schema
                elif c.schema_group and c.schema_group in group_map:
                    category_schemas[str(c.id)] = group_map[c.schema_group]
                else:
                    category_schemas[str(c.id)] = {}
            context['category_schemas'] = category_schemas
        except Exception:
            context['category_schemas'] = {}
        # Provide initial dynamic fields for client-side prepopulation (create view: empty or from form)
        try:
            initial_dynamic = {}
            form = kwargs.get('form')
            if form and getattr(form.instance, 'dynamic_fields', None):
                initial_dynamic = form.instance.dynamic_fields
            context['initial_dynamic_fields'] = initial_dynamic
        except Exception:
            context['initial_dynamic_fields'] = {}
        # AI availability flag for templates
        try:
            from .ai_listing_helper import listing_ai
            context['ai_enabled'] = listing_ai.enabled
        except Exception:
            context['ai_enabled'] = False
        # Get user's stores for the store selector
        if self.request.user.is_authenticated:
            context['stores'] = user_store
        else:
            context['stores'] = Store.objects.none()
        return context

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        return kwargs

    def form_valid(self, form):
        from django.conf import settings
        from django.shortcuts import render, redirect

        # Use centralized plan permissions to decide if user can create a listing
        from storefront.utils.plan_permissions import PlanPermissions
        user_listing_count = Listing.objects.filter(seller=self.request.user, is_active=True).count()

        # Get or create the user's single storefront
        user_store = Store.objects.filter(owner=self.request.user).first()

        # If the form included an explicit store choice, prefer that (but ensure ownership)
        selected_store = None
        try:
            selected_store = form.cleaned_data.get('store')
        except Exception:
            selected_store = None

        if selected_store:
            # Ensure the selected store belongs to the current user
            if selected_store.owner != self.request.user:
                messages.error(self.request, "Invalid store selection.")
                return render(self.request, 'listings/listing_form.html', {'form': form, 'categories': Category.objects.filter(is_active=True), 'stores': Store.objects.filter(owner=self.request.user)})
            user_store = selected_store

        # If plan does not allow more listings, show upgrade prompt
        if not PlanPermissions.can_create_listing(self.request.user, user_store):
            limits = PlanPermissions.get_plan_limits(self.request.user, user_store)
            messages.warning(self.request, f"You've reached the listing limit ({limits.get('max_products')}) for your plan. Upgrade to add more listings.")
            return redirect('storefront:seller_dashboard')

        # If there's still no user_store (and no explicit selection), require the user to create or select a storefront.
        # We intentionally DO NOT auto-create a store here so that the user explicitly chooses where the listing should appear.
        if not user_store:
            messages.info(self.request, "You need to create a storefront before you can list items. Please create a store first.")
            return redirect(reverse('storefront:store_create') + '?from=listing')

        # Attach store to the listing instance
        form.instance.seller = self.request.user
        form.instance.store = user_store
        # Attempt to save form and handle Cloudinary upload time-skew errors gracefully
        try:
            response = super().form_valid(form)
        except Exception as e:
            # If Cloudinary reports a stale request (timestamp skew), show a friendly error
            try:
                from cloudinary import exceptions as cloud_ex
                if isinstance(e, cloud_ex.BadRequest) or ('Stale request' in str(e)):
                    logger.exception('Cloudinary BadRequest during listing create: %s', e)
                    messages.error(self.request, 'Image upload failed due to a timestamp mismatch with the image service. Please check your server clock or retry. If the problem persists, contact support.')
                    context = self.get_context_data(form=form)
                    return render(self.request, 'listings/listing_form.html', context)
            except Exception:
                # fall through to generic error handling
                pass
            # For any other exception, re-raise after logging so it surfaces in debug
            logger.exception('Unhandled exception during form_valid: %s', e)
            raise

        # Handle main image
        if 'image' in self.request.FILES:
            form.instance.image = self.request.FILES['image']
            form.instance.save()

        # Handle multiple image uploads for gallery
        images = self.request.FILES.getlist('images')
        for image in images:
            # Validate file type and size
            try:
                is_image = getattr(image, 'content_type', '').startswith('image/')
                size_ok = getattr(image, 'size', 0) <= 10 * 1024 * 1024
            except Exception:
                is_image = False
                size_ok = False
            if is_image and size_ok:
                # Defensive de-duplication: avoid creating duplicate gallery images
                try:
                    duplicate = False
                    # Compare by filename and size where possible
                    incoming_name = getattr(image, 'name', '')
                    incoming_size = getattr(image, 'size', None)
                    for existing in form.instance.images.all():
                        try:
                            existing_name = getattr(existing.image, 'name', '') or ''
                            existing_size = None
                            try:
                                existing_size = existing.image.size
                            except Exception:
                                existing_size = None
                            if incoming_name and existing_name and incoming_name in existing_name:
                                duplicate = True
                                break
                            if incoming_size is not None and existing_size is not None and incoming_size == existing_size:
                                duplicate = True
                                break
                        except Exception:
                            continue
                    if duplicate:
                        continue
                except Exception:
                    # On any error, fall back to attempting to create the image
                    pass

                ListingImage.objects.create(
                    listing=form.instance,
                    image=image
                )

        # Create activity log
        Activity.objects.create(
            user=self.request.user,
            action=f"Created listing: {form.instance.title}"
        )

        # In-app notification for listing creation
        try:
            from notifications.utils import notify_system_message
            try:
                action_url = form.instance.get_absolute_url()
            except Exception:
                action_url = ''
            notify_system_message(self.request.user, 'Listing Created', f'Your listing "{form.instance.title}" was created.', action_url=action_url)
        except Exception:
            logger.exception('Failed to create in-app notification for listing creation')

        messages.success(self.request, "Listing created successfully!")
        # Broadcast new listing to connected users so clients can refresh live
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            channel_layer = get_channel_layer()
            # Minimal listing payload
            # Build a small serializable payload for live listing broadcasts.
            # Ensure we do not include method objects (call methods when present).
            image_url = ''
            if hasattr(form.instance, 'get_image_url'):
                try:
                    img_callable = getattr(form.instance, 'get_image_url')
                    image_url = img_callable() if callable(img_callable) else str(img_callable)
                except Exception:
                    image_url = ''

            listing_data = {
                'id': form.instance.id,
                'title': form.instance.title,
                'price': str(form.instance.price),
                'image_url': image_url,
                'seller_name': form.instance.seller.get_full_name() or form.instance.seller.username,
                'total_favorites': int(getattr(form.instance, 'total_favorites', 0) or 0),
                'url': form.instance.get_absolute_url() if hasattr(form.instance, 'get_absolute_url') else '',
            }
            # Send to all users' notification groups (graceful fallback for small scale)
            from django.contrib.auth import get_user_model
            User = get_user_model()
            user_ids = list(User.objects.filter(is_active=True).values_list('id', flat=True))
            for uid in user_ids:
                async_to_sync(channel_layer.group_send)(
                    f'notifications_user_{uid}',
                    {
                        'type': 'listing_created',
                        'listing': listing_data,
                    }
                )
        except Exception:
            logger.exception('Failed to broadcast listing_created')
        return response

    def post(self, request, *args, **kwargs):
        """Handle AI-prefill before saving when user requests AI assistance."""
        # Prepare form with POST data
        form = self.get_form()

        # If user requested AI assist, generate suggestions and re-render the form
        use_ai = request.POST.get('use_ai') in ['on', 'true', '1']
        if use_ai:
            try:
                # Generate AI suggestions using the form helper
                ai_data = form.generate_with_ai()
                # Re-bind a form instance with current POST (mutable copy handled in generate_with_ai)
                form = self.get_form()

                context = self.get_context_data(form=form)
                context.update({
                    'ai_suggestions': ai_data,
                    'ai_used': True,
                    'quick_mode': request.POST.get('quick_mode') == '1'
                })
                return render(request, 'listings/listing_form.html', context)
            except Exception as e:
                logger.exception('AI generate failed: %s', e)
                messages.error(request, 'AI generation failed. Showing standard form.')

        # Fallback to default CreateView POST handling
        return super().post(request, *args, **kwargs)

# Update the ListingUpdateView class
class ListingUpdateView(LoginRequiredMixin, UserPassesTestMixin, UpdateView):
    model = Listing
    form_class = ListingForm  # Now uses the updated ListingForm

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['categories'] = Category.objects.filter(is_active=True)
        # Add existing images for display
        context['existing_images'] = self.object.images.all()
        # Get user's stores
        if self.request.user.is_authenticated:
            context['stores'] = Store.objects.filter(owner=self.request.user)
        else:
            context['stores'] = Store.objects.none()
        # Pass existing store for template
        context['current_store'] = self.object.store
        # Provide category schemas for dynamic form rendering (id -> schema), with group fallback
        try:
            cats = Category.objects.filter(is_active=True).only('id', 'fields_schema', 'schema_group')
            group_map = {}
            for c in cats:
                if c.schema_group and c.fields_schema:
                    group_map[c.schema_group] = c.fields_schema
            category_schemas = {}
            for c in cats:
                if c.fields_schema:
                    category_schemas[str(c.id)] = c.fields_schema
                elif c.schema_group and c.schema_group in group_map:
                    category_schemas[str(c.id)] = group_map[c.schema_group]
                else:
                    category_schemas[str(c.id)] = {}
            context['category_schemas'] = category_schemas
        except Exception:
            context['category_schemas'] = {}
        # Ensure the template has the listing's existing dynamic fields for prepopulation
        try:
            context['initial_dynamic_fields'] = self.object.dynamic_fields if getattr(self, 'object', None) and getattr(self.object, 'dynamic_fields', None) else {}
        except Exception:
            context['initial_dynamic_fields'] = {}
        return context
    
    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['user'] = self.request.user
        # Set initial store value
        if 'initial' not in kwargs:
            kwargs['initial'] = {}
        if self.object.store:
            kwargs['initial']['store'] = self.object.store
        return kwargs
    def test_func(self):
        listing = self.get_object()
        return self.request.user == listing.seller

    # Update the ListingUpdateView class form_valid method
    def form_valid(self, form):
        form.instance.seller = self.request.user
        
        # Handle store field - for updates, keep existing if not provided
        store = form.cleaned_data.get('store')
        if store:
            form.instance.store = store
        elif not form.instance.store:
            # If no store selected and no existing store, use user's first store
            user_store = Store.objects.filter(owner=self.request.user).first()
            if user_store:
                form.instance.store = user_store
            else:
                messages.warning(self.request, "No store selected. Please create a store first.")
                return redirect('storefront:store_create')
        
        # Handle main image update - only if new image provided
        if 'image' in self.request.FILES and self.request.FILES['image']:
            form.instance.image = self.request.FILES['image']
        elif 'image-clear' in self.request.POST:
            # Only clear if explicitly requested
            form.instance.image = None
        
        # Enforce is_featured rules
        try:
            if 'is_featured' in form.cleaned_data and form.cleaned_data.get('is_featured'):
                store = form.instance.store or form.cleaned_data.get('store')
                from storefront.models import Subscription
                from django.utils import timezone as _tz
                now = _tz.now()
                has_active = Subscription.objects.filter(store=store, status='active').exists() if store else False
                has_valid_trial = Subscription.objects.filter(
                    store=store, 
                    status='trialing', 
                    trial_ends_at__gt=now
                ).exists() if store else False
                if not (has_active or has_valid_trial):
                    form.instance.is_featured = False
                    messages.info(self.request, "Featured listings require an active subscription or valid trial. The featured flag was not applied.")
        except Exception:
            pass

        response = super().form_valid(form)
        
        # Handle multiple image uploads
        images = self.request.FILES.getlist('images')
        for image in images:
            try:
                is_image = getattr(image, 'content_type', '').startswith('image/')
                size_ok = getattr(image, 'size', 0) <= 10 * 1024 * 1024
            except Exception:
                is_image = False
                size_ok = False
            if is_image and size_ok:
                # Defensive de-duplication to prevent double uploads
                try:
                    duplicate = False
                    incoming_name = getattr(image, 'name', '')
                    incoming_size = getattr(image, 'size', None)
                    for existing in form.instance.images.all():
                        try:
                            existing_name = getattr(existing.image, 'name', '') or ''
                            existing_size = None
                            try:
                                existing_size = existing.image.size
                            except Exception:
                                existing_size = None
                            if incoming_name and existing_name and incoming_name in existing_name:
                                duplicate = True
                                break
                            if incoming_size is not None and existing_size is not None and incoming_size == existing_size:
                                duplicate = True
                                break
                        except Exception:
                            continue
                    if duplicate:
                        continue
                except Exception:
                    pass

                ListingImage.objects.create(
                    listing=form.instance,
                    image=image
                )
        
        # Handle image deletion
        if 'delete_images' in self.request.POST:
            delete_ids = self.request.POST.getlist('delete_images')
            if delete_ids:
                ListingImage.objects.filter(
                    id__in=delete_ids, 
                    listing=form.instance
                ).delete()
        
        # Create activity log
        Activity.objects.create(
            user=self.request.user,
            action=f"Updated listing: {form.instance.title}"
        )
        # In-app notification for listing update
        try:
            from notifications.utils import notify_system_message
            try:
                action_url = form.instance.get_absolute_url()
            except Exception:
                action_url = ''
            notify_system_message(self.request.user, 'Listing Updated', f'Your listing "{form.instance.title}" was updated.', action_url=action_url)
        except Exception:
            logger.exception('Failed to create in-app notification for listing update')
        
        messages.success(self.request, "Listing updated successfully!")
        return response
    
    def get_success_url(self):
        return reverse('listing-detail', kwargs={'pk': self.object.pk})
            
class ListingDeleteView(LoginRequiredMixin, UserPassesTestMixin, DeleteView):
    model = Listing
    success_url = '/'

    def test_func(self):
        listing = self.get_object()
        return self.request.user == listing.seller
    
    def delete(self, request, *args, **kwargs):
        listing = self.get_object()
        owner = listing.seller
        title = listing.title
        response = super().delete(request, *args, **kwargs)
        # Activity log and in-app notification for deletion
        try:
            Activity.objects.create(
                user=owner,
                action=f"Deleted listing: {title}"
            )
            from notifications.utils import notify_system_message
            notify_system_message(owner, 'Listing Deleted', f'Your listing "{title}" was deleted.', action_url='')
        except Exception:
            logger.exception('Failed to create in-app notification for listing deletion')
        return response


def all_listings(request):
    # Get all active listings
    listings = Listing.objects.filter(is_active=True)
    
    # Get filter parameters
    category_id = request.GET.get('category')
    location = request.GET.get('location')
    min_price = request.GET.get('min_price')
    max_price = request.GET.get('max_price')
    search_query = request.GET.get('q')
    sort_by = request.GET.get('sort', 'newest')
    featured = request.GET.get('featured')
    recent = request.GET.get('recent')
    instock = request.GET.get('instock', 'true')  # Default to true
    
    # Apply filters
    if category_id and category_id != 'all':
        listings = listings.filter(category__id=category_id)
    
    if location and location != 'all':
        listings = listings.filter(location=location)
    
    if min_price:
        try:
            listings = listings.filter(price__gte=float(min_price))
        except (ValueError, TypeError):
            pass
    
    if max_price:
        try:
            listings = listings.filter(price__lte=float(max_price))
        except (ValueError, TypeError):
            pass
    
    if search_query:
        listings = listings.filter(
            Q(title__icontains=search_query) | 
            Q(description__icontains=search_query) |
            Q(brand__icontains=search_query) |
            Q(model__icontains=search_query)
        )
    
    # Apply new filters
    if featured == 'true':
        listings = listings.filter(is_featured=True)
    
    if recent == 'true':
        one_week_ago = timezone.now() - timedelta(days=7)
        listings = listings.filter(date_created__gte=one_week_ago)
    
    if instock == 'true':
        listings = listings.filter(stock__gt=0)
    
    # Apply sorting
    if sort_by == 'price_low':
        listings = listings.order_by('price')
    elif sort_by == 'price_high':
        listings = listings.order_by('-price')
    elif sort_by == 'oldest':
        listings = listings.order_by('date_created')
    elif sort_by == 'featured':
        listings = listings.order_by('-is_featured', '-date_created')
    elif sort_by == 'popular':
        listings = listings.annotate(
            favorite_count=Count('favorites')
        ).order_by('-favorite_count', '-date_created')
    else:  # newest is default
        listings = listings.order_by('-date_created')
    
    # Get categories for filter dropdown
    categories = Category.objects.filter(is_active=True)
    
    # Get unique locations from listings
    from collections import defaultdict
    locations_count = defaultdict(int)
    for code, name in Listing.HOMABAY_LOCATIONS:
        count = Listing.objects.filter(location=code, is_active=True).count()
        if count > 0:
            locations_count[code] = count
    
    # Create locations list with counts
    locations = []
    for code, name in Listing.HOMABAY_LOCATIONS:
        if code in locations_count:
            locations.append({
                'code': code,
                'name': name,
                'count': locations_count[code]
            })
    
    # Get cart items for initial page load
    cart_items = {}
    cart_total = 0
    cart_item_count = 0
    
    if request.user.is_authenticated:
        try:
            # Get or create cart for user
            cart, created = Cart.objects.get_or_create(user=request.user)
            cart_items = {str(item.listing.id): item.quantity for item in cart.items.all()}
            cart_total = cart.get_total_price()
            cart_item_count = cart.items.count()
        except Exception as e:
            print(f"Cart error: {str(e)}")
            # If cart doesn't exist, create one
            try:
                cart = Cart.objects.create(user=request.user)
                cart_total = 0
                cart_item_count = 0
            except:
                cart_total = 0
                cart_item_count = 0
    
    # Get user favorites

    listings = listings.annotate(
        total_favorites=Count('favorites', distinct=True)
    )
    user_favorites = []
    if request.user.is_authenticated:
        user_favorites = list(Favorite.objects.filter(
            user=request.user
        ).values_list('listing_id', flat=True))
    # Get user favorites count if authenticated
    user_favorite_count = 0
    if request.user.is_authenticated:
        user_favorite_count = Favorite.objects.filter(user=request.user).count()

    # For AJAX requests, return JSON
    # X-Requested-With header indicates this is an AJAX/XHR request (sent by fetch API)
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        paginator = Paginator(listings, 12)
        page_number = request.GET.get('page', 1)
        
        try:
            page_obj = paginator.page(page_number)
        except:
            page_obj = paginator.page(1)
            
        listings_data = []
        for listing in page_obj:
            # Calculate stock status
            stock_status = 'in_stock'
            if listing.stock == 0:
                stock_status = 'out_of_stock'
            elif listing.stock <= 10:
                stock_status = 'low_stock'
            
            # Check if listing is in user's cart
            cart_quantity = cart_items.get(str(listing.id), 0)
            
            # Check if listing is favorited
            is_favorited = listing.id in user_favorites
            
            # Check if listing is recent (within 7 days)
            is_recent = False
            if listing.date_created:
                is_recent = listing.date_created > timezone.now() - timedelta(days=7)
            
            listing_data = {
                'id': listing.id,
                'title': listing.title,
                'price': float(listing.price),
                'stock': listing.stock,
                'stock_status': stock_status,
                'location': listing.location,
                'location_name': listing.get_location_display(),
                'category': listing.category.name if listing.category else '',
                'category_id': listing.category.id if listing.category else None,
                'category_icon': listing.category.icon if listing.category else 'bi-tag',
                'image_url': listing.get_image_url(),
                'is_featured': listing.is_featured,
                'is_recent': is_recent,
                'is_sold': listing.is_sold,
                'url': listing.get_absolute_url(),
                'cart_quantity': cart_quantity,
                'is_favorited': is_favorited,
                'favorite_count': listing.total_favorites,
                'user_favorite_count': user_favorite_count,
                'date_created': listing.date_created.strftime('%Y-%m-%d %H:%M') if listing.date_created else '',
            }
            
            # Add store info if available
            if listing.store:
                listing_data['store_name'] = listing.store.name
                listing_data['store_logo'] = listing.store.get_logo_url()
                listing_data['store_url'] = listing.store.get_absolute_url()
            
            listings_data.append(listing_data)
        
        return JsonResponse({
            'success': True,
            'listings': listings_data,
            'cart_items': cart_items,
            'cart_total': float(cart_total),
            'cart_item_count': cart_item_count,
            'user_favorite_count': user_favorite_count,
            'pagination': {
                'has_next': page_obj.has_next(),
                'has_previous': page_obj.has_previous(),
                'current_page': page_obj.number,
                'num_pages': paginator.num_pages,
                'total_count': paginator.count,
                'next_page': page_obj.next_page_number() if page_obj.has_next() else None,
                'previous_page': page_obj.previous_page_number() if page_obj.has_previous() else None,
            },
            'user_authenticated': request.user.is_authenticated,
        })
    
    # Pagination for regular requests
    paginator = Paginator(listings, 12)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    # For regular requests, return the full page
    context = {
        'listings': page_obj,
        'categories': categories,
        'locations': locations,
        'selected_category': category_id,
        'selected_location': location,
        'min_price': min_price,
        'max_price': max_price,
        'search_query': search_query,
        'sort_by': sort_by,
        'total_listings_count': listings.count(),
        'user_favorites': user_favorites,
        'user_favorite_count': user_favorite_count,
        'total_favorites_count': Favorite.objects.count(),
        'cart_items': cart_items,
        'cart_total': cart_total,
        'cart_item_count': cart_item_count,
    }
    
    # Add featured listings for carousel
    context['featured_listings'] = Listing.objects.filter(
        is_featured=True, 
        is_active=True,
        is_sold=False
    ).order_by('-date_created')[:6]
    
    # Add popular categories
    context['popular_categories'] = Category.objects.filter(
        is_active=True
    ).annotate(
        listing_count=Count('listing', filter=Q(listing__is_active=True))
    ).filter(listing_count__gt=0).order_by('-listing_count')[:12]
    
    # Add recently viewed
    if request.user.is_authenticated:
        recently_viewed = RecentlyViewed.objects.filter(
            user=request.user
        ).select_related('listing').order_by('-viewed_at')[:6]
        context['recently_viewed'] = [rv.listing for rv in recently_viewed]

    return render(request, 'listings/all_listings.html', context)

def all_listings_json(request):
    # Get all active listings
    listings = Listing.objects.filter(is_active=True)
    
    # Get filter parameters from request
    category_id = request.GET.get('category')
    location = request.GET.get('location')
    min_price = request.GET.get('min_price')
    max_price = request.GET.get('max_price')
    search_query = request.GET.get('q')
    sort_by = request.GET.get('sort', 'newest')
    featured = request.GET.get('featured')
    recent = request.GET.get('recent')
    instock = request.GET.get('instock')
    
    # Apply filters (same logic as all_listings view)
    if category_id and category_id != 'all':
        listings = listings.filter(category__id=category_id)
    
    if location and location != 'all':
        listings = listings.filter(location=location)
    
    if min_price:
        try:
            listings = listings.filter(price__gte=float(min_price))
        except ValueError:
            pass
    
    if max_price:
        try:
            listings = listings.filter(price__lte=float(max_price))
        except ValueError:
            pass
    
    if search_query:
        listings = listings.filter(
            Q(title__icontains=search_query) | 
            Q(description__icontains=search_query) |
            Q(brand__icontains=search_query) |
            Q(model__icontains=search_query)
        )
    
    # Apply new filters
    if featured == 'true':
        listings = listings.filter(is_featured=True)
    
    if recent == 'true':
        one_week_ago = timezone.now() - timedelta(days=7)
        listings = listings.filter(date_created__gte=one_week_ago)
    
    if instock == 'true':
        listings = listings.filter(stock__gt=0)
    
    # Apply sorting
    if sort_by == 'price_low':
        listings = listings.order_by('price')
    elif sort_by == 'price_high':
        listings = listings.order_by('-price')
    elif sort_by == 'oldest':
        listings = listings.order_by('date_created')
    elif sort_by == 'featured':
        listings = listings.order_by('-is_featured', '-date_created')
    elif sort_by == 'popular':
        listings = listings.annotate(
            favorite_count=Count('favorites')
        ).order_by('-favorite_count', '-date_created')
    else:  # newest is default
        listings = listings.order_by('-date_created')
    
    # Get cart items for authenticated user
    cart_items = {}
    if request.user.is_authenticated:
        try:
            cart = request.user.cart
            for item in cart.items.all():
                cart_items[str(item.listing.id)] = {
                    'quantity': item.quantity,
                    'item_total': float(item.get_total_price())
                }
        except Exception:
            pass
    
    # Get favorite listings for authenticated user
    favorite_ids = []
    if request.user.is_authenticated:
        favorite_ids = list(Favorite.objects.filter(
            user=request.user
        ).values_list('listing_id', flat=True))
    
    # Pagination
    paginator = Paginator(listings, 12)
    page_number = request.GET.get('page', 1)
    
    try:
        page_obj = paginator.page(page_number)
    except:
        page_obj = paginator.page(1)
    
    # Prepare listings data for JSON response
    listings_data = []
    for listing in page_obj:
        # Calculate stock status
        stock_status = 'in_stock'
        if listing.stock == 0:
            stock_status = 'out_of_stock'
        elif listing.stock <= 10:
            stock_status = 'low_stock'
        
        # Check if listing is in user's cart
        cart_quantity = 0
        if str(listing.id) in cart_items:
            cart_quantity = cart_items[str(listing.id)]['quantity']
        
        # Check if listing is favorited
        is_favorited = False
        if request.user.is_authenticated:
            is_favorited = listing.id in favorite_ids
        
        # Check if listing is recent (within 7 days)
        is_recent = False
        if listing.date_created:
            is_recent = listing.date_created > timezone.now() - timedelta(days=7)
        
        listing_data = {
            'id': listing.id,
            'title': listing.title,
            'price': float(listing.price),
            'formatted_price': f"KSh {listing.price:,.2f}",
            'stock': listing.stock,
            'stock_status': stock_status,
            'location': listing.location,
            'location_name': listing.get_location_display(),
            'category': listing.category.name if listing.category else '',
            'category_id': listing.category.id if listing.category else None,
            'category_icon': listing.category.icon if listing.category else 'bi-tag',
            'image_url': listing.get_image_url(),
            'is_featured': listing.is_featured,
            'is_recent': is_recent,
            'is_sold': listing.is_sold,
            'url': listing.get_absolute_url(),
            'cart_quantity': cart_quantity,
            'is_favorited': is_favorited,
            'favorite_count': Favorite.objects.filter(listing=listing).count(),
            'user_favorite_count': Favorite.objects.filter(user=request.user).count() if request.user.is_authenticated else 0,
            'date_created': listing.date_created.strftime('%Y-%m-%d %H:%M') if listing.date_created else '',
            'relative_date': listing.date_created.strftime('%b %d') if listing.date_created else '',
        }
        
        # Add store info if available
        if listing.store:
            listing_data['store_name'] = listing.store.name
            listing_data['store_logo'] = listing.store.get_logo_url()
            listing_data['store_url'] = listing.store.get_absolute_url()
        
        listings_data.append(listing_data)
    
    # Return JSON response
    return JsonResponse({
        'success': True,
        'listings': listings_data,
        'cart_items': cart_items,
        'favorite_count': Favorite.objects.filter(listing=listing).count() if listing else 0,
        'user_favorite_count': Favorite.objects.filter(user=request.user).count() if request.user.is_authenticated else 0,
        'is_authenticated': request.user.is_authenticated,
        'user_id': request.user.id if request.user.is_authenticated else None,
        'user_favorites': list(request.user.favorites.values_list('listing_id', flat=True)) if request.user.is_authenticated else [],
        'filters': {
            'category': category_id,
            'location': location,
            'min_price': min_price,
            'max_price': max_price,
            'search_query': search_query,
            'sort_by': sort_by,
            'featured': featured,
            'recent': recent,
            'instock': instock,
        },
        'pagination': {
            'has_next': page_obj.has_next(),
            'has_previous': page_obj.has_previous(),
            'current_page': page_obj.number,
            'num_pages': paginator.num_pages,
            'total_count': paginator.count,
            'next_page': page_obj.next_page_number() if page_obj.has_next() else None,
            'previous_page': page_obj.previous_page_number() if page_obj.has_previous() else None,
        },
        'user_authenticated': request.user.is_authenticated,
    })

@login_required
def get_cart_items(request):
    cart = request.user.cart
    cart_items = cart.items.all()
    
    items_data = []
    for item in cart_items:
        items_data.append({
            'listing_id': item.listing.id,
            'quantity': item.quantity,
            'title': item.listing.title,
            'price': str(item.listing.price),
            'total': str(item.get_total_price())
        })
    
    return JsonResponse({
        'cart_items': items_data,
        'cart_item_count': cart.items.count(),
        'cart_total': str(cart.get_total_price())
    })


@login_required
def cart_summary(request):
    """Return a lightweight cart summary used by the frontend.

    JSON structure expected by the client:
    {
        'cart_total': <float>,
        'cart_item_count': <int>,
        'item_totals': { '<item_id>': { 'item_total': <float>, 'quantity': <int>, 'stock': <int> }, ... }
    }
    """
    try:
        cart, _ = Cart.objects.get_or_create(user=request.user)
        item_totals = {}
        for ci in cart.items.all():
            item_totals[str(ci.id)] = {
                'item_total': float(ci.get_total_price()),
                'quantity': ci.quantity,
                'stock': ci.listing.stock if ci.listing and hasattr(ci.listing, 'stock') else 999
            }

        data = {
            'cart_total': float(cart.get_total_price()),
            'cart_item_count': cart.items.count(),
            'item_totals': item_totals,
        }
        return JsonResponse(data)
    except Exception as e:
        logger.exception('Failed to build cart summary')
        return JsonResponse({'error': 'Failed to build cart summary'}, status=500)


@login_required
@require_POST
def toggle_favorite(request, listing_id):
    from .models import Favorite, Listing
    
    listing = Listing.objects.get(id=listing_id)
    
    # Check if already favorited
    favorite, created = Favorite.objects.get_or_create(
        user=request.user,
        listing=listing
    )
    
    if not created:
        # Remove favorite
        favorite.delete()
        is_favorited = False
    else:
        is_favorited = True
    
    # Get updated counts
    listing_favorite_count = Favorite.objects.filter(listing=listing).count()
    user_favorite_count = Favorite.objects.filter(user=request.user).count()

    # Broadcast favorite change so clients can update live
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        channel_layer = get_channel_layer()
        listing_data = {
            'id': listing.id,
            'total_favorites': listing_favorite_count,
            'is_favorited': is_favorited,
            'by_user_id': request.user.id,
        }
        # Send to all users' notification groups (small scale/fallback)
        from django.contrib.auth import get_user_model
        User = get_user_model()
        user_ids = list(User.objects.filter(is_active=True).values_list('id', flat=True))
        for uid in user_ids:
            async_to_sync(channel_layer.group_send)(
                f'notifications_user_{uid}',
                {
                    'type': 'listing_liked',
                    'listing': listing_data,
                }
            )
    except Exception:
        logger.exception('Failed to broadcast listing_liked')
    
    return JsonResponse({
        'success': True,
        'is_favorited': is_favorited,
        'listing_favorite_count': listing_favorite_count,
        'user_favorite_count': user_favorite_count,
        'message': 'Added to favorites!' if is_favorited else 'Removed from favorites!'
    })

@login_required
def user_favorites(request):
    favorites = Favorite.objects.filter(user=request.user).select_related('listing')
    
    context = {
        'favorites': favorites,
        'favorite_count': favorites.count(),
        'page_title': 'My Favorites',
    }
    
    return render(request, 'listings/favorites.html', context)

@login_required
def favorite_listings(request):
    favorites = Favorite.objects.filter(user=request.user).select_related('listing')
    return render(request, 'listings/favorites.html', {'favorites': favorites})

@login_required
def my_listings(request):
    listings = Listing.objects.filter(seller=request.user).order_by('-date_created')
    
    # Calculate statistics
    total_listings = listings.count()
    active_listings = listings.filter(is_active=True, is_sold=False).count()
    sold_listings = listings.filter(is_sold=True).count()
    featured_listings = listings.filter(is_featured=True, is_active=True).count()
    
    context = {
        'listings': listings,
        'total_listings': total_listings,
        'active_listings': active_listings,
        'sold_listings': sold_listings,
        'featured_listings': featured_listings,
    }
    
    return render(request, 'listings/my_listings.html', context)
# In your listings/views.py
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.db import transaction
from django.utils import timezone
from .models import Cart, CartItem, Order, OrderItem, Payment, Escrow, Listing
from .forms import CheckoutForm
from django.http import JsonResponse
from django.views.decorators.http import require_POST
import json




# Update the existing view_cart function to handle AJAX
@login_required
def view_cart(request):
    cart, created = Cart.objects.get_or_create(user=request.user)
    
    # Handle AJAX requests
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        cart_data = {
            'items': [],
            'total_price': float(cart.get_total_price()),
            'item_count': cart.items.count()
        }
        
        for item in cart.items.all():
            cart_data['items'].append({
                'id': item.id,
                'listing': {
                    'id': item.listing.id,
                    'title': item.listing.title,
                    'price': float(item.listing.price),
                    # Use the model helper which safely returns Cloudinary or local URLs
                    'image_url': item.listing.get_image_url(),
                    'category': item.listing.category.name
                },
                'quantity': item.quantity,
                'total_price': float(item.get_total_price())
            })
        
        return JsonResponse(cart_data)
    
    return render(request, 'listings/cart.html', {'cart': cart})

# In listings/views.py

import json
from django.http import JsonResponse

@login_required
@require_POST
@ajax_required
def update_cart_item(request, item_id):
    """AJAX endpoint for updating cart item quantity - FIXED RESPONSE"""
    try:
        data = json.loads(request.body)
    except:
        data = request.POST
    
    action = data.get('action')
    quantity = data.get('quantity')
    
    cart_item = get_object_or_404(CartItem, id=item_id, cart__user=request.user)
    cart = cart_item.cart
    
    if action == 'increase':
        if cart_item.quantity < cart_item.listing.stock:
            cart_item.quantity += 1
            cart_item.save()
    elif action == 'decrease':
        if cart_item.quantity > 1:
            cart_item.quantity -= 1
            cart_item.save()
    elif action == 'set' and quantity is not None:
        try:
            quantity = int(quantity)
            if 1 <= quantity <= cart_item.listing.stock:
                cart_item.quantity = quantity
                cart_item.save()
        except (ValueError, TypeError):
            pass
    
    # Get updated cart totals
    cart.refresh_from_db()
    item_count = cart.items.count()
    cart_total = cart.get_total_price()
    
    # Calculate individual item totals for UI updates
    item_totals = {}
    for item in cart.items.all():
        item_totals[str(item.id)] = {
            'quantity': item.quantity,
            'item_total': float(item.get_total_price()),
            'stock': item.listing.stock,
            'item_id': item.id
        }

    # Broadcast cart update to the current user's notification group (so other sessions update)
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        channel_layer = get_channel_layer()
        cart_payload = {
            'cart_item_count': item_count,
            'cart_total': float(cart_total),
            'item_totals': item_totals,
        }
        async_to_sync(channel_layer.group_send)(
            f'notifications_user_{request.user.id}',
            {
                'type': 'cart_updated',
                'cart': cart_payload,
            }
        )
    except Exception:
        logger.exception('Failed to broadcast cart_updated from update_cart_item')

    return JsonResponse({
        'success': True,
        'message': 'Cart updated successfully',
        'cart_item_count': item_count,
        'cart_total': float(cart_total),
        'item_count': item_count,
        'item_totals': item_totals
    })

    # NOTE: we return above; broadcasting is handled in add/update/remove functions where appropriate

@login_required
@require_POST
@ajax_required
def remove_from_cart(request, item_id):
    """AJAX endpoint for removing cart items - FIXED RESPONSE"""
    cart_item = get_object_or_404(CartItem, id=item_id, cart__user=request.user)
    cart = cart_item.cart
    
    # Store listing title for message
    listing_title = cart_item.listing.title
    
    # Remove the item
    cart_item.delete()
    
    # Get updated cart totals
    cart.refresh_from_db()
    item_count = cart.items.count()
    cart_total = cart.get_total_price()
    
    # Broadcast cart update to current user's notification group
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        channel_layer = get_channel_layer()
        cart_payload = {
            'cart_item_count': item_count,
            'cart_total': float(cart_total),
            'removed_item_id': item_id,
        }
        async_to_sync(channel_layer.group_send)(
            f'notifications_user_{request.user.id}',
            {
                'type': 'cart_updated',
                'cart': cart_payload,
            }
        )
    except Exception:
        logger.exception('Failed to broadcast cart_updated from remove_from_cart')

    return JsonResponse({
        'success': True,
        'message': f'{listing_title} removed from cart',
        'cart_item_count': item_count,
        'cart_total': float(cart_total),
        'item_count': item_count,
        'removed_item_id': item_id
    })

@login_required
@require_POST
@ajax_required
def clear_cart(request):
    """Clear entire cart - FIXED RESPONSE"""
    cart = get_object_or_404(Cart, user=request.user)
    cart_items_count = cart.items.count()
    
    # Clear all items
    cart.items.all().delete()
    
    # Broadcast cart cleared to current user's notification group
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        channel_layer = get_channel_layer()
        cart_payload = {
            'cart_item_count': 0,
            'cart_total': 0,
            'item_count': 0,
        }
        async_to_sync(channel_layer.group_send)(
            f'notifications_user_{request.user.id}',
            {
                'type': 'cart_updated',
                'cart': cart_payload,
            }
        )
    except Exception:
        logger.exception('Failed to broadcast cart_updated from clear_cart')

    return JsonResponse({
        'success': True,
        'message': f'Cart cleared ({cart_items_count} items removed)',
        'cart_item_count': 0,
        'cart_total': 0,
        'item_count': 0
    })

from django.http import JsonResponse, HttpResponse
import json

@login_required
@require_POST
@ajax_required
def add_to_cart(request, listing_id):
    """Add item to cart - FIXED for AJAX handling"""
    listing = get_object_or_404(Listing, id=listing_id, is_sold=False)

   
    
    # Check if this is an AJAX request
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    
    # Check if item is in stock
    if listing.stock <= 0:
        response_data = {
            'success': False, 
            'error': 'This item is out of stock.',
            'redirect_url': reverse('view_cart')
        }
        
        if is_ajax:
            return JsonResponse(response_data)
        else:
            messages.error(request, response_data['error'])
            return redirect(response_data['redirect_url'])
    
    # Users shouldn't add their own listings to cart
    if listing.seller == request.user:
        response_data = {
            'success': False, 
            'error': 'You cannot add your own listing to cart.',
            'redirect_url': reverse('listing-detail', args=[listing_id])
        }
        
        if is_ajax:
            return JsonResponse(response_data)
        else:
            messages.error(request, response_data['error'])
            return redirect(response_data['redirect_url'])
    
    # Get quantity from request
    try:
        if request.content_type == 'application/json':
            data = json.loads(request.body)
            quantity = int(data.get('quantity', 1))
        else:
            quantity = int(request.POST.get('quantity', 1))
    except (ValueError, TypeError, json.JSONDecodeError):
        quantity = 1
    
    cart, created = Cart.objects.get_or_create(user=request.user)
    cart_item, created = CartItem.objects.get_or_create(
        cart=cart,
        listing=listing,
        defaults={'quantity': quantity}
    )
    
    if not created:
        # Check if we're not exceeding available stock
        if cart_item.quantity + quantity > listing.stock:
            response_data = {
                'success': False, 
                'error': f'Only {listing.stock} units available.',
                'redirect_url': reverse('view_cart')
            }
            
            if is_ajax:
                return JsonResponse(response_data)
            else:
                messages.error(request, response_data['error'])
                return redirect(response_data['redirect_url'])
        
        cart_item.quantity += quantity
        cart_item.save()
        message = f'Updated quantity of {listing.title} in your cart.'
        action = 'updated'
    else:
        message = f'Added {listing.title} to your cart.'
        action = 'added'
    
    # Get updated cart info
    cart.refresh_from_db()
    cart_item_count = cart.items.count()
    cart_total = float(cart.get_total_price())
    
    response_data = {
        'success': True,
        'message': message,
        'action': action,
        'cart_item_count': cart_item_count,
        'cart_total': cart_total,
        'item_count': cart_item_count,
        'listing_title': listing.title,
        'redirect_url': reverse('view_cart')
    }
    
    # Broadcast cart update to current user's notification group so other sessions update live
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        channel_layer = get_channel_layer()
        cart_payload = {
            'cart_item_count': cart_item_count,
            'cart_total': float(cart_total),
            'action': action,
            'listing_id': listing.id,
            'listing_title': listing.title,
        }
        async_to_sync(channel_layer.group_send)(
            f'notifications_user_{request.user.id}',
            {
                'type': 'cart_updated',
                'cart': cart_payload,
            }
        )
    except Exception:
        logger.exception('Failed to broadcast cart_updated from add_to_cart')

    if is_ajax:
        return JsonResponse(response_data)
    else:
        # For non-AJAX requests, show message and redirect
        messages.success(request, message)
        return redirect(reverse('view_cart'))
    
@login_required
def checkout(request):
    cart = get_object_or_404(Cart, user=request.user)
    
    # Validate stock before checkout
    for cart_item in cart.items.all():
        if cart_item.quantity > cart_item.listing.stock:
            messages.error(request, f"Sorry, only {cart_item.listing.stock} units of '{cart_item.listing.title}' are available.")
            return redirect('view_cart')
    
    if request.method == 'POST':
        logger.info('Checkout POST received for user=%s; POST keys=%s', request.user, list(request.POST.keys()))
        # Initialize form with user's existing info
        initial_data = {
            'first_name': request.user.first_name,
            'last_name': request.user.last_name,
            'email': request.user.email,
            'phone_number': getattr(request.user, 'phone_number', ''),
        }
        
        # Get latest successful order for shipping info
        latest_order = Order.objects.filter(
            user=request.user,
            status__in=['delivered', 'shipped']
        ).order_by('-created_at').first()
        
        if latest_order:
            initial_data.update({
                'shipping_address': latest_order.shipping_address,
                'city': latest_order.city,
                'postal_code': latest_order.postal_code,
            })
        
        # Check if using alternate shipping (support multiple truthy values)
        raw_alt = request.POST.get('use_alternate_shipping')
        use_alternate = str(raw_alt).lower() in ['on', 'true', '1', 'yes']

        # If not using alternate shipping, merge user's account info into POST before binding
        if not use_alternate:
            merged_post = request.POST.copy()
            merged_post.update(initial_data)
            logger.debug('Merged POST for checkout (using account info) keys=%s', list(merged_post.keys()))
            form = CheckoutForm(merged_post)
        else:
            form = CheckoutForm(request.POST)
        
        if form.is_valid():
            logger.info('Checkout form is valid for user=%s; cleaned keys=%s', request.user, list(form.cleaned_data.keys()))
            # Use payment_method from form if provided, default to mpesa
            selected_pm = form.cleaned_data.get('payment_method') or request.session.get('selected_payment_method') or 'mpesa'
            request.session['selected_payment_method'] = selected_pm
            try:
                with transaction.atomic():
                    # Create order
                    order = Order.objects.create(
                        user=request.user,
                        total_price=cart.get_total_price(),
                        first_name=form.cleaned_data['first_name'],
                        last_name=form.cleaned_data['last_name'],
                        email=form.cleaned_data['email'],
                        phone_number=form.cleaned_data['phone_number'],
                        shipping_address=form.cleaned_data['shipping_address'],
                        city=form.cleaned_data['city'],
                        postal_code=form.cleaned_data['postal_code'],
                    )
                    
                    # Create order items
                    for cart_item in cart.items.all():
                        OrderItem.objects.create(
                            order=order,
                            listing=cart_item.listing,
                            quantity=cart_item.quantity,
                            price=cart_item.listing.price
                        )
                    
                    # Create payment record
                    payment = Payment.objects.create(
                        order=order,
                        amount=order.total_price
                    )
                    
                    # Create escrow record
                    Escrow.objects.create(
                        order=order,
                        amount=order.total_price
                    )
                    
                    # Clear cart
                    cart.items.all().delete()

                    notify_order_created(request.user, order)
                    
                    messages.success(request, "Order created successfully! Please complete payment.")
                    logger.info('Order %s created for user=%s, redirecting to payment', order.id, request.user)
                    return redirect('process_payment', order_id=order.id)
                    
            except Exception as e:
                logger.exception('Exception creating order for user=%s: %s', request.user, e)
                messages.error(request, f"An error occurred during checkout: {str(e)}")
                return render(request, 'listings/checkout.html', {
                    'cart': cart,
                    'form': form,
                    'use_alternate_shipping': use_alternate
                })
        else:
            # Log validation errors for debugging
            try:
                logger.warning('Checkout form invalid for user=%s; errors=%s; POST keys=%s', request.user, form.errors.as_json(), list(request.POST.keys()))
            except Exception:
                logger.warning('Checkout form invalid for user=%s; could not serialize errors; POST keys=%s', request.user, list(request.POST.keys()))
            # Inform the user that some fields are invalid
            messages.error(request, 'Please correct the highlighted fields before placing your order.')
            return render(request, 'listings/checkout.html', {
                'cart': cart,
                'form': form,
                'use_alternate_shipping': use_alternate
            })
    else:
        # Pre-fill form with user's info
        initial_data = {
            'first_name': request.user.first_name,
            'last_name': request.user.last_name,
            'email': request.user.email,
            'phone_number': getattr(request.user, 'phone_number', ''),
        }
        
        # Get latest successful order for shipping info
        latest_order = Order.objects.filter(
            user=request.user,
            status__in=['delivered', 'shipped']
        ).order_by('-created_at').first()
        
        if latest_order:
            initial_data.update({
                'shipping_address': latest_order.shipping_address,
                'city': latest_order.city,
                'postal_code': latest_order.postal_code,
            })
            
        form = CheckoutForm(initial=initial_data)
    
    return render(request, 'listings/checkout.html', {
        'cart': cart,
        'form': form,
        'use_alternate_shipping': False,
        'has_previous_orders': latest_order is not None
    })

from django.views.decorators.csrf import csrf_exempt
from django.http import HttpResponse
import json
from .mpesa_utils import mpesa_gateway

import logging
logger = logging.getLogger(__name__)

# Replace the existing process_payment function with this:
@login_required
def process_payment(request, order_id):
    order = get_object_or_404(Order, id=order_id, user=request.user)
    selected_payment_method = request.session.get('selected_payment_method', 'mpesa')
    if 'selected_payment_method' in request.session:
        del request.session['selected_payment_method']
    if order.status != 'pending':
        messages.warning(request, "This order has already been processed.")
        return redirect('order_detail', order_id=order.id)
    
    if request.method == 'POST':
        payment_method = request.POST.get('payment_method')
        # Enforce server-side: only M-Pesa payments are allowed
        if payment_method != 'mpesa' and payment_method is not None:
            messages.error(request, "Only M-Pesa payments are accepted at this time.")
            return render(request, 'listings/payment.html', {'order': order})

        if payment_method == 'mpesa' or payment_method is None:
            phone_number = request.POST.get('phone_number')
            if not phone_number:
                messages.error(request, "Please provide your M-Pesa phone number.")
                return render(request, 'listings/payment.html', {'order': order})

            # Format phone like in AJAX path
            formatted_phone = phone_number.replace(' ', '')
            if formatted_phone.startswith('0'):
                formatted_phone = '254' + formatted_phone[1:]
            elif not formatted_phone.startswith('254'):
                formatted_phone = '254' + formatted_phone

            # Allow re-initiation: log previous mpesa ids if present
            prev_checkout = order.payment.mpesa_checkout_request_id
            if prev_checkout:
                logger.info('Re-initiating M-Pesa for order %s; previous checkout id=%s', order.id, prev_checkout)

            # Initiate M-Pesa payment
            success, message = order.payment.initiate_mpesa_payment(formatted_phone)

            if success:
                messages.success(request, f"M-Pesa payment initiated: {message}")
                return render(request, 'listings/payment.html', {'order': order})
            else:
                messages.error(request, f"Failed to initiate M-Pesa payment: {message}")
                return render(request, 'listings/payment.html', {'order': order})
        # end POST handling
    
    return render(request, 'listings/payment.html', {'order': order})

def _notify_sellers_after_payment(order):
    """Notify all sellers in an order after successful payment"""
    # Group order items by seller
    from collections import defaultdict
    seller_items = defaultdict(list)
    
    for order_item in order.order_items.all():
        seller_items[order_item.listing.seller].append(order_item)
    
    # Notify each seller
    for seller, items in seller_items.items():
        notify_payment_received(seller, order.user, order)
        
        # Create activity log
        Activity.objects.create(
            user=seller,
            action=f"Payment received for order #{order.id}"
        )

@login_required
def initiate_mpesa_payment(request, order_id):
    """AJAX endpoint to initiate M-Pesa payment"""
    if request.method == 'POST':
        order = get_object_or_404(Order, id=order_id, user=request.user)
        
        if order.status != 'pending':
            return JsonResponse({
                'success': False,
                'error': 'This order has already been processed.'
            })
        
        phone_number = request.POST.get('phone_number')
        
        if not phone_number:
            return JsonResponse({
                'success': False,
                'error': 'Phone number is required.'
            })
        
        # Format phone number (remove spaces and ensure it starts with 254)
        formatted_phone = phone_number.replace(' ', '')
        if formatted_phone.startswith('0'):
            formatted_phone = '254' + formatted_phone[1:]
        elif not formatted_phone.startswith('254'):
            formatted_phone = '254' + formatted_phone
        
        # Initiate payment
        # Allow re-initiation: clear previous checkout id so a fresh STK push is performed
        if order.payment.mpesa_checkout_request_id:
            logger.info('Clearing previous mpesa_checkout_request_id for order %s before re-initiation', order.id)
            order.payment.mpesa_checkout_request_id = ''
            order.payment.mpesa_merchant_request_id = ''
            order.payment.mpesa_result_code = None
            order.payment.mpesa_result_desc = ''
            order.payment.save()

        success, message = order.payment.initiate_mpesa_payment(formatted_phone)
        
        if success:
            return JsonResponse({
                'success': True,
                'message': message,
                'checkout_request_id': order.payment.mpesa_checkout_request_id
            })
        else:
            return JsonResponse({
                'success': False,
                'error': message
            })
    
    return JsonResponse({'success': False, 'error': 'Invalid request method'})


@login_required
def check_payment_status(request, order_id):
    """AJAX endpoint to check M-Pesa payment status with active MPESA status check"""
    order = get_object_or_404(Order, id=order_id, user=request.user)
    payment = order.payment

    # First check the current payment state in our DB
    if payment.status == 'completed':
        return JsonResponse({
            'success': True,
            'payment_status': 'completed',
            'message': 'Payment completed successfully',
            'redirect_url': reverse('order_detail', args=[order.id])
        })
    elif payment.status == 'failed':
        return JsonResponse({
            'success': True, 
            'payment_status': 'failed',
            'message': payment.mpesa_result_desc or 'Payment failed',
            'redirect_url': reverse('process_payment', args=[order.id])
        })
        
    # If payment was initiated via MPESA, check status with MPESA API
    if (payment.status == 'initiated' and 
        payment.method == 'mpesa' and 
        payment.mpesa_checkout_request_id):
            
        from .mpesa_utils import mpesa_gateway
        status_response = mpesa_gateway.check_transaction_status(
            payment.mpesa_checkout_request_id
        )
        
        if status_response['success']:
            result_code = status_response.get('result_code')
            
            # Update payment record based on MPESA response
            if result_code == '0':  # Success
                # Persist MPesa response details onto the payment record
                payment.mpesa_result_code = result_code
                payment.mpesa_result_desc = status_response.get('result_desc', '')
                payment.mpesa_callback_data = status_response.get('response_data') or status_response
                # Save intermediate fields before marking completed so DB has trace
                payment.save()

                payment.mark_as_completed(
                    status_response.get('response_data', {}).get('MpesaReceiptNumber')
                )
                return JsonResponse({
                    'success': True,
                    'payment_status': 'completed',
                    'message': 'Payment completed successfully',
                    'redirect_url': reverse('order_detail', args=[order.id])
                })
                
            elif result_code == '1037':  # Timeout waiting for user input
                payment.status = 'failed'
                payment.mpesa_result_code = result_code
                payment.mpesa_result_desc = 'Transaction timed out waiting for user input'
                payment.save()
                return JsonResponse({
                    'success': True,
                    'payment_status': 'failed',
                    'message': 'Transaction timed out. Please try again.',
                    'redirect_url': reverse('process_payment', args=[order.id])
                })
                
            elif result_code == '1032':  # Cancelled by user
                payment.status = 'failed'
                payment.mpesa_result_code = result_code
                payment.mpesa_result_desc = 'Transaction cancelled by user'
                payment.save()
                return JsonResponse({
                    'success': True,
                    'payment_status': 'failed',
                    'message': 'Transaction was cancelled. Please try again if you want to complete the payment.',
                    'redirect_url': reverse('process_payment', args=[order.id])
                })
                
            elif result_code == '1':  # Still processing
                return JsonResponse({
                    'success': True,
                    'payment_status': 'processing',
                    'message': 'Please complete the payment on your phone...'
                })
            else:
                # Any other failure case
                payment.status = 'failed'
                payment.mpesa_result_code = result_code
                payment.mpesa_result_desc = status_response.get('result_desc', 'Payment failed')
                payment.save()
                return JsonResponse({
                    'success': True,
                    'payment_status': 'failed',
                    'message': status_response.get('result_desc', 'Payment failed. Please try again.'),
                    'redirect_url': reverse('process_payment', args=[order.id])
                })
                
        else:
            # Error checking status - tell frontend to keep trying
            logger.error(f"Error checking MPESA status: {status_response.get('error')}")
            return JsonResponse({
                'success': True,
                'payment_status': 'processing',
                'message': 'Checking payment status...'
            })
    
    # For non-MPESA or non-initiated payments, just return current status
    return JsonResponse({
        'success': True,
        'payment_status': 'processing',
        'message': 'Payment is being processed...'
    })

@login_required
def get_unread_messages_count(request):
    """AJAX endpoint to get unread messages count for current user"""
    try:
        # Adjust this query based on your Message model structure
        unread_count = Message.objects.filter(
            recipient=request.user,
            is_read=False
        ).count()
        
        return JsonResponse({
            'unread_messages_count': unread_count,
            'status': 'success'
        })
    except Exception as e:
        return JsonResponse({
            'unread_messages_count': 0,
            'status': 'error',
            'error': str(e)
        }, status=500)
    
@csrf_exempt
@require_POST
def mpesa_callback(request):
    """
    Handle M-Pesa callback with payment result and trigger notifications
    """
    try:
        callback_data = json.loads(request.body)
        
        # Log the callback for debugging
        logger.info(f"M-Pesa Callback Received: {callback_data}")
        
        # Extract the main body
        stk_callback = callback_data.get('Body', {}).get('stkCallback', {})
        checkout_request_id = stk_callback.get('CheckoutRequestID')
        result_code = stk_callback.get('ResultCode')
        result_desc = stk_callback.get('ResultDesc')
        
        if not checkout_request_id:
            return JsonResponse({'ResultCode': 1, 'ResultDesc': 'Invalid callback data'})
        
        # Find the payment with this checkout request ID
        try:
            payment = Payment.objects.get(mpesa_checkout_request_id=checkout_request_id)
            payment.mpesa_result_code = result_code
            payment.mpesa_result_desc = result_desc
            payment.mpesa_callback_data = callback_data
            
            if result_code == 0:
                # Payment was successful
                callback_metadata = stk_callback.get('CallbackMetadata', {}).get('Item', [])
                
                # Extract transaction details
                transaction_data = {}
                for item in callback_metadata:
                    transaction_data[item.get('Name')] = item.get('Value')
                
                mpesa_receipt_number = transaction_data.get('MpesaReceiptNumber')
                
                if mpesa_receipt_number:
                    payment.mark_as_completed(mpesa_receipt_number)
                    
                    # Notify all sellers in the order
                    _notify_sellers_after_payment(payment.order)
                    
                    # Create activity log
                    Activity.objects.create(
                        user=payment.order.user,
                        action=f"M-Pesa payment completed for Order #{payment.order.id}. Receipt: {mpesa_receipt_number}"
                    )
                    
                    logger.info(f"M-Pesa payment successful for order #{payment.order.id}. Receipt: {mpesa_receipt_number}")
                
            else:
                # Payment failed
                payment.status = 'failed'
                payment.save()
                
                logger.warning(f"M-Pesa payment failed for order #{payment.order.id}. Reason: {result_desc}")
            
            return JsonResponse({'ResultCode': 0, 'ResultDesc': 'Callback processed successfully'})
            
        except Payment.DoesNotExist:
            logger.error(f"Payment not found for checkout request ID: {checkout_request_id}")
            return JsonResponse({'ResultCode': 1, 'ResultDesc': 'Payment not found'})
            
    except Exception as e:
        logger.error(f"Error processing M-Pesa callback: {str(e)}")
        return JsonResponse({'ResultCode': 1, 'ResultDesc': 'Error processing callback'})

@login_required
def mpesa_debug_info(request):
    """Debug endpoint to check M-Pesa configuration"""
    from .mpesa_utils import mpesa_gateway
    
    debug_info = {
        'has_credentials': mpesa_gateway.has_valid_credentials,
        'environment': mpesa_gateway.environment,
        'business_shortcode': mpesa_gateway.business_shortcode,
        'callback_url': mpesa_gateway.callback_url,
    }
    
    # Test access token (without exposing secrets)
    if mpesa_gateway.has_valid_credentials:
        access_token = mpesa_gateway.get_access_token()
        debug_info['access_token_obtained'] = bool(access_token)
        debug_info['access_token_length'] = len(access_token) if access_token else 0
    
    return JsonResponse(debug_info)

@login_required
def order_list(request):
    """Show orders where user is either buyer or seller"""
    # Get filter parameters
    status_filter = request.GET.get('status')
    role_filter = request.GET.get('role')
    
    # Base queries
    buyer_orders = Order.objects.filter(user=request.user)
    seller_orders = Order.objects.filter(order_items__listing__seller=request.user)
    
    # Apply status filter if provided
    if status_filter and status_filter != 'all':
        buyer_orders = buyer_orders.filter(status=status_filter)
        seller_orders = seller_orders.filter(status=status_filter)
    
    # Apply role filter if provided
    if role_filter == 'buyer':
        all_orders = buyer_orders.distinct()
    elif role_filter == 'seller':
        all_orders = seller_orders.distinct()
    else:
        # Combine using union (proper way to combine distinct queries)
        all_orders = buyer_orders.union(seller_orders).order_by('-created_at')
    
    # Ensure proper ordering
    all_orders = all_orders.order_by('-created_at')
    
    # Get counts for display
    buyer_orders_count = buyer_orders.count()
    seller_orders_count = seller_orders.distinct().count()
    
    # Prepare order data with all necessary calculations
    orders_data = []
    for order in all_orders:
        # Get all order items for this order
        order_items = order.order_items.all()
        
        # Calculate seller-specific data
        seller_items = []
        seller_total = 0
        shipped_count = 0
        is_seller_for_this_order = False
        
        # Check if current user is a seller for this order
        for item in order_items:
            if item.listing.seller == request.user:
                is_seller_for_this_order = True
                seller_items.append(item)
                seller_total += float(item.get_total_price())
                if item.shipped:
                    shipped_count += 1
        
        # Calculate unique sellers for buyer view
        unique_sellers = set()
        for item in order_items:
            unique_sellers.add(item.listing.seller)
        
        orders_data.append({
            'order': order,
            'order_items': order_items,
            'is_buyer': order.user == request.user,
            'is_seller_for_this_order': is_seller_for_this_order,
            'seller_items': seller_items,
            'seller_total': seller_total,
            'shipped_count': shipped_count,
            'seller_items_count': len(seller_items),
            'unique_sellers': list(unique_sellers),
            'unique_sellers_count': len(unique_sellers),
        })
    
    # Paginate the orders_data list
    paginator = Paginator(orders_data, 10)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    # Build the delivery app order URL for managing orders
    try:
        from django.urls import reverse
        delivery_order_url = reverse('delivery:manage_order', args=[0])
        # Replace the 0 with {order_id} placeholder for JavaScript to replace
        delivery_app_order_url = delivery_order_url.replace('/0/', '/{order_id}/')
    except Exception:
        delivery_app_order_url = None
    
    context = {
        'orders': page_obj,
        'page_obj': page_obj,
        'status_filter': status_filter,
        'role_filter': role_filter,
        'buyer_orders_count': buyer_orders_count,
        'seller_orders_count': seller_orders_count,
        'total_orders_count': buyer_orders_count + seller_orders_count,
        'delivery_app_order_url': delivery_app_order_url,
    }
    
    return render(request, 'listings/order_list.html', context)
@login_required
def order_detail(request, order_id):
    """Order detail view that works for both buyers and sellers"""
    order = get_object_or_404(Order, id=order_id)
    
    # Check if user has permission to view this order
    if order.user != request.user and not order.order_items.filter(listing__seller=request.user).exists():
        messages.error(request, "You don't have permission to view this order.")
        return redirect('order_list')
    
    # Determine user's role in this order
    is_buyer = order.user == request.user
    is_seller = order.order_items.filter(listing__seller=request.user).exists()
    
    # Get items relevant to the user - FIXED FOR MULTI-SELLER
    if is_seller and not is_buyer:
        # Show only items that belong to this seller
        order_items = order.order_items.filter(listing__seller=request.user)
        seller_specific_total = sum(float(item.get_total_price()) for item in order_items)
    else:
        # Show all items for buyer or user who is both buyer and seller
        order_items = order.order_items.all()
        seller_specific_total = order.total_price
    
    context = {
        'order': order,
        'order_items': order_items,
        'is_buyer': is_buyer,
        'is_seller': is_seller,
        'seller_specific_total': seller_specific_total,  # Add this for seller view
        'can_ship': is_seller and order.status == 'paid',
        'can_confirm': is_buyer and order.status == 'shipped',
        'can_dispute': is_buyer and order.status in ['shipped', 'delivered'],
        'delivery_app_order_url': getattr(settings, 'DELIVERY_APP_ORDER_URL', None),
    }
    # Attach delivery request information if available (for display of status/proof)
    try:
        from delivery.models import DeliveryRequest
        delivery = None
        if getattr(order, 'delivery_request_id', None):
            delivery = DeliveryRequest.objects.filter(id=order.delivery_request_id).first()
        if not delivery and order.tracking_number:
            delivery = DeliveryRequest.objects.filter(tracking_number=order.tracking_number).first()
        context['delivery_request'] = delivery
    except Exception:
        context['delivery_request'] = None
    
    return render(request, 'listings/order_detail.html', context) # Seller views


@login_required
@require_POST
def ajax_edit_listing(request, listing_id):
    """AJAX endpoint to allow listing owners to edit simple fields (price, stock, title)."""
    try:
        listing = get_object_or_404(Listing, id=listing_id)
        if listing.seller != request.user:
            return JsonResponse({'success': False, 'error': 'Forbidden'}, status=403)

        # Parse JSON body or form
        try:
            data = json.loads(request.body)
        except Exception:
            data = request.POST

        updated = False
        # Allow editing price, stock, title
        if 'price' in data and data['price'] is not None:
            try:
                new_price = float(data.get('price'))
                if float(listing.price) != new_price:
                    listing.price = new_price
                    updated = True
            except Exception:
                pass

        if 'stock' in data and data['stock'] is not None:
            try:
                new_stock = int(data.get('stock'))
                if int(listing.stock) != new_stock:
                    listing.stock = new_stock
                    updated = True
            except Exception:
                pass

        if 'title' in data and data['title'] is not None:
            new_title = data.get('title').strip()
            if new_title and new_title != listing.title:
                listing.title = new_title
                updated = True

        if updated:
            listing.save()

        return JsonResponse({'success': True, 'updated': updated, 'listing': {'id': listing.id, 'price': float(listing.price), 'stock': int(listing.stock), 'title': listing.title}})
    except Exception as e:
        logger.exception('ajax_edit_listing error: %s', e)
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
@require_POST
def ajax_delete_listing(request, listing_id):
    """AJAX endpoint to allow owners to delete a listing and broadcast deletion."""
    try:
        listing = get_object_or_404(Listing, id=listing_id)
        if listing.seller != request.user:
            return JsonResponse({'success': False, 'error': 'Forbidden'}, status=403)

        listing_id = listing.id
        listing_title = listing.title
        listing.delete()

        # Broadcast deletion to all users' notification groups
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            channel_layer = get_channel_layer()
            from django.contrib.auth import get_user_model
            User = get_user_model()
            user_ids = list(User.objects.filter(is_active=True).values_list('id', flat=True))
            payload = {'id': listing_id}
            for uid in user_ids:
                try:
                    async_to_sync(channel_layer.group_send)(
                        f'notifications_user_{uid}',
                        {
                            'type': 'listing_deleted',
                            'listing': payload,
                        }
                    )
                except Exception:
                    logger.exception('Failed to send listing_deleted to user %s', uid)
        except Exception:
            logger.exception('Failed to broadcast listing_deleted')

        return JsonResponse({'success': True, 'deleted_id': listing_id, 'message': f'Listing "{listing_title}" deleted'})
    except Exception as e:
        logger.exception('ajax_delete_listing error: %s', e)
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@login_required
def seller_orders(request):
    # By request: show only orders that contain items exclusively from this seller
    # i.e. total order_items == order_items belonging to this seller
    orders = Order.objects.annotate(
        total_items=Count('order_items'),
        seller_items=Count('order_items', filter=Q(order_items__listing__seller=request.user))
    ).filter(total_items=F('seller_items')).order_by('-created_at')

    return render(request, 'listings/seller_orders.html', {
        'orders': orders,
        'delivery_app_order_url': getattr(settings, 'DELIVERY_APP_ORDER_URL', None)
    })

@login_required
def mark_order_shipped(request, order_id):
    # Shipping is managed by the Delivery app. Do not modify order state here.
    order = get_object_or_404(Order, id=order_id)

    # Check permission briefly and then redirect with an informational message
    if not order.order_items.filter(listing__seller=request.user).exists():
        messages.error(request, "You don't have permission to modify this order.")
        return redirect('seller_orders')

    # Prefer explicit Delivery app URL if configured
    from django.conf import settings
    delivery_app_url = getattr(settings, 'DELIVERY_APP_ORDER_URL', None)

    messages.info(request, "Shipping is handled via the Delivery app. Please use the Delivery app to mark shipments.")

    if delivery_app_url:
        try:
            return redirect(delivery_app_url.format(order_id=order.id))
        except Exception:
            # Fall back to seller_orders if formatting fails
            return redirect('seller_orders')

    return redirect('seller_orders')

from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
import json
import hmac
import hashlib
import os


@csrf_exempt
@require_POST
def delivery_webhook_receiver(request):
    """
    Receive status updates from delivery system
    Example events: delivery_out_for_delivery, delivery_delivered, delivery_failed
    """
    try:
        # Verify signature
        signature = request.headers.get('X-Webhook-Signature')
        # Use raw request body bytes to compute HMAC to avoid any decode/encode differences
        body_bytes = request.body

        # Normalize signature header (support formats like 'sha256=...')
        if signature and signature.startswith('sha256='):
            signature = signature.split('=', 1)[1]

        # Validate signature — prefer environment variable for uniformity with tests
        secret_val = os.environ.get('DELIVERY_WEBHOOK_SECRET', getattr(settings, 'DELIVERY_WEBHOOK_SECRET', ''))
        # Ensure no surrounding whitespace or comments
        secret_val = (secret_val or '').strip()
        secret = secret_val.encode('utf-8')

        # Decode body once for logging/parsing
        body_text = body_bytes.decode('utf-8', errors='replace')

        # Compute expected signature for raw body
        expected_raw = hmac.new(secret, body_bytes, hashlib.sha256).hexdigest()

        # Also compute expected signature for canonicalized JSON (sorted keys)
        expected_sorted = None
        try:
            parsed = json.loads(body_text)
            canonical = json.dumps(parsed, separators=(',', ':'), ensure_ascii=False, sort_keys=True).encode('utf-8')
            expected_sorted = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
        except Exception:
            expected_sorted = None

        # If in DEBUG, allow skipping verification for local testing
        if getattr(settings, 'DEBUG', False):
            logger.info('DEBUG mode: skipping webhook signature verification')
            # proceed to parse and handle the payload
        else:
            # Accept if signature matches either raw or canonical form
            if not ((signature or '') and (hmac.compare_digest(signature, expected_raw) or (expected_sorted and hmac.compare_digest(signature, expected_sorted)))):
                # Log details for debugging signature mismatches (local/dev only)
                expected_display = expected_raw if not expected_sorted else f"raw:{expected_raw} sorted:{expected_sorted}"
                logger.warning(
                    "Webhook signature mismatch. received=%r expected=%r body=%r",
                    signature,
                    expected_display,
                    (body_text[:1000])
                )
                # Also print to stdout so it's visible in the devserver console
                try:
                    print(f"Webhook signature mismatch. received={signature!r} expected={expected_display!r} body={(body_text[:1000])!r}")
                except Exception:
                    pass
                return JsonResponse({'error': 'Invalid signature'}, status=403)

        # Parse payload
        data = json.loads(body_text)
        event_type = data.get('event')
        order_id = data.get('order_id')
        tracking_number = data.get('tracking_number')
        
        # Get order
        try:
            order = Order.objects.get(id=order_id)
        except Order.DoesNotExist:
            return JsonResponse({'error': 'Order not found'}, status=404)
        
        # Handle different event types
        if event_type == 'delivery_out_for_delivery':
            order.delivery_status = 'out_for_delivery'
            order.save()
            
            # Notify buyer
            notify_delivery_status(order.user, order, "Your order is out for delivery!")
            
        elif event_type == 'delivery_delivered':
            order.delivery_status = 'delivered'
            order.delivered_at = timezone.now()
            order.save()
            
            # Notify seller and buyer
            notify_delivery_confirmed(order.user, order.seller, order)
            
        elif event_type == 'delivery_failed':
            order.delivery_status = 'failed'
            order.save()
            
            # Notify both parties
            messages = {
                'buyer': "Delivery failed. We'll contact you to reschedule.",
                'seller': f"Delivery failed for order #{order.id}"
            }
            notify_delivery_status(order.user, order, messages['buyer'])
            notify_delivery_status(order.seller, order, messages['seller'])
        
        # Log activity
        Activity.objects.create(
            user=order.user,
            action=f"Delivery status updated via webhook: {event_type} for order #{order.id}"
        )
        
        return JsonResponse({'status': 'success', 'message': 'Webhook processed'})
        
    except Exception as e:
        logger.error(f"Webhook processing error: {str(e)}")
        return JsonResponse({'error': str(e)}, status=500)

def _create_delivery_request(order):
    """Create delivery request in the delivery system"""
    try:
        try:
            from integrations.delivery import DeliverySystemIntegration
        except ImportError:
            logger.error("DeliverySystemIntegration could not be imported. Delivery integration is unavailable.")
            return None

        delivery_integration = DeliverySystemIntegration()
        return delivery_integration.create_delivery_from_order(order)
        
    except Exception as e:
        logger.error(f"Delivery system integration failed: {str(e)}")
        return None

# Update confirm_delivery to notify seller
@login_required
def confirm_delivery(request, order_id):
    """Buyer confirms delivery - MANDATORY for fund release"""
    order = get_object_or_404(Order, id=order_id, user=request.user)
    # Ensure delivery app has recorded proof of delivery before allowing buyer to confirm
    from delivery.models import DeliveryRequest
    delivery = None
    if getattr(order, 'delivery_request_id', None):
        delivery = DeliveryRequest.objects.filter(id=order.delivery_request_id).first()
    if not delivery and order.tracking_number:
        delivery = DeliveryRequest.objects.filter(tracking_number=order.tracking_number).first()

    if not delivery or delivery.status != 'delivered':
        messages.warning(request, "Delivery confirmation is not yet available. Please wait until the delivery app records the delivered status and proof of delivery.")
        return redirect('order_detail', order_id=order.id)

    # Require proof of delivery to exist on the delivery record
    proof = getattr(delivery, 'proof_of_delivery', None) or (delivery.metadata or {}).get('proof')
    if not proof:
        messages.warning(request, "Cannot confirm delivery: proof of delivery not yet recorded. Please contact support or wait for delivery proof.")
        return redirect('order_detail', order_id=order.id)

    if order.status == 'delivered':
        messages.info(request, "Order already confirmed delivered.")
        return redirect('order_detail', order_id=order.id)

    # Update order status
    order.status = 'delivered'
    order.delivered_at = timezone.now()
    order.save()
    
    # Release escrow funds to all sellers
    _release_escrow_to_sellers(order)
    
    # Notify sellers about delivery confirmation and fund release
    _notify_sellers_delivery_confirmed(order)
    
    # Create activity log
    Activity.objects.create(
        user=request.user,
        action=f"Order #{order.id} delivered and confirmed"
    )
    
    messages.success(request, "Thank you for confirming delivery! Funds have been released to the seller(s).")
    return redirect('order_detail', order_id=order.id)

def _release_escrow_to_sellers(order):
    """Release escrow funds to all sellers in the order"""
    # Group order items by seller to handle multiple sellers
    from collections import defaultdict
    seller_amounts = defaultdict(float)
    
    for order_item in order.order_items.all():
        seller = order_item.listing.seller
        seller_amounts[seller] += float(order_item.get_total_price())
    
    # Release funds to each seller
    for seller, amount in seller_amounts.items():
        # In a real system, you'd actually transfer funds here
        # For now, we'll just mark the escrow as released
        logger.info(f"Releasing KSh {amount} to seller {seller.username} for order #{order.id}")
    
    # Update escrow status
    order.escrow.status = 'released'
    order.escrow.released_at = timezone.now()
    order.escrow.save()

def _notify_sellers_delivery_confirmed(order):
    """Notify all sellers that delivery was confirmed and funds released"""
    sellers = set(item.listing.seller for item in order.order_items.all())
    
    for seller in sellers:
        notify_delivery_confirmed(seller, order.user, order)
        
        # Create activity log for seller
        Activity.objects.create(
            user=seller,
            action=f"Delivery confirmed and funds released for Order #{order.id}"
        )
@login_required
def create_dispute(request, order_id):
    order = get_object_or_404(Order, id=order_id, user=request.user)
    
    if order.status not in ['shipped', 'delivered']:
        messages.warning(request, "You can only dispute orders that have been shipped or delivered.")
        return redirect('order_detail', order_id=order.id)
    
    order.status = 'disputed'
    order.save()
    
    order.escrow.status = 'disputed'
    order.escrow.save()
    
    # Create activity log
    Activity.objects.create(
        user=request.user,
        action=f"Dispute created for Order #{order.id}"
    )
    
    messages.info(request, "Dispute created. Our team will review your case and contact you shortly.")
    return redirect('order_detail', order_id=order.id)


from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.decorators import login_required
from .models import Review, Order, ReviewPhoto, Listing
from .forms import ReviewForm

# Add these imports at the top
from django.forms import modelformset_factory
from .forms import ReviewForm, OrderReviewForm, ReviewPhotoForm

# Replace the existing leave_review function with this comprehensive version
@login_required
def leave_review(request, review_type=None, object_id=None):
    """Handle all types of reviews: listing, seller, and order"""
    
    context = {}
    
    # Determine what we're reviewing
    if review_type == 'listing':
        listing = get_object_or_404(Listing, id=object_id)
        seller = listing.seller
        
        if request.user == seller:
            messages.error(request, "You cannot review your own listing.")
            return redirect('listing-detail', pk=listing.id)
        
        # Check if user can review (must have purchased this listing)
        can_review = OrderItem.objects.filter(
            order__user=request.user,
            order__status='delivered',
            listing=listing
        ).exists()
        
        if not can_review:
            messages.error(request, "You can only review items you've purchased and received.")
            return redirect('listing-detail', pk=listing.id)
        
        existing_review = Review.objects.filter(
            user=request.user,
            review_type='listing',
            listing=listing
        ).first()
        
        form = ReviewForm(request.POST or None, instance=existing_review, review_type='listing')
        context.update({
            'review_type': 'listing',
            'listing': listing,
            'seller': seller,
            'title': f"Review: {listing.title}"
        })
        
    elif review_type == 'seller':
        seller = get_object_or_404(User, id=object_id)
        
        if request.user == seller:
            messages.error(request, "You cannot review yourself.")
            return redirect('profile', pk=seller.id)
        
        # Check if user has purchased from this seller
        can_review = OrderItem.objects.filter(
            order__user=request.user,
            order__status='delivered',
            listing__seller=seller
        ).exists()
        
        if not can_review:
            messages.error(request, "You can only review sellers you've purchased from.")
            return redirect('profile', pk=seller.id)
        
        existing_review = Review.objects.filter(
            user=request.user,
            review_type='seller',
            seller=seller
        ).first()
        
        form = ReviewForm(request.POST or None, instance=existing_review, review_type='seller')
        context.update({
            'review_type': 'seller',
            'seller': seller,
            'title': f"Review Seller: {seller.username}"
        })
        
    elif review_type == 'order':
        order = get_object_or_404(Order, id=object_id, user=request.user)
        
        if order.status != 'delivered':
            messages.error(request, "You can only review delivered orders.")
            return redirect('order_detail', order_id=order.id)
        
        existing_review = Review.objects.filter(
            user=request.user,
            review_type='order',
            order=order
        ).first()
        
        # Get all sellers in this order
        sellers = set(item.listing.seller for item in order.order_items.all())
        
        if request.method == 'POST':
            form = OrderReviewForm(request.POST, instance=existing_review)
            photo_form = ReviewPhotoForm(request.POST, request.FILES)
            
            if form.is_valid() and photo_form.is_valid():
                review = form.save(commit=False)
                review.user = request.user
                review.review_type = 'order'
                review.order = order
                review.seller = sellers.pop() if sellers else None  # Use first seller if multiple
                review.save()
                
                # Handle photo uploads
                photos = request.FILES.getlist('photos')
                for photo in photos[:5]:  # Limit to 5 photos
                    if photo.content_type.startswith('image/') and photo.size <= 10 * 1024 * 1024:
                        ReviewPhoto.objects.create(
                            review=review,  # This is the review instance
                            image=photo
                        )
                # Notify all sellers in the order
                for seller in sellers:
                    notify_new_review(seller, request.user, review, review_type='order')
                
                messages.success(request, "Thank you for your comprehensive order review!")
                return redirect('order_detail', order_id=order.id)
        else:
            form = OrderReviewForm(instance=existing_review)
            photo_form = ReviewPhotoForm()
        
        context.update({
            'review_type': 'order',
            'order': order,
            'sellers': sellers,
            'items': order.order_items.all(),
            'photo_form': photo_form,
            'title': f"Review Order #{order.id}"
        })
        context['form'] = form
        return render(request, 'listings/review_order.html', context)

    
    else:
        messages.error(request, "Invalid review type.")
        return redirect('home')
    
    # Handle listing and seller reviews
    if request.method == 'POST':
        if form.is_valid():
            review = form.save(commit=False)
            review.user = request.user
            review.review_type = review_type
            
            if review_type == 'listing':
                review.listing = listing
                review.seller = seller
            elif review_type == 'seller':
                review.seller = seller
            
            review.save()
            
            # Notify seller
            if review_type == 'listing':
                notify_new_review(seller, request.user, review, listing)
            elif review_type == 'seller':
                notify_new_review(seller, request.user, review, review_type='seller')
            
            messages.success(request, "Thank you for your review!")
            
            if review_type == 'listing':
                return redirect('listing-detail', pk=listing.id)
            else:
                return redirect('profile', pk=seller.id)
    
    context['form'] = form
    return render(request, 'listings/create_review.html', context)


@login_required
def create_order_review(request, order_id):
    """Legacy function that redirects to the new review system"""
    return redirect('leave_review', review_type='order', object_id=order_id)


def get_reviews(request, review_type=None, object_id=None):
    """API endpoint to get reviews for different types"""
    if review_type == 'listing':
        reviews = Review.objects.filter(
            review_type='listing',
            listing_id=object_id,
            is_public=True
        ).select_related('user').order_by('-created_at')
    elif review_type == 'seller':
        reviews = Review.objects.filter(
            review_type='seller',
            seller_id=object_id,
            is_public=True
        ).select_related('user').order_by('-created_at')
    elif review_type == 'order':
        reviews = Review.objects.filter(
            review_type='order',
            order_id=object_id,
            is_public=True
        ).select_related('user').order_by('-created_at')
    else:
        return JsonResponse({'error': 'Invalid review type'}, status=400)
    
    reviews_data = []
    for review in reviews:
        reviews_data.append({
            'id': review.id,
            'user': review.user.username,
            'user_avatar': review.user.profile_picture.url if hasattr(review.user, 'profile_picture') else '',
            'rating': review.rating,
            'comment': review.comment,
            'created_at': review.created_at.strftime('%B %d, %Y'),
            'communication_rating': review.communication_rating,
            'delivery_rating': review.delivery_rating,
            'accuracy_rating': review.accuracy_rating,
            'is_verified': review.is_verified_purchase,
            'photos': [photo.image.url for photo in review.review_photos.all()]
        })
    
    # Calculate averages
    avg_rating = reviews.aggregate(Avg('rating'))['rating__avg'] or 0
    avg_communication = reviews.aggregate(Avg('communication_rating'))['communication_rating__avg'] or 0
    avg_delivery = reviews.aggregate(Avg('delivery_rating'))['delivery_rating__avg'] or 0
    avg_accuracy = reviews.aggregate(Avg('accuracy_rating'))['accuracy_rating__avg'] or 0
    
    return JsonResponse({
        'reviews': reviews_data,
        'count': reviews.count(),
        'average_rating': round(avg_rating, 1),
        'average_communication': round(avg_communication, 1),
        'average_delivery': round(avg_delivery, 1),
        'average_accuracy': round(avg_accuracy, 1)
    })

@login_required
def delivery_status_api(request, tracking_number):
    """API endpoint to get delivery status from external system"""
    try:
        order = Order.objects.get(tracking_number=tracking_number)
        
        # Check if user has permission: buyer or any seller in the order
        is_buyer = request.user == order.user
        is_seller = order.order_items.filter(listing__seller=request.user).exists()
        if not (is_buyer or is_seller):
            return JsonResponse({'error': 'Unauthorized'}, status=403)
        
        # In a real implementation, you'd query the delivery system API
        # For now, simulate with order status
        status_map = {
            'pending': {'icon': 'clock', 'status_display': 'Processing', 'message': 'Preparing for dispatch'},
            'shipped': {'icon': 'truck', 'status_display': 'In Transit', 'message': 'On the way to delivery hub'},
            'in_transit': {'icon': 'truck', 'status_display': 'In Transit', 'message': 'On the way to delivery hub'},
            'out_for_delivery': {'icon': 'geo-alt', 'status_display': 'Out for Delivery', 'message': 'Delivery driver en route'},
            'delivered': {'icon': 'check-circle', 'status_display': 'Delivered', 'message': 'Package delivered successfully'},
            'failed': {'icon': 'exclamation-triangle', 'status_display': 'Delivery Failed', 'message': 'Delivery attempt failed'},
            'delivery_failed': {'icon': 'exclamation-triangle', 'status_display': 'Delivery Failed', 'message': 'Delivery attempt failed'},
            'assigned': {'icon': 'person', 'status_display': 'Assigned', 'message': 'Driver assigned to pickup'},
            'accepted': {'icon': 'clock', 'status_display': 'Accepted', 'message': 'Delivery accepted by carrier'},
            'picked_up': {'icon': 'box-seam', 'status_display': 'Picked Up', 'message': 'Package picked up from seller'},
        }
        
        status_data = status_map.get(
            order.delivery_status or 'pending',
            status_map['pending']
        )
        
        status_data.update({
            'status': order.delivery_status or 'pending',
            'last_updated': order.updated_at.strftime('%Y-%m-%d %H:%M'),
            'estimated_delivery': (order.created_at + timedelta(days=3)).strftime('%b %d, %Y')
                if order.delivery_status != 'delivered' else None
        })
        
        return JsonResponse(status_data)
        
    except Order.DoesNotExist:
        return JsonResponse({'error': 'Order not found'}, status=404)

@login_required
def create_order_review(request, order_id):
    """Create review page for an order - lets user choose which item/seller to review"""
    order = get_object_or_404(Order, id=order_id, user=request.user)
    
    # Only allow reviews for delivered orders
    if order.status != 'delivered':
        messages.error(request, "You can only review delivered orders.")
        return redirect('order_detail', order_id=order.id)
    
    # Get unique sellers and items from this order
    sellers = set()
    items = []
    
    for item in order.order_items.all():
        sellers.add(item.listing.seller)
        items.append({
            'item': item,
            'has_listing_review': Review.objects.filter(
                user=request.user,
                listing=item.listing
            ).exists(),
            'has_seller_review': Review.objects.filter(
                user=request.user,
                listing__seller=item.listing.seller
            ).exists()
        })
    
    context = {
        'order': order,
        'sellers': list(sellers),
        'items': items,
    }
    
    return render(request, 'listings/order_review.html', context)