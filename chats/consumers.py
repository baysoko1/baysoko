import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.contrib.auth import get_user_model
from .models import Conversation, Message
import asyncio
from websockets.exceptions import ConnectionClosedOK, ConnectionClosedError
# asgiref.exceptions.TimeoutError is not always available; use asyncio.TimeoutError
from django.utils import timezone

User = get_user_model()
logger = logging.getLogger(__name__)

class ChatConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope["user"]
        if not self.user.is_authenticated:
            await self.close()
            return

        # Group for this specific user
        self.user_group_name = f"user_{self.user.id}"
        await self.channel_layer.group_add(self.user_group_name, self.channel_name)
        await self.accept()
        logger.info(f"WebSocket connected for user {self.user.id}")
        # Notify other participants that this user is now online
        try:
            participant_ids = await self.get_all_conversation_participants()
            for pid in participant_ids:
                if pid == self.user.id:
                    continue
                await self.channel_layer.group_send(
                    f'user_{pid}',
                    {
                        'type': 'presence_notification',
                        'user_id': self.user.id,
                        'user_name': self.user.get_full_name() or self.user.username,
                        'online': True,
                    }
                )
        except Exception:
            logger.exception('Failed to broadcast presence on connect')

    async def disconnect(self, close_code):
        if hasattr(self, 'user_group_name'):
            await self.channel_layer.group_discard(self.user_group_name, self.channel_name)
        # Notify others that this user went offline
        try:
            from django.utils import timezone
            last_seen = timezone.now().isoformat()
            participant_ids = await self.get_all_conversation_participants()
            for pid in participant_ids:
                if pid == self.user.id:
                    continue
                await self.channel_layer.group_send(
                    f'user_{pid}',
                    {
                        'type': 'presence_notification',
                        'user_id': self.user.id,
                        'online': False,
                        'last_seen': last_seen,
                    }
                )
        except Exception:
            logger.exception('Failed to broadcast presence on disconnect')

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
            event_type = data.get('type')
            if event_type == 'typing.start':
                await self.handle_typing_start(data)
            elif event_type == 'typing.stop':
                await self.handle_typing_stop(data)
            elif event_type == 'mark_read':
                await self.handle_mark_read(data)
            # Optionally handle sending messages via WebSocket
            # elif event_type == 'send_message':
            #     await self.handle_send_message(data)
        except Exception as e:
            logger.error(f"Error in WebSocket receive: {e}")

    async def handle_typing_start(self, data):
        conversation_id = data.get('conversation_id')
        if not conversation_id:
            return
        # Verify user is participant
        participant_ids = await self.get_conversation_participants(conversation_id)
        if self.user.id not in participant_ids:
            return
        # Send to other participants only
        for pid in participant_ids:
            if pid != self.user.id:
                await self.channel_layer.group_send(
                    f"user_{pid}",
                    {
                        'type': 'typing_notification',
                        'conversation_id': conversation_id,
                        'user_id': self.user.id,
                        'user_name': self.user.get_full_name() or self.user.username,
                        'typing': True,
                    }
                )

    async def handle_typing_stop(self, data):
        conversation_id = data.get('conversation_id')
        if not conversation_id:
            return
        participant_ids = await self.get_conversation_participants(conversation_id)
        if self.user.id not in participant_ids:
            return
        for pid in participant_ids:
            if pid != self.user.id:
                await self.channel_layer.group_send(
                    f"user_{pid}",
                    {
                        'type': 'typing_notification',
                        'conversation_id': conversation_id,
                        'user_id': self.user.id,
                        'typing': False,
                    }
                )

    async def handle_mark_read(self, data):
        conversation_id = data.get('conversation_id')
        message_ids = data.get('message_ids', [])
        if not conversation_id or not message_ids:
            return
        # Mark messages as read (update DB)
        await self.mark_messages_read(conversation_id, message_ids)
        # Notify sender(s) that messages have been read
        # For simplicity, we'll broadcast to other participants that specific messages were read
        participant_ids = await self.get_conversation_participants(conversation_id)
        for pid in participant_ids:
            if pid != self.user.id:
                await self.channel_layer.group_send(
                    f"user_{pid}",
                    {
                        'type': 'read_receipt',
                        'conversation_id': conversation_id,
                        'message_ids': message_ids,
                        'read_by': self.user.id,
                    }
                )
        # After marking read, update unread counts for all participants
        try:
            from .utils import broadcast_unread_async
            for pid in participant_ids:
                await broadcast_unread_async(pid)
        except Exception:
            logger.exception('Failed to broadcast unread counts after mark_read')

    # Handler for messages sent from server to client
    async def chat_message(self, event):
        """Send a new message to the WebSocket client."""
        await self.send(text_data=json.dumps({
            'type': 'new_message',
            'message': event['message']
        }))

    async def presence_notification(self, event):
        """Notify clients about another user's presence change."""
        payload = {
            'type': 'presence',
            'user_id': event.get('user_id'),
            'user_name': event.get('user_name'),
            'online': event.get('online', False),
        }
        if event.get('last_seen'):
            payload['last_seen'] = event.get('last_seen')
        await self.send(text_data=json.dumps(payload))

    async def typing_notification(self, event):
        """Send typing indicator update."""
        await self.send(text_data=json.dumps({
            'type': 'typing',
            'conversation_id': event['conversation_id'],
            'user_id': event['user_id'],
            'user_name': event.get('user_name'),
            'typing': event['typing']
        }))

    async def read_receipt(self, event):
        """Notify that messages have been read."""
        await self.send(text_data=json.dumps({
            'type': 'messages_read',
            'conversation_id': event['conversation_id'],
            'message_ids': event['message_ids'],
            'read_by': event['read_by']
        }))

    async def message_updated(self, event):
        """Notify that a message was edited or deleted."""
        await self.send(text_data=json.dumps({
            'type': 'message_update',
            'conversation_id': event['conversation_id'],
            'message_id': event['message_id'],
            'action': event['action'],      # 'edit', 'delete', 'pin'
            'data': event.get('data', {})
        }))

    # Database helpers
    @database_sync_to_async
    def get_conversation_participants(self, conversation_id):
        try:
            conv = Conversation.objects.get(id=conversation_id)
            return list(conv.participants.values_list('id', flat=True))
        except Conversation.DoesNotExist:
            return []

    @database_sync_to_async
    def get_all_conversation_participants(self):
        try:
            convs = Conversation.objects.filter(participants=self.user).prefetch_related('participants')
            ids = set()
            for c in convs:
                ids.update(list(c.participants.values_list('id', flat=True)))
            return list(ids)
        except Exception:
            return []

    @database_sync_to_async
    def mark_messages_read(self, conversation_id, message_ids):
        # Only mark messages not sent by current user
        Message.objects.filter(
            id__in=message_ids,
            conversation_id=conversation_id
        ).exclude(
            sender=self.user
        ).update(is_read=True, read_at=timezone.now())



class AgentConsumer(AsyncWebsocketConsumer):
    """A lightweight AI agent websocket consumer that accepts a title or prompt and returns generated fields.

    Expected incoming JSON: { "type": "generate", "title": "Product title here" }
    Sends back: { "type": "generate_response", "ok": True, "data": { ... } }
    """
    async def connect(self):
        self.user = self.scope.get('user')
        if not self.user or not self.user.is_authenticated:
            await self.close()
            return
        await self.accept()
        # If the user has never interacted with the assistant, send an initial greeting
        try:
            from .models import AgentChat
            from asgiref.sync import sync_to_async

            @database_sync_to_async
            def _ensure_greeting(user_id):
                try:
                    return AgentChat.objects.filter(user_id=user_id).exists()
                except Exception:
                    return False

            has_history = await _ensure_greeting(self.user.id)
            if not has_history:
                greeting = "Hello, Welcome to Baysoko. Call me Bay Bee. How may I assist you today?"
                # persist the greeting server-side for history
                @database_sync_to_async
                def _create_greeting(user_id, text):
                    try:
                        AgentChat.objects.create(user_id=user_id, role='assistant', content=text)
                        return True
                    except Exception:
                        return False

                await _create_greeting(self.user.id, greeting)
                # use safe_send to avoid raising when client disconnected
                await self.safe_send(json.dumps({'type': 'generate_response', 'ok': True, 'data': greeting}))
        except Exception:
            logger.exception('Failed to send initial assistant greeting')

    async def disconnect(self, close_code):
        try:
            await self.close()
        except Exception:
            pass

    async def safe_send(self, payload_text):
        """Send data to websocket but swallow client-disconnects and comparable transport errors.

        Using a small wrapper avoids unhandled exceptions when clients disconnect mid-send (see ConnectionClosedOK).
        """
        try:
            await self.send(text_data=payload_text)
        except (ConnectionClosedOK, ConnectionClosedError, asyncio.TimeoutError, asyncio.CancelledError) as exc:
            logger.debug('WebSocket send failed (likely client disconnected): %s', exc)
        except Exception as exc:
            # Catch-all to prevent consumer crash; log at debug to avoid noisy logs
            logger.debug('Unexpected error sending websocket message: %s', exc)

    async def receive(self, text_data=None, bytes_data=None):
        try:
            payload = json.loads(text_data or '{}')
            if payload.get('type') != 'generate':
                await self.send(text_data=json.dumps({'type': 'error', 'error': 'unsupported type'}))
                return
            title = payload.get('title') or payload.get('prompt')
            if not title:
                await self.send(text_data=json.dumps({'type': 'generate_response', 'ok': False, 'error': 'missing title'}))
                return

            # Perform generation in threadpool to avoid blocking event loop
            from asgiref.sync import sync_to_async

            # Accept optional conversation history for follow-up context
            history = payload.get('history')
            async_generate = sync_to_async(_generate_wrapper, thread_sensitive=False)
            mode = str(payload.get('mode') or '').strip().lower()
            request_id = payload.get('request_id')
            # If this looks like a question or conversational prompt, call assistant_reply
            is_question = False
            try:
                t = str(title).strip().lower() if isinstance(title, str) else ''
                assistant_starters = (
                    'how', 'what', 'why', 'where', 'when', 'help', 'assist', 'can', 'could', 'would',
                    'add', 'remove', 'show', 'find', 'track', 'list', 'tell', 'give', 'check', 'view'
                )
                assistant_keywords = (
                    'cart', 'order', 'orders', 'subscription', 'plan', 'store', 'listing', 'listings',
                    'inventory', 'stock', 'worth', 'price', 'expensive', 'cheap', 'new arrivals',
                    'favorites', 'favourites', 'recently viewed', 'checkout'
                )
                if mode == 'assistant':
                    is_question = True
                elif mode == 'listing_fields':
                    is_question = False
                elif isinstance(title, str):
                    if ('?' in t) or t.startswith(assistant_starters) or any(k in t for k in assistant_keywords):
                        is_question = True
            except Exception:
                is_question = False

            if is_question:
                # call assistant textual reply
                from asgiref.sync import sync_to_async
                async_assist = sync_to_async(_assistant_wrapper, thread_sensitive=False)
                text = await async_assist(title, history, getattr(self.user, 'id', None))
                await self.safe_send(json.dumps({'type': 'generate_response', 'ok': True, 'data': text, 'mode': mode or 'assistant', 'request_id': request_id}))
            else:
                result = await async_generate(title, history)
                await self.safe_send(json.dumps({'type': 'generate_response', 'ok': True, 'data': result, 'mode': mode or 'listing_fields', 'request_id': request_id}))
        except Exception as e:
            logger.exception('AgentConsumer error')
            try:
                await self.safe_send(json.dumps({'type': 'generate_response', 'ok': False, 'error': str(e)}))
            except Exception:
                logger.debug('Failed to send error response to agent websocket client')


def _generate_wrapper(title: str, history=None):
    """Sync wrapper to call the AI helper and return a plain dict."""
    try:
        from listings.ai_assistant import generate_listing_fields
        return generate_listing_fields(title, context=history)
    except Exception as e:
        return {'error': str(e)}


def _assistant_wrapper(prompt: str, history=None, user_id=None):
    try:
        from listings.ai_assistant import assistant_reply
        return assistant_reply(prompt, context=history, user_id=user_id)
    except Exception as e:
        return f'Assistant error: {e}'
