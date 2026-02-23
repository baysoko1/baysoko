import os
from pathlib import Path
import sys
from decouple import config, Csv
import cloudinary
import cloudinary.uploader
import cloudinary.api
import dj_database_url
from decimal import Decimal
import redis


# Check if we're running migrations - if so, delay some settings
RUNNING_MIGRATE = 'migrate' in sys.argv

# Force Django version compatibility
import django
if django.VERSION < (4, 2):
    raise RuntimeError("Django 4.2 or higher required")

BASE_DIR = Path(__file__).resolve().parent.parent
# ================================================
# DATABASE CONFIGURATION - FIXED VERSION
# ================================================

# Default configuration - ALWAYS set this at module level
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# Handle PostgreSQL if DATABASE_URL exists
DATABASE_URL = os.environ.get('DATABASE_URL')

# Debug: Print what we're working with
print(f"🔍 DATABASE_URL found: {bool(DATABASE_URL)}")
if DATABASE_URL:
    print(f"🔍 DATABASE_URL length: {len(DATABASE_URL)}")
    print(f"🔍 DATABASE_URL first 50 chars: {DATABASE_URL[:50]}...")

if DATABASE_URL and DATABASE_URL.strip():
    try:
        # Parse the DATABASE_URL
        import dj_database_url
        db_config = dj_database_url.parse(DATABASE_URL, conn_max_age=600)
        
        # CRITICAL: Make sure ENGINE is explicitly set
        db_config['ENGINE'] = 'django.db.backends.postgresql'
        
        # Remove any problematic SSL options
        if 'OPTIONS' in db_config:
            db_config['OPTIONS'].pop('sslmode', None)
            db_config['OPTIONS'].pop('ssl', None)
        
        # Set the DATABASES dict
        DATABASES = {
            'default': db_config
        }
        
        print(f"✅ DATABASES['default']['ENGINE']: {DATABASES['default'].get('ENGINE')}")
        print(f"✅ Using PostgreSQL: {db_config.get('NAME', 'Unknown')}")
        print(f"✅ Host: {db_config.get('HOST', 'Unknown')}")
        
    except Exception as e:
        print(f"⚠️  Error parsing DATABASE_URL: {e}")
        print("⚠️  Using SQLite as fallback")
        DATABASES = {
            'default': {
                'ENGINE': 'django.db.backends.sqlite3',
                'NAME': BASE_DIR / 'db.sqlite3',
            }
        }
else:
    print("⚠️  No DATABASE_URL found, using SQLite")

# ================================================
# DEBUG: Verify DATABASES is properly configured
# ================================================
print("🔍 DEBUG: Checking DATABASES configuration...")
print(f"🔍 DATABASES type: {type(DATABASES)}")
print(f"🔍 DATABASES keys: {DATABASES.keys() if DATABASES else 'No DATABASES'}")
print(f"🔍 DATABASES['default'] type: {type(DATABASES.get('default'))}")
print(f"🔍 DATABASES['default'] keys: {DATABASES.get('default', {}).keys()}")
print(f"🔍 DATABASES['default']['ENGINE']: {DATABASES.get('default', {}).get('ENGINE', 'NOT SET')}")

# Force verification: ensure a valid DATABASES dict with ENGINE is present
if 'default' in DATABASES and DATABASES.get('default', {}).get('ENGINE'):
    print(f"✅ DATABASE ENGINE CONFIRMED: {DATABASES['default']['ENGINE']}")
else:
    print("❌ DATABASE ENGINE NOT FOUND or invalid. Falling back to SQLite for safety.")
    # Safe fallback to local SQLite to allow manage.py commands to run
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
        }
    }
    print("⚠️  Using SQLite as emergency fallback database for local operations")
# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = config('SECRET_KEY', default='django-insecure-default-key-for-dev')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = config('DEBUG', default=False, cast=bool)

ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1', cast=Csv())

# Normalize ALLOWED_HOSTS to a safe default if empty or misconfigured
try:
    # If Csv returned an empty list or list with empty string, replace with sensible defaults
    if not ALLOWED_HOSTS or (isinstance(ALLOWED_HOSTS, (list, tuple)) and all((not h) for h in ALLOWED_HOSTS)):
        ALLOWED_HOSTS = ['localhost', '127.0.0.1']
    # If a single string slipped through, wrap it
    if isinstance(ALLOWED_HOSTS, str):
        ALLOWED_HOSTS = [h.strip() for h in ALLOWED_HOSTS.split(',') if h.strip()]
        if not ALLOWED_HOSTS:
            ALLOWED_HOSTS = ['localhost', '127.0.0.1']
except Exception:
    ALLOWED_HOSTS = ['localhost', '127.0.0.1']

# Add Render external hostname
RENDER_EXTERNAL_HOSTNAME = os.environ.get('RENDER_EXTERNAL_HOSTNAME')
if RENDER_EXTERNAL_HOSTNAME:
    ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)
    ALLOWED_HOSTS.append('bay-soko.onrender.com')

# Cloudinary configuration - prefer python-decouple (reads .env) but allow CLOUDINARY_URL
# Use config() so values from `.env` are picked up in development when not exported to the shell
CLOUDINARY_CLOUD_NAME = config('CLOUDINARY_CLOUD_NAME', default='')
CLOUDINARY_API_KEY = config('CLOUDINARY_API_KEY', default='')
CLOUDINARY_API_SECRET = config('CLOUDINARY_API_SECRET', default='')
# Optional: allow full CLOUDINARY_URL (cloudinary://key:secret@name)
CLOUDINARY_URL = os.environ.get('CLOUDINARY_URL', '')

# Only configure Cloudinary if credentials are provided
if CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET:
    CLOUDINARY_STORAGE = {
        'CLOUD_NAME': CLOUDINARY_CLOUD_NAME,
        'API_KEY': CLOUDINARY_API_KEY,
        'API_SECRET': CLOUDINARY_API_SECRET,
    }
    # Also provide lowercase keys for compatibility with some versions/libraries
    CLOUDINARY_STORAGE.update({
        'cloud_name': CLOUDINARY_CLOUD_NAME,
        'api_key': CLOUDINARY_API_KEY,
        'api_secret': CLOUDINARY_API_SECRET,
    })
    DEFAULT_FILE_STORAGE = 'cloudinary_storage.storage.MediaCloudinaryStorage'
    
    # Configure Cloudinary SDK
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
        secure=True  # force HTTPS for all generated URLs
    )
    print("✅ Cloudinary configured successfully (secure HTTPS enabled)")
else:
    # Fallback to local file storage
    DEFAULT_FILE_STORAGE = 'django.core.files.storage.FileSystemStorage'
    print("⚠️  Cloudinary not configured - using local file storage")

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sites',
    'django.contrib.humanize',
    'delivery.apps.DeliveryConfig',
    'channels',
    
    # Third-party apps
    'crispy_forms',
    'crispy_bootstrap5',
    'cloudinary',
    'cloudinary_storage',
    'django_extensions',
    'rest_framework',
    'django_celery_beat',
    'django_celery_results',
    'django_redis',
    
    # Allauth apps
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    
    # Social providers
    'allauth.socialaccount.providers.google',
    'allauth.socialaccount.providers.facebook',
    
    # Local apps
    'users.apps.UsersConfig',
    'listings.apps.ListingsConfig',
    'chats.apps.ChatsConfig',
    'reviews.apps.ReviewsConfig',
    'blog.apps.BlogConfig',
    'notifications.apps.NotificationsConfig',
    'storefront.apps.StorefrontConfig',
]

REST_FRAMEWORK = {
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_RENDERER_CLASSES': [
        'rest_framework.renderers.JSONRenderer',
        'rest_framework.renderers.BrowsableAPIRenderer',
    ],
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
        'rest_framework.authentication.BasicAuthentication',
    ],
}

# Custom user model
AUTH_USER_MODEL = 'users.User'

# Crispy forms configuration
CRISPY_ALLOWED_TEMPLATE_PACKS = "bootstrap5"
CRISPY_TEMPLATE_PACK = "bootstrap5"

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'baysoko.csrf_middleware.CSRFRefererBypassMiddleware',  # Add before CSRF middleware
    'baysoko.middleware_async_stream.StreamingContentFixMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'baysoko.middleware.ClearCorruptedSessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'allauth.account.middleware.AccountMiddleware',
    'users.middleware.SocialAuthExceptionMiddleware',
    'users.middleware.EmailVerificationMiddleware',
    'delivery.middleware.SellerStoreMiddleware',
    'notifications.middleware.NotificationsMiddleware',
    'storefront.middleware.SubscriptionMiddleware',
    'storefront.middleware.StoreViewMiddleware',
    'chats.middleware.OnlineStatusMiddleware',
]

ROOT_URLCONF = 'baysoko.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [
            os.path.join(BASE_DIR, 'templates'),
            os.path.join(BASE_DIR, 'templates', 'account'),
            os.path.join(BASE_DIR, 'templates', 'socialaccount'),
        ],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'listings.context_processors.cart_item_count',
                'listings.context_processors.cart_context',
                'chats.context_processors.messages_context',
                'notifications.context_processors.notifications_context',
                'delivery.context_processors.delivery_user_context',
                'storefront.context_processors.store_context',
                'storefront.context_processors.subscription_context',
                'storefront.context_processors.bulk_operations_context',
                'storefront.context_processors.subscription_context',
                'blog.context_processors.blog_sidebar',
                'baysoko.context_processors.global_counts',
            ],
        },
    },
]

WSGI_APPLICATION = 'baysoko.wsgi.application'
# Database configuration - Robust version


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Nairobi'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

# WhiteNoise configuration
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# Media files
MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# Create media directory if it doesn't exist
os.makedirs(MEDIA_ROOT, exist_ok=True)

# Default primary key field type
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Channels (WebSocket) configuration - in-memory layer for development
ASGI_APPLICATION = 'baysoko.asgi.application'

# Login/Logout redirects
LOGIN_REDIRECT_URL = 'home'
LOGIN_URL = 'login'
LOGOUT_REDIRECT_URL = 'home'

# Security settings (configurable via environment). These default to
# secure values in production but remain permissive in development.
from django.core.exceptions import ImproperlyConfigured

# Handle SECRET_KEY for production
if not DEBUG:
    if SECRET_KEY.startswith('django-insecure') or len(SECRET_KEY) < 50:
        # Generate a strong key for production if not set
        from django.core.management.utils import get_random_secret_key
        SECRET_KEY = get_random_secret_key()
        print("⚠️  Generated strong SECRET_KEY for production")
        print(f"⚠️  Set a fixed SECRET_KEY environment variable for persistence")

# If running in development and SECRET_KEY is weak, generate a stable random one
# and persist it to a local file so it survives process reloads (avoids session corruption)
if DEBUG and (SECRET_KEY.startswith('django-insecure') or len(SECRET_KEY) < 50):
    SECRET_FILE = BASE_DIR / '.secret_key'
    try:
        if SECRET_FILE.exists():
            SECRET_KEY = SECRET_FILE.read_text().strip()
        else:
            from django.core.management.utils import get_random_secret_key
            new_key = get_random_secret_key()
            try:
                SECRET_FILE.write_text(new_key)
            except Exception:
                # If we can't write the file, still use the generated key in-memory
                pass
            SECRET_KEY = new_key
            print('⚠️  Generated and saved SECRET_KEY to .secret_key for development/testing')
    except Exception:
        try:
            from django.core.management.utils import get_random_secret_key
            SECRET_KEY = get_random_secret_key()
            print('⚠️  Using generated SECRET_KEY for development/testing (not persisted)')
        except Exception:
            pass

# HSTS settings
SECURE_HSTS_SECONDS = config('SECURE_HSTS_SECONDS', default=31536000, cast=int)
SECURE_HSTS_INCLUDE_SUBDOMAINS = config('SECURE_HSTS_INCLUDE_SUBDOMAINS', default=True, cast=bool)
SECURE_HSTS_PRELOAD = config('SECURE_HSTS_PRELOAD', default=True, cast=bool)

# Determine what command is being run
RUNNING_TESTS = len(sys.argv) > 1 and sys.argv[1] == 'test'
RUNNING_RUNSERVER = len(sys.argv) > 1 and sys.argv[1] in ('runserver', 'daphne')
RUNNING_MIGRATE = 'migrate' in sys.argv

# SSL / cookie settings (default to secure values; tests and localhost can
# still override them later)
SECURE_SSL_REDIRECT = config('SECURE_SSL_REDIRECT', default=True, cast=bool)
# Add this after the security settings (around line where SECURE_SSL_REDIRECT is defined)
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
# CSRF Cookie Configuration
# These settings control how Django's CSRF protection works
SESSION_COOKIE_SECURE = config('SESSION_COOKIE_SECURE', default=True, cast=bool)
CSRF_COOKIE_SECURE = config('CSRF_COOKIE_SECURE', default=True, cast=bool)
CSRF_COOKIE_HTTPONLY = False  # Must be False for JavaScript to access CSRF token in forms

# When running tests or local dev, disable secure cookie requirements
if RUNNING_TESTS or RUNNING_RUNSERVER or DEBUG:
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False

# IMPORTANT: Do NOT set CSRF_COOKIE_HTTPONLY = True if you need JavaScript to read the token
# In production HTTPS, the cookie is still protected by:
# 1. CSRF_COOKIE_SECURE (HTTPS only)
# 2. SameSite=Strict attribute
# 3. CSRF token validation on server

SECURE_BROWSER_XSS_FILTER = config('SECURE_BROWSER_XSS_FILTER', default=True, cast=bool)
SECURE_CONTENT_TYPE_NOSNIFF = config('SECURE_CONTENT_TYPE_NOSNIFF', default=True, cast=bool)

if RUNNING_TESTS:
    SECURE_SSL_REDIRECT = False
    # Ensure the Django test client host is allowed
    try:
        # ALLOWED_HOSTS may be a list from decouple Csv
        if isinstance(ALLOWED_HOSTS, (list, tuple)):
            if 'testserver' not in ALLOWED_HOSTS:
                ALLOWED_HOSTS.append('testserver')
        else:
            ALLOWED_HOSTS = list(ALLOWED_HOSTS) + ['testserver']
    except Exception:
        ALLOWED_HOSTS = ['testserver']

# Safety: if running on local hosts, ensure we do not redirect to HTTPS even if
# DEBUG is False in the environment. This prevents local webhook/testing clients
# from being redirected to HTTPS when the dev server isn't serving TLS.
try:
    hosts = ALLOWED_HOSTS if isinstance(ALLOWED_HOSTS, (list, tuple)) else [ALLOWED_HOSTS]
    # Only disable SSL redirect automatically for local development or tests
    if any(h in ('localhost', '127.0.0.1') for h in hosts) and (DEBUG or RUNNING_TESTS or RUNNING_RUNSERVER):
        SECURE_SSL_REDIRECT = False
except Exception:
    pass

# Ensure SSL redirect is enabled for deployment checks unless running tests or local dev
if RUNNING_TESTS or RUNNING_RUNSERVER:
    SECURE_SSL_REDIRECT = False
elif DEBUG:
    SECURE_SSL_REDIRECT = False
else:
    SECURE_SSL_REDIRECT = True

# Site ID
SITE_ID = 1
SITE_URL = config('SITE_URL', default='http://localhost:8000')

# CSRF and Cross-Origin Configuration
CSRF_TRUSTED_ORIGINS = [
    'http://localhost:8000',
    'http://127.0.0.1:8000',
    'http://127.0.0.1',
    'http://localhost',
    'https://bay-soko.onrender.com',
    'https://*.onrender.com',
]

# Add custom allowed origins from environment if specified
CUSTOM_ORIGINS = config('CSRF_TRUSTED_ORIGINS', default='', cast=str)
if CUSTOM_ORIGINS:
    CSRF_TRUSTED_ORIGINS.extend([origin.strip() for origin in CUSTOM_ORIGINS.split(',') if origin.strip()])

# CSRF Configuration for Form Submissions
# In production, Django validates both the CSRF token AND the Referer header
# In development, we bypass referer checks via CSRFRefererBypassMiddleware to avoid
# false-positive rejections when browsers don't send Referer headers
# The CSRF token itself is always validated




# OpenAI Configuration for AI Listing Assistant
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-3.5-turbo')

# Feature flag for AI listing assistant
AI_LISTING_ENABLED = bool(OPENAI_API_KEY)

# Delivery integration settings
# Controls whether Orders are automatically synchronized to DeliveryRequest
DELIVERY_AUTO_SYNC_ENABLED = config('DELIVERY_AUTO_SYNC_ENABLED', default=True, cast=bool)
# Controls whether Delivery status changes should update the originating Order
DELIVERY_UPDATE_ORDER_STATUS = config('DELIVERY_UPDATE_ORDER_STATUS', default=True, cast=bool)

# Authentication backends
AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
    
]

# Allauth settings (updated to new configuration keys to avoid deprecation warnings)
# Use ACCOUNT_LOGIN_METHODS to specify allowed login methods (order-independent)
ACCOUNT_LOGIN_METHODS = {'email', 'username'}

# Configure required signup fields using the new ACCOUNT_SIGNUP_FIELDS pattern.
# Use '*' suffix to indicate a required field in the new configuration.
ACCOUNT_SIGNUP_FIELDS = ['email*', 'username*', 'password1*', 'password2*']

# Keep email verification and uniqueness as configured
ACCOUNT_EMAIL_VERIFICATION = 'optional'
ACCOUNT_UNIQUE_EMAIL = True
# Deprecated settings removed: use ACCOUNT_LOGIN_METHODS and ACCOUNT_SIGNUP_FIELDS above.
# ACCOUNT_EMAIL_REQUIRED, ACCOUNT_AUTHENTICATION_METHOD, and ACCOUNT_USERNAME_REQUIRED
# have been replaced by the new Allauth configuration keys.

# Add this after the existing SOCIALACCOUNT_PROVIDERS configuration
# Update SOCIALACCOUNT_PROVIDERS with the improved structure
SOCIALACCOUNT_PROVIDERS = {
    'google': {
        'SCOPE': ['profile', 'email'],
        'AUTH_PARAMS': {'access_type': 'online'},
        'APP': {
            'client_id': os.environ.get('GOOGLE_OAUTH_CLIENT_ID', ''),
            'secret': os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET', ''),
            'key': ''
        }
    },
    'facebook': {
        'METHOD': 'oauth2',
        'SCOPE': ['email', 'public_profile'],
        'AUTH_PARAMS': {'auth_type': 'reauthenticate'},
        'INIT_PARAMS': {'cookie': True},
        'FIELDS': [
            'id',
            'first_name',
            'last_name',
            'middle_name',
            'name',
            'name_format',
            'picture',
            'short_name'
        ],
        'EXCHANGE_TOKEN': True,
        'VERIFIED_EMAIL': False,
        'VERSION': 'v13.0',
        'APP': {
            'client_id': os.environ.get('FACEBOOK_OAUTH_CLIENT_ID', ''),
            'secret': os.environ.get('FACEBOOK_OAUTH_CLIENT_SECRET', ''),
            'key': ''
        }
    }
}

# Update these settings for better OAuth experience
LOGIN_REDIRECT_URL = '/'
ACCOUNT_LOGOUT_REDIRECT_URL = '/'
SOCIALACCOUNT_LOGIN_ON_GET = True  # Auto-redirect for social login
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_EMAIL_VERIFICATION = 'optional'
SOCIALACCOUNT_EMAIL_REQUIRED = True
SOCIALACCOUNT_QUERY_EMAIL = True
SOCIALACCOUNT_STORE_TOKENS = True

# Custom adapter and forms (keep these as they are)
SOCIALACCOUNT_ADAPTER = 'users.adapters.CustomSocialAccountAdapter'
SOCIALACCOUNT_FORMS = {
    'signup': 'users.social_forms.CustomSocialSignupForm',
}
# Disable the problematic 3rdparty signup if it's causing issues
SOCIALACCOUNT_ENABLED = True

# Login redirects
LOGIN_REDIRECT_URL = 'home'
ACCOUNT_LOGOUT_REDIRECT_URL = 'home'
SOCIALACCOUNT_LOGIN_ON_GET = False  # Show intermediate page
ACCOUNT_LOGOUT_ON_GET = True  # Logout immediately on GET request
# Social Auth Environment Variables
GOOGLE_OAUTH_CLIENT_ID = os.environ.get('GOOGLE_OAUTH_CLIENT_ID', '')
GOOGLE_OAUTH_CLIENT_SECRET = os.environ.get('GOOGLE_OAUTH_CLIENT_SECRET', '')
FACEBOOK_OAUTH_CLIENT_ID = os.environ.get('FACEBOOK_OAUTH_CLIENT_ID', '')
FACEBOOK_OAUTH_CLIENT_SECRET = os.environ.get('FACEBOOK_OAUTH_CLIENT_SECRET', '')

SOCIALACCOUNT_PROVIDERS = {
    'google': {
        'SCOPE': [
            'profile',
            'email',
        ],
        'AUTH_PARAMS': {
            'access_type': 'online',
        },
        'OAUTH_PKCE_ENABLED': True,
    },
    'facebook': {
        'METHOD': 'oauth2',
        'SCOPE': ['email', 'public_profile'],
        'AUTH_PARAMS': {'auth_type': 'reauthenticate'},
        'INIT_PARAMS': {'cookie': True},
        'FIELDS': [
            'id',
            'first_name',
            'last_name',
            'email',
        ],
        'EXCHANGE_TOKEN': True,
        'VERIFIED_EMAIL': False,
        'VERSION': 'v13.0',
    }
}

# Custom adapter
SOCIALACCOUNT_ADAPTER = 'users.adapters.CustomSocialAccountAdapter'
SOCIALACCOUNT_FORMS = {
    'signup': 'users.social_forms.CustomSocialSignupForm',
}

# OpenAI Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-3.5-turbo')

# AI Listing Assistant flag
AI_LISTING_ENABLED = bool(OPENAI_API_KEY)

# Auto connect social accounts to existing users by email
SOCIALACCOUNT_AUTO_SIGNUP = True

AFRICASTALKING_USERNAME = os.environ.get('AFRICASTALKING_USERNAME', '')
AFRICASTALKING_API_KEY = os.environ.get('AFRICASTALKING_API_KEY', '')
SMS_ENABLED = os.environ.get('SMS_ENABLED', 'False').lower() == 'true'

# Delivery System Integration
# Delivery settings
DELIVERY_SYSTEM_ENABLED = config('DELIVERY_SYSTEM_ENABLED', default=True, cast=bool)
DELIVERY_SYSTEM_URL = config('DELIVERY_SYSTEM_URL', default='')
DELIVERY_SYSTEM_API_KEY = config('DELIVERY_SYSTEM_API_KEY', default='')
DELIVERY_WEBHOOK_KEY = config('DELIVERY_WEBHOOK_KEY', default='')
DELIVERY_APP_ORDER_URL = "{DELIVERY_SYSTEM_URL}/{order_id}/manage/"

# Default pickup information
DEFAULT_PICKUP_ADDRESS = config('DEFAULT_PICKUP_ADDRESS', default='Main Store, HomaBay')
DEFAULT_PICKUP_PHONE = config('DEFAULT_PICKUP_PHONE', default='+254700000000')
DEFAULT_PICKUP_EMAIL = config('DEFAULT_PICKUP_EMAIL', default='store@baysoko.com')

# Delivery fee settings
BASE_DELIVERY_FEE = Decimal('100.00')
MAX_PACKAGE_WEIGHT = 100  # kg
MAX_PACKAGE_VOLUME = 1000000  # cubic cm (1 cubic meter)

# Google Maps API for distance calculation
GOOGLE_MAPS_API_KEY = config('GOOGLE_MAPS_API_KEY', default='')

# Delivery notification settings
ENABLE_EMAIL_NOTIFICATIONS = config('ENABLE_EMAIL_NOTIFICATIONS', default=True, cast=bool)
ENABLE_SMS_NOTIFICATIONS = config('ENABLE_SMS_NOTIFICATIONS', default=False, cast=bool)
# E-commerce platform configuration
ECOMMERCE_PLATFORM_NAME = config('ECOMMERCE_PLATFORM_NAME', default='Baysoko')
ECOMMERCE_WEBHOOK_URL = config('ECOMMERCE_WEBHOOK_URL', default='http://localhost:8000/api/delivery/webhook/baysoko/')
# E-commerce platforms
ECOMMERCE_PLATFORMS = [
    {
        'name': 'Baysoko',
        'platform_type': 'baysoko',
        'base_url': 'http://localhost:8000',
        'api_key': '',
        'webhook_secret': DELIVERY_WEBHOOK_KEY,
        'sync_enabled': True,
    }
]

# Email configuration
EMAIL_BACKEND = config('EMAIL_BACKEND', default='django.core.mail.backends.console.EmailBackend' if DEBUG else 'django.core.mail.backends.smtp.EmailBackend')
EMAIL_HOST = config('EMAIL_HOST', default='smtp.gmail.com')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default='00peteromondi@gmail.com')

# Debug email configuration
print(f"📧 EMAIL_BACKEND: {EMAIL_BACKEND}")
print(f"📧 EMAIL_HOST: {EMAIL_HOST}")
print(f"📧 EMAIL_PORT: {EMAIL_PORT}")
print(f"📧 EMAIL_USE_TLS: {EMAIL_USE_TLS}")
print(f"📧 EMAIL_HOST_USER: {'SET' if EMAIL_HOST_USER else 'NOT SET'}")
print(f"📧 EMAIL_HOST_PASSWORD: {'SET' if EMAIL_HOST_PASSWORD else 'NOT SET'}")
print(f"📧 DEFAULT_FROM_EMAIL: {DEFAULT_FROM_EMAIL}")
print(f"📧 DEBUG: {DEBUG}")

# Password reset timeout in seconds
PASSWORD_RESET_TIMEOUT = 3600 if DEBUG else 86400

# Basic logging configuration for production
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
}

# Ensure logs directory exists
import os
os.makedirs('logs', exist_ok=True)
# Additional security settings
X_FRAME_OPTIONS = 'DENY'
SECURE_REFERRER_POLICY = 'same-origin'

# Custom settings
baysoko = {
    'SITE_NAME': 'Baysoko',
    'SITE_DESCRIPTION': 'Buy and sell with people in your Homabay community',
}

# Storefront configuration
STORE_FREE_LISTING_LIMIT = int(os.environ.get('STORE_FREE_LISTING_LIMIT', '5'))
# Maximum image upload size in megabytes
MAX_IMAGE_UPLOAD_SIZE_MB = int(os.environ.get('MAX_IMAGE_UPLOAD_SIZE_MB', '10'))
MAX_IMAGE_UPLOAD_SIZE = MAX_IMAGE_UPLOAD_SIZE_MB * 1024 * 1024



# M-Pesa settings - use python-decouple `config` so `.env` values are picked up
MPESA_CONSUMER_KEY = config('MPESA_CONSUMER_KEY', default='')
MPESA_CONSUMER_SECRET = config('MPESA_CONSUMER_SECRET', default='')
MPESA_BUSINESS_SHORTCODE = config('MPESA_BUSINESS_SHORTCODE', default='')
MPESA_PASSKEY = config('MPESA_PASSKEY', default='')
# Callback URL: prefer explicit env var, fallback to sandbox callback if not provided
MPESA_CALLBACK_URL = config('MPESA_CALLBACK_URL', default='')
# Read raw environment value then sanitize: strip inline comments and quotes
_mpesa_env_raw = config('MPESA_ENVIRONMENT', default='sandbox')
_mpesa_env = str(_mpesa_env_raw).split('#', 1)[0].strip().strip("'\"").lower()
if _mpesa_env not in ('sandbox', 'production'):
    import logging
    logging.warning(f"Invalid MPESA_ENVIRONMENT '{_mpesa_env_raw}', defaulting to 'sandbox'")
    MPESA_ENVIRONMENT = 'sandbox'
else:
    MPESA_ENVIRONMENT = _mpesa_env
# Allow toggling simulation via env (useful in CI or local dev)
MPESA_SIMULATE_PAYMENTS = config('MPESA_SIMULATE_PAYMENTS', default=False, cast=bool)

# How many remaining sellers (with unshipped items) should trigger reminder notifications
SELLER_SHIPMENT_REMINDER_THRESHOLD = int(os.environ.get('SELLER_SHIPMENT_REMINDER_THRESHOLD', '2'))

# ================================================
# FINAL VERIFICATION AND FALLBACK
# ================================================

# Final check: If DATABASES is still messed up, use hardcoded config
import sys

# Check if we're running migrations or any management command
if 'manage.py' in ' '.join(sys.argv) or 'migrate' in sys.argv:
    print(f"🔍 Running command: {' '.join(sys.argv)}")
    
    # Force database configuration for management commands
    if 'DATABASE_URL' in os.environ:
        DATABASE_URL = os.environ['DATABASE_URL']
        print(f"🔍 Re-verifying DATABASE_URL for command execution")
        
        # Emergency override if engine is wrong
        if DATABASES.get('default', {}).get('ENGINE') == 'django.db.backends.dummy':
            print("🚨 EMERGENCY: Dummy backend detected, forcing PostgreSQL")
            DATABASES = {
                'default': {
                    'ENGINE': 'django.db.backends.postgresql',
                    'NAME': 'baysoko2',
                    'USER': 'baysoko2_user',
                    'PASSWORD': 'Da8a4VMjdk7X0QOuJtBxtZs3Q4ym7VzG',
                    'HOST': 'dpg-d5gd8m7pm1nc73e44la0-a',
                    'PORT': '5432',
                }
            }

# Final debug output
print("=" * 50)
print("FINAL DATABASE CONFIGURATION:")
print(f"ENGINE: {DATABASES.get('default', {}).get('ENGINE', 'UNKNOWN')}")
print(f"NAME: {DATABASES.get('default', {}).get('NAME', 'UNKNOWN')}")
print(f"HOST: {DATABASES.get('default', {}).get('HOST', 'UNKNOWN')}")
print("=" * 50)

# settings.py additions


# Celery Configuration
CELERY_BROKER_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
CELERY_RESULT_BACKEND = 'django-db'
CELERY_CACHE_BACKEND = 'django-cache'
CELERY_TIMEZONE = 'Africa/Nairobi'
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60  # 30 minutes

# Redis for caching and channels
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/1')
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        }
    }
}

# Channels Configuration
# Prefer full Redis URL strings for channels_redis. If REDIS_URL is a proper
# redis:// URL use that directly; otherwise try to parse host/port as a fallback.
try:
    if REDIS_URL and isinstance(REDIS_URL, str) and REDIS_URL.startswith('redis://'):
        channels_hosts = [REDIS_URL]
    else:
        # attempt to parse host and port
        from urllib.parse import urlparse
        parsed = urlparse(REDIS_URL)
        host = parsed.hostname or 'localhost'
        port = parsed.port or 6379
        channels_hosts = [(host, port)]
except Exception:
    channels_hosts = ['redis://localhost:6379/1']

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer"
    },
}

# API Configuration
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
        'rest_framework.authentication.BasicAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
}

CRONJOBS = [
    ('0 0 * * *', 'storefront.management.commands.check_subscriptions'),  # Run daily at midnight
]

TRIAL_SETTINGS = {
    'TRIAL_LIMIT_PER_USER': 1,
    'TRIAL_DAYS': 7,
    'ENABLE_TRIAL_TRACKING': True,
    'AUTO_DISABLE_ON_EXPIRY': True,
    'ALLOW_TRIAL_EXTENSIONS': False,
    'MAX_TRIAL_EXTENSION_DAYS': 0,
    'TRIAL_CONVERSION_TARGET': 0.3,  # 30% conversion target
}
TRIAL_LIMIT_PER_USER = 1