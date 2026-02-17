from django.apps import AppConfig


class StorefrontConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'storefront'
    verbose_name = 'Storefront'

    def ready(self):
        # Import signals to ensure they are registered
        try:
            from . import signals  # noqa: F401
        except Exception:
            pass
