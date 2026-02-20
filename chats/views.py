import json
import uuid
import logging
import mimetypes
import urllib.request
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db.models import Q, Count, OuterRef, Subquery, Max
from django.http import JsonResponse, Http404, HttpResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.utils import timezone
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from django.utils import timezone

from .models import Conversation, Message, MessageAttachment, UserOnlineStatus
from .forms import MessageForm
from .utils import broadcast_unread_sync

logger = logging.getLogger(__name__)
User = get_user_model()

# WebSocket broadcast helpers
def broadcast_to_user(user_id, event_type, data):
    """Send an event to a specific user's WebSocket group."""
    try:
        channel_layer = get_channel_layer()
        async_to_sync(channel_layer.group_send)(
            f"user_{user_id}",
            {
                'type': event_type,
                **data
            }
        )
    except Exception as e:
        logger.error(f"Failed to broadcast to user {user_id}: {e}")

def broadcast_message_created(message, participants):
    """Broadcast a new message to all participants."""
    
    message_data = {
        'id': message.id,
        'conversation_id': message.conversation_id,
        'sender_id': message.sender_id,
        'sender_name': message.sender.get_full_name() or message.sender.username,
        'sender_avatar': get_avatar_url_for(message.sender, None),  # you'll need request for full URL, consider passing request or use absolute URI later
        'content': message.content,
        'timestamp': message.timestamp.isoformat(),
        'is_read': message.is_read,
        'delivered': message.delivered,
        'attachments': [],  # fill with actual attachments if any
        'is_own': False,    # will be set client-side
        'reply_to_id': message.reply_to_id,
        'is_pinned': message.is_pinned,
    }
    for user_id in participants:
        broadcast_to_user(user_id, 'chat_message', {'message': message_data})


def broadcast_to_conversation_participants(conversation, event_type, data, exclude_user_ids=None):
    """Broadcast an event to all participants of a conversation."""
    try:
        exclude_user_ids = set(exclude_user_ids or [])
        participant_ids = [p.id for p in conversation.participants.all()]
        for uid in participant_ids:
            if uid in exclude_user_ids:
                continue
            broadcast_to_user(uid, event_type, data)
    except Exception as e:
        logger.exception(f"Failed to broadcast {event_type} to conversation {getattr(conversation, 'id', None)}: {e}")

# ----------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------
def update_user_online_status(user):
    status, created = UserOnlineStatus.objects.get_or_create(user=user)
    status.last_active = timezone.now()
    if (timezone.now() - status.last_active).seconds < 180:
        status.is_online = True
        status.last_seen = timezone.now()
    else:
        status.is_online = False
    status.save(update_fields=['is_online', 'last_active', 'last_seen'])
    return status


def get_avatar_url_for(user, request_obj=None):
    """Return a safe avatar URL for a User instance."""
    default = '/static/images/default_profile_pic.svg'
    try:
        if hasattr(user, 'get_profile_picture_url'):
            url = user.get_profile_picture_url()
        else:
            url = None
            if hasattr(user, 'profile') and user.profile:
                if hasattr(user.profile, 'get_profile_picture_url'):
                    url = user.profile.get_profile_picture_url()
                elif hasattr(user.profile, 'profile_picture') and hasattr(user.profile.profile_picture, 'url'):
                    url = user.profile.profile_picture.url
        if url:
            if request_obj and isinstance(url, str) and url.startswith('/'):
                try:
                    return request_obj.build_absolute_uri(url)
                except Exception:
                    return url
            return url
    except Exception:
        pass
    return default


def get_online_user_ids():
    try:
        three_minutes_ago = timezone.now() - timedelta(minutes=3)
        online_statuses = UserOnlineStatus.objects.filter(
            last_active__gte=three_minutes_ago,
            is_online=True
        )
        return set(status.user_id for status in online_statuses)
    except Exception as e:
        logger.error(f"Error getting online users: {e}")
        return set()


# ----------------------------------------------------------------------
# Main inbox view (renders the template)
# ----------------------------------------------------------------------
@login_required
def inbox(request):
    """Render the modern inbox interface."""
    update_user_online_status(request.user)
    return render(request, 'chats/inbox.html', {
        'user': request.user,
    })


# ----------------------------------------------------------------------
# my_listings view (still needed if you keep it; we'll replace with online users in template)
# ----------------------------------------------------------------------
@login_required
def my_listings(request):
    """API endpoint to get user's listings."""
    try:
        from listings.models import Listing
    except ImportError:
        return JsonResponse({'error': 'Listings app not installed', 'listings': []}, status=500)

    try:
        listings = Listing.objects.filter(seller=request.user).select_related('category').order_by('-date_created')[:20]
    except Exception as e:
        logger.error(f"Error querying listings: {e}", exc_info=True)
        return JsonResponse({'error': str(e), 'listings': []}, status=500)

    listings_data = []
    for listing in listings:
        image_url = ''
        try:
            if hasattr(listing, 'get_image_url') and callable(listing.get_image_url):
                image_url = listing.get_image_url()
                if image_url and request:
                    image_url = request.build_absolute_uri(image_url)
            elif hasattr(listing, 'image') and listing.image:
                image_url = request.build_absolute_uri(listing.image.url)
            elif hasattr(listing, 'images') and listing.images.exists():
                first_image = listing.images.first()
                if first_image and hasattr(first_image, 'image') and first_image.image:
                    image_url = request.build_absolute_uri(first_image.image.url)
        except Exception as e:
            logger.warning(f"Failed to get image for listing {listing.id}: {e}")

        try:
            price = str(listing.price) if listing.price is not None else '0'
        except Exception as e:
            logger.warning(f"Failed to convert price for listing {listing.id}: {e}")
            price = '0'

        listings_data.append({
            'id': listing.id,
            'title': listing.title,
            'price': price,
            'image': image_url
        })

    return JsonResponse({'listings': listings_data})


@login_required
def start_conversation(request, listing_id, recipient_id):
    """Create or get a conversation about a listing and redirect to inbox."""
    try:
        from listings.models import Listing
    except ImportError:
        return redirect('chats:inbox')

    listing = get_object_or_404(Listing, pk=listing_id)
    recipient = get_object_or_404(User, pk=recipient_id)

    conversation = Conversation.objects.filter(
        participants=request.user
    ).filter(
        participants=recipient
    ).filter(
        listing=listing
    ).first()

    if not conversation:
        conversation = Conversation.objects.create(listing=listing)
        conversation.participants.add(request.user, recipient)

    return redirect(f'/chats/?open={conversation.pk}')


# ... all the provided functions remain exactly as given ...

# ========== ADDITIONS (do not conflict) ==========

@login_required
def online_users_list(request):
    """Return full details of online users (excluding current)."""
    three_minutes_ago = timezone.now() - timedelta(minutes=3)
    online_statuses = UserOnlineStatus.objects.filter(
        last_active__gte=three_minutes_ago,
        is_online=True
    ).exclude(user=request.user).select_related('user')
    users = []
    for status in online_statuses:
        u = status.user
        users.append({
            'id': u.id,
            'name': u.get_full_name() or u.username,
            'avatar': get_avatar_url_for(u, request),
            'last_seen': status.last_seen.isoformat(),
        })
    return JsonResponse({'success': True, 'users': users})

@login_required
@csrf_exempt
def archive_participant(request):
    """Archive all conversations with a given participant."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)
    try:
        data = json.loads(request.body)
        participant_id = data.get('participant_id')
        if not participant_id:
            return JsonResponse({'success': False, 'error': 'participant_id required'}, status=400)
        # find all conversations with this participant
        convs = Conversation.objects.filter(
            participants=request.user
        ).filter(participants__id=participant_id)
        convs.update(is_archived=True)
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

@login_required
@csrf_exempt
def unarchive_participant(request):
    """Unarchive all conversations with a given participant."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)
    try:
        data = json.loads(request.body)
        participant_id = data.get('participant_id')
        if not participant_id:
            return JsonResponse({'success': False, 'error': 'participant_id required'}, status=400)
        convs = Conversation.objects.filter(
            participants=request.user
        ).filter(participants__id=participant_id)
        convs.update(is_archived=False)
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})

# Modify grouped_conversations to return both active and archived
@login_required
def grouped_conversations(request):
    """Return all conversations grouped by the other participant, split into active and archived."""
    try:
        # All conversations of the current user
        convs = Conversation.objects.filter(
            participants=request.user
        ).prefetch_related('participants', 'messages')

        # Prepare a reliable default avatar URL
        default_avatar = request.build_absolute_uri(
            staticfiles_storage.url('images/default-avatar.svg')
        ) if staticfiles_storage.exists('images/default-avatar.svg') else \
            'https://placehold.co/200x200/c2c2c2/1f1f1f?text=User'

        def build_groups(conv_qs):
            groups = {}
            for conv in conv_qs:
                other = conv.get_other_participant(request.user)
                if not other:
                    continue
                pid = other.id
                if pid not in groups:
                    avatar = get_avatar_url_for(other, request) or default_avatar
                    groups[pid] = {
                        'participant': other,
                        'avatar': avatar,
                        'conversations': [],
                        'total_unread': 0,
                        'last_message': None,
                        'last_message_time': None,
                    }
                groups[pid]['conversations'].append(conv)
                unread = conv.messages.filter(is_read=False).exclude(sender=request.user).count()
                groups[pid]['total_unread'] += unread
                last_msg = conv.messages.order_by('-timestamp').first()
                if last_msg and (not groups[pid]['last_message_time'] or last_msg.timestamp > groups[pid]['last_message_time']):
                    groups[pid]['last_message'] = last_msg
                    groups[pid]['last_message_time'] = last_msg.timestamp
            return groups

        active_convs = convs.filter(is_archived=False)
        archived_convs = convs.filter(is_archived=True)

        active_groups_dict = build_groups(active_convs)
        archived_groups_dict = build_groups(archived_convs)

        online_ids = get_online_user_ids()

        def build_result(groups_dict, archived_flag=False):
            result = []
            for pid, data in groups_dict.items():
                other = data['participant']
                try:
                    status = UserOnlineStatus.objects.get(user=other)
                    is_online = status.is_online
                    last_seen = status.last_seen
                except UserOnlineStatus.DoesNotExist:
                    is_online = False
                    last_seen = other.last_login

                last_msg = data['last_message']
                result.append({
                    'participant_id': other.id,
                    'participant_name': other.get_full_name() or other.username,
                    'participant_username': other.username,
                    'participant_avatar': data['avatar'],
                    'is_online': is_online,
                    'last_seen': last_seen.isoformat() if last_seen else None,
                    'last_message_content': last_msg.content[:100] if last_msg else '',
                    'last_message_time': last_msg.timestamp.isoformat() if last_msg else None,
                    'last_message_sender_id': last_msg.sender_id if last_msg else None,
                    'last_message_status': 'read' if last_msg and last_msg.is_read else 'delivered' if last_msg and last_msg.delivered else 'sent' if last_msg else None,
                    'total_unread': data['total_unread'],
                    'conversation_ids': [c.id for c in data['conversations']],
                    'listing_titles': [c.listing.title for c in data['conversations'] if c.listing],
                    'archived': archived_flag,
                })
            result.sort(key=lambda x: x['last_message_time'] or '', reverse=True)
            return result

        active_result = build_result(active_groups_dict, archived_flag=False)
        archived_result = build_result(archived_groups_dict, archived_flag=True)

        return JsonResponse({'success': True, 'groups': active_result, 'archived': archived_result})
    except Exception as e:
        logger.error(f"grouped_conversations error: {e}")
        return JsonResponse({'success': False, 'error': str(e)})

# Modify send_unified_message to support reply_to
@login_required
@csrf_exempt
def send_unified_message(request):
    """Send a message to a participant. Use the most recent conversation or create a new one."""
    try:
        if request.content_type == 'application/json':
            data = json.loads(request.body)
        else:
            data = request.POST

        participant_id = data.get('participant_id')
        content = data.get('content', '').strip()
        reply_to_id = data.get('reply_to_id')   # new field
        if not participant_id:
            return JsonResponse({'success': False, 'error': 'participant_id required'}, status=400)

        participant = get_object_or_404(User, id=participant_id)

        # find most recent conversation with this participant (prefer unarchived)
        conversation = Conversation.objects.filter(
            participants=request.user
        ).filter(
            participants=participant
        ).order_by('-updated_at').first()

        if not conversation:
            conversation = Conversation.objects.create()
            conversation.participants.add(request.user, participant)

        # create message
        message = Message(
            conversation=conversation,
            sender=request.user,
            content=content or '[Attachment]',
            reply_to_id=reply_to_id if reply_to_id else None,
        )

        # handle attachments (files and remote URLs)
        attachments_data = []
        saved_paths = []
        # Save uploaded files
        if request.FILES:
            for key, file in request.FILES.items():
                ext = file.name.split('.')[-1] if '.' in file.name else 'bin'
                filename = f"chat_attachments/{uuid.uuid4()}.{ext}"
                saved_path = default_storage.save(filename, ContentFile(file.read()))
                file_url = default_storage.url(saved_path)
                attachments_data.append({
                    'saved_path': saved_path,
                    'id': None,
                    'name': file.name,
                    'url': file_url,
                    'type': getattr(file, 'content_type', '') or mimetypes.guess_type(file.name)[0] or '',
                    'size': getattr(file, 'size', None),
                })
                saved_paths.append(saved_path)

        # Handle remote attachments (e.g., Cloudinary secure URLs passed by client)
        remote_urls = []
        try:
            # If JSON body contained remote_attachments
            if isinstance(data, dict) and data.get('remote_attachments'):
                remote_urls = data.get('remote_attachments') or []
            # If sent via form-data multiple remote_attachments fields
            elif request.POST:
                remote_urls = request.POST.getlist('remote_attachments') or []
        except Exception:
            remote_urls = []

        for rurl in remote_urls:
            try:
                resp = urllib.request.urlopen(rurl)
                content = resp.read()
                ctype = resp.headers.get_content_type() if hasattr(resp, 'headers') else mimetypes.guess_type(rurl)[0] or ''
                ext = mimetypes.guess_extension(ctype) or ''
                filename = f"chat_attachments/{uuid.uuid4()}{ext}"
                saved_path = default_storage.save(filename, ContentFile(content))
                file_url = default_storage.url(saved_path)
                attachments_data.append({
                    'saved_path': saved_path,
                    'id': None,
                    'name': rurl.split('/')[-1].split('?')[0] or rurl,
                    'url': file_url,
                    'type': ctype,
                    'size': len(content),
                })
                saved_paths.append(saved_path)
            except Exception as e:
                logger.exception(f"Failed to fetch remote attachment {rurl}: {e}")

        message.save()
        # Persist attachments into MessageAttachment model (if any)
        created_attachments = []
        try:
            for att in attachments_data:
                if att.get('saved_path'):
                    ma = MessageAttachment(message=message)
                    # set file name directly to saved storage path so FileField references it
                    ma.file.name = att['saved_path']
                    ma.filename = att.get('name') or ''
                    ma.content_type = att.get('type') or ''
                    ma.size = att.get('size') or None
                    ma.save()
                    created_attachments.append({
                        'id': ma.id,
                        'name': ma.filename,
                        'url': default_storage.url(ma.file.name),
                        'type': ma.content_type,
                        'size': ma.size,
                    })
        except Exception:
            logger.exception('Failed to create MessageAttachment records')
        conversation.updated_at = timezone.now()
        conversation.save(update_fields=['updated_at'])

        # Mark previous messages from other participants as delivered
        conversation.messages.filter(~Q(sender=request.user), delivered=False).update(delivered=True)

        update_user_online_status(request.user)

        # Broadcast new message to conversation participants via Channels
        try:
            message_payload = {
                'id': message.id,
                'conversation_id': conversation.id,
                'content': message.content,
                'timestamp': message.timestamp.isoformat(),
                'sender_id': request.user.id,
                'sender_name': request.user.get_full_name() or request.user.username,
                'sender_avatar': get_avatar_url_for(request.user, request),
                'attachments': created_attachments or attachments_data,
                'is_own': False,
                'is_read': False,
                'reply_to_id': message.reply_to_id,
            }
            broadcast_to_conversation_participants(conversation, 'chat_message', {'message': message_payload})
        except Exception:
            logger.exception('Failed to broadcast message via WebSocket')

        return JsonResponse({
            'success': True,
            'message_id': message.id,
            'conversation_id': conversation.id,
            'message': message_payload
        })
    except Exception as e:
        logger.error(f"send_unified_message error: {e}")
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

# Ensure unified_conversation_detail includes reply_to and is_pinned
# (already present in the provided version)



# ----------------------------------------------------------------------
# API Views (Class-based)
# ----------------------------------------------------------------------
class GroupedConversationsView(LoginRequiredMixin, View):
    def get(self, request):
        user = request.user
        try:
            # Active conversations: user is participant AND not archived by user
            active_convs = Conversation.objects.filter(
                participants=user
            ).exclude(
                archived_by=user
            ).prefetch_related('participants', 'messages')

            # Archived conversations: user is participant AND archived by user
            archived_convs = Conversation.objects.filter(
                participants=user,
                archived_by=user
            ).prefetch_related('participants', 'messages')

            def group_conversations(conversations, archived_flag=False):
                groups = {}
                for conv in conversations:
                    other = conv.get_other_participant(user)
                    if not other:
                        continue
                    pid = other.id
                    if pid not in groups:
                        avatar = get_avatar_url_for(other, request) or request.build_absolute_uri(
                            '/static/images/default-avatar.svg'
                        )
                        groups[pid] = {
                            'participant': other,
                            'avatar': avatar,
                            'conversations': [],
                            'total_unread': 0,
                            'last_message': None,
                            'last_message_time': None,
                            'archived': archived_flag,
                        }
                    groups[pid]['conversations'].append(conv)

                    unread = conv.messages.filter(is_read=False).exclude(sender=user).count()
                    groups[pid]['total_unread'] += unread

                    last_msg = conv.messages.order_by('-timestamp').first()
                    if last_msg:
                        if (not groups[pid]['last_message_time'] or
                                last_msg.timestamp > groups[pid]['last_message_time']):
                            groups[pid]['last_message'] = last_msg
                            groups[pid]['last_message_time'] = last_msg.timestamp
                return groups

            active_groups_dict = group_conversations(active_convs, archived_flag=False)
            archived_groups_dict = group_conversations(archived_convs, archived_flag=True)

            online_ids = get_online_user_ids()

            def build_result_list(groups_dict):
                result = []
                for pid, data in groups_dict.items():
                    other = data['participant']
                    try:
                        status = UserOnlineStatus.objects.get(user=other)
                        is_online = status.is_online
                        last_seen = status.last_seen
                    except UserOnlineStatus.DoesNotExist:
                        is_online = False
                        last_seen = other.last_login

                    last_msg = data['last_message']
                    result.append({
                        'participant_id': other.id,
                        'participant_name': other.get_full_name() or other.username,
                        'participant_username': other.username,
                        'participant_avatar': data['avatar'],
                        'is_online': is_online,
                        'last_seen': last_seen.isoformat() if last_seen else None,
                        'last_message_content': last_msg.content[:100] if last_msg else '',
                        'last_message_time': last_msg.timestamp.isoformat() if last_msg else None,
                        'last_message_sender_id': last_msg.sender_id if last_msg else None,
                        'last_message_status': 'read' if last_msg and last_msg.is_read else 'delivered' if last_msg and last_msg.delivered else 'sent' if last_msg else None,
                        'total_unread': data['total_unread'],
                        'conversation_ids': [c.id for c in data['conversations']],
                        'listing_titles': [c.listing.title for c in data['conversations'] if c.listing],
                        'archived': data['archived'],
                    })
                result.sort(key=lambda x: x['last_message_time'] or '', reverse=True)
                return result

            active_result = build_result_list(active_groups_dict)
            archived_result = build_result_list(archived_groups_dict)

            return JsonResponse({'success': True, 'groups': active_result, 'archived': archived_result})

        except Exception as e:
            logger.exception("GroupedConversationsView failed")
            return JsonResponse({'success': True, 'groups': [], 'archived': []})


class UnreadMessagesCountView(LoginRequiredMixin, View):
    def get(self, request):
        total = Message.objects.filter(
            ~Q(sender=request.user),
            conversation__participants=request.user,
            is_read=False
        ).count()
        return JsonResponse({'count': total})


class GetOnlineUsersView(LoginRequiredMixin, View):
    def get(self, request):
        online_ids = get_online_user_ids()
        if request.user.id in online_ids:
            online_ids.remove(request.user.id)
        return JsonResponse({'success': True, 'online_users': list(online_ids)})


class OnlineUsersListView(LoginRequiredMixin, View):
    def get(self, request):
        three_minutes_ago = timezone.now() - timedelta(minutes=3)
        online_statuses = UserOnlineStatus.objects.filter(
            last_active__gte=three_minutes_ago,
            is_online=True
        ).exclude(user=request.user).select_related('user')
        users = []
        for status in online_statuses:
            u = status.user
            users.append({
                'id': u.id,
                'name': u.get_full_name() or u.username,
                'avatar': get_avatar_url_for(u, request),
                'last_seen': status.last_seen.isoformat(),
            })
        return JsonResponse({'success': True, 'users': users})


class SearchUsersView(LoginRequiredMixin, View):
    def get(self, request):
        q = request.GET.get('q', '')
        if len(q) < 2:
            return JsonResponse({'success': True, 'users': []})

        users = User.objects.filter(
            Q(username__icontains=q) |
            Q(first_name__icontains=q) |
            Q(last_name__icontains=q)
        ).exclude(id=request.user.id).filter(is_active=True)[:20]

        data = []
        for u in users:
            data.append({
                'id': u.id,
                'name': u.get_full_name() or u.username,
                'username': u.username,
                'avatar': get_avatar_url_for(u, request),
            })
        return JsonResponse({'success': True, 'users': data})


class UnifiedConversationView(LoginRequiredMixin, View):
    def get(self, request, participant_id):
        user = request.user
        other = get_object_or_404(User, id=participant_id)

        convos = Conversation.objects.filter(
            participants=user
        ).filter(participants=other)

        if not convos.exists():
            return JsonResponse({
                'success': True,
                'participant': self._get_participant_info(other, request),
                'messages': [],
                'conversation_ids': []
            })

        messages_qs = Message.objects.filter(
            conversation__in=convos,
            is_deleted=False
        ).select_related('sender').prefetch_related('attachments').order_by('timestamp')

        last_id = request.GET.get('last_id')
        if last_id:
            try:
                last_id = int(last_id)
                messages_qs = messages_qs.filter(id__gt=last_id)
            except ValueError:
                pass

        if not last_id:
            unread_msgs = messages_qs.filter(is_read=False).exclude(sender=user)
            unread_msgs.update(is_read=True, read_at=timezone.now())
            # Notify other participant
            broadcast_to_user(
                other.id,
                'read_receipt',
                {
                    'conversation_id': convos.first().id,
                    'message_ids': list(unread_msgs.values_list('id', flat=True)),
                    'read_by': user.id
                }
            )

        messages_data = [self._serialize_message(msg, user, request) for msg in messages_qs]

        return JsonResponse({
            'success': True,
            'participant': self._get_participant_info(other, request),
            'messages': messages_data,
            'conversation_ids': list(convos.values_list('id', flat=True)),
        })

    def _get_participant_info(self, other, request):
        try:
            status = UserOnlineStatus.objects.get(user=other)
            is_online = status.is_online
            last_seen = status.last_seen
        except UserOnlineStatus.DoesNotExist:
            is_online = False
            last_seen = other.last_login

        avatar = get_avatar_url_for(other, request) or request.build_absolute_uri(
            '/static/images/default-avatar.svg'
        )

        return {
            'id': other.id,
            'name': other.get_full_name() or other.username,
            'avatar': avatar,
            'is_online': is_online,
            'last_seen': last_seen.isoformat() if last_seen else None,
        }

    def _serialize_message(self, msg, current_user, request):
        attachments = []
        for att in msg.attachments.all():
            attachments.append({
                'id': att.id,
                'url': request.build_absolute_uri(att.file.url) if att.file else None,
                'name': att.filename,
                'type': att.content_type,
                'size': att.size,
            })
        return {
            'id': msg.id,
            'conversation_id': msg.conversation_id,
            'sender_id': msg.sender_id,
            'sender_name': msg.sender.get_full_name() or msg.sender.username,
            'sender_avatar': get_avatar_url_for(msg.sender, request),
            'content': msg.content,
            'timestamp': msg.timestamp.isoformat(),
            'is_read': msg.is_read,
            'read_at': msg.read_at.isoformat() if msg.read_at else None,
            'delivered': msg.delivered,
            'attachments': attachments,
            'is_own': msg.sender_id == current_user.id,
            'reply_to_id': msg.reply_to_id,
            'is_pinned': msg.is_pinned,
        }


class SendUnifiedMessageView(LoginRequiredMixin, View):
    def post(self, request):
        user = request.user

        if request.content_type == 'application/json':
            try:
                data = json.loads(request.body)
            except json.JSONDecodeError:
                data = {}
            participant_id = data.get('participant_id')
            content = data.get('content', '')
            reply_to_id = data.get('reply_to_id')
            files = []
        else:
            participant_id = request.POST.get('participant_id')
            content = request.POST.get('content', '')
            reply_to_id = request.POST.get('reply_to_id')
            files = request.FILES.getlist('attachment')

        if not participant_id:
            return JsonResponse({'success': False, 'error': 'participant_id required'}, status=400)

        other = get_object_or_404(User, id=participant_id)

        # Find or create an unarchived conversation
        conversation = Conversation.objects.filter(
            participants=user
        ).filter(participants=other, archived_by__isnull=True).first()
        if not conversation:
            conversation = Conversation.objects.create()
            conversation.participants.add(user, other)

        message = Message.objects.create(
            conversation=conversation,
            sender=user,
            content=content or '[Attachment]',
            reply_to_id=reply_to_id if reply_to_id else None,
        )
        # Broadcast new message via WebSocket
        participants = [request.user.id, other.id]
        broadcast_message_created(message, participants)

        # Update unread counts for recipients (excluding sender)
        for uid in participants:
            if uid != request.user.id:
                try:
                    broadcast_unread_sync(uid)
                except Exception:
                    logger.exception(f"Failed to broadcast unread for {uid}")

        # Also broadcast conversation-level unread counts/deltas for conversation list UI
        try:
            for uid in participants:
                if uid == request.user.id:
                    continue
                conv_unread = conversation.messages.filter(is_read=False).exclude(sender_id=uid).count()
                broadcast_to_user(uid, 'conversation_unread', {
                    'conversation_id': conversation.id,
                    'unread_count': conv_unread,
                })
        except Exception:
            logger.exception('Failed to broadcast conversation-level unread counts')

        attachments_data = []
        # Save uploaded files (if any)
        for f in files:
            try:
                ext = f.name.split('.')[-1] if '.' in f.name else 'bin'
                filename = f'chat_attachments/{user.id}/{uuid.uuid4()}.{ext}'
                saved_path = default_storage.save(filename, ContentFile(f.read()))
                file_url = default_storage.url(saved_path)
                att = MessageAttachment.objects.create(
                    message=message,
                    file=saved_path,
                    filename=f.name,
                    content_type=getattr(f, 'content_type', ''),
                    size=getattr(f, 'size', None),
                )
                attachments_data.append({
                    'id': att.id,
                    'url': request.build_absolute_uri(file_url),
                    'name': att.filename,
                    'type': att.content_type,
                    'size': att.size,
                })
            except Exception:
                logger.exception('Failed to save uploaded attachment')

        # Handle remote attachments (e.g., Cloudinary URLs) sent as 'remote_attachments'
        remote_urls = []
        try:
            # JSON payload case
            if request.content_type == 'application/json':
                data = json.loads(request.body or '{}')
                remote_urls = data.get('remote_attachments', []) or []
            else:
                # form-data case: may be multiple remote_attachments entries
                remote_urls = request.POST.getlist('remote_attachments') or []
        except Exception:
            remote_urls = []

        if remote_urls:
            import urllib.request
            for url in remote_urls:
                try:
                    resp = urllib.request.urlopen(url)
                    content = resp.read()
                    # derive filename from URL
                    parsed = url.split('?')[0].rstrip('/')
                    name = parsed.split('/')[-1] or f'{uuid.uuid4()}.bin'
                    ext = name.split('.')[-1] if '.' in name else 'bin'
                    save_name = f'chat_attachments/{user.id}/{uuid.uuid4()}.{ext}'
                    saved_path = default_storage.save(save_name, ContentFile(content))
                    file_url = default_storage.url(saved_path)
                    att = MessageAttachment.objects.create(
                        message=message,
                        file=saved_path,
                        filename=name,
                        content_type=resp.headers.get_content_type() if hasattr(resp, 'headers') else '',
                        size=len(content),
                    )
                    attachments_data.append({
                        'id': att.id,
                        'url': request.build_absolute_uri(file_url),
                        'name': att.filename,
                        'type': att.content_type,
                        'size': att.size,
                    })
                except Exception:
                    logger.exception('Failed to fetch and save remote attachment: %s', url)

        conversation.messages.filter(~Q(sender=user), delivered=False).update(delivered=True)
        conversation.updated_at = timezone.now()
        conversation.save(update_fields=['updated_at'])
        

        return JsonResponse({
            'success': True,
            'message_id': message.id,
            'conversation_id': conversation.id,
            'message': self._serialize_message(message, user, request, attachments_data)
        })

    def _serialize_message(self, msg, current_user, request, extra_attachments=None):
        attachments = extra_attachments if extra_attachments is not None else []
        if not attachments:
            for att in msg.attachments.all():
                attachments.append({
                    'id': att.id,
                    'url': request.build_absolute_uri(att.file.url) if att.file else None,
                    'name': att.filename,
                    'type': att.content_type,
                    'size': att.size,
                })
        # attach reply_to snapshot if available so sender sees replied content immediately
        reply_snapshot = None
        if getattr(msg, 'reply_to_id', None):
            try:
                replied = Message.objects.select_related('sender').get(id=msg.reply_to_id)
                reply_snapshot = {
                    'id': replied.id,
                    'content': replied.content,
                    'sender_id': replied.sender_id,
                    'sender_name': replied.sender.get_full_name() or replied.sender.username,
                }
            except Message.DoesNotExist:
                reply_snapshot = None

        return {
            'id': msg.id,
            'sender_id': msg.sender_id,
            'sender_name': msg.sender.get_full_name() or msg.sender.username,
            'sender_avatar': get_avatar_url_for(msg.sender, request),
            'content': msg.content,
            'timestamp': msg.timestamp.isoformat(),
            'is_read': msg.is_read,
            'delivered': msg.delivered,
            'reply_to_id': msg.reply_to_id,
            'reply_to': reply_snapshot,
            'is_pinned': msg.is_pinned,
            'attachments': attachments,
            'is_own': msg.sender_id == current_user.id,
        }


class SendTypingView(LoginRequiredMixin, View):
    def post(self, request, conversation_id):
        try:
            conversation = get_object_or_404(Conversation, id=conversation_id, participants=request.user)
            cache.set(f'typing_{conversation_id}_{request.user.id}', True, timeout=3)
            return JsonResponse({'success': True})
        except Http404:
            return JsonResponse({'success': False, 'error': 'Conversation not found'}, status=404)
        except Exception as e:
            logger.error(f"SendTypingView error: {e}")
            return JsonResponse({'success': False, 'error': 'Server error'}, status=500)


class CheckTypingView(LoginRequiredMixin, View):
    def get(self, request, conversation_id):
        try:
            conversation = get_object_or_404(Conversation, id=conversation_id, participants=request.user)
            other = conversation.participants.exclude(id=request.user.id).first()
            if not other:
                return JsonResponse({'typing': False})
            is_typing = cache.get(f'typing_{conversation_id}_{other.id}', False)
            return JsonResponse({
                'typing': is_typing,
                'user_name': other.get_full_name() or other.username,
            })
        except Http404:
            return JsonResponse({'typing': False})
        except Exception as e:
            logger.error(f"CheckTypingView error: {e}")
            return JsonResponse({'typing': False})


class EditMessageView(LoginRequiredMixin, View):
    def post(self, request, message_id):
        try:
            msg = Message.objects.get(id=message_id, sender=request.user)
            # notify all participants in the conversation about the edit
            conversation = msg.conversation
            broadcast_to_conversation_participants(
                conversation,
                'message_updated',
                {
                    'conversation_id': msg.conversation_id,
                    'message_id': msg.id,
                    'action': 'edit',
                    'data': {'content': msg.content}
                }
            )
        except Message.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Message not found'}, status=404)


@login_required
def download_conversation_images(request, participant_id):
    """Stream a ZIP archive of image attachments for the conversation with the given participant."""
    try:
        other = get_object_or_404(User, id=participant_id)
        convos = Conversation.objects.filter(participants=request.user).filter(participants=other)
        if not convos.exists():
            return JsonResponse({'success': False, 'error': 'No conversation found'}, status=404)

        atts = MessageAttachment.objects.filter(
            message__conversation__in=convos,
        ).filter(content_type__startswith='image/').order_by('message__timestamp')

        if not atts.exists():
            return JsonResponse({'success': False, 'error': 'No image attachments found'}, status=404)

        import io, zipfile, os

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for att in atts:
                try:
                    filename = att.filename or os.path.basename(getattr(att.file, 'name', 'attachment'))
                    # Read file from storage/backend
                    with default_storage.open(att.file.name, 'rb') as f:
                        data = f.read()
                        zf.writestr(filename, data)
                except Exception as e:
                    logger.warning(f"Failed to add attachment {att.id} to zip: {e}")

        buf.seek(0)
        resp = HttpResponse(buf.getvalue(), content_type='application/zip')
        resp['Content-Disposition'] = f'attachment; filename=conversation_{participant_id}_images.zip'
        return resp
    except Http404:
        return JsonResponse({'success': False, 'error': 'Participant not found'}, status=404)
    except Exception as e:
        logger.exception('download_conversation_images error')
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

        data = json.loads(request.body)
        new_content = data.get('content')
        if new_content is None:
            return JsonResponse({'success': False, 'error': 'No content'}, status=400)

        msg.content = new_content
        msg.save()
        return JsonResponse({'success': True})


class PinMessageView(LoginRequiredMixin, View):
    def post(self, request, message_id):
        try:
            msg = Message.objects.get(id=message_id, conversation__participants=request.user)
        except Message.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Message not found'}, status=404)

        msg.is_pinned = not msg.is_pinned
        msg.save()
        # notify all participants in the conversation about the pin change
        broadcast_to_conversation_participants(
            msg.conversation,
            'message_updated',
            {
                'conversation_id': msg.conversation_id,
                'message_id': msg.id,
                'action': 'pin',
                'data': {'content': msg.content, 'is_pinned': msg.is_pinned}
            }
        )
        return JsonResponse({'success': True, 'is_pinned': msg.is_pinned})


class DeleteMessagesView(LoginRequiredMixin, View):
    def post(self, request):
        data = json.loads(request.body)
        msg_ids = data.get('message_ids', [])
        if not msg_ids:
            return JsonResponse({'success': False, 'error': 'No message IDs'}, status=400)

        deleted = Message.objects.filter(id__in=msg_ids, sender=request.user).delete()
        # Broadcast delete to relevant conversations' participants
        try:
            # find affected conversations and notify their participants
            convs = Conversation.objects.filter(messages__id__in=msg_ids).distinct()
            for conv in convs:
                broadcast_to_conversation_participants(
                    conv,
                    'message_updated',
                    {
                        'conversation_id': conv.id,
                        'message_ids': msg_ids,
                        'action': 'delete',
                    }
                )
        except Exception:
            logger.exception('Failed to broadcast message deletions')
        return JsonResponse({'success': True, 'deleted_count': deleted[0]})


class GetMessageStatusView(LoginRequiredMixin, View):
    def get(self, request, conversation_id):
        conversation = get_object_or_404(Conversation, id=conversation_id, participants=request.user)
        messages = conversation.messages.filter(sender=request.user).values('id', 'is_read', 'delivered')
        status_data = {msg['id']: {'is_read': msg['is_read'], 'delivered': msg['delivered']} for msg in messages}
        return JsonResponse({'success': True, 'status_data': status_data})


class ArchiveConversationView(LoginRequiredMixin, View):
    def post(self, request):
        data = json.loads(request.body)
        participant_id = data.get('participant_id')
        if not participant_id:
            return JsonResponse({'success': False, 'error': 'Missing participant_id'}, status=400)

        convos = Conversation.objects.filter(
            participants=request.user
        ).filter(participants__id=participant_id)
        for convo in convos:
            convo.archived_by.add(request.user)
        return JsonResponse({'success': True})


class UnarchiveConversationView(LoginRequiredMixin, View):
    def post(self, request):
        data = json.loads(request.body)
        participant_id = data.get('participant_id')
        if not participant_id:
            return JsonResponse({'success': False, 'error': 'Missing participant_id'}, status=400)

        convos = Conversation.objects.filter(
            participants=request.user
        ).filter(participants__id=participant_id)
        for convo in convos:
            convo.archived_by.remove(request.user)
        return JsonResponse({'success': True})


class MuteConversationView(LoginRequiredMixin, View):
    def post(self, request, conversation_id):
        conversation = get_object_or_404(Conversation, id=conversation_id, participants=request.user)
        if conversation.muted_by.filter(id=request.user.id).exists():
            conversation.muted_by.remove(request.user)
            status = 'unmuted'
        else:
            conversation.muted_by.add(request.user)
            status = 'muted'
        return JsonResponse({'success': True, 'status': status})