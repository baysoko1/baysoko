# baysoko/asgi.py
import os
from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'baysoko.settings')

# Initialize Django ASGI application early to ensure the AppRegistry
# is populated before importing code that may import ORM models.
django_asgi_app = get_asgi_application()

# Import routing modules after Django is ready
import delivery.routing
import storefront.routing
import chats.routing
import notifications.routing
import listings.routing
from users.consumers import AuthConsumer
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.auth import AuthMiddlewareStack
from django.urls import path

# Combine all WebSocket URL patterns
websocket_urlpatterns = (
    delivery.routing.websocket_urlpatterns
    + storefront.routing.websocket_urlpatterns
    + notifications.routing.websocket_urlpatterns
    + chats.routing.websocket_urlpatterns
    + listings.routing.websocket_urlpatterns
    + [path("ws/auth/", AuthConsumer.as_asgi())]
)

application = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AuthMiddlewareStack(
        URLRouter(websocket_urlpatterns)
    ),
})