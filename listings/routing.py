from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/cart/(?P<user_id>\d+)/$', consumers.CartConsumer.as_asgi()),
]
