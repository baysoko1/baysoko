"""
AI-powered listing creation views
"""
import json
from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.views.decorators.http import require_POST, require_GET
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.contrib import messages
from .models import Category
"""
AI-powered listing creation views
"""
import json
from django.shortcuts import render, redirect
from django.http import JsonResponse
from django.views.decorators.http import require_POST, require_GET
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from django.contrib import messages
from django.db import transaction
from django.utils import timezone
import logging
from .models import Category, Listing, ListingImage, Activity
# Import Store from storefront app (avoid importing Store from listings.models)
try:
    from storefront.models import Store
except Exception:
    Store = None
from .forms import AIListingForm
from .ai_listing_helper import listing_ai

logger = logging.getLogger(__name__)


@login_required
@require_POST
@csrf_exempt
def ai_generate_listing(request):
    """AJAX endpoint to generate listing with AI."""
    if not request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'error': 'Invalid request'}, status=400)
    
    try:
        from storefront.ai_copilot import has_seller_ai_access

        data = json.loads(request.body)
        title = data.get('title', '').strip()
        description = data.get('description', '').strip()
        current_category = data.get('category', '')
        user_store = None
        if Store is not None:
            user_store = Store.objects.filter(owner=request.user).first()

        if not has_seller_ai_access(request.user, store=user_store):
            return JsonResponse({
                'error': 'Baysoko AI Copilot is available on Premium and Enterprise plans.',
                'success': False,
            }, status=403)
        
        if not title:
            return JsonResponse({'error': 'Title is required'}, status=400)
        
        # Generate listing data with AI
        user_input = {
            'title': title,
            'description': description,
            'category': current_category,
        }
        
        # Add any other fields from the request
        for field in ['price', 'brand', 'model', 'condition', 'location', 'delivery_option']:
            if field in data:
                user_input[field] = data[field]
        
        ai_data = listing_ai.generate_listing_data(user_input)
        
        # Map category name to ID if needed
        if ai_data.get('category') and not current_category:
            category = Category.objects.filter(
                name__iexact=ai_data['category']
            ).first()
            if category:
                ai_data['category_id'] = category.id
                ai_data['category_name'] = category.name
            else:
                # Create or get default category
                default_cat = Category.objects.filter(name='Other').first()
                if default_cat:
                    ai_data['category_id'] = default_cat.id
                    ai_data['category_name'] = default_cat.name
        
        # Get category suggestions
        category_suggestions = listing_ai.suggest_categories(title, description)
        
        return JsonResponse({
            'success': True,
            'data': ai_data,
            'category_suggestions': category_suggestions,
            'ai_enabled': listing_ai.enabled,
            'ai_error': getattr(listing_ai, 'last_error', None)
        })
        
    except Exception as e:
        logger.error(f"AI generation error: {str(e)}", exc_info=True)
        return JsonResponse({
            'error': str(e),
            'success': False
        }, status=500)



@login_required
@require_GET
def ai_listing_wizard(request):
    """Deprecated: redirect to the standard listing create page which now supports AI flows."""
    try:
        user_stores = Store.objects.filter(owner=request.user)
        if not user_stores.exists():
            messages.info(request, "You need to create a store first before listing items.")
            return redirect('storefront:store_create')
    except Exception:
        pass

    # Redirect to canonical listing-create view; client can toggle AI there
    return redirect('listing-create')



@login_required
def create_with_ai(request):
    """Create listing with AI assistance."""
    # Check if user has a store
    try:
        user_stores = Store.objects.filter(owner=request.user)
        if not user_stores.exists():
            messages.info(request, "You need to create a store first before listing items.")
            return redirect('storefront:store_create')
    except:
        pass
    
    if request.method == 'POST':
        form = AIListingForm(request.POST, request.FILES, user=request.user)
        
        # Check if user wants AI assistance
        use_ai = request.POST.get('use_ai') == 'on'
        
        if use_ai:
            # Generate AI suggestions
            ai_data = form.generate_with_ai()
            
            # Re-initialize form with AI data
            form = AIListingForm(request.POST, request.FILES, user=request.user)
            
            # Add AI suggestions to context
            context = {
                'form': form,
                'ai_suggestions': ai_data,
                'ai_used': True,
                'categories': Category.objects.filter(is_active=True),
            }
            
            return render(request, 'listings/listing_form_ai.html', context)
        
        if form.is_valid():
            try:
                with transaction.atomic():
                    listing = form.save(commit=False)
                    listing.seller = request.user
                    
                    # Handle store assignment
                    store = form.cleaned_data.get('store')
                    if store:
                        listing.store = store
                    
                    # Ensure stock is at least 1
                    if not listing.stock or listing.stock < 1:
                        listing.stock = 1
                    
                    listing.save()
                    
                    # Handle multiple images
                    images = request.FILES.getlist('images')
                    for image in images:
                        if image.content_type.startswith('image/') and image.size <= 10 * 1024 * 1024:
                            ListingImage.objects.create(
                                listing=listing,
                                image=image
                            )
                    
                    # Create activity log
                    Activity.objects.create(
                        user=request.user,
                        action=f"Created listing with AI assistance: {listing.title}"
                    )
                    
                    messages.success(request, "Listing created successfully with AI assistance!")
                    return redirect('listing-detail', pk=listing.id)
                    
            except Exception as e:
                messages.error(request, f"Error creating listing: {str(e)}")
                logger.error(f"Error creating listing: {str(e)}", exc_info=True)
        else:
            messages.error(request, "Please correct the errors below.")
    else:
        form = AIListingForm(user=request.user)
    
    return render(request, 'listings/listing_form_ai.html', {
        'form': form,
        'ai_enabled': listing_ai.enabled,
        'ai_error': getattr(listing_ai, 'last_error', None),
        'categories': Category.objects.filter(is_active=True),
    })


@login_required
@require_POST
def ai_quick_listing(request):
    """Create a listing quickly with minimal input using AI."""
    # Check if user has a store
    try:
        user_stores = Store.objects.filter(owner=request.user)
        if not user_stores.exists():
            messages.error(request, "You need to create a store first before listing items.")
            return redirect('storefront:store_create')
    except:
        pass
    
    title = request.POST.get('title', '').strip()
    price = request.POST.get('price', '')
    
    if not title:
        messages.error(request, "Title is required.")
        return redirect('ai_listing_wizard')
    
    # Generate complete listing with AI
    user_input = {
        'title': title,
        'price': price,
    }
    
    try:
        ai_data = listing_ai.generate_listing_data(user_input)
        
        # Create form with AI data
        form_data = {
            'title': ai_data['title'],
            'description': ai_data['description'],
            'price': ai_data['price'] or 0,
            'condition': ai_data['condition'],
            'delivery_option': ai_data['delivery_option'],
            'location': ai_data['location'],
            'brand': ai_data.get('brand', ''),
            'model': ai_data.get('model', ''),
            'dimensions': ai_data.get('dimensions', ''),
            'weight': ai_data.get('weight', ''),
            'color': ai_data.get('color', ''),
            'material': ai_data.get('material', ''),
            'meta_description': ai_data.get('meta_description', ''),
            'stock': 1,  # Default stock
            'use_ai': True,
        }
        
        # Get or set category
        category_name = ai_data.get('category', 'Other')
        category = Category.objects.filter(name__iexact=category_name).first()
        if category:
            form_data['category'] = category.id
        
        # Get user's first store
        user_store = Store.objects.filter(owner=request.user).first()
        if user_store:
            form_data['store'] = user_store.id
        
        form = AIListingForm(form_data, user=request.user)

        return render(request, 'listings/listing_form.html', {
            'form': form,
            'ai_suggestions': ai_data,
            'ai_used': True,
            'quick_mode': True,
            'categories': Category.objects.filter(is_active=True),
            'ai_error': getattr(listing_ai, 'last_error', None),
        })
        
    except Exception as e:
        logger.error(f"AI quick listing failed: {str(e)}", exc_info=True)
        messages.error(request, f"AI generation failed. Please try again or use the standard form.")
        return redirect('ai_listing_wizard')


# Add this logger
import logging
logger = logging.getLogger(__name__)
