
import os
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'baysoko.settings')

# Initialize Django ASGI application early to ensure the AppRegistry
# is populated before importing code that may import ORM models.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from django.urls import path


def get_websocket_application():
    # Import consumers inside this function so models are imported after Django is ready
    from users.consumers import AuthConsumer
    return AuthConsumer.as_asgi()


application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AuthMiddlewareStack(
        URLRouter(
			delivery.routing.websocket_urlpatterns
			+ storefront.routing.websocket_urlpatterns
			+ [
				path("ws/auth/", AuthConsumer.as_asgi()),
			]
		)
    ),
})