# storefront/views_bulk.py
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_POST, require_GET
from django.db.models import Q, Count, Sum, F, Value, CharField
from django.db import transaction
from django.core.paginator import Paginator
from django.utils import timezone
from django.views import View
from django.contrib.auth.mixins import LoginRequiredMixin
from django.utils.decorators import method_decorator
from django.urls import reverse
from datetime import timedelta, datetime
import json
import csv
from io import BytesIO, StringIO
import zipfile
import os

from .models import Store
from .models_bulk import BatchJob, ExportJob, ImportTemplate, BulkOperationLog
from django.db import DatabaseError, OperationalError
import logging
from .forms_bulk import (
    BulkProductUpdateForm, BulkImportForm, 
    ExportSettingsForm, TemplateForm
)
from listings.models import Listing, Category
from .decorators import store_owner_required, plan_required
from .ai_copilot import (
    has_seller_ai_access,
    run_bulk_import_preflight,
)

# Celery tasks will be in tasks.py
from .tasks_bulk import (
    process_bulk_update_task,
    process_import_task,
    generate_export_task
)

logger = logging.getLogger(__name__)


def _format_mapping_value(value):
    if isinstance(value, dict):
        return [{'type': 'pair', 'label': str(k).replace('_', ' ').title(), 'value': v} for k, v in value.items()]
    if isinstance(value, list):
        return [{'type': 'item', 'value': item} for item in value]
    return value


def _build_parameter_display(parameters):
    display = []
    for key, value in (parameters or {}).items():
        display.append({
            'label': str(key).replace('_', ' ').title(),
            'value': _format_mapping_value(value),
        })
    return display

@login_required
@store_owner_required
@plan_required('bulk_operations')
def bulk_operations_dashboard(request, slug):
    """Bulk operations dashboard"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)

    # Wrap operations that depend on optional models/tables so the site
    # doesn't crash when migrations haven't been applied yet.
    try:
        recent_jobs = BatchJob.objects.filter(store=store).select_related('created_by').order_by('-created_at')[:10]
        job_stats = {
            'total': BatchJob.objects.filter(store=store).count(),
            'completed': BatchJob.objects.filter(store=store, status__in=['completed', 'completed_with_errors']).count(),
            'processing': BatchJob.objects.filter(store=store, status='processing').count(),
            'failed': BatchJob.objects.filter(store=store, status='failed').count(),
        }
        recent_exports = ExportJob.objects.filter(store=store, status='completed').order_by('-created_at')[:5]
        templates = ImportTemplate.objects.filter(store=store, is_active=True).order_by('-download_count')[:5]
    except (DatabaseError, OperationalError) as e:
        # If the BatchJob/ExportJob/ImportTemplate tables don't exist yet,
        # don't raise a 500 — show an empty dashboard and a helpful message.
        recent_jobs = []
        job_stats = {'total': 0, 'completed': 0, 'processing': 0, 'failed': 0}
        recent_exports = []
        templates = []
        messages.warning(request, 'Bulk operations are currently unavailable (database not initialized). Run migrations.')
    
    context = {
        'store': store,
        'recent_jobs': recent_jobs,
        'job_stats': job_stats,
        'recent_exports': recent_exports,
        'templates': templates,
    }
    
    return render(request, 'storefront/bulk/dashboard.html', context)

@login_required
@store_owner_required
def bulk_update_products(request, slug):
    """Bulk update products view"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    
    if request.method == 'POST':
        form = BulkProductUpdateForm(store, request.POST)
        if form.is_valid():
            # Create batch job
            batch_job = BatchJob.objects.create(
                store=store,
                job_type='product_update',
                status='pending',
                created_by=request.user,
                parameters=form.cleaned_data,
                total_items=0,  # Will be calculated in task
                started_at=timezone.now(),
            )
            
            # Start async task
            process_bulk_update_task.delay(batch_job.id)
            
            messages.success(
                request,
                f'Bulk update job #{batch_job.id} has been queued. '
                f'You will be notified when it completes.'
            )
            return redirect('storefront:bulk_job_detail', slug=slug, job_id=batch_job.id)
    else:
        form = BulkProductUpdateForm(store)
    
    context = {
        'store': store,
        'form': form,
    }
    
    return render(request, 'storefront/bulk/update_products.html', context)

@login_required
@store_owner_required
def bulk_import_data(request, slug):
    """Bulk import data view"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    
    if request.method == 'POST':
        form = BulkImportForm(store, request.POST, request.FILES)
        if form.is_valid():
            # Create batch job
            batch_job = form.save(commit=False)
            batch_job.store = store
            batch_job.job_type = 'import'
            batch_job.status = 'pending'
            batch_job.created_by = request.user
            batch_job.parameters = {
                'template_type': form.cleaned_data['template_type'],
                'update_existing': form.cleaned_data['update_existing'],
                'create_new': form.cleaned_data['create_new'],
                'skip_errors': form.cleaned_data['skip_errors'],
                'auto_fetch_images': request.POST.get('auto_fetch_images') in ['on', 'true', '1'],
                'template_id': form.cleaned_data['template'].id if form.cleaned_data['template'] else None,
            }

            # If the client submitted an explicit field mapping (JSON), include it
            fm = request.POST.get('field_mapping')
            if fm:
                try:
                    batch_job.parameters['field_mapping'] = json.loads(fm)
                except Exception:
                    batch_job.parameters['field_mapping'] = fm
            batch_job.save()
            
            # Start async task; if broker (Redis) is unavailable, fall back to synchronous execution
            try:
                process_import_task.delay(batch_job.id)
                messages.success(
                    request,
                    f'Import job #{batch_job.id} has been queued. '
                    f'Processing {batch_job.file.name}.'
                )
            except Exception as exc:
                # Broker unavailable — run the task synchronously so the import still completes.
                logger.exception(
                    'Failed to enqueue import job %s: %s — falling back to synchronous run',
                    batch_job.id, exc,
                )
                try:
                    # Transition to 'processing' before running so the job detail page
                    # reflects the correct state if the user refreshes mid-run.
                    batch_job.status = 'processing'
                    batch_job.started_at = timezone.now()
                    batch_job.save(update_fields=['status', 'started_at'])
                    logger.info('Running import job %s synchronously (no broker)', batch_job.id)
                    # Call the underlying task function directly — .run() does not exist on
                    # Celery tasks; calling the task object itself invokes the body without
                    # going through the broker or result backend.
                    process_import_task(batch_job.id)
                    messages.success(
                        request,
                        f'Import job #{batch_job.id} was processed synchronously.'
                    )
                except Exception as exc2:
                    logger.exception('Synchronous import job %s failed: %s', batch_job.id, exc2)
                    messages.warning(
                        request,
                        f'Import job #{batch_job.id} was created but could not be processed: {exc2}. '
                        'Start your Celery worker and Redis broker to process it.'
                    )
            return redirect('storefront:bulk_job_detail', slug=slug, job_id=batch_job.id)
    else:
        form = BulkImportForm(store)
    
    # Get sample templates
    templates = ImportTemplate.objects.filter(store=store, is_active=True)
    
    context = {
        'store': store,
        'form': form,
        'templates': templates,
        'seller_ai_access': has_seller_ai_access(request.user, store=store),
    }
    
    return render(request, 'storefront/bulk/import_data.html', context)


@login_required
@require_POST
@store_owner_required
@plan_required('bulk_operations')
def ai_bulk_import_preflight(request, slug):
    """Analyze an uploaded import file and return cleanup guidance for sellers."""
    store = get_object_or_404(Store, slug=slug, owner=request.user)

    if not has_seller_ai_access(request.user, store=store):
        return JsonResponse({
            'success': False,
            'error': 'Baysoko AI Copilot for bulk uploads is available on Premium and Enterprise plans.',
            'upgrade_url': reverse('storefront:subscription_manage', kwargs={'slug': store.slug}),
        }, status=403)

    uploaded_file = request.FILES.get('file')
    if not uploaded_file:
        return JsonResponse({'success': False, 'error': 'Please choose a file first.'}, status=400)

    try:
        result = run_bulk_import_preflight(uploaded_file)
        return JsonResponse({'success': True, 'result': result})
    except Exception as exc:
        logger.exception('AI bulk import preflight failed for store %s: %s', store.id, exc)
        return JsonResponse({
            'success': False,
            'error': f'We could not analyze this file yet: {exc}',
        }, status=400)

@login_required
@store_owner_required
def export_data(request, slug):
    """Export data view"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    
    if request.method == 'POST':
        form = ExportSettingsForm(store, request.POST)
        if form.is_valid():
            # Create export job
            export_job = ExportJob.objects.create(
                store=store,
                export_type=form.cleaned_data['export_type'],
                format=form.cleaned_data['format'],
                filters={
                    'date_range': form.cleaned_data['date_range'],
                    'start_date': form.cleaned_data['start_date'].isoformat() if form.cleaned_data['start_date'] else None,
                    'end_date': form.cleaned_data['end_date'].isoformat() if form.cleaned_data['end_date'] else None,
                    'include_inactive': form.cleaned_data['include_inactive'],
                    'include_out_of_stock': form.cleaned_data['include_out_of_stock'],
                },
                columns=form.cleaned_data['selected_columns'],
                status='pending',
                created_by=request.user,
            )
            
            # Start async task
            generate_export_task.delay(export_job.id)
            
            messages.success(
                request,
                f'Export job #{export_job.id} has been queued. '
                f'You will be able to download the file when it\'s ready.'
            )
            return redirect('storefront:export_job_detail', slug=slug, job_id=export_job.id)
    else:
        form = ExportSettingsForm(store)
    
    context = {
        'store': store,
        'form': form,
    }
    
    return render(request, 'storefront/bulk/export_data.html', context)

@login_required
@store_owner_required
def manage_templates(request, slug):
    """Manage import templates"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    
    templates = ImportTemplate.objects.filter(store=store).order_by('-created_at')
    
    if request.method == 'POST' and 'create_template' in request.POST:
        form = TemplateForm(store, request.POST, request.FILES)
        if form.is_valid():
            template = form.save(commit=False)
            template.store = store
            template.created_by = request.user
            template.save()
            
            messages.success(request, 'Template created successfully.')
            return redirect('storefront:manage_templates', slug=slug)
    else:
        form = TemplateForm(store)
    
    context = {
        'store': store,
        'templates': templates,
        'form': form,
    }
    
    return render(request, 'storefront/bulk/templates.html', context)

@login_required
@store_owner_required
def bulk_job_list(request, slug):
    """List all bulk jobs"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    try:
        jobs = BatchJob.objects.filter(store=store).select_related('created_by').order_by('-created_at')
    except (DatabaseError, OperationalError):
        messages.warning(request, 'Bulk jobs unavailable (database not initialized).')
        jobs = []
    
    # Apply filters
    status = request.GET.get('status')
    job_type = request.GET.get('type')
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    
    if status:
        jobs = jobs.filter(status=status)
    if job_type:
        jobs = jobs.filter(job_type=job_type)
    if date_from:
        jobs = jobs.filter(created_at__date__gte=date_from)
    if date_to:
        jobs = jobs.filter(created_at__date__lte=date_to)
    
    # Pagination
    paginator = Paginator(jobs, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    context = {
        'store': store,
        'page_obj': page_obj,
        'status_choices': getattr(BatchJob, 'JOB_STATUS', []),
        'type_choices': getattr(BatchJob, 'JOB_TYPES', []),
    }
    
    return render(request, 'storefront/bulk/job_list.html', context)

@login_required
@store_owner_required
def bulk_job_detail(request, slug, job_id):
    """View batch job details"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    try:
        job = get_object_or_404(BatchJob, id=job_id, store=store)
        # Get logs
        logs = BulkOperationLog.objects.filter(batch_job=job).order_by('-created_at')
    except (DatabaseError, OperationalError):
        messages.warning(request, 'Bulk job details unavailable (database not initialized).')
        return redirect('storefront:bulk_dashboard', slug=slug)
    
    # Paginate logs
    paginator = Paginator(logs, 100)
    page_number = request.GET.get('page')
    logs_page = paginator.get_page(page_number)
    
    context = {
        'store': store,
        'job': job,
        'logs_page': logs_page,
        'job_parameter_rows': _build_parameter_display(job.parameters),
    }
    
    return render(request, 'storefront/bulk/job_detail.html', context)

@login_required
@store_owner_required
def export_job_list(request, slug):
    """List all export jobs"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    
    try:
        jobs = ExportJob.objects.filter(store=store).select_related('created_by').order_by('-created_at')
    except (DatabaseError, OperationalError):
        messages.warning(request, 'Export jobs unavailable (database not initialized).')
        jobs = []
    
    # Apply filters
    status = request.GET.get('status')
    export_type = request.GET.get('type')
    
    if status:
        jobs = jobs.filter(status=status)
    if export_type:
        jobs = jobs.filter(export_type=export_type)
    
    # Pagination
    paginator = Paginator(jobs, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    context = {
        'store': store,
        'page_obj': page_obj,
    }
    
    return render(request, 'storefront/bulk/export_list.html', context)

@login_required
@store_owner_required
def export_job_detail(request, slug, job_id):
    """View export job details"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    job = get_object_or_404(ExportJob, id=job_id, store=store)
    
    context = {
        'store': store,
        'job': job,
        'filter_rows': _build_parameter_display(job.filters),
    }
    
    return render(request, 'storefront/bulk/export_detail.html', context)

@login_required
@store_owner_required
def download_export(request, slug, job_id):
    """Download exported file"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    job = get_object_or_404(ExportJob, id=job_id, store=store)
    
    if job.status != 'completed' or not job.file:
        messages.error(request, 'Export file is not ready or not found.')
        return redirect('storefront:export_job_detail', slug=slug, job_id=job_id)
    
    # Increment download count
    job.download_count += 1
    job.save()
    
    # Serve file
    response = HttpResponse(job.file.read(), content_type='application/octet-stream')
    response['Content-Disposition'] = f'attachment; filename="{job.filename}"'
    return response

@login_required
@store_owner_required
def delete_template(request, slug, template_id):
    """Delete import template"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    template = get_object_or_404(ImportTemplate, id=template_id, store=store)
    
    if request.method == 'POST':
        template.delete()
        messages.success(request, 'Template deleted successfully.')
        return redirect('storefront:manage_templates', slug=slug)
    
    context = {
        'store': store,
        'template': template,
    }
    
    return render(request, 'storefront/bulk/delete_template.html', context)

@login_required
@store_owner_required
def download_template(request, slug, template_id):
    """Download import template"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    template = get_object_or_404(ImportTemplate, id=template_id, store=store)
    
    if not template.file:
        messages.error(request, 'Template file not found.')
        return redirect('storefront:manage_templates', slug=slug)
    
    # Increment download count
    template.download_count += 1
    template.save()
    
    # Serve file
    response = HttpResponse(template.file.read(), content_type='application/octet-stream')
    response['Content-Disposition'] = f'attachment; filename="{template.name}.{template.file.name.split(".")[-1]}"'
    return response

@login_required
@store_owner_required
def cancel_job(request, slug, job_id):
    """Cancel a batch job"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    job = get_object_or_404(BatchJob, id=job_id, store=store)
    
    if job.status in ['pending', 'processing']:
        job.status = 'cancelled'
        job.completed_at = timezone.now()
        job.save()
        messages.success(request, f'Job #{job_id} has been cancelled.')
    else:
        messages.error(request, f'Cannot cancel job with status: {job.get_status_display()}')
    
    return redirect('storefront:bulk_job_detail', slug=slug, job_id=job_id)

@require_GET
@login_required
def get_job_progress(request, slug, job_id):
    """Get job progress (AJAX)"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    job = get_object_or_404(BatchJob, id=job_id, store=store)
    
    return JsonResponse({
        'id': job.id,
        'status': job.status,
        'progress_percentage': job.progress_percentage,
        'processed_items': job.processed_items,
        'total_items': job.total_items,
        'success_count': job.success_count,
        'error_count': job.error_count,
        'started_at': job.started_at.isoformat() if job.started_at else None,
        'completed_at': job.completed_at.isoformat() if job.completed_at else None,
    })




@require_GET
@login_required
def get_export_columns(request, slug):
    """Get available columns for export type (AJAX)"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    export_type = request.GET.get('export_type', 'products')
    
    # Define columns for each export type
    columns_map = {
        'products': [
            {'value': 'id', 'label': 'Product ID', 'default': True},
            {'value': 'title', 'label': 'Title', 'default': True},
            {'value': 'sku', 'label': 'SKU', 'default': True},
            {'value': 'description', 'label': 'Description', 'default': False},
            {'value': 'price', 'label': 'Price', 'default': True},
            {'value': 'stock', 'label': 'Stock', 'default': True},
            {'value': 'category', 'label': 'Category', 'default': True},
            {'value': 'condition', 'label': 'Condition', 'default': True},
            {'value': 'location', 'label': 'Location', 'default': False},
            {'value': 'created_at', 'label': 'Created Date', 'default': False},
            {'value': 'is_active', 'label': 'Status', 'default': True},
        ],
        'inventory': [
            {'value': 'sku', 'label': 'SKU', 'default': True},
            {'value': 'title', 'label': 'Product Name', 'default': True},
            {'value': 'current_stock', 'label': 'Current Stock', 'default': True},
            {'value': 'minimum_stock', 'label': 'Minimum Stock', 'default': True},
            {'value': 'reorder_level', 'label': 'Reorder Level', 'default': True},
            {'value': 'last_restocked', 'label': 'Last Restocked', 'default': False},
            {'value': 'stock_value', 'label': 'Stock Value', 'default': True},
            {'value': 'monthly_sales', 'label': 'Monthly Sales', 'default': False},
        ],
        'customers': [
            {'value': 'id', 'label': 'Customer ID', 'default': True},
            {'value': 'name', 'label': 'Name', 'default': True},
            {'value': 'email', 'label': 'Email', 'default': True},
            {'value': 'phone', 'label': 'Phone', 'default': True},
            {'value': 'total_orders', 'label': 'Total Orders', 'default': True},
            {'value': 'total_spent', 'label': 'Total Spent', 'default': True},
            {'value': 'last_order', 'label': 'Last Order Date', 'default': False},
            {'value': 'joined_date', 'label': 'Joined Date', 'default': True},
        ],
        'orders': [
            {'value': 'order_id', 'label': 'Order ID', 'default': True},
            {'value': 'customer_name', 'label': 'Customer Name', 'default': True},
            {'value': 'customer_email', 'label': 'Customer Email', 'default': True},
            {'value': 'order_date', 'label': 'Order Date', 'default': True},
            {'value': 'total_amount', 'label': 'Total Amount', 'default': True},
            {'value': 'status', 'label': 'Status', 'default': True},
            {'value': 'payment_method', 'label': 'Payment Method', 'default': False},
            {'value': 'shipping_address', 'label': 'Shipping Address', 'default': False},
            {'value': 'items', 'label': 'Items', 'default': True},
        ],
    }
    
    columns = columns_map.get(export_type, columns_map['products'])
    
    return JsonResponse({
        'columns': columns,
        'export_type': export_type
    })

# Quick bulk actions
@require_POST
@login_required
@store_owner_required
def quick_bulk_action(request, slug):
    """Quick bulk actions for selected products"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    
    try:
        data = json.loads(request.body)
        product_ids = data.get('product_ids', [])
        action = data.get('action')
        value = data.get('value')
        
        if not product_ids:
            return JsonResponse({'success': False, 'error': 'No products selected'})
        
        products = Listing.objects.filter(
            id__in=product_ids,
            store=store
        )
        
        count = 0
        
        with transaction.atomic():
            if action == 'activate':
                products.update(is_active=True)
                count = len(products)
                
            elif action == 'deactivate':
                products.update(is_active=False)
                count = len(products)
                
            elif action == 'delete':
                count = products.count()
                products.delete()
                
            elif action == 'add_to_category' and value:
                category = get_object_or_404(Category, id=value)
                for product in products:
                    product.category = category
                    product.save()
                count = len(products)
                
            elif action == 'set_stock' and value is not None:
                for product in products:
                    product.stock = int(value)
                    product.save()
                count = len(products)
                
            else:
                return JsonResponse({'success': False, 'error': 'Invalid action'})
        
        return JsonResponse({
            'success': True,
            'message': f'{count} products updated successfully',
            'count': count
        })
        
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

# Generate sample template
@login_required
@store_owner_required
def generate_sample_template(request, slug):
    """Generate and download a sample template"""
    store = get_object_or_404(Store, slug=slug, owner=request.user)
    template_type = request.GET.get('type', 'products')
    
    # Create sample data based on template type
    if template_type == 'products':
        headers = [
            'SKU', 'Title', 'Description', 'Price', 'Stock', 
            'Category', 'Condition', 'Location', 'Tags', 'Is Active'
        ]
        sample_data = [
            ['PROD001', 'Sample Product', 'Product description here', '1000.00', '50', 
             'Electronics', 'New', 'Homabay', 'electronics,gadgets', 'Yes'],
            ['PROD002', 'Another Product', 'Another description', '2500.00', '25',
             'Clothing', 'Used', 'Nairobi', 'clothing,used', 'Yes'],
        ]
    
    elif template_type == 'inventory':
        headers = ['SKU', 'Current Stock', 'Reorder Level', 'Cost Price']
        sample_data = [
            ['PROD001', '50', '10', '800.00'],
            ['PROD002', '25', '5', '2000.00'],
        ]
    
    else:
        headers = ['Column1', 'Column2', 'Column3']
        sample_data = [['Sample1', 'Sample2', 'Sample3']]
    
    # Create CSV response
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{template_type}_template.csv"'
    
    writer = csv.writer(response)
    writer.writerow(headers)
    for row in sample_data:
        writer.writerow(row)
    
    return response
