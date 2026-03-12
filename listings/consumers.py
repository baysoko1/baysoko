from channels.generic.websocket import AsyncWebsocketConsumer
import json
from django.contrib.auth import get_user_model
from asgiref.sync import sync_to_async
from .models import Cart

User = get_user_model()

class CartConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get('user')
        # Accept connection only if user is authenticated and matches the URL param
        # user id is in self.scope['url_route']['kwargs']['user_id']
        try:
            url_user_id = int(self.scope['url_route']['kwargs'].get('user_id', 0))
        except Exception:
            url_user_id = 0
        if self.user.is_authenticated and self.user.id == url_user_id:
            await self.accept()
        else:
            # Reject unauthorized connections
            await self.close()

    async def receive(self, text_data=None, bytes_data=None):
        try:
            data = json.loads(text_data or '{}')
        except Exception:
            return
        action = data.get('action')
        if action == 'get_summary':
            summary = await sync_to_async(self._build_summary)()
            await self.send(text_data=json.dumps({'type': 'cart_summary', 'payload': summary}))

    def _build_summary(self):
        try:
            user = self.user
            cart, _ = Cart.objects.get_or_create(user=user)
            item_totals = {}
            for ci in cart.items.all():
                item_totals[str(ci.id)] = {
                    'item_total': float(ci.get_total_price()),
                    'quantity': ci.quantity,
                    'stock': ci.listing.stock if ci.listing and hasattr(ci.listing, 'stock') else 999
                }
            return {
                'cart_total': float(cart.get_total_price()),
                'cart_item_count': sum(int(item.quantity or 0) for item in cart.items.all()),
                'item_totals': item_totals,
            }
        except Exception:
            return {'cart_total': 0, 'cart_item_count': 0, 'item_totals': {}}

    async def disconnect(self, close_code):
        pass
