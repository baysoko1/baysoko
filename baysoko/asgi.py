"""
ASGI config for baysoko project.

It exposes the ASGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.2/howto/deployment/asgi/
"""

import os

from django.core.asgi import get_asgi_application
 

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter


from django.urls import path
from users.consumers import AuthConsumer  # we'll create this


os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'baysoko.settings')

django_asgi_app = get_asgi_application()

# Import delivery websocket routing lazily
import delivery.routing
import storefront.routing

application = ProtocolTypeRouter({
	'http': django_asgi_app,
	'websocket': AuthMiddlewareStack(
		URLRouter(
			delivery.routing.websocket_urlpatterns
			+ storefront.routing.websocket_urlpatterns
			+ [
				path("ws/auth/", AuthConsumer.as_asgi()),
			]
		)
	),
})
