from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth import get_user_model
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone
from datetime import timedelta
import re
from .models import DeliveryRoute, DeliveryRequest
from django.core.exceptions import ValidationError

from .models import (
    DeliveryRequest, DeliveryPerson, DeliveryService, DeliveryZone,
    DeliveryProof, DeliveryRating, DeliveryTimeSlot, DeliveryPricingRule,
    DeliveryPackageType, DeliveryProfile
)


class DeliveryUserCreationForm(UserCreationForm):
    first_name = forms.CharField(max_length=150, required=True)
    last_name = forms.CharField(max_length=150, required=True)
    email = forms.EmailField(required=True)

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ('first_name', 'last_name', 'username', 'email', 'password1', 'password2')


class DeliveryProfileForm(forms.ModelForm):
    class Meta:
        model = DeliveryProfile
        fields = ['phone_number', 'address', 'city']
        widgets = {
            'phone_number': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '0712345678'}),
            'address': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Street, Building, Landmark'}),
            'city': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'City'}),
        }

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        # Lock phone when already verified on the main user profile
        try:
            if self.user and getattr(self.user, 'phone_verified', False):
                self.fields['phone_number'].disabled = True
                if getattr(self.user, 'phone_number', None):
                    self.fields['phone_number'].initial = self.user.phone_number
        except Exception:
            pass

    def clean_phone_number(self):
        phone = self.cleaned_data.get('phone_number')
        try:
            if self.user and getattr(self.user, 'phone_verified', False):
                return getattr(self.user, 'phone_number', phone)
        except Exception:
            pass
        return phone


class DeliveryRequestForm(forms.ModelForm):
    """Form for creating/updating delivery requests"""
    
    class Meta:
        model = DeliveryRequest
        fields = [
            'order_id', 'priority', 'pickup_name', 'pickup_address',
            'pickup_phone', 'pickup_email', 'pickup_latitude', 'pickup_longitude', 'pickup_notes',
            'recipient_name', 'recipient_address', 'recipient_phone', 'recipient_email',
            'recipient_latitude', 'recipient_longitude',
            'package_description', 'package_weight', 'declared_value',
            'package_length', 'package_width', 'package_height',
            'is_fragile', 'requires_signature', 'delivery_service',
            'delivery_zone', 'estimated_delivery_time', 'notes'
        ]
        widgets = {
            'pickup_address': forms.Textarea(attrs={'rows': 3}),
            'recipient_address': forms.Textarea(attrs={'rows': 3}),
            'package_description': forms.Textarea(attrs={'rows': 3}),
            'estimated_delivery_time': forms.DateTimeInput(attrs={'type': 'datetime-local'}),
            'notes': forms.Textarea(attrs={'rows': 2}),
            'pickup_latitude': forms.NumberInput(attrs={'step': '0.000001', 'min': '-90', 'max': '90'}),
            'pickup_longitude': forms.NumberInput(attrs={'step': '0.000001', 'min': '-180', 'max': '180'}),
            'recipient_latitude': forms.NumberInput(attrs={'step': '0.000001', 'min': '-90', 'max': '90'}),
            'recipient_longitude': forms.NumberInput(attrs={'step': '0.000001', 'min': '-180', 'max': '180'}),
            'package_length': forms.NumberInput(attrs={'step': '0.01'}),
            'package_width': forms.NumberInput(attrs={'step': '0.01'}),
            'package_height': forms.NumberInput(attrs={'step': '0.01'}),
        }
    
    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
        
        # Set initial values
        if not self.instance.pk:
            self.fields['estimated_delivery_time'].initial = timezone.now() + timedelta(hours=24)
        
        # Make some fields required
        self.fields['pickup_phone'].required = True
        self.fields['recipient_phone'].required = True
        self.fields['package_weight'].required = True
        
        # Add Bootstrap classes
        for field_name, field in self.fields.items():
            if field_name not in ['is_fragile', 'requires_signature']:
                # Preserve widget attributes set in Meta.widgets, but ensure Bootstrap class
                existing = field.widget.attrs
                existing.setdefault('class', 'form-control')
                field.widget.attrs.update(existing)

        # Ensure numeric fields have proper min/max where applicable
        for coord in ['pickup_latitude', 'recipient_latitude']:
            if coord in self.fields:
                self.fields[coord].widget.attrs.update({'min': '-90', 'max': '90', 'step': '0.000001'})
        for coord in ['pickup_longitude', 'recipient_longitude']:
            if coord in self.fields:
                self.fields[coord].widget.attrs.update({'min': '-180', 'max': '180', 'step': '0.000001'})
        for dim in ['package_length', 'package_width', 'package_height']:
            if dim in self.fields:
                self.fields[dim].widget.attrs.update({'min': '0', 'step': '0.01'})
    
    def clean_pickup_phone(self):
        phone = self.cleaned_data.get('pickup_phone')
        if phone:
            # Validate phone number format
            phone_regex = r'^\+?1?\d{9,15}$'
            if not re.match(phone_regex, phone):
                raise forms.ValidationError("Enter a valid phone number.")
        return phone
    
    def clean_recipient_phone(self):
        phone = self.cleaned_data.get('recipient_phone')
        if phone:
            phone_regex = r'^\+?1?\d{9,15}$'
            if not re.match(phone_regex, phone):
                raise forms.ValidationError("Enter a valid phone number.")
        return phone
    
    def clean_package_weight(self):
        weight = self.cleaned_data.get('package_weight')
        if weight and weight <= 0:
            raise forms.ValidationError("Package weight must be greater than 0.")
        if weight and weight > 100:  # 100kg limit
            raise forms.ValidationError("Package weight cannot exceed 100kg.")
        return weight
    
    def clean_estimated_delivery_time(self):
        delivery_time = self.cleaned_data.get('estimated_delivery_time')
        if delivery_time and delivery_time < timezone.now():
            raise forms.ValidationError("Estimated delivery time cannot be in the past.")
        return delivery_time
    def clean(self):
        cleaned_data = super().clean()
        
        # Validate pickup and delivery coordinates together
        pickup_lat = cleaned_data.get('pickup_latitude')
        pickup_lng = cleaned_data.get('pickup_longitude')
        delivery_lat = cleaned_data.get('recipient_latitude')
        delivery_lng = cleaned_data.get('recipient_longitude')
        
        # If one coordinate is provided, the other should be too
        if (pickup_lat and not pickup_lng) or (pickup_lng and not pickup_lat):
            raise ValidationError("Both pickup latitude and longitude must be provided together")
        
        if (delivery_lat and not delivery_lng) or (delivery_lng and not delivery_lat):
            raise ValidationError("Both recipient latitude and longitude must be provided together")
        
        # Validate package dimensions
        length = cleaned_data.get('package_length')
        width = cleaned_data.get('package_width')
        height = cleaned_data.get('package_height')
        
        if length and width and height:
            volume = float(length) * float(width) * float(height)
            if volume > 1000000:  # 1 cubic meter limit
                raise ValidationError("Package volume exceeds maximum allowed size (1 cubic meter)")
        
        return cleaned_data

class DeliveryPersonForm(forms.ModelForm):
    """Form for delivery person registration/update"""
    
    class Meta:
        model = DeliveryPerson
        fields = [
            'employee_id', 'phone', 'vehicle_type', 'vehicle_registration',
            'max_weight_capacity', 'service_radius', 'verification_document'
        ]
        widgets = {
            'phone': forms.TextInput(attrs={'placeholder': '+254...'}),
            'vehicle_registration': forms.TextInput(attrs={'placeholder': 'KAA 123A'}),
        }
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Add Bootstrap classes
        for field_name, field in self.fields.items():
            if field_name != 'verification_document':
                field.widget.attrs.update({'class': 'form-control input-modern'})
    
    def clean_phone(self):
        phone = self.cleaned_data.get('phone')
        if phone:
            phone_regex = r'^\+?1?\d{9,15}$'
            if not re.match(phone_regex, phone):
                raise forms.ValidationError("Enter a valid phone number.")
        return phone
    
    def clean_max_weight_capacity(self):
        capacity = self.cleaned_data.get('max_weight_capacity')
        if capacity and capacity <= 0:
            raise forms.ValidationError("Weight capacity must be greater than 0.")
        return capacity
    
    def clean_service_radius(self):
        radius = self.cleaned_data.get('service_radius')
        if radius and radius <= 0:
            raise forms.ValidationError("Service radius must be greater than 0.")
        return radius


class DeliveryServiceForm(forms.ModelForm):
    """Form for delivery service management"""
    
    class Meta:
        model = DeliveryService
        fields = [
            'name', 'service_type', 'description', 'base_price',
            'price_per_kg', 'price_per_km', 'estimated_days_min',
            'estimated_days_max', 'is_active', 'service_areas',
            'api_endpoint', 'api_key'
        ]
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
            'service_areas': forms.Textarea(attrs={'rows': 3}),
            'api_key': forms.PasswordInput(render_value=True),
        }
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Add Bootstrap classes
        for field_name, field in self.fields.items():
            if field_name != 'is_active':
                field.widget.attrs.update({'class': 'form-control'})
    
    def clean_base_price(self):
        price = self.cleaned_data.get('base_price')
        if price and price < 0:
            raise forms.ValidationError("Base price cannot be negative.")
        return price
    
    def clean_price_per_kg(self):
        price = self.cleaned_data.get('price_per_kg')
        if price and price < 0:
            raise forms.ValidationError("Price per kg cannot be negative.")
        return price
    
    def clean_price_per_km(self):
        price = self.cleaned_data.get('price_per_km')
        if price and price < 0:
            raise forms.ValidationError("Price per km cannot be negative.")
        return price


class DeliveryZoneForm(forms.ModelForm):
    """Form for delivery zone management"""
    
    class Meta:
        model = DeliveryZone
        fields = [
            'name', 'description', 'polygon_coordinates',
            'center_latitude', 'center_longitude', 'radius_km',
            'delivery_fee', 'min_order_amount', 'is_active'
        ]
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
            'polygon_coordinates': forms.Textarea(attrs={'rows': 3}),
        }
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Add validation for coordinates
        self.fields['center_latitude'].widget.attrs.update({
            'min': '-90', 'max': '90', 'step': '0.000001'
        })
        self.fields['center_longitude'].widget.attrs.update({
            'min': '-180', 'max': '180', 'step': '0.000001'
        })
        
        # Add Bootstrap classes
        for field_name, field in self.fields.items():
            if field_name != 'is_active':
                field.widget.attrs.update({'class': 'form-control'})
    
    def clean_center_latitude(self):
        lat = self.cleaned_data.get('center_latitude')
        if lat and (lat < -90 or lat > 90):
            raise forms.ValidationError("Latitude must be between -90 and 90.")
        return lat
    
    def clean_center_longitude(self):
        lng = self.cleaned_data.get('center_longitude')
        if lng and (lng < -180 or lng > 180):
            raise forms.ValidationError("Longitude must be between -180 and 180.")
        return lng
    
    def clean_delivery_fee(self):
        fee = self.cleaned_data.get('delivery_fee')
        if fee and fee < 0:
            raise forms.ValidationError("Delivery fee cannot be negative.")
        return fee


class DeliveryRouteForm(forms.ModelForm):
    """Form for creating/editing delivery routes"""

    class Meta:
        model = DeliveryRoute
        fields = [
            'route_name', 'delivery_person', 'start_location', 'end_location',
            'total_distance', 'estimated_duration', 'start_time', 'end_time',
            'is_completed'
        ]
        widgets = {
            'start_time': forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': 'form-control'}),
            'end_time': forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': 'form-control'}),
            'start_location': forms.TextInput(attrs={'class': 'form-control'}),
            'end_location': forms.TextInput(attrs={'class': 'form-control'}),
        }
    
    deliveries = forms.ModelMultipleChoiceField(
        queryset=DeliveryRequest.objects.all(),
        required=False,
        widget=forms.SelectMultiple(attrs={'class': 'form-control'})
    )
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields['deliveries'].initial = self.instance.deliveries.all()
    
    def save(self, commit=True):
        instance = super().save(commit=commit)
        if commit:
            instance.deliveries.set(self.cleaned_data.get('deliveries', []))
        return instance

    class Media:
        js = ()

    def __init__(self, *args, **kwargs):
        from .models import DeliveryRoute, DeliveryRequest
        super().__init__(*args, **kwargs)

        # Ensure deliveries queryset is set
        self.fields['deliveries'].queryset = DeliveryRequest.objects.all()

        # Dynamically add model fields to the form if not present
        # Use the DeliveryRoute model to build fields where appropriate
        if 'route_name' not in self.fields:
            self.fields['route_name'] = forms.CharField(max_length=100, widget=forms.TextInput(attrs={'class': 'form-control'}))
        if 'delivery_person' not in self.fields:
            self.fields['delivery_person'] = forms.ModelChoiceField(queryset=DeliveryRoute._meta.get_field('delivery_person').related_model.objects.all(), required=True, widget=forms.Select(attrs={'class': 'form-control'}))
        if 'start_location' not in self.fields:
            self.fields['start_location'] = forms.CharField(widget=forms.TextInput(attrs={'class': 'form-control'}))
        if 'end_location' not in self.fields:
            self.fields['end_location'] = forms.CharField(widget=forms.TextInput(attrs={'class': 'form-control'}))
        if 'total_distance' not in self.fields:
            self.fields['total_distance'] = forms.DecimalField(max_digits=8, decimal_places=2, widget=forms.NumberInput(attrs={'class': 'form-control'}))
        if 'estimated_duration' not in self.fields:
            self.fields['estimated_duration'] = forms.IntegerField(widget=forms.NumberInput(attrs={'class': 'form-control'}))
        if 'start_time' not in self.fields:
            self.fields['start_time'] = forms.DateTimeField(widget=forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': 'form-control'}))
        if 'end_time' not in self.fields:
            self.fields['end_time'] = forms.DateTimeField(required=False, widget=forms.DateTimeInput(attrs={'type': 'datetime-local', 'class': 'form-control'}))
        if 'is_completed' not in self.fields:
            self.fields['is_completed'] = forms.BooleanField(required=False, widget=forms.CheckboxInput())

    def save(self, commit=True):
        # Create or update DeliveryRoute instance
        from .models import DeliveryRoute

        instance = None
        if self.instance and self.instance.pk:
            instance = self.instance
        else:
            instance = DeliveryRoute()

        instance.route_name = self.cleaned_data.get('route_name', instance.route_name)
        instance.start_location = self.cleaned_data.get('start_location', instance.start_location)
        instance.end_location = self.cleaned_data.get('end_location', instance.end_location)
        instance.total_distance = self.cleaned_data.get('total_distance', instance.total_distance)
        instance.estimated_duration = self.cleaned_data.get('estimated_duration', instance.estimated_duration)
        instance.start_time = self.cleaned_data.get('start_time', instance.start_time)
        instance.end_time = self.cleaned_data.get('end_time', instance.end_time)
        instance.is_completed = self.cleaned_data.get('is_completed', instance.is_completed)
        if commit:
            instance.save()
            # save M2M deliveries
            deliveries = self.cleaned_data.get('deliveries')
            if deliveries is not None:
                instance.deliveries.set(deliveries)
        return instance


class DeliveryProofForm(forms.ModelForm):
    """Form for delivery proof submission"""
    
    class Meta:
        model = DeliveryProof
        fields = [
            'proof_type', 'file', 'signature_data',
            'verification_code', 'recipient_name',
            'recipient_id_type', 'recipient_id_number', 'notes'
        ]
        widgets = {
            'notes': forms.Textarea(attrs={'rows': 2}),
            'signature_data': forms.HiddenInput(),
        }
    
    def __init__(self, *args, **kwargs):
        self.delivery = kwargs.pop('delivery', None)
        super().__init__(*args, **kwargs)
        
        # Add Bootstrap classes
        for field_name, field in self.fields.items():
            if field_name not in ['proof_type', 'recipient_id_type']:
                field.widget.attrs.update({'class': 'form-control'})
    
    def clean(self):
        cleaned_data = super().clean()
        proof_type = cleaned_data.get('proof_type')
        
        # Validate based on proof type
        if proof_type == 'signature' and not cleaned_data.get('signature_data'):
            self.add_error('signature_data', 'Signature is required for signature proof.')
        
        elif proof_type == 'photo' and not cleaned_data.get('file'):
            self.add_error('file', 'Photo is required for photo proof.')
        
        elif proof_type == 'code' and not cleaned_data.get('verification_code'):
            self.add_error('verification_code', 'Verification code is required for code proof.')
        
        elif proof_type == 'id_verification':
            if not cleaned_data.get('recipient_name'):
                self.add_error('recipient_name', 'Recipient name is required for ID verification.')
            if not cleaned_data.get('recipient_id_type'):
                self.add_error('recipient_id_type', 'ID type is required for ID verification.')
            if not cleaned_data.get('recipient_id_number'):
                self.add_error('recipient_id_number', 'ID number is required for ID verification.')
        
        return cleaned_data


class DeliveryRatingForm(forms.ModelForm):
    """Form for rating delivery service"""
    
    class Meta:
        model = DeliveryRating
        fields = [
            'rating', 'comment', 'on_time',
            'packaging_quality', 'communication',
            'would_recommend', 'issues'
        ]
        widgets = {
            'comment': forms.Textarea(attrs={'rows': 3}),
            'issues': forms.Textarea(attrs={'rows': 2}),
        }
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Add Bootstrap classes
        for field_name, field in self.fields.items():
            if field_name not in ['on_time', 'would_recommend']:
                field.widget.attrs.update({'class': 'form-control'})
    
    def clean_rating(self):
        rating = self.cleaned_data.get('rating')
        if rating and (rating < 1 or rating > 5):
            raise forms.ValidationError("Rating must be between 1 and 5.")
        return rating


class DeliveryTimeSlotForm(forms.ModelForm):
    """Form for delivery time slot management"""
    
    class Meta:
        model = DeliveryTimeSlot
        fields = [
            'delivery_service', 'day_of_week',
            'start_time', 'end_time', 'max_orders',
            'is_active'
        ]
        widgets = {
            'start_time': forms.TimeInput(attrs={'type': 'time'}),
            'end_time': forms.TimeInput(attrs={'type': 'time'}),
        }
    
    def clean(self):
        cleaned_data = super().clean()
        start_time = cleaned_data.get('start_time')
        end_time = cleaned_data.get('end_time')
        
        if start_time and end_time and start_time >= end_time:
            self.add_error('end_time', 'End time must be after start time.')
        
        return cleaned_data


class DeliveryPricingRuleForm(forms.ModelForm):
    """Form for delivery pricing rules"""
    
    class Meta:
        model = DeliveryPricingRule
        fields = [
            'name', 'rule_type', 'condition',
            'base_price', 'price_modifier',
            'is_active', 'priority', 'applies_to'
        ]
        widgets = {
            'condition': forms.Textarea(attrs={'rows': 3}),
        }
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        # Add Bootstrap classes
        for field_name, field in self.fields.items():
            if field_name not in ['is_active']:
                field.widget.attrs.update({'class': 'form-control'})
    
    def clean_condition(self):
        condition = self.cleaned_data.get('condition')
        if condition:
            try:
                import json
                # Try to parse JSON
                parsed = json.loads(condition)
                if not isinstance(parsed, dict):
                    raise forms.ValidationError("Condition must be a valid JSON object.")
            except json.JSONDecodeError:
                raise forms.ValidationError("Invalid JSON format for condition.")
        return condition


class DeliveryPackageTypeForm(forms.ModelForm):
    """Form for package type management"""
    
    class Meta:
        model = DeliveryPackageType
        fields = [
            'name', 'description', 'base_price',
            'max_weight', 'max_length', 'max_width',
            'max_height', 'icon', 'is_active'
        ]
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
        }
    
    def clean_max_weight(self):
        weight = self.cleaned_data.get('max_weight')
        if weight and weight <= 0:
            raise forms.ValidationError("Maximum weight must be greater than 0.")
        return weight
    
    def clean_base_price(self):
        price = self.cleaned_data.get('base_price')
        if price and price < 0:
            raise forms.ValidationError("Base price cannot be negative.")
        return price


class BulkDeliveryForm(forms.Form):
    """Form for bulk delivery creation"""
    file = forms.FileField(
        label='CSV File',
        help_text='Upload a CSV file with delivery details'
    )
    
    def clean_file(self):
        file = self.cleaned_data.get('file')
        if file:
            # Check file extension
            if not file.name.endswith('.csv'):
                raise forms.ValidationError("File must be a CSV file.")
            
            # Check file size (max 5MB)
            if file.size > 5 * 1024 * 1024:
                raise forms.ValidationError("File size must be less than 5MB.")
        
        return file


class DriverStatusForm(forms.Form):
    """Form for driver status update"""
    STATUS_CHOICES = [
        ('available', 'Available'),
        ('busy', 'Busy'),
        ('offline', 'Offline'),
        ('on_break', 'On Break'),
    ]
    
    status = forms.ChoiceField(
        choices=STATUS_CHOICES,
        widget=forms.Select(attrs={'class': 'form-control'})
    )
    location_lat = forms.FloatField(
        required=False,
        widget=forms.HiddenInput()
    )
    location_lng = forms.FloatField(
        required=False,
        widget=forms.HiddenInput()
    )


class DeliveryFilterForm(forms.Form):
    """Form for filtering deliveries"""
    status = forms.ChoiceField(
        choices=[('', 'All Statuses')] + list(DeliveryRequest.STATUS_CHOICES),
        required=False,
        widget=forms.Select(attrs={'class': 'form-control'})
    )
    date_from = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'})
    )
    date_to = forms.DateField(
        required=False,
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'})
    )
    search = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Search...'})
    )
