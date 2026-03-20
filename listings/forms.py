# listings/forms.py
from django import forms
from django.db.models import Q
from django.utils import timezone
from .models import Listing, Category, Review, Payment
from . import ai_listing_helper


def _get_video_duration_seconds(uploaded_file):
    try:
        import json
        import os
        import tempfile
        import subprocess
        import shutil

        ffprobe = shutil.which('ffprobe')
        if not ffprobe:
            return None

        temp_path = None
        if hasattr(uploaded_file, 'temporary_file_path'):
            path = uploaded_file.temporary_file_path()
        else:
            suffix = os.path.splitext(getattr(uploaded_file, 'name', '') or '')[1]
            fd, temp_path = tempfile.mkstemp(suffix=suffix)
            with os.fdopen(fd, 'wb') as tmp:
                for chunk in uploaded_file.chunks():
                    tmp.write(chunk)
            path = temp_path

        result = subprocess.run(
            [ffprobe, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'format=duration', '-of', 'json', path],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or '{}')
        duration = data.get('format', {}).get('duration')
        if duration is None:
            return None
        return float(duration)
    except Exception:
        return None
    finally:
        try:
            if 'temp_path' in locals() and temp_path:
                os.remove(temp_path)
        except Exception:
            pass
        # Rewind in-memory uploads so later save/upload code still sees the full file.
        try:
            if hasattr(uploaded_file, 'seek'):
                uploaded_file.seek(0)
        except Exception:
            pass

class MultiFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultiFileField(forms.FileField):
    """FileField variant that accepts the list returned by a multiple file input."""
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('widget', MultiFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_file_clean = super().clean
        if isinstance(data, (list, tuple)):
            return [single_file_clean(item, initial) for item in data]
        return single_file_clean(data, initial)


class ListingForm(forms.ModelForm):
    video_descriptions = MultiFileField(
        required=False,
        widget=MultiFileInput(attrs={
            'accept': 'video/*',
            'multiple': True
        }),
        help_text='Optional short video descriptions (up to 3, max 45s, 15MB each).'
    )

    class Meta:
        model = Listing
        fields = ['title', 'description', 'price', 'category', 'store', 'location', 
                 'image', 'condition', 'delivery_option', 'stock', 'brand', 
                 'model', 'dimensions', 'weight', 'color', 'material', 
                 'meta_description']
        widgets = {
            'title': forms.TextInput(attrs={'placeholder': 'Enter a catchy title for your item', 'class': 'form-control'}),
            'price': forms.NumberInput(attrs={'min': '0', 'step': '0.01', 'placeholder': '0.00', 'class': 'form-control'}),
            'stock': forms.NumberInput(attrs={'min': '1', 'step': '1', 'placeholder': '1', 'class': 'form-control'}),
            'description': forms.Textarea(attrs={'rows': 4, 'placeholder': 'Describe your item in detail...', 'class': 'form-control'}),
            'category': forms.Select(attrs={'class': 'form-select form-control'}),
            'location': forms.HiddenInput(),
            'condition': forms.Select(attrs={'class': 'form-select form-control'}),
            'delivery_option': forms.Select(attrs={'class': 'form-select form-control'}),
            'brand': forms.TextInput(attrs={'placeholder': 'e.g., Samsung, Nike, Apple, etc.', 'class': 'form-control'}),
            'model': forms.TextInput(attrs={'placeholder': 'Model name/number', 'class': 'form-control'}),
            'dimensions': forms.TextInput(attrs={'placeholder': 'e.g., 10x5x3 inches or 30x20x15 cm', 'class': 'form-control'}),
            'weight': forms.TextInput(attrs={'placeholder': 'e.g., 0.5 kg or 150g', 'class': 'form-control'}),
            'color': forms.TextInput(attrs={'placeholder': 'e.g., Black, White, Blue, Red', 'class': 'form-control'}),
            'material': forms.TextInput(attrs={'placeholder': 'e.g., Metal, Wood, Cotton, Plastic', 'class': 'form-control'}),
            'meta_description': forms.Textarea(attrs={'rows': 2, 'placeholder': 'SEO description (auto-generated if empty)', 'maxlength': '160', 'class': 'form-control'}),
        }
    
    # Update the __init__ method of ListingForm class
    def __init__(self, *args, **kwargs):
        # Accept an optional 'user' kwarg to limit the store choices
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)

        # Lazy-import Store to avoid circular imports
        try:
            from storefront.models import Store
        except Exception:
            Store = None

        if Store:
            # Get user's stores
            user_stores_qs = Store.objects.none()
            if user and user.is_authenticated:
                user_stores_qs = Store.objects.filter(owner=user)

            # For updates, include the current store even if user no longer owns it
            if self.instance and self.instance.pk and self.instance.store:
                # Add current store to queryset if it's not already there
                if self.instance.store not in user_stores_qs:
                    user_stores_qs = user_stores_qs | Store.objects.filter(id=self.instance.store.id)

            # Determine if the field is required
            # For new listings (no pk), store is REQUIRED
            # For updates (has pk), store is optional (can keep existing)
            is_required = not self.instance.pk  # Required for new, optional for updates
            
            # Only create the field if we have stores or it's an update
            if user_stores_qs.exists() or (self.instance and self.instance.pk):
                self.fields['store'] = forms.ModelChoiceField(
                    queryset=user_stores_qs,
                    required=is_required,
                    label='Store',
                    help_text='Select which store/business this listing belongs to' + (' (required)' if is_required else ' (optional - leave blank to keep current store)')
                )
                
                # Set initial value for updates
                if self.instance and self.instance.pk and self.instance.store:
                    self.initial['store'] = self.instance.store
            else:
                # No stores available - hide the field
                if 'store' in self.fields:
                    del self.fields['store']

        # Location is derived from store and should not be edited manually
        if 'location' in self.fields:
            self.fields['location'].required = False
            self.fields['location'].help_text = 'Location is based on the selected store.'
            # If instance/store exists, try to prefill from store
            try:
                store = self.instance.store if self.instance and self.instance.pk else None
                if store and store.location:
                    inferred = None
                    try:
                        inferred = Listing(store=store)._infer_location_from_store()
                    except Exception:
                        inferred = None
                    if inferred:
                        self.initial['location'] = inferred
            except Exception:
                pass

    def clean_store(self):
        """Additional validation for store field"""
        store = self.cleaned_data.get('store')
        
        # For new listings, store is required
        if not self.instance.pk and not store:
            raise forms.ValidationError("Please select a store for your listing.")
        
        # For updates, if no store selected, keep the current one
        if self.instance.pk and not store:
            store = self.instance.store
        
        return store

    # Update the clean_image method to be more permissive for updates
    def clean_image(self):
        image = self.cleaned_data.get('image')
        
        # For updates, allow empty image (keep existing one)
        # Only require image for new listings
        if not image and not self.instance.pk:
            raise forms.ValidationError("Main image is required for new listings.")
        
        if image:
            # Cloudinary handles file validation, but you can add custom validation
            if hasattr(image, 'size') and image.size > 10 * 1024 * 1024:  # 10MB limit
                raise forms.ValidationError("Image file too large ( > 10MB )")
        
        return image
    
    def save(self, commit=True):
        listing = super().save(commit=False)
        
        # Set is_featured automatically based on store's subscription
        if listing.store:
            listing.is_featured = self._get_featured_status(listing.store)
        
        # dynamic_fields may be set in cleaned_data by clean(); ensure it is assigned
        try:
            if 'dynamic_fields' in getattr(self, 'cleaned_data', {}):
                listing.dynamic_fields = self.cleaned_data.get('dynamic_fields') or {}
        except Exception:
            pass

        if commit:
            listing.save()
        
        return listing

    def clean(self):
        cleaned_data = super().clean()
        category = cleaned_data.get('category')
        valid_choices = {c[0] for c in Listing.HOMABAY_LOCATIONS}

        # Force listing location to store location (store-owned listings only)
        try:
            store = cleaned_data.get('store')
            if not store and self.instance and self.instance.pk:
                store = getattr(self.instance, 'store', None)
            submitted_location = cleaned_data.get('location')
            existing_location = getattr(self.instance, 'location', None) if self.instance and self.instance.pk else None
            if store and getattr(store, 'location', None):
                inferred = None
                try:
                    inferred = Listing(store=store)._infer_location_from_store()
                except Exception:
                    inferred = None
                if not inferred:
                    # Try a best-effort match against choices by label/value
                    store_text = str(store.location or '').lower()
                    for code, label in Listing.HOMABAY_LOCATIONS:
                        if store_text == code.lower():
                            inferred = code
                            break
                        label_l = str(label).lower()
                        if label_l in store_text or store_text in label_l:
                            inferred = code
                            break
                        tokens = [t for t in label_l.replace('_', ' ').replace('-', ' ').split() if len(t) > 3]
                        if any(t in store_text for t in tokens):
                            inferred = code
                            break
                if inferred:
                    cleaned_data['location'] = inferred
                elif submitted_location in valid_choices:
                    # Preserve a valid client-selected code when the store text is too granular to infer cleanly.
                    cleaned_data['location'] = submitted_location
                elif existing_location in valid_choices:
                    # Backward-compatible path for old listings whose store text no longer maps cleanly.
                    cleaned_data['location'] = existing_location
                else:
                    self.add_error('location', 'Store location must match a known listing location. Update the store location to proceed.')
            elif store and not getattr(store, 'location', None):
                if submitted_location in valid_choices:
                    cleaned_data['location'] = submitted_location
                elif existing_location in valid_choices:
                    cleaned_data['location'] = existing_location
                else:
                    self.add_error('location', 'Store location is missing. Update the store location to proceed.')
        except Exception:
            pass

        # Get JSON payload from POST (hidden input name 'dynamic_fields')
        raw = None
        try:
            raw = self.data.get('dynamic_fields') if hasattr(self, 'data') else None
        except Exception:
            raw = None

        import json
        dynamic_data = {}
        if raw:
            try:
                dynamic_data = json.loads(raw)
            except Exception:
                raise forms.ValidationError('Invalid dynamic fields data.')

        # Validate against category schema if provided (with group fallback)
        schema = {}
        if category:
            schema = getattr(category, 'fields_schema', None) or {}
            if (not schema or schema == {}) and getattr(category, 'schema_group', None):
                # look for another category in same group with a schema
                fallback = Category.objects.filter(schema_group=category.schema_group).exclude(fields_schema={}).first()
                if fallback and getattr(fallback, 'fields_schema', None):
                    schema = fallback.fields_schema or {}
        if schema:
            for field_def in schema.get('fields', []):
                fname = field_def.get('name')
                required = field_def.get('required', False)
                ftype = field_def.get('type', 'text')
                label = field_def.get('label', fname)
                value = dynamic_data.get(fname)

                if required and (value is None or value == ''):
                    self.add_error(None, f"{label} is required.")
                    continue

                if value is not None and value != '':
                    # Type checks
                    if ftype == 'number':
                        try:
                            # allow numeric strings
                            float(value)
                        except Exception:
                            self.add_error(None, f"{label} must be a number.")
                    if ftype == 'select' and 'choices' in field_def:
                        if value not in field_def.get('choices', []):
                            self.add_error(None, f"{label} is an invalid choice.")

        cleaned_data['dynamic_fields'] = dynamic_data
        return cleaned_data

    def clean_video_descriptions(self):
        videos = self.files.getlist('video_descriptions')
        if not videos:
            return []
        if len(videos) > 3:
            raise forms.ValidationError("You can upload up to 3 video descriptions.")
        for video in videos:
            content_type = getattr(video, 'content_type', '') or ''
            if not content_type.startswith('video/'):
                raise forms.ValidationError("Only video files are allowed for video descriptions.")
            if getattr(video, 'size', 0) > 15 * 1024 * 1024:
                raise forms.ValidationError("Each video must be 15MB or smaller.")
            duration = _get_video_duration_seconds(video)
            if duration is not None and duration > 45:
                raise forms.ValidationError("Each video must be 45 seconds or shorter.")
        return videos
    
    def _get_featured_status(self, store):
        """Determine if listing should be featured based on store's active subscription"""
        from storefront.models import Subscription
        from django.utils import timezone
        from django.db.models import Q
        
        # Check for active premium or enterprise subscription
        active_premium_subscription = Subscription.objects.filter(
            store=store,
            plan__in=['premium', 'enterprise']
        ).filter(
            Q(status='active') | Q(status='trialing', trial_ends_at__gt=timezone.now())
        ).exists()
        
        return active_premium_subscription
        
class CheckoutForm(forms.Form):
    shipping_address = forms.CharField(
        max_length=200,
        widget=forms.Textarea(attrs={'rows': 3}),
        help_text="Where should we deliver your items?"
    )
    shipping_latitude = forms.DecimalField(required=False, widget=forms.HiddenInput())
    shipping_longitude = forms.DecimalField(required=False, widget=forms.HiddenInput())
    shipping_place_id = forms.CharField(required=False, widget=forms.HiddenInput())
    phone_number = forms.CharField(
        max_length=15,
        help_text="Your phone number for delivery updates"
    )
    use_alternate_shipping = forms.BooleanField(required=False, initial=False)
    
    first_name = forms.CharField(max_length=30,
        help_text="Your first name"

    )
    last_name = forms.CharField(max_length=30,
        help_text="Your last name"
    )
    email = forms.EmailField(
        help_text="Your email address"
    )
    city = forms.CharField(max_length=50, required=False,
        help_text="City (optional)"
    )
    postal_code = forms.CharField(max_length=20, required=False,
        help_text="Postal code (optional)"
    )

    # Optionally accept a payment method (kept for compatibility)
    payment_method = forms.CharField(required=False, max_length=20)

# listings/forms.py (add these forms)

class ReviewForm(forms.ModelForm):
    class Meta:
        model = Review
        fields = ['rating', 'comment', 'communication_rating', 'delivery_rating', 'accuracy_rating']
        widgets = {
            'comment': forms.Textarea(attrs={'rows': 4, 'placeholder': 'Share your experience...'}),
        }
    
    def __init__(self, *args, **kwargs):
        review_type = kwargs.pop('review_type', 'listing')
        super().__init__(*args, **kwargs)
        
        # Customize form based on review type
        if review_type == 'seller':
            self.fields['communication_rating'].required = True
            self.fields['delivery_rating'].required = True
        elif review_type == 'order':
            self.fields['communication_rating'].required = True
            self.fields['delivery_rating'].required = True
            self.fields['accuracy_rating'].required = True


class OrderReviewForm(forms.ModelForm):
    """Special form for reviewing an entire order"""
    class Meta:
        model = Review
        fields = ['rating', 'comment', 'communication_rating', 'delivery_rating', 'accuracy_rating']
        widgets = {
            'comment': forms.Textarea(attrs={'rows': 5, 'placeholder': 'Share your overall experience with this order...'}),
        }
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Make all detailed ratings required for order reviews
        for field in ['communication_rating', 'delivery_rating', 'accuracy_rating']:
            self.fields[field].required = True
            self.fields[field].widget.attrs.update({'class': 'detailed-rating'})


# Create a custom widget in forms.py

class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True

class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_file_clean = super().clean
        if isinstance(data, (list, tuple)):
            result = [single_file_clean(d, initial) for d in data]
        else:
            result = single_file_clean(data, initial)
        return result


class ReviewPhotoForm(forms.Form):
    photos = MultipleFileField(
        required=False,
        label="Upload photos (optional)",
        help_text="Upload up to 5 images (max 10MB each)"
    )
    
    def clean_photos(self):
        photos = self.cleaned_data.get('photos')
        if photos:
            if not isinstance(photos, list):
                photos = [photos]
            
            if len(photos) > 5:
                raise forms.ValidationError("You can upload up to 5 photos.")
            
            for photo in photos:
                if not photo.content_type.startswith('image/'):
                    raise forms.ValidationError("Only image files are allowed.")
                if photo.size > 10 * 1024 * 1024:  # 10MB
                    raise forms.ValidationError(f"Image {photo.name} is too large (max 10MB).")
        return photos
    
class AIListingForm(ListingForm):
    """AI-assisted listing form that can auto-fill missing fields."""
    use_ai = forms.BooleanField(
        required=False,
        initial=False,
        label='Use AI to auto-fill missing fields',
        help_text='Let AI help complete your listing based on the information you provide'
    )
    
    class Meta:
        model = Listing
        fields = ['title', 'description', 'price', 'category', 'store', 'location', 
                 'image', 'condition', 'delivery_option', 'stock', 'brand', 
                 'model', 'dimensions', 'weight', 'color', 'material', 'meta_description']
        widgets = {
            'title': forms.TextInput(attrs={
                'placeholder': 'Enter a catchy title for your item',
                'class': 'form-control ai-suggestable'
            }),
            'description': forms.Textarea(attrs={
                'rows': 6, 
                'placeholder': 'Describe your item in detail...',
                'class': 'form-control ai-suggestable'
            }),
            'price': forms.NumberInput(attrs={'min': '0', 'step': '0.01', 'placeholder': '0.00', 'class': 'form-control'}),
            'stock': forms.NumberInput(attrs={'min': '1', 'step': '1', 'placeholder': '1', 'class': 'form-control'}),
            'category': forms.Select(attrs={'class': 'form-select form-control ai-suggestable'}),
            'location': forms.HiddenInput(),
            'condition': forms.Select(attrs={'class': 'form-select form-control ai-suggestable'}),
            'delivery_option': forms.Select(attrs={'class': 'form-select form-control ai-suggestable'}),
            'brand': forms.TextInput(attrs={'placeholder': 'e.g., Samsung, Nike, Apple, etc.', 'class': 'form-control'}),
            'model': forms.TextInput(attrs={'placeholder': 'Model name/number', 'class': 'form-control'}),
            'dimensions': forms.TextInput(attrs={'placeholder': 'e.g., 10x5x3 inches or 30x20x15 cm', 'class': 'form-control'}),
            'weight': forms.TextInput(attrs={'placeholder': 'e.g., 0.5 kg or 150g', 'class': 'form-control'}),
            'color': forms.TextInput(attrs={'placeholder': 'e.g., Black, White, Blue, Red', 'class': 'form-control'}),
            'material': forms.TextInput(attrs={'placeholder': 'e.g., Metal, Wood, Cotton, Plastic', 'class': 'form-control'}),
            'meta_description': forms.Textarea(attrs={
                'rows': 2,
                'placeholder': 'SEO description (auto-generated if empty)',
                'maxlength': '160',
                'class': 'form-control'
            }),
        }
    
    def __init__(self, *args, **kwargs):
        user = kwargs.pop('user', None)
        super().__init__(*args, **kwargs)
          
    def generate_with_ai(self):
        """Generate missing fields using AI."""
        from .ai_listing_helper import listing_ai
        from django.http import QueryDict
        
        user_input = {
            'title': self.data.get('title', ''),
            'description': self.data.get('description', ''),
            'category': self.data.get('category', ''),
            'condition': self.data.get('condition', ''),
            'price': self.data.get('price', ''),
            'brand': self.data.get('brand', ''),
            'model': self.data.get('model', ''),
            'dimensions': self.data.get('dimensions', ''),
            'weight': self.data.get('weight', ''),
            'color': self.data.get('color', ''),
            'material': self.data.get('material', ''),
            'delivery_option': self.data.get('delivery_option', ''),
            'location': self.data.get('location', ''),
            'meta_description': self.data.get('meta_description', ''),
        }
        
        ai_data = listing_ai.generate_listing_data(user_input)

        # Ensure self.data is mutable (QueryDict from request.POST is immutable by default)
        if hasattr(self, 'data'):
            try:
                # QueryDict.copy() returns a mutable QueryDict
                if isinstance(self.data, QueryDict):
                    self.data = self.data.copy()
                else:
                    # If it's a different mapping, try to make a shallow copy
                    self.data = dict(self.data)
            except Exception:
                # If copying fails, fall back to a new dict
                try:
                    self.data = dict(self.data)
                except Exception:
                    self.data = {}

        # Update form data with AI suggestions (only when appropriate)
        for field, value in ai_data.items():
            if field in self.fields:
                current_value = ''
                try:
                    current_value = self.data.get(field, '')
                except Exception:
                    # data might be a plain dict now
                    current_value = self.data.get(field, '') if isinstance(self.data, dict) else ''

                # Always fill empty fields
                if not current_value or str(current_value).strip() == '':
                    # For QueryDict, assignment via [] works on the mutable copy
                    try:
                        self.data[field] = value
                    except Exception:
                        # fallback: set in cleaned_data or initial
                        self.initial[field] = value
                # For description and meta_description, use AI if user input is minimal
                elif field == 'description' and len(str(current_value)) < 50:
                    try:
                        self.data[field] = value
                    except Exception:
                        self.initial[field] = value
                elif field == 'meta_description' and len(str(current_value)) < 20:
                    try:
                        self.data[field] = value
                    except Exception:
                        self.initial[field] = value

        return ai_data
