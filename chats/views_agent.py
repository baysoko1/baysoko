from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.decorators import login_required
from django.utils.decorators import method_decorator
from django.views import View
from django.core.cache import cache
from django.utils import timezone
import json

from listings.ai_assistant import try_database_query, assistant_reply


@csrf_exempt
def agent_search_api(request):
    """Simple search API used by the chat widget for search intents."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)
    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
        q = body.get('q') or body.get('query') or ''
        if not q:
            return JsonResponse({'success': False, 'error': 'query required'}, status=400)
        # Delegate to ai_assistant quick listing search
        # try_database_query may return listings if pattern matches; otherwise fall back to listing search
        res = try_database_query(q, user_id=request.user.id if request.user.is_authenticated else None)
        if res and res.get('data'):
            return JsonResponse({'success': True, 'results': res.get('data'), 'summary': res.get('text', '')})
        # fallback: ask assistant for list suggestions (will call _query_listings inside)
        ans = assistant_reply(q, context=None, user_id=request.user.id if request.user.is_authenticated else None)
        return JsonResponse({'success': True, 'results': ans.get('platform_items', []), 'summary': ans.get('text', '')})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@csrf_exempt
def agent_feedback_api(request):
    """Collect feedback (like/dislike) from the widget.
    Expects JSON: { message_id: int or null, feedback: 'like'|'dislike', value: true|false }
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)
    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
        kind = str(body.get('feedback') or '').strip().lower()
        value = bool(body.get('value'))
        prompt = str(body.get('prompt') or '').strip()
        assistant_text = str(body.get('assistant_text') or '').strip()
        # Store lightweight per-user feedback profile for adaptive assistant behavior
        if request.user.is_authenticated and kind in {'like', 'dislike'}:
            cache_key = f'agent_feedback_profile:{request.user.id}'
            prof = cache.get(cache_key) or {
                'like': 0,
                'dislike': 0,
                'last_feedback_at': None,
                'last_disliked_prompt': '',
                'last_disliked_answer': '',
            }
            delta = 1 if value else -1
            prof[kind] = max(0, int(prof.get(kind, 0) or 0) + delta)
            if kind == 'dislike' and value:
                prof['last_disliked_prompt'] = prompt[:400]
                prof['last_disliked_answer'] = assistant_text[:1200]
            prof['last_feedback_at'] = timezone.now().isoformat()
            cache.set(cache_key, prof, timeout=60 * 60 * 24 * 30)

        # Keep logging as well for diagnostics
        logger = None
        try:
            import logging
            logger = logging.getLogger('baysoko.agent_feedback')
        except Exception:
            logger = None
        if logger:
            logger.info('Agent feedback: %s', body)
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@csrf_exempt
def agent_send_api(request):
    """Fallback HTTP endpoint to accept user messages when WS is unavailable.
    This will forward to assistant_reply and persist minimal history via AgentChat if available.
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'POST required'}, status=405)
    try:
        body = json.loads(request.body.decode('utf-8') or '{}')
        content = body.get('content') or ''
        conversation_id = body.get('conversation_id')
        user_id = request.user.id if request.user.is_authenticated else None
        if not content:
            return JsonResponse({'success': False, 'error': 'content required'}, status=400)
        # Generate assistant reply (DB-first + Gemini fallback)
        ans = assistant_reply(content, context=None, user_id=user_id)
        return JsonResponse({'success': True, 'reply': ans})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)
