# storefront/views_inventory.py
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST, require_GET
from django.db.models import Q, Sum, F, Count, Case, When, Value, IntegerField, Avg
from django.db import transaction
from django.core.paginator import Paginator
from django.utils import timezone
from datetime import timedelta
import json
from io import BytesIO
import csv
import logging

from .models import Store, InventoryAlert, ProductVariant, StockMovement, InventoryAudit
from .models_bulk import BatchJob, ImportTemplate
from .tasks_bulk import process_import_task
from listings.models import Order, OrderItem
from .forms_inventory import (
    InventoryAlertForm, ProductVariantForm, 
    StockAdjustmentForm, InventoryAuditForm,
    BulkStockUpdateForm
)
from listings.models import Listing, Category
from .decorators import store_owner_required, plan_required
from .ai_copilot import build_seller_copilot_context, has_seller_ai_access

logger = logging.getLogger(__name__)

@login_required
@store_owner_required('inventory')
@plan_required('inventory')
def inventory_dashboard(request, slug):
    """Main inventory dashboard with overview"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    
    # Inventory metrics
    total_products = store.listings.count()
    low_stock_items = store.listings.filter(stock__lte=5, stock__gt=0).count()
    out_of_stock_items = store.listings.filter(stock=0).count()
    
    # Stock value
    stock_value = store.listings.aggregate(
        total_value=Sum(F('price') * F('stock'))
    )['total_value'] or 0
    
    # Recent stock movements (include StockMovement records and recent successful orders)
    stock_moves = list(StockMovement.objects.filter(store=store).select_related('product', 'created_by').order_by('-created_at')[:10])

    # Recent successful orders that affect this store
    recent_orders = Order.objects.filter(
        order_items__listing__store=store,
        status__in=['paid', 'delivered']
    ).distinct().order_by('-created_at')[:10]

    # Convert orders into movement-like dicts for unified display
    order_movements = []
    for order in recent_orders:
        # sum quantities for items in this store
        items = order.order_items.filter(listing__store=store)
        total_qty = 0
        first_listing = None
        for it in items:
            total_qty += it.quantity
            if not first_listing:
                first_listing = it.listing

        if not first_listing:
            continue

        # For display, sales reduce stock -> represent as negative quantity
        qty = -total_qty

        # previous_stock approximated as current stock + items sold
        prev_stock = None
        try:
            prev_stock = (first_listing.stock or 0) + total_qty
        except Exception:
            prev_stock = None

        order_movements.append({
            'created_at': order.created_at,
            'product': first_listing,
            'movement_type': 'order',
            'get_movement_type_display': 'Order',
            'quantity': qty,
            'previous_stock': prev_stock,
            'new_stock': first_listing.stock,
            'created_by': getattr(order, 'user', None),
            'notes': f'Order #{order.id}'
        })

    # Merge and sort movements by timestamp
    recent_combined = []
    # include model instances and dicts together
    recent_combined.extend(stock_moves)
    recent_combined.extend(order_movements)
    recent_combined.sort(key=lambda x: x.created_at if hasattr(x, 'created_at') else x.get('created_at'), reverse=True)
    recent_movements = recent_combined[:10]
    
    # Active alerts
    active_alerts = InventoryAlert.objects.filter(
        store=store,
        is_active=True
    ).count()
    
    # Stock turnover (last 30 days) - use order items sold across store listings
    thirty_days_ago = timezone.now() - timedelta(days=30)
    sales_items = OrderItem.objects.filter(
        listing__store=store,
        order__status__in=['paid', 'delivered'],
        order__created_at__gte=thirty_days_ago
    ).aggregate(total_sold=Sum('quantity'))['total_sold'] or 0

    # Calculate average stock across products (current average)
    avg_stock = store.listings.aggregate(
        avg_stock=Avg('stock')
    )['avg_stock'] or 0

    # Turnover rate: units sold / average stock over the period; express as percentage
    turnover_rate = (sales_items / avg_stock * 100) if avg_stock > 0 else 0
    
    # Category distribution
    category_distribution = store.listings.values(
        'category__name'
    ).annotate(
        count=Count('id'),
        total_stock=Sum('stock')
    ).order_by('-count')[:5]
    
    context = {
        'store': store,
        'total_products': total_products,
        'low_stock_items': low_stock_items,
        'out_of_stock_items': out_of_stock_items,
        'stock_value': stock_value,
        'active_alerts': active_alerts,
        'turnover_rate': round(turnover_rate, 2),
        'recent_movements': recent_movements,
        'category_distribution': category_distribution,
        'seller_ai': build_seller_copilot_context(request.user, store=store),
        'seller_ai_access': has_seller_ai_access(request.user, store=store),
    }
    
    return render(request, 'storefront/inventory/dashboard.html', context)

@login_required
@store_owner_required('inventory')
def inventory_list(request, slug):
    """Detailed inventory listing with filters"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    
    # Get filter parameters
    category_id = request.GET.get('category')
    stock_status = request.GET.get('stock_status')
    search_query = request.GET.get('q')
    sort_by = request.GET.get('sort', 'name')
    sort_order = request.GET.get('order', 'asc')
    
    # Base queryset
    products = store.listings.select_related('category').prefetch_related('variants')
    
    # Apply filters
    if category_id:
        products = products.filter(category_id=category_id)
    
    if stock_status:
        if stock_status == 'low':
            products = products.filter(stock__lte=5, stock__gt=0)
        elif stock_status == 'out':
            products = products.filter(stock=0)
        elif stock_status == 'good':
            products = products.filter(stock__gt=10)
    
    if search_query:
        products = products.filter(
            Q(title__icontains=search_query) |
            Q(description__icontains=search_query) |
            Q(sku__icontains=search_query)
        )
    
    # Apply sorting
    if sort_by == 'name':
        products = products.order_by('title' if sort_order == 'asc' else '-title')
    elif sort_by == 'stock':
        products = products.order_by('stock' if sort_order == 'asc' else '-stock')
    elif sort_by == 'price':
        products = products.order_by('price' if sort_order == 'asc' else '-price')
    elif sort_by == 'sales':
        products = products.annotate(
            sales_count=Count('order_items')
        ).order_by('sales_count' if sort_order == 'asc' else '-sales_count')
    
    # Pagination
    paginator = Paginator(products, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    # Get categories for filter dropdown
    categories = Category.objects.filter(
        listing__store=store
    ).distinct()
    
    context = {
        'store': store,
        'page_obj': page_obj,
        'categories': categories,
        'selected_category': category_id,
        'selected_status': stock_status,
        'search_query': search_query or '',
        'sort_by': sort_by,
        'sort_order': sort_order,
    }
    
    return render(request, 'storefront/inventory/list.html', context)

@login_required
@store_owner_required('inventory')
def inventory_alerts(request, slug):
    """Manage inventory alerts"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    
    alerts = InventoryAlert.objects.filter(store=store).select_related('product')
    
    if request.method == 'POST':
        form = InventoryAlertForm(store, request.POST)
        if form.is_valid():
            alert = form.save(commit=False)
            alert.store = store
            alert.save()
            messages.success(request, 'Inventory alert created successfully.')
            return redirect('storefront:inventory_alerts', slug=slug)
    else:
        form = InventoryAlertForm(store)
    
    context = {
        'store': store,
        'alerts': alerts,
        'form': form,
    }
    
    return render(request, 'storefront/inventory/alerts.html', context)

@login_required
@store_owner_required('inventory')
def manage_variants(request, slug, product_id):
    """Manage product variants"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    product = get_object_or_404(Listing, id=product_id, store=store)
    
    variants = product.variants.all()
    
    if request.method == 'POST':
        form = ProductVariantForm(request.POST)
        if form.is_valid():
            variant = form.save(commit=False)
            variant.listing = product
            variant.save()
            messages.success(request, 'Product variant added successfully.')
            return redirect('storefront:manage_variants', slug=slug, product_id=product_id)
    else:
        form = ProductVariantForm()
    
    context = {
        'store': store,
        'product': product,
        'variants': variants,
        'form': form,
    }
    
    return render(request, 'storefront/inventory/variants.html', context)

@login_required
@store_owner_required('inventory')
def adjust_stock(request, slug):
    """Adjust stock levels"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    
    if request.method == 'POST':
        form = StockAdjustmentForm(store, request.POST)
        if form.is_valid():
            with transaction.atomic():
                data = form.cleaned_data
                product = data['product']
                variant = data['variant']
                quantity = data['quantity']
                notes = data['notes']
                quick_flag = bool(request.POST.get('quick_adjust'))
                if quick_flag:
                    notes = (notes or '') + (' ' if notes else '') + '[Quick Adjustment]'
                adjustment_type = request.POST.get('adjustment_type', 'add')
                
                # Determine which stock to adjust
                if variant:
                    previous_stock = variant.stock
                    if adjustment_type == 'add':
                        variant.stock += quantity
                    elif adjustment_type == 'remove':
                        variant.stock = max(0, variant.stock - quantity)
                    else:  # set
                        variant.stock = max(0, quantity)
                    variant.save()
                    
                    # Create stock movement record
                    StockMovement.objects.create(
                        store=store,
                        product=product,
                        variant=variant,
                        movement_type='adjustment',
                        quantity=quantity,
                        previous_stock=previous_stock,
                        new_stock=variant.stock,
                        notes=notes,
                        created_by=request.user
                    )
                else:
                    previous_stock = product.stock
                    if adjustment_type == 'add':
                        product.stock += quantity
                    elif adjustment_type == 'remove':
                        product.stock = max(0, product.stock - quantity)
                    else:  # set
                        product.stock = max(0, quantity)
                    product.save()
                    
                    # Create stock movement record
                    StockMovement.objects.create(
                        store=store,
                        product=product,
                        movement_type='adjustment',
                        quantity=quantity,
                        previous_stock=previous_stock,
                        new_stock=product.stock,
                        notes=notes,
                        created_by=request.user
                    )
                
                messages.success(request, 'Stock adjusted successfully.')
                # If request came from AJAX (frontend expects JSON), return JSON
                if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.META.get('HTTP_X_REQUESTED_WITH') == 'XMLHttpRequest':
                    return JsonResponse({'success': True, 'product_id': product.id, 'variant_id': variant.id if variant else None, 'new_stock': variant.stock if variant else product.stock, 'quick_adjust': quick_flag})
                return redirect('storefront:inventory_dashboard', slug=slug)
        else:
            # Form invalid
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.META.get('HTTP_X_REQUESTED_WITH') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'error': 'Invalid input', 'errors': form.errors}, status=400)
            messages.error(request, 'Invalid input for stock adjustment.')
            return redirect('storefront:adjust_stock', slug=slug)
    else:
        form = StockAdjustmentForm(store)

    # Provide products and helper data for the template to render and JS to operate
    products = store.listings.all().order_by('title')
    low_stock_products = store.listings.filter(stock__lte=5, stock__gt=0).order_by('stock')
    recent_movements = StockMovement.objects.filter(store=store).select_related('product', 'created_by').order_by('-created_at')[:10]

    context = {
        'store': store,
        'form': form,
        'products': products,
        'low_stock_products': low_stock_products,
        'recent_movements': recent_movements,
    }

    return render(request, 'storefront/inventory/adjust_stock.html', context)

@login_required
@store_owner_required('inventory')
def bulk_stock_update(request, slug):
    """Bulk update stock levels"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    
    if request.method == 'POST':
        form = BulkStockUpdateForm(store, request.POST)
        if form.is_valid():
            data = form.cleaned_data
            update_type = data['update_type']
            value = float(data['value'])
            apply_to = data['apply_to']
            # additional flags from template
            skip_out_of_stock = bool(request.POST.get('skip_out_of_stock'))
            prevent_negative = bool(request.POST.get('prevent_negative'))
            notes = request.POST.get('notes', '')
            
            # Get products to update
            if apply_to == 'all':
                products = store.listings.all()
            elif apply_to == 'category' and data.get('category'):
                products = store.listings.filter(category=data['category'])
            elif apply_to == 'selected' and data.get('selected_products'):
                products = data.get('selected_products')
            elif apply_to == 'low_stock':
                products = store.listings.filter(stock__lte=5, stock__gt=0)
            elif apply_to == 'out_of_stock':
                products = store.listings.filter(stock=0)
            else:
                products = store.listings.none()
            
            # Apply updates
            updated_count = 0
            for product in products:
                # skip out of stock products if requested
                if skip_out_of_stock and (product.stock == 0):
                    continue

                # compute new stock as float then coerce to int based on flags
                if update_type == 'percentage':
                    computed = product.stock * (1 + value / 100)
                elif update_type == 'fixed':
                    computed = product.stock + value
                else:  # set
                    computed = value

                if prevent_negative:
                    new_stock = max(0, int(computed))
                else:
                    new_stock = int(computed)

                previous_stock = product.stock
                # Persist new stock
                product.stock = new_stock
                product.save()

                # Create StockMovement record to track the bulk change
                try:
                    StockMovement.objects.create(
                        store=store,
                        product=product,
                        variant=None,
                        movement_type='adjustment',
                        quantity=(new_stock - previous_stock),
                        previous_stock=previous_stock,
                        new_stock=new_stock,
                        notes=(f'Bulk update. {notes}' if notes else 'Bulk update'),
                        created_by=request.user
                    )
                except Exception as e:
                    # Log the failure with context so we can investigate without failing the bulk run
                    logger.exception(
                        "Failed creating StockMovement for product id=%s store=%s new_stock=%s prev_stock=%s: %s",
                        getattr(product, 'id', None),
                        getattr(store, 'id', None),
                        new_stock,
                        previous_stock,
                        str(e)
                    )

                updated_count += 1
            
            messages.success(request, f'Updated stock for {updated_count} products.')
            return redirect('storefront:inventory_list', slug=slug)
    
    else:
        form = BulkStockUpdateForm(store)
    
    # helper stats for template
    products_qs = store.listings.all()
    total_products = products_qs.count()
    average_stock = products_qs.aggregate(avg=Avg('stock'))['avg'] or 0
    low_stock_count = products_qs.filter(stock__lte=5, stock__gt=0).count()
    out_of_stock_count = products_qs.filter(stock=0).count()
    good_count = products_qs.filter(stock__gt=10).count()
    # distribution percentages (safe)
    if total_products > 0:
        stock_distribution = {
            'good': int(good_count / total_products * 100),
            'low': int(low_stock_count / total_products * 100),
            'out': int(out_of_stock_count / total_products * 100),
        }
    else:
        stock_distribution = {'good': 0, 'low': 0, 'out': 0}

    context = {
        'store': store,
        'form': form,
        'products': products_qs.order_by('title'),
        'categories': Category.objects.filter(listing__store=store).distinct(),
        'total_products': total_products,
        'average_stock': average_stock,
        'low_stock_count': low_stock_count,
        'out_of_stock_count': out_of_stock_count,
        'stock_distribution': stock_distribution,
    }
    
    return render(request, 'storefront/inventory/bulk_update.html', context)

@login_required
@store_owner_required('inventory')
def export_inventory(request, slug):
    """Export inventory to CSV"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    
    products = store.listings.select_related('category').prefetch_related('variants')
    
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{store.slug}_inventory_{timezone.now().date()}.csv"'
    
    writer = csv.writer(response)
    writer.writerow([
        'SKU', 'Product Name', 'Category', 'Price', 'Stock', 
        'Cost Price', 'Weight (g)', 'Dimensions', 'Status'
    ])
    
    for product in products:
        writer.writerow([
            getattr(product, 'sku', ''),
            product.title,
            product.category.name if product.category else '',
            product.price,
            product.stock,
            getattr(product, 'cost_price', ''),
            product.weight or '',
            product.dimensions or '',
            'Active' if product.is_active else 'Inactive'
        ])
    
    return response

@login_required
@store_owner_required('inventory')
def import_inventory(request, slug):
    """Import inventory from CSV"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    
    if request.method == 'POST' and (request.FILES.get('csv_file') or request.FILES.get('file')):
        # Support both inventory template (csv_file) and bulk form (file)
        upload = request.FILES.get('csv_file') or request.FILES.get('file')

        # Build job parameters from form fields
        params = {
            'template_type': request.POST.get('template_type', 'inventory'),
            'update_existing': request.POST.get('update_existing') in ['on', 'true', '1'],
            'create_new': request.POST.get('create_new') in ['on', 'true', '1'],
            'skip_errors': request.POST.get('skip_errors') in ['on', 'true', '1'],
            'template_id': request.POST.get('template') or None,
        }

        # Include mapping if provided
        fm = request.POST.get('field_mapping')
        if fm:
            try:
                params['field_mapping'] = json.loads(fm)
            except Exception:
                params['field_mapping'] = fm

        # Create a BatchJob to process import in background (keeps behavior consistent with bulk import)
        try:
            batch_job = BatchJob.objects.create(
                store=store,
                job_type='import',
                status='pending',
                created_by=request.user,
                parameters=params,
                file=upload
            )
            # Enqueue processing task; handle broker unavailability gracefully
            try:
                process_import_task.delay(batch_job.id)
                messages.success(request, f'Import job #{batch_job.id} has been queued. Processing {batch_job.file.name}.')
            except Exception as exc:
                logger.exception('Failed to enqueue import job %s: %s — falling back to synchronous run', batch_job.id, exc)
                try:
                    process_import_task.run(batch_job.id)
                    messages.success(request, f'Import job #{batch_job.id} was processed synchronously.')
                except Exception as exc2:
                    logger.exception('Synchronous import job %s failed: %s', batch_job.id, exc2)
                    messages.warning(request, (
                        f'Import job #{batch_job.id} was created but could not be processed: {exc2}. '
                        'Start your Celery worker and Redis broker to process it.'
                    ))

            return redirect('storefront:bulk_job_detail', slug=slug, job_id=batch_job.id)
        except Exception as e:
            messages.error(request, f'Error creating import job: {str(e)}')

    # Provide templates for select in template dropdown
    templates = ImportTemplate.objects.filter(store=store, is_active=True)

    return render(request, 'storefront/inventory/import.html', {'store': store, 'templates': templates})

@login_required
@store_owner_required('inventory')
def stock_movements(request, slug):
    """View stock movement history"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    
    # Base stock movement queryset
    stock_qs = StockMovement.objects.filter(store=store).select_related('product', 'variant', 'created_by')

    # Filters
    movement_type = request.GET.get('type')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')

    if date_from:
        stock_qs = stock_qs.filter(created_at__date__gte=date_from)

    if date_to:
        stock_qs = stock_qs.filter(created_at__date__lte=date_to)

    if movement_type and movement_type != 'order':
        stock_qs = stock_qs.filter(movement_type=movement_type)

    stock_moves = list(stock_qs.order_by('-created_at'))

    # Include orders as movements when requested or by default
    order_qs = Order.objects.filter(order_items__listing__store=store, status__in=['paid', 'delivered']).distinct()
    if date_from:
        order_qs = order_qs.filter(created_at__date__gte=date_from)
    if date_to:
        order_qs = order_qs.filter(created_at__date__lte=date_to)
    if movement_type == 'order':
        # only orders
        order_qs = order_qs.order_by('-created_at')
    else:
        order_qs = order_qs.order_by('-created_at')

    order_movements = []
    for order in order_qs:
        items = order.order_items.filter(listing__store=store)
        total_qty = 0
        first_listing = None
        for it in items:
            total_qty += it.quantity
            if not first_listing:
                first_listing = it.listing
        if not first_listing:
            continue
        qty = -total_qty
        prev_stock = None
        try:
            prev_stock = (first_listing.stock or 0) + total_qty
        except Exception:
            prev_stock = None

        order_movements.append({
            'created_at': order.created_at,
            'product': first_listing,
            'movement_type': 'order',
            'get_movement_type_display': 'Order',
            'quantity': qty,
            'previous_stock': prev_stock,
            'new_stock': first_listing.stock,
            'created_by': getattr(order, 'user', None),
            'notes': f'Order #{order.id}'
        })

    # Merge stock movements (model instances) and order dicts
    combined = []
    combined.extend(stock_moves)
    combined.extend(order_movements)
    combined.sort(key=lambda x: x.created_at if hasattr(x, 'created_at') else x.get('created_at'), reverse=True)

    # If filtering by movement_type == 'order', only include order entries
    if movement_type == 'order':
        combined = [c for c in combined if (not hasattr(c, 'movement_type') and c.get('movement_type') == 'order') or (hasattr(c, 'movement_type') and getattr(c, 'movement_type') == 'order')]

    # Pagination over combined list
    paginator = Paginator(combined, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # Compute simple stats for display from combined items
    stock_added = 0
    stock_removed = 0
    for mv in combined:
        qty = mv.quantity if hasattr(mv, 'quantity') else mv.get('quantity', 0)
        if qty and qty > 0:
            stock_added += qty
        elif qty and qty < 0:
            stock_removed += abs(qty)
    net_change = stock_added - stock_removed

    context = {
        'store': store,
        'page_obj': page_obj,
        'movement_types': StockMovement.MOVEMENT_TYPES + [('order', 'Order')],
        'stock_added': stock_added,
        'stock_removed': stock_removed,
        'net_change': net_change,
    }
    
    return render(request, 'storefront/inventory/movements.html', context)

@require_POST
@login_required
@store_owner_required('inventory')
def delete_alert(request, slug, alert_id):
    """Delete inventory alert"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    alert = get_object_or_404(InventoryAlert, id=alert_id, store=store)
    
    alert.delete()
    messages.success(request, 'Alert deleted successfully.')
    
    return redirect('storefront:inventory_alerts', slug=slug)

@require_POST
@login_required
@store_owner_required('inventory')
def toggle_alert(request, slug, alert_id):
    """Toggle alert active status"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    alert = get_object_or_404(InventoryAlert, id=alert_id, store=store)
    
    alert.is_active = not alert.is_active
    alert.save()
    
    status = "activated" if alert.is_active else "deactivated"
    messages.success(request, f'Alert {status} successfully.')
    
    return redirect('storefront:inventory_alerts', slug=slug)

@require_POST
@login_required
@store_owner_required('inventory')
def delete_variant(request, slug, variant_id):
    """Delete product variant"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    variant = get_object_or_404(ProductVariant, id=variant_id, listing__store=store)
    
    variant.delete()
    messages.success(request, 'Variant deleted successfully.')
    
    return redirect('storefront:manage_variants', slug=slug, product_id=variant.listing_id)

# AJAX Views
@require_GET
@login_required
@store_owner_required('inventory')
def get_product_variants(request, slug, product_id):
    """Get variants for a product (AJAX)"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    product = get_object_or_404(Listing, id=product_id, store=store)
    
    variants = product.variants.filter(is_active=True).values('id', 'name', 'value', 'stock')
    
    return JsonResponse({'variants': list(variants)})


@require_GET
@login_required
@store_owner_required('inventory')
def inventory_search(request, slug):
    """Search inventory products by title or SKU (AJAX)"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    q = request.GET.get('q', '').strip()
    results = []
    if q:
        qs = Listing.objects.filter(store=store).filter(
            Q(title__icontains=q) | Q(sku__icontains=q)
        ).order_by('title')[:10]

        for p in qs:
            results.append({
                'id': p.id,
                'title': p.title,
                'sku': p.sku,
                'stock': p.stock,
                'price': str(p.price) if getattr(p, 'price', None) is not None else None,
            })

    return JsonResponse({'products': results})

@require_POST
@login_required
@store_owner_required('inventory')
def quick_stock_update(request, slug, product_id):
    """Quick stock update via AJAX"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    product = get_object_or_404(Listing, id=product_id, store=store)
    
    try:
        data = json.loads(request.body)
        new_stock = int(data.get('stock', 0))
        variant_id = data.get('variant_id')
        
        with transaction.atomic():
            if variant_id:
                variant = get_object_or_404(ProductVariant, id=variant_id, listing=product)
                previous_stock = variant.stock
                variant.stock = max(0, new_stock)
                variant.save()
                
                # Record movement
                StockMovement.objects.create(
                    store=store,
                    product=product,
                    variant=variant,
                    movement_type='adjustment',
                    quantity=new_stock - previous_stock,
                    previous_stock=previous_stock,
                    new_stock=variant.stock,
                    notes='Quick update via dashboard',
                    created_by=request.user
                )
            else:
                previous_stock = product.stock
                product.stock = max(0, new_stock)
                product.save()
                
                # Record movement
                StockMovement.objects.create(
                    store=store,
                    product=product,
                    movement_type='adjustment',
                    quantity=new_stock - previous_stock,
                    previous_stock=previous_stock,
                    new_stock=product.stock,
                    notes='Quick update via dashboard',
                    created_by=request.user
                )
        
        return JsonResponse({
            'success': True,
            'new_stock': new_stock,
            'product_id': product_id,
            'variant_id': variant_id
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        }, status=400)

# Celery Tasks
from celery import shared_task
from django.core.mail import send_mail
from django.template.loader import render_to_string

@shared_task
def check_inventory_alerts():
    """Check and trigger inventory alerts"""
    from .models import InventoryAlert
    
    alerts = InventoryAlert.objects.filter(
        is_active=True,
        product__is_active=True
    ).select_related('product', 'store', 'store__owner')
    
    triggered_alerts = []
    
    for alert in alerts:
        if alert.check_condition():
            # Update last triggered
            alert.last_triggered = timezone.now()
            alert.save()
            
            triggered_alerts.append(alert)
            
            # Send notifications
            if 'email' in alert.notification_method:
                send_stock_alert_email.delay(alert.id)
            
            if 'sms' in alert.notification_method:
                send_stock_alert_sms.delay(alert.id)
    
    return f"Checked {len(alerts)} alerts, triggered {len(triggered_alerts)}"

@shared_task
def send_stock_alert_email(alert_id):
    """Send email notification for stock alert"""
    from .models import InventoryAlert
    
    try:
        alert = InventoryAlert.objects.get(id=alert_id)
        store = alert.store
        product = alert.product
        
        subject = f"Stock Alert: {product.title} is {alert.get_alert_type_display()}"
        
        context = {
            'store': store,
            'product': product,
            'alert': alert,
            'current_stock': product.stock,
        }
        
        html_message = render_to_string('storefront/emails/stock_alert.html', context)
        text_message = render_to_string('storefront/emails/stock_alert.txt', context)

        try:
            from baysoko.utils.email_helpers import render_and_send
            recipients = [e for e in [getattr(store.owner, 'email', None)] if e]
            if recipients:
                render_and_send('storefront/emails/stock_alert.html', 'storefront/emails/stock_alert.txt', context, subject, recipients)
        except Exception:
            # Fallback to direct send_mail if helper unavailable
            try:
                send_mail(
                    subject=subject,
                    message=text_message,
                    from_email='noreply@baysoko.com',
                    recipient_list=[getattr(store.owner, 'email', None)],
                    html_message=html_message,
                    fail_silently=True,
                )
            except Exception:
                print(f"Error sending stock alert email via fallback for alert {alert_id}")
        
    except Exception as e:
        print(f"Error sending stock alert email: {e}")

@shared_task
def send_stock_alert_sms(alert_id):
    """Send SMS notification for stock alert"""
    # Implement SMS sending logic using Africa's Talking
    pass

