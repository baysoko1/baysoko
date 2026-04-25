# storefront/tasks_bulk.py
from celery import shared_task
from django.db import transaction
from django.utils import timezone
from django.core.files.base import ContentFile
from django.utils.text import slugify
import json
import csv
from io import StringIO, BytesIO
# Lazy-import heavy libraries (pandas/openpyxl) inside tasks that need them
from datetime import datetime
import logging
from datetime import timedelta
from django.db.models import Q
from decimal import Decimal, InvalidOperation


logger = logging.getLogger('storefront.bulk')

import math


def _clean_json(obj):
    """Recursively sanitize object for JSON storage: replace NaN/Inf with None and
    convert non-serializable objects to strings."""
    if isinstance(obj, dict):
        return {k: _clean_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_json(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or not math.isfinite(obj):
            return None
        return obj
    if isinstance(obj, (str, bool, int)) or obj is None:
        return obj
    try:
        # Attempt to cast decimals or other numbers
        if hasattr(obj, 'to_python'):
            return _clean_json(str(obj))
        return str(obj)
    except Exception:
        return None

def _detect_encoding(raw_bytes: bytes) -> str:
    """Detect the character encoding of *raw_bytes*.

    Tries chardet first for accurate detection, then falls back to probing
    common encodings in order of prevalence so that CSV files exported from
    Excel (Latin-1 / Windows-1252) are handled gracefully even when chardet
    is not installed.
    """
    try:
        import chardet

        result = chardet.detect(raw_bytes)
        encoding = (result.get("encoding") or "").strip()
        if encoding:
            logger.debug("chardet detected encoding: %s", encoding)
            return encoding
    except ImportError:
        pass

    # Fallback: probe common encodings in order of prevalence.
    for candidate in ("utf-8-sig", "utf-8", "latin-1", "windows-1252", "cp1250"):
        try:
            raw_bytes.decode(candidate)
            logger.debug("Probed encoding: %s", candidate)
            return candidate
        except (UnicodeDecodeError, LookupError):
            continue

    # Last resort — latin-1 accepts every byte value, so it never raises.
    return "latin-1"


def _looks_like_serialized_csv_row(cells):
    if len(cells) != 1:
        return False
    raw = str(cells[0] or '').strip()
    return raw.count(',') >= 3 and '"' in raw


def _split_serialized_csv_row(raw_value):
    return [str(cell).strip() for cell in next(csv.reader([raw_value]))]


def _pad_row(row, width):
    if len(row) < width:
        return row + [''] * (width - len(row))
    return row[:width]


def _normalize_tabular_rows(rows):
    if not rows:
        return [], []

    headers = [str(cell).strip() for cell in rows[0]]
    width = len(headers)
    normalized_rows = []
    header_signature = ''.join(ch for ch in ','.join(headers[: min(5, len(headers))]).lower() if ch.isalnum())

    for raw_row in rows[1:]:
        row = [str(cell).strip() for cell in raw_row]
        if _looks_like_serialized_csv_row(row):
            row = _split_serialized_csv_row(row[0])
        row = _pad_row(row, width)

        if row:
            first_signature = ''.join(ch for ch in str(row[0]).lower() if ch.isalnum())
            if first_signature and first_signature.startswith(header_signature):
                continue

        if not any(row):
            continue

        normalized_rows.append(row)

    return headers, normalized_rows


def _load_csv_rows(file_content, job_id=None):
    detected_enc = _detect_encoding(file_content)
    logger.info('Import job %s: detected CSV encoding %s', job_id, detected_enc)
    try:
        text = file_content.decode(detected_enc)
    except (UnicodeDecodeError, LookupError):
        logger.warning(
            'Import job %s: could not decode with %s, falling back to latin-1',
            job_id, detected_enc,
        )
        text = file_content.decode('latin-1', errors='replace')

    raw_rows = list(csv.reader(StringIO(text)))
    headers, rows = _normalize_tabular_rows(raw_rows)
    if not headers:
        return []
    return [dict(zip(headers, row)) for row in rows]


from .models import Store
from listings.models import Listing, Category
from .models_bulk import BatchJob, ExportJob, ImportTemplate, BulkOperationLog
from django.core.exceptions import FieldDoesNotExist
# image helpers are optional; import safely
try:
    from .image_fetcher import (
        fetch_and_attach,
        download_image,
        validate_image_bytes,
        save_image_to_listing,
    )
except Exception:
    fetch_and_attach = None
    download_image = None
    validate_image_bytes = None
    save_image_to_listing = None


JOB_TERMINAL_SUCCESS = {'completed', 'completed_with_errors'}


def _complete_job(job, error_count, errors):
    job.status = 'completed' if error_count == 0 else 'completed_with_errors'
    job.completed_at = timezone.now()
    job.errors = errors
    job.results = {
        'success_count': job.success_count,
        'error_count': error_count,
        'completed_with_errors': error_count > 0,
    }
    job.save()
    logger.info(
        'Job %s: status → %s (success=%s, errors=%s)',
        job.id, job.status, job.success_count, error_count,
    )
    return job.status


def _normalize_location(value):
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    normalized = raw.lower().replace('-', ' ').replace('_', ' ')
    for key, label in Listing.HOMABAY_LOCATIONS:
        if normalized in {key.lower().replace('_', ' '), label.lower()}:
            return key
        label_tokens = [token for token in label.lower().replace('-', ' ').split() if len(token) > 3]
        if any(token in normalized for token in label_tokens):
            return key
    return None


def _normalize_choice(value, choices):
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    normalized = raw.lower().replace('-', ' ').replace('_', ' ')
    for key, label in choices:
        if normalized in {key.lower().replace('_', ' '), label.lower()}:
            return key
        if normalized.startswith(key.lower()) or normalized.startswith(label.lower()):
            return key
    return None


def _parse_decimal(value, default=None):
    if value in (None, ''):
        return default
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return default


def _parse_image_candidates(data):
    image_keys = ['image_url', 'image_urls', 'images', 'image', 'main_image']
    candidates = []
    for key in image_keys:
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            values = value
        else:
            values = str(value).replace('\n', ',').split(',')
        for candidate in values:
            candidate = str(candidate).strip()
            if candidate and candidate.lower().startswith(('http://', 'https://')):
                candidates.append(candidate)
    # preserve order while deduplicating
    seen = set()
    unique_candidates = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique_candidates.append(candidate)
    return unique_candidates


def _attach_image_urls_to_product(product, image_urls):
    if not image_urls or download_image is None or validate_image_bytes is None or save_image_to_listing is None:
        return

    attached_any = False
    for index, image_url in enumerate(image_urls, start=1):
        img_bytes = download_image(image_url)
        if not img_bytes or not validate_image_bytes(img_bytes):
            continue

        base_name = slugify(product.title)[:50] or f'listing-{product.pk}'
        extension = image_url.rsplit('.', 1)[-1].split('?')[0].lower() if '.' in image_url.rsplit('/', 1)[-1] else 'jpg'
        extension = extension if extension in {'jpg', 'jpeg', 'png', 'webp', 'gif'} else 'jpg'
        filename = f'{base_name}-{index}.{extension}'

        listing_image = save_image_to_listing(product, img_bytes, filename=filename, caption='Imported image')
        if not listing_image:
            continue

        if not product.image:
            try:
                product.image = listing_image.image
                product.save(update_fields=['image'])
            except Exception:
                logger.exception('Failed to promote imported image to primary listing image for product %s', product.pk)

        attached_any = True

    if not attached_any:
        raise ValueError('No valid product images could be downloaded from the provided image URLs')

@shared_task(bind=True)
def process_bulk_update_task(self, job_id):
    """Process bulk update job"""
    try:
        job = BatchJob.objects.get(id=job_id)
        job.status = 'processing'
        job.started_at = timezone.now()
        job.save()
        
        params = job.parameters
        action = params.get('action')
        
        # Get products to update
        product_ids = params.get('products', [])
        apply_to_all = params.get('apply_to_all', False)
        
        if apply_to_all:
            products = job.store.listings.all()
        elif product_ids:
            products = job.store.listings.filter(id__in=product_ids)
        else:
            # Apply filters
            products = job.store.listings.all()
            
            category_id = params.get('filter_category')
            if category_id:
                products = products.filter(category_id=category_id)
            
            stock_status = params.get('filter_stock_status')
            if stock_status == 'in_stock':
                products = products.filter(stock__gt=0)
            elif stock_status == 'low_stock':
                products = products.filter(stock__lte=5, stock__gt=0)
            elif stock_status == 'out_of_stock':
                products = products.filter(stock=0)
            
            price_min = params.get('filter_price_min')
            price_max = params.get('filter_price_max')
            if price_min:
                products = products.filter(price__gte=price_min)
            if price_max:
                products = products.filter(price__lte=price_max)
        
        job.total_items = products.count()
        job.save()
        
        success_count = 0
        error_count = 0
        errors = []
        
        with transaction.atomic():
            for i, product in enumerate(products):
                try:
                    if action == 'update_price':
                        update_method = params.get('price_update_method')
                        value = float(params.get('price_value', 0))
                        
                        if update_method == 'percentage':
                            product.price *= (1 + value / 100)
                        elif update_method == 'fixed':
                            product.price += value
                        else:  # set
                            product.price = value
                        
                        # Ensure price is not negative
                        if product.price < 0:
                            product.price = 0
                    
                    elif action == 'update_stock':
                        update_method = params.get('stock_update_method')
                        value = int(params.get('stock_value', 0))
                        
                        if update_method == 'percentage':
                            product.stock = int(product.stock * (1 + value / 100))
                        elif update_method == 'fixed':
                            product.stock += value
                        else:  # set
                            product.stock = value
                        
                        # Ensure stock is not negative
                        if product.stock < 0:
                            product.stock = 0
                    
                    elif action == 'update_status':
                        new_status = params.get('new_status')
                        product.is_active = (new_status == 'active')
                    
                    elif action == 'update_category':
                        category_id = params.get('new_category')
                        if category_id:
                            category = Category.objects.get(id=category_id)
                            product.category = category
                    
                    elif action == 'add_tags':
                        tags_to_add = params.get('tags_to_add', '')
                        if tags_to_add:
                            current_tags = set(product.tags or [])
                            new_tags = [tag.strip() for tag in tags_to_add.split(',') if tag.strip()]
                            product.tags = list(current_tags.union(new_tags))
                    
                    elif action == 'remove_tags':
                        tags_to_remove = params.get('tags_to_remove', '')
                        if tags_to_remove:
                            current_tags = set(product.tags or [])
                            tags_to_remove_set = set(tag.strip() for tag in tags_to_remove.split(',') if tag.strip())
                            product.tags = list(current_tags - tags_to_remove_set)
                    
                    product.save()
                    
                    # Log success
                    BulkOperationLog.objects.create(
                        batch_job=job,
                        item_identifier=f"Product: {product.title} (ID: {product.id})",
                        action=action,
                        status='success',
                        details=_clean_json({'product_id': product.id, 'changes': params})
                    )
                    
                    success_count += 1
                    
                except Exception as e:
                    error_count += 1
                    error_msg = str(e)
                    errors.append({
                        'product_id': product.id if product else None,
                        'error': error_msg
                    })
                    
                    # Log error
                    BulkOperationLog.objects.create(
                        batch_job=job,
                        item_identifier=f"Product ID: {product.id if product else 'Unknown'}",
                        action=action,
                        status='error',
                        error_message=error_msg,
                        details=_clean_json({'product_id': product.id if product else None})
                    )
                
                # Update progress
                job.processed_items = i + 1
                job.success_count = success_count
                job.error_count = error_count
                job.save(update_fields=['processed_items', 'success_count', 'error_count'])
        
        # Update job completion
        _complete_job(job, error_count, errors)
        
        logger.info(f"Bulk update job {job_id} completed: {success_count} success, {error_count} errors")
        
        return {
            'job_id': job_id,
            'success_count': success_count,
            'error_count': error_count,
            'status': job.status
        }
        
    except Exception as e:
        logger.error(f"Error processing bulk update job {job_id}: {str(e)}")
        
        try:
            job = BatchJob.objects.get(id=job_id)
            job.status = 'failed'
            job.completed_at = timezone.now()
            job.errors = [{'error': str(e)}]
            job.save()
        except:
            pass
        
        raise

@shared_task(bind=True)
def process_import_task(self, job_id):
    """Process import job"""
    try:
        job = BatchJob.objects.get(id=job_id)
        job.status = 'processing'
        job.started_at = timezone.now()
        job.save()
        logger.info('Import job %s: status → processing', job_id)

        params = job.parameters
        template_id = params.get('template_id')
        
        # Get template if specified
        template = None
        if template_id:
            try:
                template = ImportTemplate.objects.get(id=template_id)
            except ImportTemplate.DoesNotExist:
                pass
        
        # Read file
        file_content = job.file.read()
        file_ext = job.file.name.split('.')[-1].lower()

        # Import pandas lazily to avoid hard dependency during startup
        try:
            import pandas as pd
        except Exception:
            pd = None

        # Get field mapping from job parameters (form submits this as JSON) or template
        field_mapping = {}
        try:
            fm = params.get('field_mapping')
            if fm:
                if isinstance(fm, str):
                    import json as _json
                    field_mapping = _json.loads(fm)
                elif isinstance(fm, dict):
                    field_mapping = fm
        except Exception:
            field_mapping = {}
        if not field_mapping:
            field_mapping = template.field_mapping if template else {}

        # Load rows depending on file type; prefer pandas but fall back for CSV
        rows = []
        if file_ext == 'csv':
            rows = _load_csv_rows(file_content, job_id=job_id)
        elif file_ext in ['xlsx', 'xls']:
            if pd is None:
                raise ImportError('pandas is required to process Excel imports')
            df = pd.read_excel(BytesIO(file_content))
            headers, normalized_rows = _normalize_tabular_rows(
                [[str(cell or '').strip() for cell in df.columns.tolist()]]
                + [[str(cell or '').strip() for cell in row] for row in df.values.tolist()]
            )
            rows = [dict(zip(headers, row)) for row in normalized_rows]
        else:
            raise ValueError(f"Unsupported file format: {file_ext}")

        # If still no mapping provided, attempt to auto-detect mapping from CSV headers
        if not field_mapping and rows:
            # rows may be list of dicts; use the first row's keys as detected headers
            first = rows[0]
            detected_headers = list(first.keys()) if isinstance(first, dict) else []
            guesses = {
                'title': ['title', 'product name', 'name'],
                'sku': ['sku', 'item code', 'product code'],
                'description': ['description', 'desc', 'details'],
                'price': ['price', 'cost', 'amount'],
                'stock': ['stock', 'quantity', 'qty'],
                'category': ['category', 'cat'],
                'condition': ['condition'],
                'tags': ['tags', 'tag'],
                'location': ['location', 'town', 'city'],
                'is_active': ['is active', 'active', 'enabled', 'published'],
                'image_url': ['image', 'image_url', 'main image', 'primary image', 'photo'],
                'image_urls': ['images', 'gallery images', 'additional images'],
            }
            auto_map = {}
            for h in detected_headers:
                hn = str(h).strip().lower()
                for field, tokens in guesses.items():
                    if any(tok in hn for tok in tokens):
                        auto_map[h] = field
                        break
            if auto_map:
                field_mapping = auto_map

        # Process rows
        total_rows = len(rows)
        job.total_items = total_rows
        job.save()

        success_count = 0
        error_count = 0
        errors = []

        for index, row in enumerate(rows):
            try:
                row_data = row if isinstance(row, dict) else row.to_dict()

                # Apply field mapping
                mapped_data = {}
                for csv_col, model_field in field_mapping.items():
                    if csv_col in row_data:
                        mapped_data[model_field] = row_data[csv_col]
                    else:
                        # try case-insensitive match
                        for k in row_data.keys():
                            if k and k.strip().lower() == str(csv_col).strip().lower():
                                mapped_data[model_field] = row_data[k]
                                break

                # Process based on template type
                template_type = params.get('template_type', 'products')

                if template_type == 'products':
                    process_product_import_row(job.store, mapped_data, params)

                # Log success
                BulkOperationLog.objects.create(
                    batch_job=job,
                    item_identifier=f"Row {index + 2}",  # +2 for header row and 1-index
                    action='import',
                    status='success',
                    details=_clean_json(mapped_data)
                )

                success_count += 1

            except Exception as e:
                error_count += 1
                error_msg = str(e)
                errors.append({
                    'row': index + 2,
                    'error': error_msg,
                    'data': _clean_json(row_data) if 'row_data' in locals() else None
                })

                # Log error
                BulkOperationLog.objects.create(
                    batch_job=job,
                    item_identifier=f"Row {index + 2}",
                    action='import',
                    status='error',
                    error_message=error_msg,
                    details=_clean_json(row_data) if 'row_data' in locals() else None
                )

            # Update progress
            job.processed_items = index + 1
            job.success_count = success_count
            job.error_count = error_count
            job.save(update_fields=['processed_items', 'success_count', 'error_count'])
        
        # Update job completion
        _complete_job(job, error_count, errors)
        
        logger.info(f"Import job {job_id} completed: {success_count} success, {error_count} errors")
        
        return {
            'job_id': job_id,
            'success_count': success_count,
            'error_count': error_count,
            'status': job.status
        }
        
    except Exception as e:
        logger.error(f"Error processing import job {job_id}: {str(e)}")
        
        try:
            job = BatchJob.objects.get(id=job_id)
            job.status = 'failed'
            job.completed_at = timezone.now()
            job.errors = [{'error': str(e)}]
            job.save()
        except:
            pass
        
        raise

def process_product_import_row(store, data, params):
    """Process a single product import row"""
    import math
    
    sku = data.get('sku')
    title = data.get('title')
    
    # Ensure title is a non-empty string (not NaN or other float values)
    if isinstance(title, float) and (math.isnan(title) or not math.isfinite(title)):
        title = None
    if isinstance(title, str):
        title = title.strip() if title else None
    
    if not title:
        raise ValueError("Product title is required")
    
    # Ensure SKU is a string if present
    if sku is not None:
        if isinstance(sku, float) and (math.isnan(sku) or not math.isfinite(sku)):
            sku = None
        elif isinstance(sku, str):
            sku = sku.strip() if sku else None
        else:
            sku = str(sku) if sku else None
    
    # Look for existing product
    product = None
    if sku:
        # Some deployments may not have a `sku` field on Listing.
        # Safely attempt to use `sku` field; if it doesn't exist, fall back to `slug` lookup.
        try:
            Listing._meta.get_field('sku')
            product = Listing.objects.filter(store=store, sku=sku).first()
        except FieldDoesNotExist:
            # fallback: try matching slug
            try:
                product = Listing.objects.filter(store=store, slug=sku).first()
            except Exception:
                product = None
    if not product and title:
        product = Listing.objects.filter(store=store, title__iexact=title).first()
    
    update_existing = params.get('update_existing', True)
    create_new = params.get('create_new', True)
    
    if product and not update_existing:
        return  # Skip existing products
    
    if not product and not create_new:
        return  # Don't create new products
    
    with transaction.atomic():
        if not product:
            # Create new product
            product = Listing(store=store, seller=store.owner)
        existing_related_images = product.images.count() if product.pk else 0

        delivery_choice = _normalize_choice(data.get('delivery_option'), Listing.DELIVERY_OPTIONS)
        condition_choice = _normalize_choice(data.get('condition'), Listing.CONDITION_CHOICES)
        location_choice = _normalize_location(data.get('location'))
        image_urls = _parse_image_candidates(data)
        
        # Update fields
        for field, value in data.items():
            if hasattr(product, field) and value is not None:
                # Handle special field types
                if field == 'price':
                    parsed_price = _parse_decimal(value)
                    if parsed_price is not None and parsed_price >= 0:
                        product.price = parsed_price
                elif field == 'stock':
                    try:
                        v = int(float(value))
                        if v >= 0:
                            product.stock = v
                        else:
                            continue
                    except (ValueError, TypeError):
                        continue
                elif field == 'is_active':
                    setattr(product, field, str(value).lower() in ['true', 'yes', '1', 'active'])
                elif field == 'category' and value:
                    # Try to find category
                    category = Category.objects.filter(
                        name__iexact=str(value).strip()
                    ).first()
                    if not category:
                        category, _ = Category.objects.get_or_create(
                            name=str(value).strip()[:100],
                            defaults={'is_active': True},
                        )
                    product.category = category
                elif field == 'condition':
                    if condition_choice:
                        product.condition = condition_choice
                elif field == 'delivery_option':
                    if delivery_choice:
                        product.delivery_option = delivery_choice
                elif field == 'location':
                    if location_choice:
                        product.location = location_choice
                else:
                    # For string fields, ensure we don't set NaN or other invalid values
                    if isinstance(value, float) and (math.isnan(value) or not math.isfinite(value)):
                        # Skip invalid float values for string fields
                        continue
                    # Convert to string and strip whitespace for string fields
                    if isinstance(value, str):
                        value = value.strip()
                    elif value is not None:
                        value = str(value).strip()
                    
                    # Only set if not empty
                    if value:
                        setattr(product, field, value)
        
        # Set defaults for required fields
        if not product.description:
            product.description = title
        if not product.price:
            product.price = 0
        if product.stock is None:
            product.stock = 0
        if not product.condition:
            product.condition = 'used'
        if not product.delivery_option:
            product.delivery_option = 'pickup'
        if not product.location:
            default_location = _normalize_location(getattr(store, 'location', None))
            product.location = default_location or Listing.HOMABAY_LOCATIONS[0][0]
        if not product.seller:
            product.seller = store.owner
        
        product.save()

        if image_urls:
            _attach_image_urls_to_product(product, image_urls)

        # Auto-fetch images when requested and none exist for this listing
        try:
            auto_fetch = params.get('auto_fetch_images') or params.get('auto_fetch', False)
            if auto_fetch and fetch_and_attach is not None:
                # ensure there is an images related manager
                images_rel = getattr(product, 'images', None)
                has_images = images_rel.count() if images_rel is not None else existing_related_images
                if has_images == 0:
                    q = title or data.get('title') or f"{product.title} {store.name}"
                    listing_image = fetch_and_attach(product, q)
                    if listing_image and not product.image:
                        product.image = listing_image.image
                        product.save(update_fields=['image'])
        except Exception as e:
            logger.exception('Auto-fetch images failed for product %s: %s', getattr(product, 'id', None), e)

@shared_task(bind=True)
def generate_export_task(self, job_id):
    """Generate export file"""
    try:
        job = ExportJob.objects.get(id=job_id)
        job.status = 'processing'
        job.save()
        
        store = job.store
        export_type = job.export_type
        filters = job.filters
        columns = job.columns
        
        # Generate data based on export type
        if export_type == 'products':
            data = export_products(store, filters, columns)
        elif export_type == 'inventory':
            data = export_inventory(store, filters, columns)
        elif export_type == 'customers':
            data = export_customers(store, filters, columns)
        elif export_type == 'orders':
            data = export_orders(store, filters, columns)
        else:
            data = export_analytics(store, filters, columns)
        
        # Create file based on format
        format = job.format
        
        if format == 'csv':
            file_content = generate_csv(data, columns)
            filename = f"{export_type}_export_{timezone.now().strftime('%Y%m%d_%H%M%S')}.csv"
            content_type = 'text/csv'
        
        elif format == 'excel':
            file_content = generate_excel(data, columns)
            filename = f"{export_type}_export_{timezone.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            content_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        
        elif format == 'json':
            file_content = generate_json(data)
            filename = f"{export_type}_export_{timezone.now().strftime('%Y%m%d_%H%M%S')}.json"
            content_type = 'application/json'
        
        else:  # pdf
            file_content = generate_pdf(data, columns, store, export_type)
            filename = f"{export_type}_report_{timezone.now().strftime('%Y%m%d_%H%M%S')}.pdf"
            content_type = 'application/pdf'
        
        # Save file to job
        job.file.save(filename, ContentFile(file_content))
        job.file_size = len(file_content)
        job.status = 'completed'
        job.completed_at = timezone.now()
        job.save()
        
        logger.info(f"Export job {job_id} completed: {filename}")
        
        return {
            'job_id': job_id,
            'filename': filename,
            'file_size': job.file_size,
            'status': job.status
        }
        
    except Exception as e:
        logger.error(f"Error generating export job {job_id}: {str(e)}")
        
        try:
            job = ExportJob.objects.get(id=job_id)
            job.status = 'failed'
            job.error_message = str(e)
            job.completed_at = timezone.now()
            job.save()
        except:
            pass
        
        raise

def export_products(store, filters, columns):
    """Export products data"""
    products = store.listings.select_related('category')
    
    # Apply filters
    date_range = filters.get('date_range')
    if date_range:
        end_date = timezone.now().date()
        
        if date_range == 'today':
            start_date = end_date
        elif date_range == 'yesterday':
            start_date = end_date - timedelta(days=1)
        elif date_range == 'this_week':
            start_date = end_date - timedelta(days=end_date.weekday())
        elif date_range == 'last_week':
            start_date = end_date - timedelta(days=end_date.weekday() + 7)
            end_date = start_date + timedelta(days=6)
        elif date_range == 'this_month':
            start_date = end_date.replace(day=1)
        elif date_range == 'last_month':
            first_day_current = end_date.replace(day=1)
            end_date = first_day_current - timedelta(days=1)
            start_date = end_date.replace(day=1)
        elif date_range == 'custom':
            start_date = datetime.strptime(filters.get('start_date'), '%Y-%m-%d').date()
            end_date = datetime.strptime(filters.get('end_date'), '%Y-%m-%d').date()
        
        products = products.filter(
            created_at__date__gte=start_date,
            created_at__date__lte=end_date
        )
    
    if not filters.get('include_inactive', False):
        products = products.filter(is_active=True)
    
    if not filters.get('include_out_of_stock', True):
        products = products.filter(stock__gt=0)
    
    # Prepare data
    data = []
    for product in products:
        row = {}
        
        for column in columns:
            if column == 'id':
                row['id'] = product.id
            elif column == 'title':
                row['title'] = product.title
            elif column == 'sku':
                row['sku'] = product.sku or ''
            elif column == 'description':
                row['description'] = product.description or ''
            elif column == 'price':
                row['price'] = float(product.price)
            elif column == 'stock':
                row['stock'] = product.stock
            elif column == 'category':
                row['category'] = product.category.name if product.category else ''
            elif column == 'condition':
                row['condition'] = product.get_condition_display() if product.condition else ''
            elif column == 'location':
                row['location'] = product.get_location_display() if product.location else ''
            elif column == 'created_at':
                row['created_at'] = product.created_at.strftime('%Y-%m-%d %H:%M:%S')
            elif column == 'is_active':
                row['is_active'] = 'Active' if product.is_active else 'Inactive'
        
        data.append(row)
    
    return data


def export_inventory(store, filters, columns):
    """Export inventory (stock) data for store listings"""
    products = store.listings.select_related('category')

    data = []
    for product in products:
        row = {}
        for column in columns:
            if column == 'id':
                row['id'] = product.id
            elif column == 'title':
                row['title'] = product.title
            elif column == 'sku':
                row['sku'] = product.sku or ''
            elif column == 'stock':
                row['stock'] = product.stock
            elif column == 'price':
                row['price'] = float(product.price)
            elif column == 'category':
                row['category'] = product.category.name if product.category else ''
            else:
                row[column] = getattr(product, column, '')
        data.append(row)
    return data


def export_customers(store, filters, columns):
    """Return a basic customers export. If no orders/customers model available, return empty list."""
    try:
        from listings.models import Order
        customers = Order.objects.filter(listing__store=store).values('buyer__id', 'buyer__username').distinct()
        data = []
        for c in customers:
            row = {}
            for column in columns:
                if column in c:
                    row[column] = c.get(column)
                else:
                    # map common names
                    if column == 'id':
                        row['id'] = c.get('buyer__id')
                    elif column == 'username':
                        row['username'] = c.get('buyer__username')
                    else:
                        row[column] = ''
            data.append(row)
        return data
    except Exception:
        return []


def export_orders(store, filters, columns):
    """Export orders related to the store. Returns empty list if Order model not found."""
    try:
        from listings.models import Order
        orders = Order.objects.filter(items__listing__store=store).distinct()
        data = []
        for order in orders:
            row = {}
            for column in columns:
                if column == 'id':
                    row['id'] = order.id
                elif column == 'status':
                    row['status'] = getattr(order, 'status', '')
                elif column == 'total':
                    row['total'] = float(getattr(order, 'total', 0))
                elif column == 'buyer':
                    row['buyer'] = getattr(order.buyer, 'username', '') if getattr(order, 'buyer', None) else ''
                else:
                    row[column] = getattr(order, column, '')
            data.append(row)
        return data
    except Exception:
        return []


def export_analytics(store, filters, columns):
    """Basic analytics export: total products, total stock, avg price"""
    products = store.listings.all()
    total_products = products.count()
    total_stock = sum([p.stock or 0 for p in products])
    avg_price = 0
    try:
        avg_price = float(sum([float(p.price or 0) for p in products]) / total_products) if total_products else 0
    except Exception:
        avg_price = 0

    metrics = {
        'total_products': total_products,
        'total_stock': total_stock,
        'average_price': round(avg_price, 2),
    }

    # If columns requested, return as list of one row with requested metrics
    if columns:
        row = {col: metrics.get(col, '') for col in columns}
        return [row]
    return [metrics]

def generate_csv(data, columns):
    """Generate CSV from data"""
    output = StringIO()
    
    if not data:
        writer = csv.writer(output)
        writer.writerow(['No data available'])
        return output.getvalue()
    
    # Get headers from first row
    headers = list(data[0].keys()) if data else []
    
    writer = csv.DictWriter(output, fieldnames=headers)
    writer.writeheader()
    
    for row in data:
        writer.writerow(row)
    
    return output.getvalue()

def generate_excel(data, columns):
    """Generate Excel file from data"""
    from openpyxl import Workbook
    
    wb = Workbook()
    ws = wb.active
    
    if not data:
        ws.append(['No data available'])
    else:
        # Write headers
        headers = list(data[0].keys()) if data else []
        ws.append(headers)
        
        # Write data
        for row in data:
            ws.append([row.get(header, '') for header in headers])
    
    # Save to bytes
    output = BytesIO()
    wb.save(output)
    output.seek(0)
    
    return output.read()

def generate_json(data):
    """Generate JSON from data"""
    return json.dumps(data, indent=2, default=str)

def generate_pdf(data, columns, store, export_type):
    """Generate PDF report from data"""
    from reportlab.lib.pagesizes import letter, landscape
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas
    
    buffer = BytesIO()
    
    # Create PDF document
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(letter),
        rightMargin=72,
        leftMargin=72,
        topMargin=72,
        bottomMargin=72
    )
    
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=16,
        spaceAfter=30,
        alignment=1  # Center
    )
    
    # Build story
    story = []
    
    # Title
    story.append(Paragraph(f"{store.name} - {export_type.title()} Report", title_style))
    story.append(Paragraph(f"Generated: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}", styles['Normal']))
    story.append(Spacer(1, 20))
    
    if not data:
        story.append(Paragraph("No data available", styles['Normal']))
    else:
        # Prepare table data
        headers = list(data[0].keys()) if data else []
        table_data = [headers]
        
        for row in data:
            table_data.append([str(row.get(header, '')) for header in headers])
        
        # Create table
        table = Table(table_data)
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 10),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
            ('TEXTCOLOR', (0, 1), (-1, -1), colors.black),
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 1), (-1, -1), 8),
            ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ]))
        
        story.append(table)
    
    # Build PDF
    doc.build(story)
    
    buffer.seek(0)
    return buffer.read()
