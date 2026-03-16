"""
Celery tasks for background order synchronization
"""
from celery import shared_task
from celery.utils.log import get_task_logger
from datetime import datetime
import time
from django.utils import timezone
from datetime import timedelta

from .models import EcommercePlatform, WebhookEvent
from .sync import OrderSyncService
from .processors import process_webhook_event

logger = get_task_logger(__name__)


@shared_task
def sync_all_platforms():
    """Scheduled task to sync all active platforms"""
    platforms = EcommercePlatform.objects.filter(
        is_active=True,
        sync_enabled=True
    )
    
    for platform in platforms:
        sync_platform.delay(platform.id)
    
    return f"Started sync for {platforms.count()} platforms"


@shared_task
def sync_platform(platform_id):
    """Sync orders from specific platform"""
    try:
        platform = EcommercePlatform.objects.get(id=platform_id, is_active=True)
        
        logger.info(f"Starting sync for platform: {platform.name}")
        
        service = OrderSyncService(platform)
        result = service.sync_orders(sync_type='scheduled')
        
        if result['success']:
            logger.info(
                f"Sync completed for {platform.name}: "
                f"{result['synced']} synced, {result['failed']} failed"
            )
        else:
            logger.error(f"Sync failed for {platform.name}: {result.get('error')}")
        
        return result
        
    except EcommercePlatform.DoesNotExist:
        logger.error(f"Platform {platform_id} not found or inactive")
        return {'success': False, 'error': 'Platform not found'}
    except Exception as e:
        logger.error(f"Error syncing platform {platform_id}: {str(e)}")
        return {'success': False, 'error': str(e)}


@shared_task
def process_webhook_async(webhook_event_id):
    """Process webhook event asynchronously"""
    try:
        webhook_event = WebhookEvent.objects.get(id=webhook_event_id)
        result = process_webhook_event(webhook_event)
        return result
    except WebhookEvent.DoesNotExist:
        logger.error(f"Webhook event {webhook_event_id} not found")
        return {'success': False, 'error': 'Webhook event not found'}
    except Exception as e:
        logger.error(f"Error processing webhook {webhook_event_id}: {str(e)}")
        return {'success': False, 'error': str(e)}


@shared_task
def retry_failed_webhooks():
    """Retry failed webhook events"""
    failed_events = WebhookEvent.objects.filter(
        status='failed',
        created_at__gte=timezone.now() - timedelta(hours=24)  # Last 24 hours
    )
    
    retried = 0
    for event in failed_events:
        try:
            event.status = 'processing'
            event.save(update_fields=['status'])
            
            process_webhook_async.delay(event.id)
            retried += 1
            
        except Exception as e:
            logger.error(f"Failed to retry webhook {event.id}: {str(e)}")
    
    return f"Retried {retried} failed webhooks"


@shared_task
def cleanup_old_webhooks():
    """Clean up old webhook events"""
    cutoff_date = timezone.now() - timedelta(days=30)  # Keep 30 days
    
    deleted_count, _ = WebhookEvent.objects.filter(
        created_at__lt=cutoff_date,
        status__in=['processed', 'failed']
    ).delete()
    
    logger.info(f"Cleaned up {deleted_count} old webhook events")
    return f"Deleted {deleted_count} old webhook events"
