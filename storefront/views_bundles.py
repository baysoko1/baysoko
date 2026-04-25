# storefront/views_bundles.py
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST, require_GET
from django.db.models import Q, Count, Sum, F, Value, CharField
from django.db import transaction, DatabaseError, OperationalError, ProgrammingError
from django.core.paginator import Paginator
from django.utils import timezone
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin
from datetime import timedelta, datetime
import json

from .models import Store
from .models_bundles import (
    ProductBundle, BundleItem, BundleRule, 
    UpsellProduct, ProductTemplate
)
from .utils.db import safe_db_query
from .forms_bundles import (
    ProductBundleForm, BundleItemForm, BundleRuleForm,
    UpsellProductForm, ProductTemplateForm, QuickProductForm
)
from listings.models import Listing, Category
from .decorators import staff_required, store_owner_required

@login_required
@store_owner_required('edit')
def bundle_dashboard(request, slug):
    """Bundle management dashboard"""
    store = get_object_or_404(Store, slug=slug)
    # Get bundle statistics. Guard DB access in case migrations haven't been applied yet.
    _migrations_missing = False
    try:
        total_bundles = store.bundles.count()
        active_bundles = store.bundles.filter(is_active=True).count()
        featured_bundles = store.bundles.filter(featured=True, is_active=True).count()

        # Get recent bundles
        recent_bundles = store.bundles.select_related('category').order_by('-created_at')[:10]

        # Get bundle rules
        active_rules = store.bundle_rules.filter(is_active=True).count()

        # Get templates
        templates = store.product_templates.filter(is_active=True).count()
    except (DatabaseError, OperationalError, ProgrammingError):
        # Database tables (e.g. storefront_productbundle) may not exist in some
        # environments.  Provide safe defaults so the dashboard can still render
        # and guide the developer to run pending migrations.
        total_bundles = 0
        active_bundles = 0
        featured_bundles = 0
        recent_bundles = []
        active_rules = 0
        templates = 0
        _migrations_missing = True

    if _migrations_missing:
        messages.warning(
            request,
            'Bundle tables are not available yet. '
            'Run <code>python manage.py migrate</code> to apply pending migrations.',
        )
    
    context = {
        'store': store,
        'total_bundles': total_bundles,
        'active_bundles': active_bundles,
        'featured_bundles': featured_bundles,
        'recent_bundles': recent_bundles,
        'active_rules': active_rules,
        'templates': templates,
    }
    
    return render(request, 'storefront/bundles/dashboard.html', context)

@login_required
@store_owner_required('edit')
def bundle_list(request, slug):
    """List all bundles"""
    store = get_object_or_404(Store, slug=slug)

    try:
        bundles = store.bundles.select_related('category').prefetch_related('items')

        # Apply filters
        status = request.GET.get('status')
        featured = request.GET.get('featured')
        search = request.GET.get('search')

        if status == 'active':
            bundles = bundles.filter(is_active=True)
        elif status == 'inactive':
            bundles = bundles.filter(is_active=False)

        if featured == 'yes':
            bundles = bundles.filter(featured=True)
        elif featured == 'no':
            bundles = bundles.filter(featured=False)

        if search:
            bundles = bundles.filter(
                Q(name__icontains=search) |
                Q(description__icontains=search) |
                Q(sku__icontains=search)
            )

        # Sorting
        sort_by = request.GET.get('sort', 'created_at')
        sort_order = request.GET.get('order', 'desc')

        if sort_by == 'name':
            bundles = bundles.order_by('name' if sort_order == 'asc' else '-name')
        elif sort_by == 'price':
            bundles = bundles.order_by('bundle_price' if sort_order == 'asc' else '-bundle_price')
        elif sort_by == 'stock':
            bundles = bundles.order_by('stock' if sort_order == 'asc' else '-stock')
        else:  # created_at
            bundles = bundles.order_by('created_at' if sort_order == 'asc' else '-created_at')

        # Pagination
        paginator = Paginator(bundles, 20)
        page_number = request.GET.get('page')
        page_obj = paginator.get_page(page_number)
    except (DatabaseError, OperationalError, ProgrammingError):
        messages.warning(
            request,
            'Bundle tables are not available yet. '
            'Run <code>python manage.py migrate</code> to apply pending migrations.',
        )
        return redirect('storefront:bundle_dashboard', slug=slug)

    context = {
        'store': store,
        'page_obj': page_obj,
        'status': status,
        'featured': featured,
        'search': search or '',
        'sort_by': sort_by,
        'sort_order': sort_order,
    }

    return render(request, 'storefront/bundles/list.html', context)

@login_required
@store_owner_required('edit')
def bundle_create(request, slug):
    """Create a new bundle"""
    store = get_object_or_404(Store, slug=slug)

    try:
        if request.method == 'POST':
            form = ProductBundleForm(store, request.POST, request.FILES)
            if form.is_valid():
                bundle = form.save(commit=False)
                bundle.store = store
                bundle.save()

                messages.success(request, 'Bundle created successfully. Now add products to it.')
                return redirect('storefront:bundle_items', slug=slug, bundle_id=bundle.id)
        else:
            form = ProductBundleForm(store)
    except (DatabaseError, OperationalError, ProgrammingError):
        messages.warning(
            request,
            'Bundle tables are not available yet. '
            'Run <code>python manage.py migrate</code> to apply pending migrations.',
        )
        return redirect('storefront:bundle_dashboard', slug=slug)

    context = {
        'store': store,
        'form': form,
    }

    return render(request, 'storefront/bundles/create.html', context)

@login_required
@store_owner_required('edit')
def bundle_edit(request, slug, bundle_id):
    """Edit a bundle"""
    store = get_object_or_404(Store, slug=slug)

    try:
        bundle = get_object_or_404(ProductBundle, id=bundle_id, store=store)

        if request.method == 'POST':
            form = ProductBundleForm(store, request.POST, request.FILES, instance=bundle)
            if form.is_valid():
                form.save()

                messages.success(request, 'Bundle updated successfully.')
                return redirect('storefront:bundle_detail', slug=slug, bundle_id=bundle.id)
        else:
            form = ProductBundleForm(store, instance=bundle)
    except (DatabaseError, OperationalError, ProgrammingError):
        messages.warning(
            request,
            'Bundle tables are not available yet. '
            'Run <code>python manage.py migrate</code> to apply pending migrations.',
        )
        return redirect('storefront:bundle_dashboard', slug=slug)

    context = {
        'store': store,
        'bundle': bundle,
        'form': form,
    }

    return render(request, 'storefront/bundles/edit.html', context)

@login_required
@store_owner_required('edit')
def bundle_detail(request, slug, bundle_id):
    """View bundle details"""
    store = get_object_or_404(Store, slug=slug)

    try:
        bundle = get_object_or_404(ProductBundle, id=bundle_id, store=store)
        items = bundle.items.select_related('product').order_by('display_order')
    except (DatabaseError, OperationalError, ProgrammingError):
        messages.warning(
            request,
            'Bundle tables are not available yet. '
            'Run <code>python manage.py migrate</code> to apply pending migrations.',
        )
        return redirect('storefront:bundle_dashboard', slug=slug)

    context = {
        'store': store,
        'bundle': bundle,
        'items': items,
    }

    return render(request, 'storefront/bundles/detail.html', context)

@login_required
@store_owner_required('edit')
def bundle_items(request, slug, bundle_id):
    """Manage bundle items"""
    store = get_object_or_404(Store, slug=slug)

    try:
        bundle = get_object_or_404(ProductBundle, id=bundle_id, store=store)
        items = bundle.items.select_related('product').order_by('display_order')

        if request.method == 'POST':
            form = BundleItemForm(bundle, request.POST)
            if form.is_valid():
                item = form.save(commit=False)
                item.bundle = bundle
                item.save()

                # Add substitute options if any
                substitute_ids = request.POST.getlist('substitute_options')
                if substitute_ids:
                    substitutes = Listing.objects.filter(
                        id__in=substitute_ids,
                        store=store,
                        is_active=True
                    )
                    item.substitute_options.set(substitutes)

                # Recalculate bundle price
                bundle.save()  # This triggers price recalculation

                messages.success(request, 'Product added to bundle.')
                return redirect('storefront:bundle_items', slug=slug, bundle_id=bundle.id)
        else:
            form = BundleItemForm(bundle)

        # Get available products for substitutes
        available_products = Listing.objects.filter(
            store=store,
            is_active=True
        ).exclude(
            id__in=items.values_list('product_id', flat=True)
        )
    except (DatabaseError, OperationalError, ProgrammingError):
        messages.warning(
            request,
            'Bundle tables are not available yet. '
            'Run <code>python manage.py migrate</code> to apply pending migrations.',
        )
        return redirect('storefront:bundle_dashboard', slug=slug)

    context = {
        'store': store,
        'bundle': bundle,
        'items': items,
        'form': form,
        'available_products': available_products,
    }

    return render(request, 'storefront/bundles/items.html', context)

@require_POST
@login_required
@store_owner_required('edit')
def bundle_item_delete(request, slug, bundle_id, item_id):
    """Delete item from bundle"""
    store = get_object_or_404(Store, slug=slug)

    try:
        bundle = get_object_or_404(ProductBundle, id=bundle_id, store=store)
        item = get_object_or_404(BundleItem, id=item_id, bundle=bundle)

        item.delete()

        # Recalculate bundle price
        bundle.save()

        messages.success(request, 'Item removed from bundle.')
        return redirect('storefront:bundle_items', slug=slug, bundle_id=bundle.id)
    except (DatabaseError, OperationalError, ProgrammingError):
        messages.warning(
            request,
            'Bundle tables are not available yet. '
            'Run <code>python manage.py migrate</code> to apply pending migrations.',
        )
        return redirect('storefront:bundle_dashboard', slug=slug)

@require_POST
@login_required
@store_owner_required('edit')
def bundle_toggle_active(request, slug, bundle_id):
    """Toggle bundle active status"""
    store = get_object_or_404(Store, slug=slug)

    try:
        bundle = get_object_or_404(ProductBundle, id=bundle_id, store=store)

        bundle.is_active = not bundle.is_active
        bundle.save()

        status = "activated" if bundle.is_active else "deactivated"
        messages.success(request, f'Bundle {status} successfully.')

        return redirect('storefront:bundle_detail', slug=slug, bundle_id=bundle.id)
    except (DatabaseError, OperationalError, ProgrammingError):
        messages.warning(
            request,
            'Bundle tables are not available yet. '
            'Run <code>python manage.py migrate</code> to apply pending migrations.',
        )
        return redirect('storefront:bundle_dashboard', slug=slug)

@require_POST
@login_required
@store_owner_required('edit')
def bundle_delete(request, slug, bundle_id):
    """Delete a bundle"""
    store = get_object_or_404(Store, slug=slug)

    try:
        bundle = get_object_or_404(ProductBundle, id=bundle_id, store=store)
        bundle.delete()
        messages.success(request, 'Bundle deleted successfully.')
        return redirect('storefront:bundle_list', slug=slug)
    except (DatabaseError, OperationalError, ProgrammingError):
        messages.warning(
            request,
            'Bundle tables are not available yet. '
            'Run <code>python manage.py migrate</code> to apply pending migrations.',
        )
        return redirect('storefront:bundle_dashboard', slug=slug)

# Bundle Rules Views
@login_required
@store_owner_required('edit')
def bundle_rules(request, slug):
    """Manage bundle rules"""
    store = get_object_or_404(Store, slug=slug)

    try:
        rules = store.bundle_rules.all()

        if request.method == 'POST':
            form = BundleRuleForm(store, request.POST)
            if form.is_valid():
                rule = form.save(commit=False)
                rule.store = store
                rule.save()

                messages.success(request, 'Bundle rule created successfully.')
                return redirect('storefront:bundle_rules', slug=slug)
        else:
            form = BundleRuleForm(store)
    except (DatabaseError, OperationalError, ProgrammingError):
        messages.warning(
            request,
            'Bundle tables are not available yet. '
            'Run <code>python manage.py migrate</code> to apply pending migrations.',
        )
        return redirect('storefront:bundle_dashboard', slug=slug)

    context = {
        'store': store,
        'rules': rules,
        'form': form,
    }

    return render(request, 'storefront/bundles/rules.html', context)

@require_POST
@login_required
@store_owner_required('edit')
def bundle_rule_delete(request, slug, rule_id):
    """Delete bundle rule"""
    store = get_object_or_404(Store, slug=slug)

    try:
        rule = get_object_or_404(BundleRule, id=rule_id, store=store)
        rule.delete()
        messages.success(request, 'Bundle rule deleted successfully.')
        return redirect('storefront:bundle_rules', slug=slug)
    except (DatabaseError, OperationalError, ProgrammingError):
        messages.warning(
            request,
            'Bundle tables are not available yet. '
            'Run <code>python manage.py migrate</code> to apply pending migrations.',
        )
        return redirect('storefront:bundle_dashboard', slug=slug)

# Upsell Products Views
@login_required
@store_owner_required('edit')
def upsell_products(request, slug):
    """Manage upsell products"""
    store = get_object_or_404(Store, slug=slug)

    try:
        upsells = UpsellProduct.objects.filter(
            base_product__store=store
        ).select_related('base_product', 'upsell_product')

        if request.method == 'POST':
            form = UpsellProductForm(store, request.POST)
            if form.is_valid():
                upsell = form.save()
                messages.success(request, 'Upsell product added successfully.')
                return redirect('storefront:upsell_products', slug=slug)
        else:
            form = UpsellProductForm(store)
    except (DatabaseError, OperationalError, ProgrammingError):
        messages.warning(
            request,
            'Bundle tables are not available yet. '
            'Run <code>python manage.py migrate</code> to apply pending migrations.',
        )
        return redirect('storefront:bundle_dashboard', slug=slug)

    context = {
        'store': store,
        'upsells': upsells,
        'form': form,
    }

    return render(request, 'storefront/bundles/upsells.html', context)

@require_POST
@login_required
@store_owner_required('edit')
def upsell_delete(request, slug, upsell_id):
    """Delete upsell product"""
    store = get_object_or_404(Store, slug=slug)

    try:
        upsell = get_object_or_404(UpsellProduct, id=upsell_id, base_product__store=store)
        upsell.delete()
        messages.success(request, 'Upsell product removed successfully.')
        return redirect('storefront:upsell_products', slug=slug)
    except (DatabaseError, OperationalError, ProgrammingError):
        messages.warning(
            request,
            'Bundle tables are not available yet. '
            'Run <code>python manage.py migrate</code> to apply pending migrations.',
        )
        return redirect('storefront:bundle_dashboard', slug=slug)

# Product Templates Views
@login_required
@store_owner_required('edit')
def product_templates(request, slug):
    """Manage product templates"""
    store = get_object_or_404(Store, slug=slug)

    try:
        templates = store.product_templates.all()

        if request.method == 'POST':
            form = ProductTemplateForm(store, request.user, request.POST)
            if form.is_valid():
                template = form.save()

                # Handle default images
                image_ids = request.POST.getlist('default_images')
                if image_ids:
                    from listings.models import ListingImage
                    images = ListingImage.objects.filter(id__in=image_ids)
                    template.default_images.set(images)

                messages.success(request, 'Product template created successfully.')
                return redirect('storefront:product_templates', slug=slug)
        else:
            form = ProductTemplateForm(store, request.user)

        # Get available images for templates
        from listings.models import ListingImage
        available_images = ListingImage.objects.filter(
            listing__store=store
        ).distinct()[:50]
    except (DatabaseError, OperationalError, ProgrammingError):
        messages.warning(
            request,
            'Bundle tables are not available yet. '
            'Run <code>python manage.py migrate</code> to apply pending migrations.',
        )
        return redirect('storefront:bundle_dashboard', slug=slug)

    context = {
        'store': store,
        'templates': templates,
        'form': form,
        'available_images': available_images,
    }

    return render(request, 'storefront/bundles/templates.html', context)

@login_required
@store_owner_required('edit')
def quick_product_create(request, slug):
    """Quick product creation from template"""
    store = get_object_or_404(Store, slug=slug)

    try:
        if request.method == 'POST':
            form = QuickProductForm(store, request.POST)
            if form.is_valid():
                template = form.cleaned_data['template']

                # Prepare template variables
                variables = {}
                for key, value in request.POST.items():
                    if key.startswith('var_'):
                        var_name = key[4:]  # Remove 'var_' prefix
                        variables[var_name] = value

                # Add form data to variables
                variables.update({
                    'title': form.cleaned_data.get('title', ''),
                    'price': form.cleaned_data.get('price'),
                    'stock': form.cleaned_data.get('stock'),
                })

                # Create product from template
                product = template.create_product(**variables)

                messages.success(request, f'Product "{product.title}" created successfully from template.')
                return redirect('storefront:product_edit', pk=product.id)
        else:
            form = QuickProductForm(store)
    except (DatabaseError, OperationalError, ProgrammingError):
        messages.warning(
            request,
            'Bundle tables are not available yet. '
            'Run <code>python manage.py migrate</code> to apply pending migrations.',
        )
        return redirect('storefront:bundle_dashboard', slug=slug)

    context = {
        'store': store,
        'form': form,
    }

    return render(request, 'storefront/bundles/quick_create.html', context)

@require_GET
@login_required
@store_owner_required('edit')
def get_template_variables(request, slug, template_id):
    """Get template variables for a template (AJAX)"""
    store = get_object_or_404(Store, slug=slug)

    try:
        template = get_object_or_404(ProductTemplate, id=template_id, store=store)

        # Extract variables from title template
        import re
        variables = re.findall(r'\{(\w+)\}', template.title_template)

        return JsonResponse({
            'variables': variables,
            'template': {
                'title_template': template.title_template,
                'description_template': template.description_template,
                'price': str(template.price) if template.price else '',
                'stock': template.stock,
            }
        })
    except (DatabaseError, OperationalError, ProgrammingError) as exc:
        return JsonResponse({'error': 'Bundle tables not available. Run migrations.', 'detail': str(exc)}, status=503)

@require_POST
@login_required
@store_owner_required('edit')
def template_delete(request, slug, template_id):
    """Delete product template"""
    store = get_object_or_404(Store, slug=slug)

    try:
        template = get_object_or_404(ProductTemplate, id=template_id, store=store)
        template.delete()
        messages.success(request, 'Template deleted successfully.')
        return redirect('storefront:product_templates', slug=slug)
    except (DatabaseError, OperationalError, ProgrammingError):
        messages.warning(
            request,
            'Bundle tables are not available yet. '
            'Run <code>python manage.py migrate</code> to apply pending migrations.',
        )
        return redirect('storefront:bundle_dashboard', slug=slug)

# Bulk Image Upload
@login_required
@store_owner_required
def bulk_image_upload(request, slug):
    """Bulk upload images for products"""
    store = get_object_or_404(Store, slug=slug)
    
    if request.method == 'POST' and request.FILES.getlist('images'):
        images = request.FILES.getlist('images')
        product_id = request.POST.get('product_id')
        
        if not product_id:
            messages.error(request, 'Please select a product.')
            return redirect('storefront:bulk_image_upload', slug=slug)
        
        product = get_object_or_404(Listing, id=product_id, store=store)
        
        uploaded_count = 0
        failed_images = []
        
        for image in images:
            try:
                # Validate image
                if not image.content_type.startswith('image/'):
                    raise ValueError('Invalid file type')
                
                if image.size > 10 * 1024 * 1024:  # 10MB
                    raise ValueError('File too large')
                
                # Create listing image
                from listings.models import ListingImage
                ListingImage.objects.create(
                    listing=product,
                    image=image,
                    uploaded_by=request.user
                )
                
                uploaded_count += 1
                
            except Exception as e:
                failed_images.append({
                    'name': image.name,
                    'error': str(e)
                })
        
        # If this is an AJAX request, return JSON so the frontend can handle progress and results
        is_ajax = request.headers.get('x-requested-with') == 'XMLHttpRequest'

        if is_ajax:
            return JsonResponse({
                'success': True if uploaded_count > 0 else False,
                'uploaded': uploaded_count,
                'failed': failed_images
            })

        if uploaded_count > 0:
            messages.success(request, f'{uploaded_count} images uploaded successfully.')
        
        if failed_images:
            messages.warning(request, f'{len(failed_images)} images failed to upload.')
        
        return redirect('storefront:product_edit', pk=product.id)
    
    # Get products for dropdown
    products = store.listings.filter(is_active=True).order_by('title')
    
    context = {
        'store': store,
        'products': products,
    }
    
    return render(request, 'storefront/bundles/bulk_images.html', context)

# Product Recommendations
@login_required
@store_owner_required('edit')
def product_recommendations(request, slug):
    """Manage product recommendations"""
    store = get_object_or_404(Store, slug=slug)
    
    # Get products that are frequently bought together
    from django.db.models import Count
    
    # This is a simplified example - you'd need actual order data
    products = store.listings.filter(is_active=True).annotate(
        order_count=Count('order_items')
    ).order_by('-order_count')[:20]
    
    context = {
        'store': store,
        'products': products,
    }
    
    return render(request, 'storefront/bundles/recommendations.html', context)

# AJAX endpoints for drag and drop
@require_POST
@login_required
@store_owner_required('edit')
def update_bundle_item_order(request, slug, bundle_id):
    """Update bundle item display order (AJAX)"""
    store = get_object_or_404(Store, slug=slug)

    try:
        bundle = get_object_or_404(ProductBundle, id=bundle_id, store=store)
        data = json.loads(request.body)
        items = data.get('items', [])

        with transaction.atomic():
            for item_data in items:
                item = BundleItem.objects.get(
                    id=item_data['id'],
                    bundle=bundle
                )
                item.display_order = item_data['order']
                item.save()

        return JsonResponse({'success': True})

    except (DatabaseError, OperationalError, ProgrammingError) as e:
        return JsonResponse({'success': False, 'error': 'Bundle tables not available. Run migrations.'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

@require_POST
@login_required
@store_owner_required('edit')
def update_bundle_order(request, slug):
    """Update bundle display order (AJAX)"""
    store = get_object_or_404(Store, slug=slug)

    try:
        data = json.loads(request.body)
        bundles = data.get('bundles', [])

        with transaction.atomic():
            for bundle_data in bundles:
                bundle = ProductBundle.objects.get(
                    id=bundle_data['id'],
                    store=store
                )
                bundle.display_order = bundle_data['order']
                bundle.save()

        return JsonResponse({'success': True})

    except (DatabaseError, OperationalError, ProgrammingError) as e:
        return JsonResponse({'success': False, 'error': 'Bundle tables not available. Run migrations.'})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})