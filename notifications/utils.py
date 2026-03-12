from django.contrib.auth import get_user_model
from .models import Notification, NotificationPreference

User = get_user_model()

def create_notification(recipient, notification_type, title, message, 
                       sender=None, related_object_id=None, 
                       related_content_type='', action_url='', action_text=''):
    """
    Utility function to create notifications
    """
    # Get or create notification preferences
    preferences, created = NotificationPreference.objects.get_or_create(user=recipient)
    
    # Check if user wants this type of notification
    push_enabled = getattr(preferences, f'push_{notification_type.split("_")[0]}', True)
    
    if push_enabled:
        notification = Notification.objects.create(
            recipient=recipient,
            sender=sender,
            notification_type=notification_type,
            title=title,
            message=message,
            related_object_id=related_object_id,
            related_content_type=related_content_type,
            action_url=action_url,
            action_text=action_text
        )
        # Attempt to broadcast via WebSocket so connected clients see it immediately
        try:
            from asgiref.sync import async_to_sync
            # broadcast_notification_via_websocket is defined later in this module
            async_to_sync(broadcast_notification_via_websocket)(notification)
        except Exception:
            # logger may be defined later; use safe fallback
            try:
                logger.debug('WebSocket broadcast failed or channel layer not configured')
            except Exception:
                pass

        # Best-effort push via OneSignal if configured
        try:
            NotificationService.send_push_notification(
                recipient,
                title,
                message,
                data={
                    'notification_id': notification.id,
                    'action_url': action_url,
                    'notification_type': notification_type,
                }
            )
        except Exception:
            pass

        return notification
    return None


# notifications/utils.py
import requests
from django.conf import settings
from django.core.mail import send_mail, get_connection
from django.template.loader import render_to_string
from django.utils.html import strip_tags
import logging

logger = logging.getLogger(__name__)

class NotificationService:
    @staticmethod
    def send_sms(phone_number, message):
        """Send SMS notification using Africa's Talking or similar service"""
        # Skip SMS sending entirely when Brevo SMS is disabled in settings.
        if not getattr(settings, 'BREVO_SMS_ENABLED', False) and not getattr(settings, 'SMS_ENABLED', False):
            logger.debug('SMS sending skipped: BREVO_SMS_ENABLED and SMS_ENABLED are both False')
            return False

        try:
            # Prefer Brevo if enabled
            if getattr(settings, 'BREVO_SMS_ENABLED', False) and getattr(settings, 'BREVO_API_KEY', None):
                try:
                    from baysoko.utils.sms import send_sms_brevo
                    res = send_sms_brevo(phone_number, message)
                    return res.get('success', False)
                except Exception:
                    logger.exception('Brevo SMS send failed, falling back')

            if getattr(settings, 'SMS_ENABLED', False):
                # Example with Africa's Talking
                import africastalking
                africastalking.initialize(
                    settings.AFRICASTALKING_USERNAME,
                    settings.AFRICASTALKING_API_KEY
                )
                sms = africastalking.SMS
                response = sms.send(message, [phone_number])
                logger.info(f"SMS sent to {phone_number}: {response}")
                return True
        except Exception as e:
            logger.error(f"SMS sending failed: {str(e)}")
        return False

    @staticmethod
    def send_email(to_email, subject, template_name, context):
        """Send HTML email notification using centralized threaded sender."""
        try:
            html_message = render_to_string(template_name, context)
            plain_message = strip_tags(html_message)
            # Use centralized threaded sender with provider-first approach
            try:
                from baysoko.utils.email_helpers import _send_email_threaded
                _send_email_threaded(subject, plain_message, html_message, [to_email])
                return True
            except Exception:
                # Fallback to Django send_mail with SMTP connection
                final_conn = get_connection(backend='django.core.mail.backends.smtp.EmailBackend')
                send_mail(
                    subject=subject,
                    message=plain_message,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[to_email],
                    html_message=html_message,
                    connection=final_conn,
                    fail_silently=False,
                )
                return True
        except Exception as e:
            logger.error(f"Email sending failed: {str(e)}")
            return False

    @staticmethod
    def send_push_notification(user, title, message, data=None):
        """Send push notification (implement based on your push service)"""
        try:
            app_id = getattr(settings, 'ONESIGNAL_APP_ID', '')
            api_key = getattr(settings, 'ONESIGNAL_API_KEY', '')
            rest_url = getattr(settings, 'ONESIGNAL_REST_URL', 'https://onesignal.com/api/v1/notifications')
            if not app_id or not api_key:
                return False
            external_id = str(user.id)
            payload = {
                'app_id': app_id,
                'include_external_user_ids': [external_id],
                'headings': {'en': title},
                'contents': {'en': message},
                'data': data or {},
            }
            headers = {
                'Authorization': f'Basic {api_key}',
                'Content-Type': 'application/json',
            }
            resp = requests.post(rest_url, json=payload, headers=headers, timeout=8)
            return resp.status_code in (200, 201)
        except Exception as e:
            logger.error(f"Push send failed: {str(e)}")
            return False

def notify_new_order(seller, buyer, order):
    """Notify seller about new order"""
    notification_service = NotificationService()
    
    # SMS to seller
    sms_message = f"New order #{order.id} from {buyer.get_full_name() or buyer.username}. Total: KSh {order.total_price}. Please process within 24 hours."
    notification_service.send_sms(seller.phone_number, sms_message)
    
    # Email to seller
    email_context = {
        'seller': seller,
        'buyer': buyer,
        'order': order,
        'order_items': order.order_items.all()
    }
    
    notification_service.send_email(
        seller.email,
        f"New Order #{order.id} - Baysoko",
        'emails/new_order_seller.html',
        email_context
    )
    
    # In-app notification
    # Create in-app notification and broadcast via websocket
    create_and_broadcast_notification(
        recipient=seller,
        notification_type='new_order',
        title="New Order Received",
        message=f"You have a new order #{order.id} from {buyer.get_full_name() or buyer.username}",
        related_object_id=order.id,
        related_content_type='order',
        action_url='',
        action_text='View Order'
    )

def notify_order_shipped(buyer, seller, order, tracking_number=None):
    """Notify buyer that order has been shipped"""
    notification_service = NotificationService()
    
    # SMS to buyer
    sms_message = f"Your order #{order.id} has been shipped."
    if tracking_number:
        sms_message += f" Track your delivery: {tracking_number}"
    
    notification_service.send_sms(order.phone_number, sms_message)
    
    # Email to buyer
    email_context = {
        'buyer': buyer,
        'seller': seller,
        'order': order,
        'tracking_number': tracking_number
    }
    
    notification_service.send_email(
        buyer.email,
        f"Order #{order.id} Shipped - Baysoko",
        'emails/order_shipped.html',
        email_context
    )

def notify_payment_received(seller, buyer, order):
    """Notify seller that payment was received"""
    notification_service = NotificationService()
    
    # SMS to seller
    sms_message = f"Payment received for order #{order.id}. Amount: KSh {order.total_price}. Please prepare the order for shipping."
    notification_service.send_sms(seller.phone_number, sms_message)
    
    # This will trigger the new order notification as well
    notify_new_order(seller, buyer, order)

def notify_delivery_assigned(order, driver_name, estimated_delivery):
    """Notify buyer about delivery assignment"""
    notification_service = NotificationService()
    
    sms_message = f"Delivery assigned for order #{order.id}. Driver: {driver_name}. Estimated delivery: {estimated_delivery}"
    notification_service.send_sms(order.phone_number, sms_message)

def notify_delivery_status(recipient, order, message):
    """Generic delivery status notification for buyer or seller.

    Args:
        recipient: User instance to receive notification (buyer or seller)
        order: Order instance related to the message
        message: Short text message to deliver
    """
    from django.urls import reverse
    notification_service = NotificationService()

    # Prefer order-level phone number, fall back to recipient attribute
    phone = getattr(order, 'phone_number', None) or getattr(recipient, 'phone_number', None)
    if phone:
        try:
            notification_service.send_sms(phone, str(message))
        except Exception:
            logger.exception('Failed sending delivery status SMS')

    # Create in-app notification
    try:
        order_id = getattr(order, 'id', None)
        return create_notification(
            recipient=recipient,
            notification_type='delivery_status',
            title='Delivery Update',
            message=str(message),
            related_object_id=order_id,
            related_content_type='order',
            action_url=reverse('order_detail', kwargs={'order_id': order_id}) if order_id else '',
            action_text='View Order'
        )
    except Exception:
        logger.exception('Failed creating in-app delivery status notification')
        return None

def notify_delivery_confirmed(seller, buyer, order):
    """Notify seller that delivery was confirmed and funds released"""
    notification_service = NotificationService()
    
    sms_message = f"Delivery confirmed for order #{order.id}. Funds of KSh {order.total_price} have been released to your account."
    notification_service.send_sms(seller.phone_number, sms_message)

def notify_order_delivered(buyer, order):
    """Notify buyer that order has been delivered"""
    notification_service = NotificationService()
    
    sms_message = f"Your order #{order.id} has been delivered. Thank you for shopping with us!"
    notification_service.send_sms(order.phone_number, sms_message)

# In notifications/utils.py, update the notify_new_review function:

def notify_new_review(seller, user, review, listing=None, review_type=None):
    """Send notification about a new review"""
    from .models import Notification
    
    if review_type == 'seller':
        title = f'New Seller Review from {user.username}'
        message = f'{user.username} has reviewed you as a seller.'
    elif review_type == 'order':
        title = f'New Order Review from {user.username}'
        message = f'{user.username} has reviewed their order experience.'
    else:
        # Default to listing review
        title = f'New Review on {listing.title}' if listing else f'New Review from {user.username}'
        message = f'{user.username} has reviewed {listing.title if listing else "your item"}.'
    
    notification = create_and_broadcast_notification(
        recipient=seller,
        sender=user,
        notification_type='review',
        title=title,
        message=message,
        related_object_id=getattr(review, 'id', None),
        related_content_type='review'
    )
    
    # You could also send email notification here if configured
    if hasattr(seller, 'email') and seller.email:
        try:
            # Prefer centralized NotificationService to ensure provider-first sending
            email_ctx = {
                'seller': seller,
                'user': user,
                'review': review,
                'listing': listing,
                'site_url': settings.SITE_URL,
            }
            NotificationService.send_email(
                seller.email,
                f'New Review Notification - {settings.SITE_NAME}',
                'emails/review_notification.html',
                email_ctx
            )
        except Exception as e:
            logger.error(f"Failed to send review notification email: {e}")
    
    return notification
def notify_listing_favorited(seller, user, listing):
    """Notify seller about favorite"""
    return create_notification(
        recipient=seller,
        sender=user,
        notification_type='favorite',
        title="Listing Favorited",
        message=f"{user.username} added your listing '{listing.title}' to favorites",
        related_object_id=listing.id,
        related_content_type='listing',
        action_url=f'/listing/{listing.id}/',
        action_text='View Listing'
    )

def notify_system_message(recipient, title, message, action_url=''):
    """Send system notification"""
    return create_notification(
        recipient=recipient,
        notification_type='system',
        title=title,
        message=message,
        related_content_type='system',
        action_url=action_url,
        action_text='View Details'
    )

def create_notification(recipient, notification_type, title, message, 
                       sender=None, related_object_id=None, 
                       related_content_type='', action_url='', action_text=''):
    """
    Utility function to create notifications with improved error handling
    """
    try:
        # Get or create notification preferences
        preferences, created = NotificationPreference.objects.get_or_create(user=recipient)
        
        # Check if user wants this type of notification
        push_enabled = getattr(preferences, f'push_{notification_type.split("_")[0]}', True)
        
        if push_enabled:
            notification = Notification.objects.create(
                recipient=recipient,
                sender=sender,
                notification_type=notification_type,
                title=title,
                message=message,
                related_object_id=related_object_id,
                related_content_type=related_content_type,
                action_url=action_url,
                action_text=action_text
            )
            
            # Log notification creation
            logger.info(f"Notification created for {recipient.username}: {title}")
            
            return notification
        return None
    except Exception as e:
        logger.error(f"Error creating notification: {str(e)}")
        return None

def notify_order_created(buyer, order):
    """Notify buyer about order creation"""
    from django.urls import reverse
    return create_notification(
        recipient=buyer,
        notification_type='order_created',
        title='Order Placed Successfully',
        message=f'Your order #{order.id} has been placed. Please complete payment.',
        related_object_id=order.id,
        related_content_type='order',
        action_url=reverse('order_detail', kwargs={'order_id': order.id}),
        action_text='View Order'
    )

def notify_order_paid(buyer, order):
    """Notify buyer about payment confirmation"""
    from django.urls import reverse
    return create_notification(
        recipient=buyer,
        notification_type='payment_success',
        title='Payment Confirmed',
        message=f'Payment for order #{order.id} has been confirmed. Your order is now being processed.',
        related_object_id=order.id,
        related_content_type='order',
        action_url=reverse('order_detail', kwargs={'order_id': order.id}),
        action_text='Track Order'
    )

def notify_order_status_update(buyer, order, status):
    """Notify buyer about order status update"""
    from django.urls import reverse
    status_messages = {
        'processing': 'Your order is being prepared by the seller.',
        'shipped': 'Your order has been shipped and is on its way.',
        'delivered': 'Your order has been delivered successfully.',
        'cancelled': 'Your order has been cancelled.',
        'refunded': 'Your order has been refunded.',
    }
    
    message = status_messages.get(status, f'Your order status has been updated to {status}.')
    
    return create_notification(
        recipient=buyer,
        notification_type='order_update',
        title=f'Order #{order.id} Update',
        message=message,
        related_object_id=order.id,
        related_content_type='order',
        action_url=reverse('order_detail', kwargs={'order_id': order.id}),
        action_text='View Details'
    )


# ===================== WebSocket Broadcast Functions =====================

async def broadcast_notification_via_websocket(notification):
    """
    Broadcast a notification to the user's WebSocket connection.
    
    This function sends the notification in real-time via WebSocket instead of
    relying on polling. If WebSocket is not connected, the notification is still
    stored in the database for fallback polling.
    
    Args:
        notification: Notification model instance
    """
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        
        channel_layer = get_channel_layer()
        group_name = f"notifications_user_{notification.recipient_id}"
        
        notification_data = {
            'id': notification.id,
            'title': notification.title,
            'message': notification.message,
            'type': notification.notification_type,
            'is_read': notification.is_read,
            'time_since': notification.time_since,
            'action_url': notification.action_url,
            'action_text': notification.action_text,
            'created_at': notification.created_at.isoformat(),
            'sender': notification.sender.username if notification.sender else None,
        }
        
        await channel_layer.group_send(
            group_name,
            {
                'type': 'notification.created',
                'notification': notification_data
            }
        )
        
        logger.info(f"Notification {notification.id} broadcasted via WebSocket to group {group_name}")
    
    except Exception as e:
        # Silently fail - notification is already in DB for polling fallback
        logger.warning(f"Failed to broadcast notification via WebSocket: {str(e)}")


def broadcast_notification_read_status(notification):
    """
    Broadcast notification read status change via WebSocket.
    
    Args:
        notification: Notification model instance
    """
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer
        
        channel_layer = get_channel_layer()
        group_name = f"notifications_user_{notification.recipient_id}"
        
        async_to_sync(channel_layer.group_send)(
            group_name,
            {
                'type': 'notification.marked_read',
                'notification_id': notification.id
            }
        )
    except Exception as e:
        logger.warning(f"Failed to broadcast notification read status: {str(e)}")


def broadcast_bulk_read():
    """
    Broadcast bulk read action via WebSocket.
    This is called with user context, so we need the user ID passed in.
    
    Args:
        user_id: ID of the user
    """
    # This will be handled in signals with proper context
    pass


# ===================== Helper to Create & Broadcast Notification =====================

def create_and_broadcast_notification(recipient, notification_type, title, message,
                                     sender=None, related_object_id=None,
                                     related_content_type='', action_url='', action_text=''):
    """
    Creates a notification and immediately broadcasts it via WebSocket.
    
    This is a convenience function that combines creation with WebSocket broadcast.
    Falls back gracefully if WebSocket is unavailable.
    
    Args:
        recipient: User receiving the notification
        notification_type: Type of notification
        title: Notification title
        message: Notification message
        sender: User sending the notification (optional)
        related_object_id: ID of related object (optional)
        related_content_type: Type of related object (optional)
        action_url: URL for action button (optional)
        action_text: Text for action button (optional)
    
    Returns:
        Notification instance or None
    """
    notification = create_notification(
        recipient=recipient,
        notification_type=notification_type,
        title=title,
        message=message,
        sender=sender,
        related_object_id=related_object_id,
        related_content_type=related_content_type,
        action_url=action_url,
        action_text=action_text
    )
    
    if notification:
        # Try to broadcast via WebSocket (async operation)
        try:
            from asgiref.sync import async_to_sync
            async_to_sync(broadcast_notification_via_websocket)(notification)
        except Exception as e:
            logger.warning(f"Could not broadcast notification via WebSocket: {str(e)}")
            # Notification is still in DB, will be picked up by polling fallback
    
    return notification
