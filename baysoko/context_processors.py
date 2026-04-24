from typing import Dict


def global_counts(request) -> Dict[str, int]:
	"""Provide lightweight global counts for badges.

	This function is defensive: it attempts to reuse existing app-level
	helpers where available and falls back to zero on any error. It is
	intentionally inexpensive (simple counts) to avoid slowing template
	rendering.
	"""
	if not getattr(request, 'user', None) or not request.user.is_authenticated:
		return {
			'unread_notifications_count': 0,
			'unread_messages_count': 0,
			'cart_item_count': 0,
			'cart_total': 0,
		}

	# Notifications
	try:
		from notifications.models import Notification
		unread_notifications_count = Notification.objects.filter(recipient=request.user, is_read=False).count()
	except Exception:
		unread_notifications_count = 0

	# Messages (sum unread per conversation using Conversation.get_unread_count)
	try:
		from chats.models import Conversation
		unread_messages_count = 0
		qs = Conversation.objects.filter(participants=request.user)
		for conv in qs.only('id'):
			try:
				unread_messages_count += conv.get_unread_count(request.user)
			except Exception:
				# be defensive per-conversation
				continue
	except Exception:
		unread_messages_count = 0

	# Cart: reuse existing listings context processor if present
	cart_item_count = 0
	cart_total = 0
	try:
		from listings import context_processors as listings_cp
		cart_data = listings_cp.cart_item_count(request)
		# cart_item_count context processors may return full dict
		if isinstance(cart_data, dict):
			cart_item_count = int(cart_data.get('cart_item_count', 0) or 0)
			cart_total = float(cart_data.get('cart_total', 0) or 0)
	except Exception:
		cart_item_count = 0
		cart_total = 0

	return {
		'unread_notifications_count': unread_notifications_count,
		'unread_messages_count': unread_messages_count,
		'cart_item_count': cart_item_count,
		'cart_total': cart_total,
	}


def onesignal_config(request) -> Dict[str, str]:
	"""Expose OneSignal config to templates."""
	try:
		from django.conf import settings
		return {
			'ONESIGNAL_APP_ID': getattr(settings, 'ONESIGNAL_APP_ID', ''),
			'GOOGLE_MAPS_API_KEY': getattr(settings, 'GOOGLE_MAPS_API_KEY', ''),
			'GOOGLE_OAUTH_CLIENT_ID': getattr(settings, 'GOOGLE_OAUTH_CLIENT_ID', ''),
			'GOOGLE_ANDROID_CLIENT_ID': getattr(settings, 'GOOGLE_ANDROID_CLIENT_ID', ''),
		}
	except Exception:
		return {
			'ONESIGNAL_APP_ID': '',
			'GOOGLE_MAPS_API_KEY': '',
			'GOOGLE_OAUTH_CLIENT_ID': '',
			'GOOGLE_ANDROID_CLIENT_ID': '',
		}
