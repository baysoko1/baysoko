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
        try:
            if settings.SMS_ENABLED:
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
        # Implementation for Firebase Cloud Messaging or similar
        pass

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
    from notifications.models import Notification
    Notification.objects.create(
        user=seller,
        title="New Order Received",
        message=f"You have a new order #{order.id} from {buyer.get_full_name() or buyer.username}",
        notification_type='new_order',
        data={'order_id': order.id}
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
    
    notification = Notification.objects.create(
        recipient=seller,
        sender=user,
        notification_type='review',
        title=title,
        message=message,
        related_object=review
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