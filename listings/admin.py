from django.contrib import admin
from .models import Category, Listing

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ['name', 'description', 'schema_group']
    search_fields = ['name']
    fieldsets = (
        (None, {
            'fields': ('name', 'description', 'icon', 'is_active', 'order', 'is_featured')
        }),
        ('Schema', {
            'classes': ('collapse',),
            'fields': ('schema_group', 'fields_schema'),
            'description': 'Optionally specify a group key to share schemas between categories.'
        }),
    )
    readonly_fields = []
    # consider using a JSON editor widget elsewhere if desired


@admin.register(Listing)
class ListingAdmin(admin.ModelAdmin):
    list_display = ['title', 'seller', 'price', 'category', 'location', 'is_sold', 'date_created']
    list_filter = ['category', 'location', 'is_sold', 'date_created']
    search_fields = ['title', 'description']
    date_hierarchy = 'date_created'

from django.contrib import admin
from .models import Order
import json

@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ['id', 'user', 'status', 'webhook_status', 'tracking_number', 'created_at']
    list_filter = ['status', 'webhook_status']
    actions = ['resend_webhook']
    
    def webhook_status(self, obj):
        if obj.tracking_number:
            return "✓ Delivered to carrier"
        elif hasattr(obj, 'webhook_sent') and obj.webhook_sent:
            return "✓ Sent"
        else:
            return "⏳ Pending"
    webhook_status.short_description = "Webhook Status"
    
    def resend_webhook(self, request, queryset):
        from .webhook_service import webhook_service
        success = 0
        for order in queryset:
            if webhook_service.send_order_event(order, 'order_updated'):
                success += 1
        self.message_user(request, f"Resent webhook for {success} orders")
    resend_webhook.short_description = "Resend webhook to delivery system"