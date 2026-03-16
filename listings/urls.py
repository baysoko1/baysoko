from django.urls import path
from .views import ListingListView, ListingDetailView, ListingCreateView, ListingUpdateView, ListingDeleteView
from . import views
from . import ai_views
from . import order_views
from django.shortcuts import redirect
from notifications.views import notification_list
from . import api_views


urlpatterns = [
    path('', ListingListView.as_view(), name='home'),
    path('listing/<int:pk>/', ListingDetailView.as_view(), name='listing-detail'),
    path('all-listings/', views.all_listings, name='all-listings'),
    path('my-listings/', views.my_listings, name='my-listings'),
    path('api/my-listings/', api_views.MyListingsView.as_view(), name='my_listings'),
    path('favorites/', views.favorite_listings, name='favorites'),
    path('listing/<int:listing_id>/toggle_favorite/', views.toggle_favorite, name='toggle_favorite'),
    path('favorites/toggle/<int:listing_id>/', views.toggle_favorite, name='toggle_favorite'),
    path('favorites/', views.user_favorites, name='user_favorites'),
    path('reels/<str:kind>/<int:video_id>/<str:action>/', views.reel_action, name='reel_action'),
    path('listing/new/', ListingCreateView.as_view(), name='listing-create'),
    path('listing/<int:pk>/update/', ListingUpdateView.as_view(), name='listing-update'),
    path('listing/<int:pk>/delete/', ListingDeleteView.as_view(), name='listing-delete'),
    path('cart/add/<int:listing_id>/', views.add_to_cart, name='add_to_cart'),
    path('cart/', views.view_cart, name='view_cart'),
    path('cart/update/<int:item_id>/', views.update_cart_item, name='update_cart_item'),
    path('cart/remove/<int:item_id>/', views.remove_from_cart, name='remove_from_cart'),
    path('cart/clear/', views.clear_cart, name='clear_cart'),
    path('api/cart/clear/', views.clear_cart, name='clear-cart'),
    path('cart/get-cart-items/', views.get_cart_items, name='get_cart_items'),
    path('cart/summary/', views.cart_summary, name='cart_summary'),
    path('listings/json/', views.all_listings_json, name='all_listings_json'),
    path('checkout/', views.checkout, name='checkout'),
    path('checkout/delivery-fee/', views.checkout_delivery_fee, name='checkout_delivery_fee'),
    path('order/<int:order_id>/payment/', views.process_payment, name='process_payment'),
    path('orders/<int:order_id>/initiate-mpesa/', views.initiate_mpesa_payment, name='initiate_mpesa_payment'),
    path('order/<int:order_id>/check-payment-status/', views.check_payment_status, name='check_payment_status'),
    path('api/mpesa-callback/', views.mpesa_callback, name='mpesa_callback'),
    path('order/<int:order_id>/', views.order_detail, name='order_detail'),
    path('orders/', views.order_list, name='order_list'),
    path('api/orders/<str:tracking_number>/delivery-status/', views.delivery_status_api, name='delivery_status_api'),
    path('seller/orders/', views.seller_orders, name='seller_orders'),
    path('order/<int:order_id>/ship/', order_views.mark_order_shipped, name='mark_order_shipped'),
    path('order/<int:order_id>/deliver/', order_views.confirm_delivery, name='confirm_delivery'),
    path('order/<int:order_id>/dispute/', order_views.create_dispute, name='create_dispute'),
    path('order/<int:order_id>/update-status/', order_views.update_order_status, name='update_order_status'),
    path('order/<int:order_id>/resolve-dispute/', order_views.resolve_dispute, name='resolve_dispute'),
    path('order/<int:order_id>/mediate-dispute/', order_views.mediate_dispute, name='mediate_dispute'),
    path('review/<str:review_type>/<int:object_id>/', views.leave_review, name='leave_review'),
    path('order/<int:order_id>/review/', views.create_order_review, name='create_order_review'),
    path('api/reviews/<str:review_type>/<int:object_id>/', views.get_reviews, name='get_reviews'),
    
    # Legacy URLs for backward compatibility
    path('listing/<int:listing_id>/review/', lambda request, listing_id: redirect('leave_review', review_type='listing', object_id=listing_id)),
    path('seller/<int:seller_id>/review/', lambda request, seller_id: redirect('leave_review', review_type='seller', object_id=seller_id)),
    path('api/delivery-webhook/', views.delivery_webhook_receiver, name='delivery_webhook_receiver'),
    # AI Listing URLs (AI features handled in canonical listing form)
    path('listing/ai-generate/', ai_views.ai_generate_listing, name='ai_generate_listing'),
    path('listing/ai-quick/', ai_views.ai_quick_listing, name='ai_quick_listing'),
    path('order/<int:order_id>/review/', views.create_order_review, name='create_review'),
    path('ajax/get-unread-messages-count/', views.get_unread_messages_count, name='get_unread_messages_count'),
    # AJAX endpoints for inline edit/delete with WebSocket broadcasts
    path('ajax/listing/<int:listing_id>/edit/', views.ajax_edit_listing, name='ajax_edit_listing'),
    path('ajax/listing/<int:listing_id>/delete/', views.ajax_delete_listing, name='ajax_delete_listing'),
    # Newsletter subscription endpoint used by homepage AJAX form
    path('newsletter/subscribe/', views.newsletter_subscribe, name='newsletter_subscribe'),
    
]
