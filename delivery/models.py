from django.db import models
from django.conf import settings
from django.db.models import JSONField
from django.core.validators import MinValueValidator, MaxValueValidator
from decimal import Decimal
import uuid
from django.utils import timezone

# Delivery System Models
class DeliveryService(models.Model):
    """Delivery service provider (e.g., FedEx, DHL, local couriers)"""
    SERVICE_TYPES = [
        ('standard', 'Standard Delivery'),
        ('express', 'Express Delivery'),
        ('same_day', 'Same Day Delivery'),
        ('next_day', 'Next Day Delivery'),
        ('scheduled', 'Scheduled Delivery'),
    ]
    
    name = models.CharField(max_length=100)
    service_type = models.CharField(max_length=20, choices=SERVICE_TYPES)
    description = models.TextField(blank=True)
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    price_per_kg = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    price_per_km = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    estimated_days_min = models.PositiveIntegerField(default=1)
    estimated_days_max = models.PositiveIntegerField(default=5)
    is_active = models.BooleanField(default=True)
    service_areas = models.TextField(help_text="Comma-separated areas served")
    api_endpoint = models.URLField(blank=True, null=True)
    api_key = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['name']
        verbose_name = 'Delivery Service'
        verbose_name_plural = 'Delivery Services'
    
    def __str__(self):
        return f"{self.name} ({self.get_service_type_display()})"


class DeliveryPerson(models.Model):
    """Delivery personnel/riders"""
    STATUS_CHOICES = [
        ('available', 'Available'),
        ('busy', 'On Delivery'),
        ('offline', 'Offline'),
        ('on_break', 'On Break'),
    ]
    
    VEHICLE_TYPES = [
        ('motorcycle', 'Motorcycle'),
        ('bicycle', 'Bicycle'),
        ('car', 'Car'),
        ('truck', 'Truck'),
        ('van', 'Van'),
        ('foot', 'On Foot'),
    ]
    
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='delivery_person')
    employee_id = models.CharField(max_length=20, unique=True)
    phone = models.CharField(max_length=20)
    vehicle_type = models.CharField(max_length=20, choices=VEHICLE_TYPES)
    vehicle_registration = models.CharField(max_length=50, blank=True, null=True)
    current_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='offline')
    current_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    current_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    is_available = models.BooleanField(default=True)
    max_weight_capacity = models.DecimalField(max_digits=8, decimal_places=2, help_text="Maximum weight in kg")
    service_radius = models.IntegerField(help_text="Service radius in km", default=20)
    rating = models.DecimalField(max_digits=3, decimal_places=2, default=0.0)
    total_deliveries = models.IntegerField(default=0)
    completed_deliveries = models.IntegerField(default=0)
    is_verified = models.BooleanField(default=False)
    verification_document = models.FileField(upload_to='delivery/documents/', blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = 'Delivery Person'
        verbose_name_plural = 'Delivery Personnel'
    
    def __str__(self):
        return f"{self.user.get_full_name()} - {self.employee_id}"
    
    def update_location(self, lat, lng):
        """Update delivery person's current location"""
        self.current_latitude = lat
        self.current_longitude = lng
        self.save(update_fields=['current_latitude', 'current_longitude'])


class DeliveryProfile(models.Model):
    """Profile completion data for delivery app users (non-driver, non-admin)."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='delivery_profile')
    phone_number = models.CharField(max_length=20)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Delivery Profile - {self.user.username}"


class DeliveryZone(models.Model):
    """Geographical zones for delivery"""
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    polygon_coordinates = models.TextField(help_text="JSON array of [lat,lng] points")
    center_latitude = models.DecimalField(max_digits=9, decimal_places=6)
    center_longitude = models.DecimalField(max_digits=9, decimal_places=6)
    radius_km = models.DecimalField(max_digits=6, decimal_places=2)
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2)
    min_order_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['name']
    
    def __str__(self):
        return f"{self.name} - KES {self.delivery_fee}"


class DeliveryRouteRate(models.Model):
    """Fixed route rates between key towns."""
    ROUTE_POINTS = [
        ('homabay', 'Homabay'),
        ('mbita', 'Mbita'),
        ('oyugis', 'Oyugis'),
        ('kendu', 'Kendu Bay'),
        ('suba', 'Suba'),
        ('rodi', 'Rodi Kopany'),
        ('ndhiwa', 'Ndhiwa'),
    ]

    origin = models.CharField(max_length=30, choices=ROUTE_POINTS)
    destination = models.CharField(max_length=30, choices=ROUTE_POINTS)
    base_fee = models.DecimalField(max_digits=10, decimal_places=2)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['origin', 'destination']
        ordering = ['origin', 'destination']

    def __str__(self):
        return f"{self.origin} → {self.destination} (KES {self.base_fee})"


class DeliveryRequest(models.Model):
    """Delivery request from e-commerce platform"""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('assigned', 'Assigned to Rider'),
        ('picked_up', 'Picked Up'),
        ('in_transit', 'In Transit'),
        ('out_for_delivery', 'Out for Delivery'),
        ('delivered', 'Delivered'),
        ('failed', 'Delivery Failed'),
        ('cancelled', 'Cancelled'),
        ('returned', 'Returned to Sender'),
    ]
    
    # E-commerce integration fields
    order_id = models.CharField(max_length=50, unique=True)
    external_order_ref = models.CharField(max_length=100, blank=True, null=True)
    
    # Delivery details
    tracking_number = models.CharField(max_length=50, unique=True, default=uuid.uuid4)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    priority = models.IntegerField(default=1, help_text="1=Low, 2=Medium, 3=High, 4=Urgent")
    
    # Pickup information
    pickup_name = models.CharField(max_length=100)
    pickup_address = models.TextField()
    pickup_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    pickup_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    pickup_phone = models.CharField(max_length=20)
    pickup_email = models.EmailField(blank=True, null=True)
    pickup_notes = models.TextField(blank=True)
    
    # Delivery information
    recipient_name = models.CharField(max_length=100)
    recipient_address = models.TextField()
    recipient_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    recipient_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    recipient_phone = models.CharField(max_length=20)
    recipient_email = models.EmailField(blank=True, null=True)
    
    # Package details
    package_description = models.TextField()
    package_weight = models.DecimalField(max_digits=8, decimal_places=3, help_text="Weight in kg")
    package_length = models.DecimalField(max_digits=8, decimal_places=2, help_text="Length in cm", blank=True, null=True)
    package_width = models.DecimalField(max_digits=8, decimal_places=2, help_text="Width in cm", blank=True, null=True)
    package_height = models.DecimalField(max_digits=8, decimal_places=2, help_text="Height in cm", blank=True, null=True)
    declared_value = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_fragile = models.BooleanField(default=False)
    requires_signature = models.BooleanField(default=False)
    
    # Delivery service
    delivery_service = models.ForeignKey(DeliveryService, on_delete=models.SET_NULL, null=True, blank=True)
    delivery_zone = models.ForeignKey(DeliveryZone, on_delete=models.SET_NULL, null=True, blank=True)
    delivery_person = models.ForeignKey(DeliveryPerson, on_delete=models.SET_NULL, null=True, blank=True, related_name='assignments')
    
    # Financial details
    delivery_fee = models.DecimalField(max_digits=10, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    insurance_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_status = models.CharField(max_length=20, choices=[
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('partial', 'Partially Paid'),
        ('failed', 'Payment Failed'),
    ], default='pending')
    
    # Timestamps
    pickup_time = models.DateTimeField(null=True, blank=True)
    estimated_delivery_time = models.DateTimeField(null=True, blank=True)
    actual_delivery_time = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Additional fields
    notes = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['status']),
            models.Index(fields=['tracking_number']),
            models.Index(fields=['order_id']),
            models.Index(fields=['delivery_person']),
            models.Index(fields=['created_at']),
        ]
    
    def __str__(self):
        return f"Delivery #{self.tracking_number} - {self.status}"

    def save(self, *args, **kwargs):
        # Normalize any naive datetimes to avoid runtime warnings when USE_TZ=True.
        try:
            tz = timezone.get_current_timezone()
            for field_name in ('created_at', 'pickup_time', 'estimated_delivery_time', 'actual_delivery_time'):
                value = getattr(self, field_name, None)
                if value and timezone.is_naive(value):
                    setattr(self, field_name, timezone.make_aware(value, tz))
        except Exception:
            # Best-effort only; do not block saves.
            pass
        return super().save(*args, **kwargs)
    
    def calculate_distance(self):
        """Calculate distance between pickup and delivery points"""
        # This would use a geolocation service in production
        if self.pickup_latitude and self.pickup_longitude and self.recipient_latitude and self.recipient_longitude:
            # Simplified distance calculation using Haversine formula
            from math import radians, sin, cos, sqrt, atan2
            
            R = 6371  # Earth's radius in kilometers
            
            lat1 = radians(float(self.pickup_latitude))
            lon1 = radians(float(self.pickup_longitude))
            lat2 = radians(float(self.recipient_latitude))
            lon2 = radians(float(self.recipient_longitude))
            
            dlon = lon2 - lon1
            dlat = lat2 - lat1
            
            a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
            c = 2 * atan2(sqrt(a), sqrt(1-a))
            
            distance = R * c
            return round(distance, 2)
        return None
    
    def update_status(self, new_status, notes="", changed_by_user=None):
        """Update delivery status with history tracking"""
        try:
            if isinstance(self.metadata, dict) and self.metadata.get('external_delivery'):
                if self.payment_status != 'paid' and new_status not in ['pending', 'accepted', 'cancelled']:
                    raise ValueError("Payment required before dispatching this delivery.")
        except Exception:
            pass
        old_status = self.status
        self.status = new_status
        
        # Update timestamps for specific statuses
        if new_status == 'picked_up':
            self.pickup_time = timezone.now()
        elif new_status == 'delivered':
            self.actual_delivery_time = timezone.now()
            if self.delivery_person:
                self.delivery_person.completed_deliveries += 1
                self.delivery_person.save()
        
        self.save()
        
        # Create status history
        DeliveryStatusHistory.objects.create(
            delivery_request=self,
            old_status=old_status,
            new_status=new_status,
            changed_by=changed_by_user,  # Pass the user who made the change
            notes=notes
        )
        
        # Send notifications
        try:
            self.send_status_notification()
        except Exception:
            # best-effort: don't fail status updates if notifications are unavailable
            pass

    def send_status_notification(self):
        """Send a notification about status change to the customer or interested party."""
        try:
            from .utils import send_delivery_notification
            from django.contrib.auth import get_user_model
            User = get_user_model()

            recipient = None
            if isinstance(self.metadata, dict):
                user_id = self.metadata.get('user_id')
                if user_id:
                    recipient = User.objects.filter(id=user_id).first()

            # Fallback: try to find user via order mapping
            if not recipient and self.order_id:
                try:
                    oid = int(self.order_id.split('_')[-1]) if isinstance(self.order_id, str) and self.order_id.startswith('ECOMM_') else int(self.order_id)
                    from listings.models import Order
                    order = Order.objects.filter(id=oid).first()
                    if order and getattr(order, 'user', None):
                        recipient = order.user
                except Exception:
                    recipient = None

            if recipient:
                send_delivery_notification(delivery=self, notification_type='status_update', recipient=recipient)
        except Exception:
            # swallow errors; notifications are best-effort
            pass


class DeliveryStatusHistory(models.Model):
    """Track delivery status changes"""
    delivery_request = models.ForeignKey(DeliveryRequest, on_delete=models.CASCADE, related_name='status_history')
    old_status = models.CharField(max_length=20)
    new_status = models.CharField(max_length=20)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        default=None  # Add default
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = 'Delivery Status Histories'
    
    def __str__(self):
        return f"{self.delivery_request.tracking_number}: {self.old_status} → {self.new_status}"
    
    def get_changed_by_display(self):
        """Safe method to get changed by display name"""
        if self.changed_by:
            return self.changed_by.get_full_name() or self.changed_by.username
        return "System"

    def get_old_status_display(self):
        """Return human-readable label for old_status using DeliveryRequest choices"""
        try:
            choices = dict(DeliveryRequest.STATUS_CHOICES)
            return choices.get(self.old_status, self.old_status)
        except Exception:
            return self.old_status

    def get_new_status_display(self):
        """Return human-readable label for new_status using DeliveryRequest choices"""
        try:
            choices = dict(DeliveryRequest.STATUS_CHOICES)
            return choices.get(self.new_status, self.new_status)
        except Exception:
            return self.new_status


class DeliveryConfirmation(models.Model):
    """Record buyer confirmation of receipt for a delivery."""
    delivery_request = models.ForeignKey(DeliveryRequest, on_delete=models.CASCADE, related_name='confirmations')
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='delivery_confirmations'
    )
    confirmed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-confirmed_at']
        unique_together = ('delivery_request', 'confirmed_by')

    def __str__(self):
        return f"Confirmation for {self.delivery_request.tracking_number} by {self.confirmed_by.username} at {self.confirmed_at}"

    def process_release(self):
        """Process escrow release after buyer confirmation.

        This locates the corresponding Order (if any) and releases associated
        Escrow and Payment records. This method is idempotent.
        """
        try:
            # Try to find an Order referenced by delivery_request.order_id
            from listings.models import Order, Escrow, Payment
            # order_id may be numeric or prefixed (e.g., 'ECOMM_123')
            oid = None
            raw = self.delivery_request.order_id
            if not raw:
                return False
            try:
                oid = int(str(raw).split('_')[-1])
            except Exception:
                try:
                    oid = int(raw)
                except Exception:
                    oid = None

            if not oid:
                return False

            order = Order.objects.filter(id=oid).first()
            if not order:
                return False

            # Mark escrow ready for admin approval (do not auto-release)
            escrow = getattr(order, 'escrow', None)
            if escrow and escrow.status == 'held':
                try:
                    escrow.ready_for_release = True
                    escrow.save()
                except Exception:
                    pass

            return True
        except Exception:
            return False

class DeliveryProof(models.Model):
    """Proof of delivery"""
    PROOF_TYPES = [
        ('signature', 'Signature'),
        ('photo', 'Photo'),
        ('code', 'Verification Code'),
        ('id_verification', 'ID Verification'),
    ]
    
    delivery_request = models.ForeignKey(DeliveryRequest, on_delete=models.CASCADE, related_name='proofs')
    proof_type = models.CharField(max_length=20, choices=PROOF_TYPES)
    file = models.FileField(upload_to='delivery/proofs/', blank=True, null=True)
    signature_data = models.TextField(blank=True, null=True)
    verification_code = models.CharField(max_length=10, blank=True, null=True)
    recipient_name = models.CharField(max_length=100, blank=True, null=True)
    recipient_id_type = models.CharField(max_length=50, blank=True, null=True)
    recipient_id_number = models.CharField(max_length=50, blank=True, null=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        verbose_name_plural = 'Delivery Proofs'
    
    def __str__(self):
        return f"Proof for {self.delivery_request.tracking_number}"


class DeliveryOTP(models.Model):
    """OTP codes generated for driver verification at delivery time."""
    delivery_request = models.ForeignKey(DeliveryRequest, on_delete=models.CASCADE, related_name='otps')
    hashed_code = models.CharField(max_length=255)
    created_by = models.ForeignKey(DeliveryPerson, on_delete=models.SET_NULL, null=True, blank=True)
    used = models.BooleanField(default=False)
    attempts = models.PositiveIntegerField(default=0)
    request_ip = models.CharField(max_length=45, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ['-created_at']

    def is_valid(self, code):
        from django.utils import timezone
        from django.contrib.auth.hashers import check_password
        if self.used:
            return False
        if timezone.now() > self.expires_at:
            return False
        try:
            return check_password(str(code), self.hashed_code)
        except Exception:
            return False

    def mark_used(self):
        self.used = True
        self.save(update_fields=['used'])


class DeliveryAuditLog(models.Model):
    """Simple audit log for delivery events and OTP operations."""
    delivery_request = models.ForeignKey(DeliveryRequest, on_delete=models.CASCADE, related_name='audit_logs')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    event_type = models.CharField(max_length=100)
    message = models.TextField(blank=True)
    meta = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class DeliveryRoute(models.Model):
    """Route optimization for multiple deliveries"""
    delivery_person = models.ForeignKey(DeliveryPerson, on_delete=models.CASCADE, related_name='routes')
    route_name = models.CharField(max_length=100)
    deliveries = models.ManyToManyField(DeliveryRequest, related_name='routes')
    start_location = models.CharField(max_length=255)
    end_location = models.CharField(max_length=255)
    total_distance = models.DecimalField(max_digits=8, decimal_places=2, help_text="Distance in km")
    estimated_duration = models.IntegerField(help_text="Duration in minutes")
    start_time = models.DateTimeField()
    end_time = models.DateTimeField(null=True, blank=True)
    is_completed = models.BooleanField(default=False)
    route_data = models.JSONField(default=dict)  # Store optimized route coordinates
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.route_name} - {self.delivery_person.user.get_full_name()}"


class DeliveryRating(models.Model):
    """Rating and feedback for delivery service"""
    delivery_request = models.OneToOneField(DeliveryRequest, on_delete=models.CASCADE, related_name='rating')
    rating = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        help_text="Rating from 1 to 5"
    )
    comment = models.TextField(blank=True)
    on_time = models.BooleanField(default=True)
    packaging_quality = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        default=5
    )
    communication = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        default=5
    )
    would_recommend = models.BooleanField(default=True)
    issues = models.TextField(blank=True, help_text="Any issues faced")
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"Rating {self.rating}/5 for {self.delivery_request.tracking_number}"


class DeliveryNotification(models.Model):
    """Delivery notifications"""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='delivery_notifications')
    delivery_request = models.ForeignKey(DeliveryRequest, on_delete=models.CASCADE, null=True, blank=True)
    notification_type = models.CharField(max_length=50)
    title = models.CharField(max_length=200)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'is_read']),
        ]
    
    def __str__(self):
        return f"{self.title} - {self.user.email}"
class DeliveryPricingRule(models.Model):
    """Dynamic pricing rules for deliveries"""
    RULE_TYPES = [
        ('distance', 'Distance-based'),
        ('weight', 'Weight-based'),
        ('time', 'Time-based'),
        ('zone', 'Zone-based'),
        ('peak', 'Peak Hours'),
    ]
    
    name = models.CharField(max_length=100)
    rule_type = models.CharField(max_length=20, choices=RULE_TYPES)
    condition = models.JSONField(default=dict)  # e.g., {"min_distance": 5, "max_distance": 10}
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    price_modifier = models.DecimalField(max_digits=5, decimal_places=2, default=1.0)
    is_active = models.BooleanField(default=True)
    priority = models.IntegerField(default=1)
    applies_to = models.ManyToManyField(DeliveryService, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-priority', 'name']
    
    def __str__(self):
        return f"{self.name} ({self.get_rule_type_display()})"


class DeliveryPackageType(models.Model):
    """Pre-defined package types"""
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    base_price = models.DecimalField(max_digits=10, decimal_places=2)
    max_weight = models.DecimalField(max_digits=8, decimal_places=3)
    max_length = models.DecimalField(max_digits=8, decimal_places=2)
    max_width = models.DecimalField(max_digits=8, decimal_places=2)
    max_height = models.DecimalField(max_digits=8, decimal_places=2)
    icon = models.CharField(max_length=50, blank=True)
    is_active = models.BooleanField(default=True)
    
    def __str__(self):
        return self.name


class DeliveryTimeSlot(models.Model):
    """Available delivery time slots"""
    DAY_CHOICES = [
        (0, 'Monday'),
        (1, 'Tuesday'),
        (2, 'Wednesday'),
        (3, 'Thursday'),
        (4, 'Friday'),
        (5, 'Saturday'),
        (6, 'Sunday'),
    ]
    
    delivery_service = models.ForeignKey(DeliveryService, on_delete=models.CASCADE, related_name='time_slots')
    day_of_week = models.IntegerField(choices=DAY_CHOICES)
    start_time = models.TimeField()
    end_time = models.TimeField()
    max_orders = models.IntegerField(default=100)
    orders_booked = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    
    class Meta:
        ordering = ['day_of_week', 'start_time']
        unique_together = ['delivery_service', 'day_of_week', 'start_time', 'end_time']
    
    def __str__(self):
        return f"{self.get_day_of_week_display()} {self.start_time}-{self.end_time}"
    
    def is_available(self):
        return self.is_active and self.orders_booked < self.max_orders


class DeliveryInsurance(models.Model):
    """Insurance options for deliveries"""
    name = models.CharField(max_length=100)
    description = models.TextField()
    coverage_amount = models.DecimalField(max_digits=12, decimal_places=2)
    premium_rate = models.DecimalField(max_digits=5, decimal_places=4, help_text="Rate per declared value")
    min_premium = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    max_premium = models.DecimalField(max_digits=10, decimal_places=2, default=1000)
    is_active = models.BooleanField(default=True)
    
    def __str__(self):
        return f"{self.name} - {self.coverage_amount} coverage"


class DeliveryAnalytics(models.Model):
    """Aggregated delivery analytics"""
    date = models.DateField()
    total_deliveries = models.IntegerField(default=0)
    completed_deliveries = models.IntegerField(default=0)
    failed_deliveries = models.IntegerField(default=0)
    cancelled_deliveries = models.IntegerField(default=0)
    total_revenue = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    average_delivery_time = models.DurationField(null=True, blank=True)
    peak_hour = models.TimeField(null=True, blank=True)
    most_active_zone = models.ForeignKey(DeliveryZone, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ['date']
        verbose_name_plural = 'Delivery Analytics'
    
    def __str__(self):
        return f"Analytics for {self.date}"
