import json
import logging
import os
import re
import difflib
import time as _time
import random
import requests
from django.conf import settings
from django.core.cache import cache
from django.db.models import Q, Sum, F, DecimalField
from django.utils import timezone
from django.urls import resolve, Resolver404
from urllib.parse import urlsplit
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

PROMPT_TEMPLATE = '''Generate a JSON with the following fields based on the product title:
- category
- description (50-100 words)
- key_features (list of 3–5 short phrases)
- target_audience

Product Title: "{title}"
'''

def _extract_json(text: str):
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return None

def _parse_shorthand_listing(text: str):
    """Extract simple structured fields from short freeform listing text."""
    if not text:
        return {}
    t = text
    res = {}
    try:
        m = re.search(r"(?P<currency>kshs|kes|ksh|shs|usd|\$)\s*[:\-]??\s*(?P<price>[0-9,]+(?:\.[0-9]+)?)", t, re.I)
        if not m:
            m = re.search(r"(?P<price>[0-9,]+(?:\.[0-9]+)?)\s*(?P<currency>kshs|kes|ksh|shs|usd)\b", t, re.I)
        if m:
            res['price'] = m.group('price').replace(',', '')
            if m.groupdict().get('currency'):
                res['currency'] = m.group('currency').upper()
        u = re.search(r"per\s+([a-zA-Z0-9\s\.]+)", t, re.I)
        if u:
            res['unit'] = u.group(0).strip()
        q = re.search(r"(?:quantity|qty|stock)\s*[:=]?\s*(?P<qty>\d+)", t, re.I)
        if not q:
            q = re.search(r"\b(?P<qty>\d+)\s*(?:pcs|pieces|units|bags|bags?)\b", t, re.I)
        if q:
            res['quantity'] = q.group('qty')
        loc = re.search(r"location\s*[:\-]?\s*([A-Za-z0-9\s,\-]+)", t, re.I)
        if loc:
            res['location'] = loc.group(1).strip()
        else:
            m2 = re.search(r"\bin\s+([A-Z][a-zA-Z0-9\s]+)", t)
            if m2:
                res['location'] = m2.group(1).strip()
    except Exception:
        pass
    return res

def _enrich_parsed(parsed: dict):
    """Enrich parsed AI output with server-side category mapping and dynamic_fields."""
    try:
        from .models import Category
        cat_name = parsed.get('category')
        if cat_name:
            cat = Category.objects.filter(name__iexact=cat_name.strip()).first()
            if cat:
                parsed['category_id'] = cat.id
                parsed['category_name'] = cat.name
    except Exception:
        logger.debug('Category enrichment failed', exc_info=True)

    try:
        dynamic_keys = ['brand', 'model', 'color', 'material', 'dimensions', 'weight', 'price', 'meta_description']
        dyn = {}
        for k in dynamic_keys:
            if k in parsed and parsed.get(k) is not None:
                dyn[k] = parsed.get(k)
        if dyn:
            parsed['dynamic_fields'] = dyn
    except Exception:
        logger.debug('Failed to construct dynamic_fields', exc_info=True)

    return parsed

def generate_listing_fields(title: str, context=None):
    """Generate structured listing fields using Google Gemini."""
    context_text = ''
    try:
        fail_until = cache.get('gemini_fail_until')
        if fail_until:
            now_ts = timezone.now().timestamp()
            if isinstance(fail_until, (int, float)) and fail_until > now_ts:
                logger.warning('Gemini circuit open until %s; returning fallback', fail_until)
                fallback = {
                    'category': 'Other',
                    'description': f'Auto-generated description for "{title}" is currently unavailable. Please add a description manually.',
                    'key_features': [],
                    'target_audience': 'general',
                }
                return _enrich_parsed(fallback)
    except Exception:
        pass

    if context:
        if isinstance(context, list):
            parts = []
            for m in context:
                if isinstance(m, dict) and 'role' in m and 'content' in m:
                    parts.append(f"{m.get('role')}: {m.get('content')}")
                elif isinstance(m, str):
                    parts.append(str(m))
            context_text = '\n'.join(parts)
        else:
            context_text = str(context)

    prompt = f"Conversation history:\n{context_text}\n\n" + PROMPT_TEMPLATE.format(title=title) if context_text else PROMPT_TEMPLATE.format(title=title)
    shorthand = _parse_shorthand_listing(title if isinstance(title, str) else '')

    model_name = getattr(settings, 'GEMINI_MODEL', None) or os.environ.get('GEMINI_MODEL', 'gemini-1.5-flash')
    gemini_env = os.environ.get('GEMINI_API_KEY')
    google_env = os.environ.get('GOOGLE_API_KEY')
    if gemini_env and google_env and gemini_env != google_env:
        logger.info('Both GOOGLE_API_KEY and GEMINI_API_KEY are set. Preferring GEMINI_API_KEY.')
        os.environ['GOOGLE_API_KEY'] = gemini_env
    api_key = gemini_env or google_env or getattr(settings, 'GEMINI_API_KEY', None)
    if api_key:
        os.environ['GEMINI_API_KEY'] = api_key
        os.environ['GOOGLE_API_KEY'] = api_key

    try:
        from google import genai
        client = genai.Client()
        cached = cache.get('gemini_working_model')
        candidates = getattr(settings, 'GEMINI_CANDIDATE_MODELS', None)
        attempt_models = []
        if cached:
            attempt_models.append(cached)
        if candidates:
            if model_name and model_name not in candidates:
                attempt_models.append(model_name)
            for c in candidates:
                if c not in attempt_models:
                    attempt_models.append(c)
        else:
            if model_name:
                attempt_models.append(model_name)

        resp = None
        working_model = None
        probe_attempts = []
        for m in attempt_models:
            try:
                probe_attempts.append({'model': m, 'attempted_at': timezone.now().isoformat()})
                resp = client.models.generate_content(model=m, contents=prompt)
                if resp is not None:
                    working_model = m
                    cache.set('gemini_working_model', working_model, 60*60*24)
                    cache.set('gemini_probe_log', {'attempts': probe_attempts, 'working_model': working_model, 'checked_at': timezone.now().isoformat()}, 60*60*24)
                    break
            except Exception:
                probe_attempts.append({'model': m, 'error': 'attempt failed', 'time': timezone.now().isoformat()})
                continue

        if resp is None:
            cache.set('gemini_probe_log', {'attempts': probe_attempts, 'working_model': None, 'checked_at': timezone.now().isoformat()}, 60*60*24)
            raise RuntimeError('No working Gemini model found via genai client')

        text = getattr(resp, 'text', None) or getattr(resp, 'response', None) or str(resp)
        parsed = _extract_json(text)
        if parsed:
            dyn = parsed.get('dynamic_fields', {}) or {}
            for k, v in shorthand.items():
                if k not in dyn and v is not None:
                    dyn[k] = v
            if dyn:
                parsed['dynamic_fields'] = dyn
            return _enrich_parsed(parsed)
        if shorthand:
            fallback = {
                'category': 'Other',
                'description': f'Auto-generated description for "{title}". Please review.',
                'key_features': [],
                'target_audience': 'general',
                'dynamic_fields': shorthand,
            }
            return _enrich_parsed(fallback)
        return {"raw": text}
    except Exception as e:
        logger.debug('genai client failed; falling back to REST API', exc_info=True)

    # REST fallback
    if not api_key:
        raise RuntimeError('GEMINI_API_KEY not configured for REST fallback')

    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    headers = {'x-goog-api-key': api_key, 'Content-Type': 'application/json'}
    candidates_rest = getattr(settings, 'GEMINI_CANDIDATE_MODELS', None)
    if candidates_rest:
        models_to_try = [m for m in candidates_rest if m]
        if model_name and model_name in models_to_try:
            models_to_try.remove(model_name)
            models_to_try.insert(0, model_name)
    else:
        models_to_try = [model_name]

    j = None
    for m in models_to_try:
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent'
        max_attempts = 2
        for attempt in range(1, max_attempts+1):
            try:
                r = requests.post(url, json=payload, headers=headers, timeout=30)
                r.raise_for_status()
                j = r.json()
                break
            except RequestException as re:
                status = None
                try:
                    status = re.response.status_code if re.response is not None else None
                except Exception:
                    status = None
                logger.warning('Gemini REST request to model %s attempt %d/%d failed: %s (status=%s)', m, attempt, max_attempts, re, status)
                try:
                    now_ts = timezone.now().timestamp()
                    if status == 429:
                        cache.set('gemini_fail_until', now_ts + 300, timeout=600)
                        break
                    elif status == 503:
                        cache.set('gemini_fail_until', now_ts + 60, timeout=300)
                        break
                    elif status == 404:
                        logger.info('Model %s not found; skipping', m)
                        break
                except Exception:
                    pass
                if attempt == max_attempts:
                    break
                sleep_for = (2 ** attempt) + random.uniform(0, 1)
                _time.sleep(sleep_for)
        if j is not None:
            break

    if j is None:
        try:
            now_ts = timezone.now().timestamp()
            cache.set('gemini_fail_until', now_ts + 120, timeout=300)
            cache.set('gemini_last_error', {'models': models_to_try, 'time': timezone.now().isoformat()}, 60*60*24)
        except Exception:
            pass
        logger.error('Gemini REST generation failed for all models: %s', models_to_try)
        fallback = {
            'category': 'Other',
            'description': f'Auto-generated description for "{title}" is currently unavailable. Please review and complete the fields.',
            'key_features': [],
            'target_audience': 'general',
            'dynamic_fields': shorthand or {},
        }
        return _enrich_parsed(fallback)

    text = None
    try:
        text = j['candidates'][0]['content']['parts'][0]['text']
    except Exception:
        try:
            text = j['candidates'][0]['content'][0]['text']
        except Exception:
            try:
                text = j.get('text') or j.get('response', {}).get('text')
            except Exception:
                text = str(j)
    parsed = _extract_json(text)
    if parsed:
        return _enrich_parsed(parsed)
    return {"raw": text}


def _extract_terms(text):
    if not text:
        return set()
    cleaned = re.sub(r"[^a-z0-9\s]", " ", str(text).lower())
    return {t for t in cleaned.split() if len(t) > 2}


def _dedupe_platform_items(items):
    out = []
    seen = set()
    seen_titles = set()

    def _canon_title(it):
        title = str(it.get('title') or it.get('name') or it.get('store_name') or '').strip().lower()
        title = re.sub(r'[^a-z0-9\s]', ' ', title)
        title = re.sub(r'\s+', ' ', title).strip()
        # drop weak trailing qualifiers so similar actions collapse
        title = re.sub(r'\b(please|now|today|here|for you|for your account)\b', '', title).strip()
        return re.sub(r'\s+', ' ', title).strip()

    for it in (items or []):
        if not isinstance(it, dict):
            continue
        key = (
            it.get('type'),
            it.get('id'),
            it.get('url'),
            it.get('title') or it.get('name') or it.get('store_name'),
        )
        if key in seen:
            continue
        t = it.get('type')
        canon = _canon_title(it)
        if canon:
            # For action suggestions and entities without stable ids, remove near-duplicate title pills.
            title_key = (t, canon)
            if (t == 'action_suggestion' and title_key in seen_titles) or (not it.get('id') and title_key in seen_titles):
                continue
            seen_titles.add(title_key)
        seen.add(key)
        out.append(it)
    return out


def _filter_platform_items_for_prompt(prompt, text, items):
    items = _dedupe_platform_items(items)
    if not items:
        return []

    context = f"{prompt or ''} {text or ''}".lower()
    terms = _extract_terms(context)
    store_inventory_like = bool(re.search(r"\b(my stores|stores in my account|stores i own|what stores do i own|what stores are in my account)\b", context))

    def item_name(it):
        return (it.get('title') or it.get('name') or it.get('store_name') or '').lower()

    def type_match(it, allowed):
        return (it.get('type') or '') in allowed

    matched = []
    action_items = [it for it in items if isinstance(it, dict) and (it.get('type') == 'action_suggestion')]
    for it in items:
        nm = item_name(it)
        if nm and nm in context:
            matched.append(it)
            continue
        nm_terms = _extract_terms(nm)
        if nm_terms and terms and (nm_terms & terms):
            matched.append(it)

    if matched:
        if store_inventory_like:
            all_store = [it for it in items if isinstance(it, dict) and it.get('type') == 'store']
            ordered = _dedupe_platform_items(all_store + action_items)
            if ordered:
                return ordered[:10]
        ordered = _dedupe_platform_items(matched + action_items)
        return ordered[:5]

    if re.search(r"\b(subscription|subscriptions|plan|plans|billing|renew|upgrade|downgrade|cancel subscription|payment option)\b", context):
        return _dedupe_platform_items([it for it in items if type_match(it, {'subscription', 'subscription_plan', 'store'})] + action_items)[:5]
    if re.search(r"\b(affiliate|affiliates|referral|referrals|commission|commissions|payout|payouts)\b", context):
        return _dedupe_platform_items([it for it in items if type_match(it, {'affiliate'})] + action_items)[:5]
    if re.search(r"\b(order|orders|track|delivery)\b", context):
        return _dedupe_platform_items([it for it in items if type_match(it, {'order'})] + action_items)[:5]
    if re.search(r"\b(cart|checkout)\b", context):
        return _dedupe_platform_items([it for it in items if type_match(it, {'cart', 'cart_item'})] + action_items)[:5]
    if re.search(r"\b(store|stores|seller|shop)\b", context):
        return _dedupe_platform_items([it for it in items if type_match(it, {'store'})] + action_items)[:5]
    if re.search(r"\b(arrival|arrivals|listing|listings|item|items|featured|product|products|favorites|recent)\b", context):
        return _dedupe_platform_items([it for it in items if type_match(it, {'listing', 'favorite'})] + action_items)[:5]
    return _dedupe_platform_items(action_items)[:5]


def _extract_action_suggestions_from_text(text):
    raw = str(text or '').strip()
    if not raw:
        return raw, []
    blocks = [b.strip() for b in re.split(r"\n\s*\n", raw) if b.strip()]
    if not blocks:
        return raw, []

    action_items = []
    kept_blocks = list(blocks)
    tail = blocks[-1]
    looks_action_like = bool(re.match(r"^(would you like|do you want|would you prefer|you can|next step|next steps?)\b", tail, re.I))
    if looks_action_like:
        line = re.sub(r"\s+", " ", tail).strip()
        url = None
        low = line.lower()
        title = _compact_action_suggestion_title(line)
        if 'subscription' in low or 'plan' in low or 'billing' in low:
            url = '/storefront/dashboard/'
        elif 'cart' in low or 'checkout' in low:
            url = '/listings/cart/'
        elif 'order' in low:
            url = '/listings/orders/'
        elif 'affiliate' in low or 'referral' in low or 'commission' in low:
            url = '/affiliates/'
        elif 'dashboard' in low or 'seller' in low:
            url = '/storefront/dashboard/'
        action_items.append({
            'type': 'action_suggestion',
            'id': f'action_{abs(hash(line)) % 1000000}',
            'title': title,
            'reason': 'Suggested action.',
            'url': url,
        })
        kept_blocks = blocks[:-1]

    cleaned = '\n\n'.join(kept_blocks).strip() or raw
    return cleaned, action_items


def _attach_suggestion_reasons(prompt, text, items):
    context = f"{prompt or ''} {text or ''}".lower()
    out = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        item = dict(it)
        if item.get('reason') or item.get('suggestion_reason'):
            out.append(item)
            continue
        t = item.get('type')
        if t in {'listing', 'favorite', 'cart_item'}:
            if re.search(r"\b(stock|inventory|worth|value)\b", context):
                stock = item.get('stock')
                if stock is not None:
                    item['reason'] = f"Relevant listing (stock: {stock})."
                else:
                    item['reason'] = 'Relevant listing.'
            else:
                item['reason'] = 'Relevant listing.'
        elif t == 'store':
            item['reason'] = 'Relevant store.'
        elif t == 'order':
            item['reason'] = 'Relevant order.'
        elif t in {'subscription', 'subscription_plan'}:
            item['reason'] = 'Subscription option.'
        elif t == 'affiliate':
            item['reason'] = 'Affiliate resource.'
        elif t == 'action_suggestion':
            item['reason'] = item.get('reason') or 'Suggested action.'
        out.append(item)
    return out


def _finalize_assistant_response(prompt, text, items):
    text_clean, action_items = _extract_action_suggestions_from_text(text)
    combined_items = list(items or []) + action_items
    filtered = _filter_platform_items_for_prompt(prompt, text_clean, combined_items)
    enriched = _attach_suggestion_reasons(prompt, text_clean, filtered)
    enriched = _normalize_platform_item_urls(enriched)
    return {
        'text': text_clean or '',
        'platform_items': enriched,
    }


def _format_user_identity_label(user):
    if not user:
        return 'your account'
    full_name = ''
    username = ''
    try:
        full_name = (user.get_full_name() or '').strip()
    except Exception:
        full_name = ''
    try:
        username = (getattr(user, 'username', '') or '').strip()
    except Exception:
        username = ''
    if full_name and username and full_name.lower() != username.lower():
        return f"{full_name} (@{username})"
    return full_name or username or 'your account'


def _replace_account_placeholders(text, user=None):
    s = str(text or '').strip()
    if not s:
        return s
    identity = _format_user_identity_label(user)
    tokens = (
        '[User Name/Username]',
        '[UserName/Username]',
        '[Username]',
        '[username]',
        '{username}',
        '<username>',
        'User Name/Username',
    )
    for t in tokens:
        s = s.replace(t, identity)
    s = re.sub(r"\[\s*user\s*name\s*/\s*username\s*\]", identity, s, flags=re.I)
    s = re.sub(r"\buser\s*name\s*/\s*username\b", identity, s, flags=re.I)
    return s


def _compact_action_suggestion_title(line):
    raw = re.sub(r"\s+", " ", str(line or "")).strip().rstrip("?")
    low = raw.lower()
    if any(k in low for k in ("subscription", "plan", "billing", "renew", "upgrade", "downgrade")):
        return "Review subscription options"
    if any(k in low for k in ("add", "cart", "checkout", "buy", "purchase")):
        return "Go to cart and checkout"
    if any(k in low for k in ("order", "track", "delivery")):
        return "Check your orders"
    if any(k in low for k in ("store", "shop", "seller")):
        return "Open relevant stores"
    if any(k in low for k in ("listing", "item", "product", "view")):
        return "View matching listings"
    cleaned = re.sub(r"^(would you like me to|would you like|do you want to|would you prefer to|you can)\s+", "", raw, flags=re.I).strip()
    if not cleaned:
        return "Suggested next action"
    return cleaned[:64]


def _normalize_internal_url(url):
    raw = str(url or '').strip()
    if not raw:
        return None
    if raw == '/cart/':
        return '/listings/cart/'
    if raw == '/checkout/':
        return '/listings/checkout/'
    if raw == '/orders/':
        return '/listings/orders/'
    if raw.startswith('/'):
        return raw
    return None


def _url_path_exists(url):
    try:
        path = urlsplit(url or '').path
        if not path or not path.startswith('/'):
            return False
        resolve(path)
        return True
    except Resolver404:
        return False
    except Exception:
        return False


def _normalize_platform_item_urls(items):
    out = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        item = dict(it)
        item['url'] = _normalize_internal_url(item.get('url'))
        if item.get('url') and not _url_path_exists(item.get('url')):
            item['url'] = None
        out.append(item)
    return out


def _get_assistant_gemini_model():
    return (
        getattr(settings, 'BAYSOKO_ASSISTANT_GEMINI_MODEL', None)
        or os.environ.get('BAYSOKO_ASSISTANT_GEMINI_MODEL')
        or getattr(settings, 'GEMINI_MODEL', None)
        or os.environ.get('GEMINI_MODEL', 'gemini-1.5-flash')
    )


def _get_assistant_gemini_models():
    primary = _get_assistant_gemini_model()
    models = []
    if primary:
        models.append(primary)
    try:
        extra_env = os.environ.get('BAYSOKO_ASSISTANT_GEMINI_CANDIDATES', '')
        if extra_env:
            for m in [x.strip() for x in extra_env.split(',') if x.strip()]:
                if m not in models:
                    models.append(m)
    except Exception:
        pass
    try:
        for m in (getattr(settings, 'BAYSOKO_ASSISTANT_GEMINI_CANDIDATES', None) or []):
            if m and m not in models:
                models.append(m)
    except Exception:
        pass
    if not models:
        models = ['gemini-1.5-flash']
    return models


def _build_retrieval_context(retrieval_text=None, retrieval_items=None):
    lines = []
    if retrieval_text:
        lines.append(f"Retriever summary: {str(retrieval_text).strip()}")
    for it in (retrieval_items or []):
        if not isinstance(it, dict):
            continue
        t = it.get('type') or 'item'
        name = it.get('title') or it.get('name') or it.get('store_name') or f"{t} #{it.get('id')}"
        price = it.get('price')
        reason = it.get('reason') or it.get('suggestion_reason')
        parts = [f"- {t}: {name}"]
        if price not in (None, ''):
            parts.append(f"price={price}")
        if reason:
            parts.append(f"reason={reason}")
        url = it.get('url')
        if url:
            parts.append(f"url={url}")
        lines.append(" | ".join(parts))
    return "\n".join(lines).strip()


def _build_gemini_final_prompt(base_prompt, user_prompt, retrieval_text=None, retrieval_items=None):
    retrieval_context = _build_retrieval_context(retrieval_text=retrieval_text, retrieval_items=retrieval_items)
    extra = (
        "\n\nFinal response policy:\n"
        "- Always provide the final answer directly to the user.\n"
        "- Use Baysoko context and retrieved data accurately; do not fabricate facts.\n"
        "- Keep response concise, mature, and action-oriented.\n"
        "- Verify the answer matches the user prompt and retrieved Baysoko data before finalizing.\n"
        "- Avoid generic filler or disclaimers unrelated to Baysoko.\n"
        "- For account-specific questions, answer strictly from the currently signed-in user's data.\n"
        "- Interpret first-person references ('I', 'my', 'me') as the currently signed-in user unless prompt is clearly general.\n"
        "- If there are recommendations, phrase them naturally so UI suggestions can stay relevant.\n"
    )
    if retrieval_context:
        extra += f"\nRetrieved Baysoko data:\n{retrieval_context}\n"
    extra += f"\nUser prompt: {user_prompt}\n"
    return f"{base_prompt}{extra}"


def _get_feedback_profile(user_id=None):
    if not user_id:
        return {}
    try:
        return cache.get(f'agent_feedback_profile:{user_id}') or {}
    except Exception:
        return {}


def _build_feedback_adaptation_notes(user_id=None, context=None):
    notes = []
    profile = _get_feedback_profile(user_id=user_id)
    if profile:
        dislikes = int(profile.get('dislike', 0) or 0)
        likes = int(profile.get('like', 0) or 0)
        if dislikes > likes:
            notes.append("Recent user feedback shows dissatisfaction: prioritize precision and direct task completion.")
        last_bad = str(profile.get('last_disliked_prompt') or '').strip()
        if last_bad:
            notes.append(f"Avoid repeating the style from the previously disliked response context: '{last_bad[:160]}'.")
    try:
        user_msgs = []
        if isinstance(context, list):
            for h in context[-20:]:
                if isinstance(h, dict) and str(h.get('role') or '').lower() == 'user':
                    user_msgs.append(str(h.get('content') or '').lower())
        if any(re.search(r"\b(wrong|incorrect|not what i asked|irrelevant|generic|not helpful|bad answer)\b", m) for m in user_msgs):
            notes.append("User indicated prior answers were wrong/irrelevant: answer strictly with grounded Baysoko facts.")
    except Exception:
        pass
    return "\n".join(notes).strip()


def _looks_generic_response(text):
    low = str(text or '').strip().lower()
    if not low:
        return True
    generic_patterns = (
        r"\bas an ai\b",
        r"\bi do not have (access|visibility)\b",
        r"\bi can't access\b",
        r"\bit depends\b",
        r"\bgenerally speaking\b",
        r"\bthis request initiates the process\b",
        r"\bstreamlined path toward checkout\b",
    )
    return any(re.search(p, low) for p in generic_patterns)


def _contradicts_retrieval(model_text, retrieval_text=None, retrieval_items=None):
    if not model_text:
        return True
    low = str(model_text).lower()
    has_items = bool(retrieval_items)
    if has_items and re.search(r"\b(no|not|none)\b.{0,24}\b(found|available|results|listing|store|item|match)\b", low):
        return True
    if retrieval_text and _looks_generic_response(model_text):
        return True
    return False


def _generate_gemini_text(prompt_text):
    prompt_text = str(prompt_text or '')
    models = _get_assistant_gemini_models()
    try:
        from google import genai
        client = genai.Client()
        for model in models:
            try:
                resp = client.models.generate_content(model=model, contents=prompt_text)
                text = getattr(resp, 'text', None) or getattr(resp, 'response', None) or str(resp)
                if text:
                    return str(text).strip()
            except Exception:
                continue
    except Exception:
        logger.debug('Gemini client final-response generation failed; trying REST fallback', exc_info=True)

    try:
        api_key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY') or getattr(settings, 'GEMINI_API_KEY', None)
        if not api_key:
            return None
        headers = {'x-goog-api-key': api_key, 'Content-Type': 'application/json'}
        payload = {'contents': [{'parts': [{'text': prompt_text}]}]}
        for model in models:
            url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'
            max_attempts = 2
            for attempt in range(1, max_attempts + 1):
                try:
                    r = requests.post(url, json=payload, headers=headers, timeout=30)
                    r.raise_for_status()
                    j = r.json()
                    text = str(j['candidates'][0]['content']['parts'][0]['text']).strip()
                    if text:
                        return text
                except RequestException as rexc:
                    status = rexc.response.status_code if getattr(rexc, 'response', None) is not None else None
                    if status in {503, 429} and attempt < max_attempts:
                        _time.sleep((2 ** attempt) + random.uniform(0, 0.5))
                        continue
                    break
    except Exception:
        logger.debug('Gemini REST final-response generation failed', exc_info=True)
        return None


def _respond_with_gemini_final(user_prompt, base_prompt, retrieval_text=None, retrieval_items=None, fallback_text=None):
    final_prompt = _build_gemini_final_prompt(
        base_prompt=base_prompt,
        user_prompt=user_prompt,
        retrieval_text=retrieval_text,
        retrieval_items=retrieval_items,
    )
    model_text = _generate_gemini_text(final_prompt)
    if _contradicts_retrieval(model_text, retrieval_text=retrieval_text, retrieval_items=retrieval_items):
        strict_prompt = (
            final_prompt
            + "\n\nCorrection pass:\n"
            + "- Your previous draft was generic or inconsistent with retrieved Baysoko data.\n"
            + "- Regenerate a precise final answer grounded only in the provided data and user intent.\n"
            + "- Keep it concise and actionable.\n"
        )
        strict_text = _generate_gemini_text(strict_prompt)
        if strict_text and not _contradicts_retrieval(strict_text, retrieval_text=retrieval_text, retrieval_items=retrieval_items):
            model_text = strict_text
    text = model_text or fallback_text or 'Assistant is temporarily unavailable.'
    if _contradicts_retrieval(text, retrieval_text=retrieval_text, retrieval_items=retrieval_items):
        text = fallback_text or retrieval_text or text
    return _finalize_assistant_response(user_prompt, text, retrieval_items or [])


def _extract_listing_from_history(context):
    """Try to resolve the most recently suggested listing from conversation history."""
    if not isinstance(context, list):
        return None
    for h in reversed(context):
        if not isinstance(h, dict):
            continue
        role = str(h.get('role') or '').lower()
        if role not in {'assistant', 'bot'}:
            continue
        content = h.get('content')
        payload = None
        if isinstance(content, dict):
            payload = content
        elif isinstance(content, str):
            payload = _extract_json(content) or parse_json_like(content)
        if not isinstance(payload, dict):
            continue
        items = payload.get('platform_items') or payload.get('items') or []
        if not isinstance(items, list):
            continue
        for it in items:
            if isinstance(it, dict) and it.get('type') in {'listing', 'favorite', 'cart_item'} and it.get('id'):
                return it
    return None


def parse_json_like(text):
    try:
        return json.loads(text)
    except Exception:
        return None


def _is_affirmative_reply(text):
    low = str(text or '').strip().lower()
    return bool(re.search(r"\b(yes|yeah|yep|sure|okay|ok|confirm|do it|go ahead|proceed)\b", low))


def _is_negative_reply(text):
    low = str(text or '').strip().lower()
    return bool(re.search(r"\b(no|nah|cancel|stop|dont|don't|not now|leave it)\b", low))


def _extract_previous_cart_request_from_context(context, current_prompt=''):
    if not isinstance(context, list):
        return None
    current_norm = str(current_prompt or '').strip().lower()
    user_msgs = []
    for h in context:
        if not isinstance(h, dict):
            continue
        if str(h.get('role') or '').lower() != 'user':
            continue
        c = str(h.get('content') or '').strip()
        if not c:
            continue
        user_msgs.append(c)
    for c in reversed(user_msgs):
        if c.strip().lower() == current_norm:
            continue
        low = c.lower()
        if re.search(r"\b(add|put|remove|delete|take out)\b", low):
            return c
    return None


def _try_resolve_listing_for_cart(prompt, context=None):
    """Resolve listing to add to cart from prompt text or recent context."""
    try:
        from listings.models import Listing
        raw = str(prompt or '').strip()
        pronouns = {'it', 'this', 'that'}

        def _clean_query(q):
            q = str(q or '').strip()
            q = re.sub(r"^\d+\s+(?:x\s+|of\s+)?", "", q, flags=re.I).strip()
            q = re.sub(r"^(?:my\s+)?(?:cart\s+)?", "", q, flags=re.I).strip()
            q = re.sub(r"\s+(?:to\s+cart|in\s+cart)$", "", q, flags=re.I).strip()
            q = re.sub(r"\s+(?:please|now)$", "", q, flags=re.I).strip()
            q = re.sub(r"\s+", " ", q)
            return q

        def _score_candidate(query_text, listing_obj):
            q_low = str(query_text or '').strip().lower()
            title_low = str(getattr(listing_obj, 'title', '') or '').strip().lower()
            if not q_low or not title_low:
                return 0.0
            if title_low == q_low:
                return 1000.0
            q_tokens = {t for t in re.findall(r"[a-z0-9]+", q_low) if len(t) > 1}
            t_tokens = {t for t in re.findall(r"[a-z0-9]+", title_low) if len(t) > 1}
            overlap = len(q_tokens & t_tokens)
            ratio = difflib.SequenceMatcher(a=q_low, b=title_low).ratio()
            starts = 1.0 if title_low.startswith(q_low) else 0.0
            return (overlap * 10.0) + (ratio * 6.0) + (starts * 5.0)

        def _resolve_by_query(query_text):
            q = _clean_query(query_text)
            if not q:
                return None
            q_low = q.lower()
            if q_low in pronouns:
                return None
            qs = Listing.objects.filter(is_active=True, is_sold=False)
            exact = qs.filter(title__iexact=q).order_by('-date_created').first()
            if exact:
                return exact
            starts = qs.filter(title__istartswith=q).order_by('-date_created')[:10]
            if starts:
                ranked = sorted(starts, key=lambda l: (_score_candidate(q, l), getattr(l, 'date_created', None)), reverse=True)
                return ranked[0]
            contains = list(qs.filter(title__icontains=q).order_by('-date_created')[:25])
            if not contains:
                return None
            ranked = sorted(contains, key=lambda l: (_score_candidate(q, l), getattr(l, 'date_created', None)), reverse=True)
            if not ranked:
                return None
            top = ranked[0]
            top_score = _score_candidate(q, top)
            second_score = _score_candidate(q, ranked[1]) if len(ranked) > 1 else -1.0
            # Avoid auto-picking when the match is weak or ambiguous.
            if top_score < 6.0:
                return None
            if second_score >= 0 and (top_score - second_score) < 1.5:
                return None
            return top

        query = None
        m = re.search(r"add\s+(?:the\s+)?(.+?)\s+to\s+(?:my\s+)?cart", raw, re.I)
        if m:
            query = m.group(1).strip()
        if not query:
            m2 = re.search(r"add\s+(?:the\s+)?(?:\d+\s+)?(?:x\s+)?(?:items?\s+of\s+|units?\s+of\s+|pieces?\s+of\s+)?(.+?)(?:\s+please|\s+now|[?.!,]|$)", raw, re.I)
            if m2:
                query = m2.group(1).strip()
        resolved = _resolve_by_query(query) if query else None
        if resolved:
            return resolved

        # Fallback to recent context only for pronouns / unspecified item references.
        low = raw.lower()
        if (not query) or (_clean_query(query).lower() in pronouns) or re.search(r"\b(add|put)\s+(it|this|that)\b", low):
            recent = _extract_listing_from_history(context)
            if recent and recent.get('id'):
                l = Listing.objects.filter(pk=recent.get('id'), is_active=True, is_sold=False).first()
                if l:
                    return l
    except Exception:
        logger.debug('_try_resolve_listing_for_cart failed', exc_info=True)
    return None


def _extract_quantity_from_prompt(prompt, default=1):
    try:
        # Prefer explicit quantity tied to item/cart verbs, e.g. "add 5 items of bees wax".
        m = re.search(r"\b(?:add|put|remove|delete|take out)\s+(\d+)\b", str(prompt or ''), flags=re.I)
        if m:
            q = int(m.group(1))
            return max(1, min(q, 999))
        m = re.search(r"\b(\d+)\b", str(prompt or ''))
        if m:
            q = int(m.group(1))
            return max(1, min(q, 999))
    except Exception:
        pass
    return int(default or 1)


def _try_resolve_cart_item_for_removal(prompt, user_id=None, context=None):
    try:
        from listings.models import Cart
        if not user_id:
            return None, None
        cart = Cart.objects.filter(user_id=user_id).first()
        if not cart:
            return None, None
        q = None
        m = re.search(r"(?:remove|delete|take out)\s+(?:the\s+)?(.+?)\s+(?:from\s+)?(?:my\s+)?cart", str(prompt or ''), re.I)
        if m:
            q = m.group(1).strip()
        if q and q.lower() not in {'it', 'this', 'that', 'item'}:
            item = cart.items.select_related('listing').filter(
                Q(listing__title__icontains=q) | Q(listing__description__icontains=q)
            ).order_by('-id').first()
            if item:
                return cart, item
        recent = _extract_listing_from_history(context)
        if recent and recent.get('id'):
            item = cart.items.select_related('listing').filter(listing_id=recent.get('id')).first()
            if item:
                return cart, item
        # If cart has exactly one item, use it to avoid unnecessary clarification.
        only = cart.items.select_related('listing').all()[:2]
        if len(only) == 1:
            return cart, only[0]
    except Exception:
        logger.debug('_try_resolve_cart_item_for_removal failed', exc_info=True)
    return None, None


def _handle_cart_action_intent(prompt, context=None, user_id=None):
    original_prompt = str(prompt or '').strip()
    low = original_prompt.lower()
    affirmative_reply = _is_affirmative_reply(original_prompt)
    negative_reply = _is_negative_reply(original_prompt)
    resolved_from_context = False

    if (affirmative_reply or negative_reply) and not re.search(r"\b(add|put|remove|delete|take out)\b", low):
        prior = _extract_previous_cart_request_from_context(context, current_prompt=original_prompt)
        if prior:
            if negative_reply:
                return {'text': 'Okay, I will not change your cart.', 'platform_items': []}
            prompt = prior
            low = str(prior).strip().lower()
            resolved_from_context = True

    listing_hint = bool(re.search(r"\b(add|put)\b.*\b(item|items|unit|units|piece|pieces|of)\b", low))
    add_intent = bool(re.search(r"\b(add|put)\b.*\b(cart)\b", low)) or listing_hint
    remove_intent = bool(re.search(r"\b(remove|delete|take out)\b.*\b(cart)\b", low))
    if (add_intent or remove_intent) and affirmative_reply:
        resolved_from_context = True
    if not (add_intent or remove_intent):
        return None
    if not user_id:
        return {'text': 'Please sign in to manage your cart.', 'platform_items': []}
    try:
        from listings.models import Cart, CartItem
        cart, _ = Cart.objects.get_or_create(user_id=user_id)

        if add_intent:
            listing = _try_resolve_listing_for_cart(prompt, context=context)
            if not listing:
                return {'text': 'Tell me which item to add, or open a listing and ask again.', 'platform_items': []}
            stock = int(getattr(listing, 'stock', 0) or 0)
            if stock <= 0:
                return {'text': f'{listing.title} is currently out of stock.', 'platform_items': []}
            if getattr(listing, 'seller_id', None) == user_id:
                return {'text': 'You cannot add your own listing to cart.', 'platform_items': []}

            qty = _extract_quantity_from_prompt(prompt, default=1)
            if qty > stock:
                return {'text': f'Only {stock} unit(s) are available for {listing.title}.', 'platform_items': []}
            if not resolved_from_context:
                return {
                    'text': f"Please confirm: add {qty} unit(s) of {listing.title} to your cart?",
                    'platform_items': [
                        {
                            'type': 'action_suggestion',
                            'id': f'confirm_add_{listing.id}_{qty}',
                            'title': f'Confirm add {qty} {listing.title}',
                            'url': '/listings/cart/',
                            'reason': 'Confirm cart update.',
                        },
                        {
                            'type': 'action_suggestion',
                            'id': 'cancel_cart_change',
                            'title': 'Cancel cart update',
                            'url': '/listings/cart/',
                            'reason': 'No change will be made unless confirmed.',
                        },
                    ],
                }
            cart_item, created = CartItem.objects.get_or_create(cart=cart, listing=listing, defaults={'quantity': qty})
            if not created:
                next_qty = int(cart_item.quantity or 0) + qty
                if next_qty > stock:
                    return {'text': f'Only {stock} unit(s) are available for {listing.title}.', 'platform_items': []}
                cart_item.quantity = next_qty
                cart_item.save(update_fields=['quantity'])
            total_items = int(sum(int(ci.quantity or 0) for ci in cart.items.all()))
            total_price = float(cart.get_total_price() or 0)
            item = {
                'type': 'cart_item',
                'id': listing.id,
                'cart_item_id': cart_item.id,
                'title': listing.title,
                'price': str(listing.price),
                'quantity': int(cart_item.quantity or 1),
                'url': listing.get_absolute_url(),
                'image': listing.image.url if listing.image else None,
                'cart_item_count': total_items,
                'cart_total': total_price,
                'reason': 'Added to cart.',
            }
            text = f"Added {listing.title} to your cart. You now have {total_items} item(s) in cart."
            return {'text': text, 'platform_items': [item]}

        # remove intent
        cart, cart_item = _try_resolve_cart_item_for_removal(prompt, user_id=user_id, context=context)
        if not cart or not cart_item:
            return {'text': 'Tell me which cart item to remove.', 'platform_items': []}
        listing = cart_item.listing
        remove_qty = _extract_quantity_from_prompt(prompt, default=int(cart_item.quantity or 1))
        if not resolved_from_context:
            return {
                'text': f"Please confirm: remove {remove_qty} unit(s) of {listing.title} from your cart?",
                'platform_items': [
                    {
                        'type': 'action_suggestion',
                        'id': f'confirm_remove_{listing.id}_{remove_qty}',
                        'title': f'Confirm remove {remove_qty} {listing.title}',
                        'url': '/listings/cart/',
                        'reason': 'Confirm cart update.',
                    },
                    {
                        'type': 'action_suggestion',
                        'id': 'cancel_cart_change',
                        'title': 'Cancel cart update',
                        'url': '/listings/cart/',
                        'reason': 'No change will be made unless confirmed.',
                    },
                ],
            }
        if remove_qty >= int(cart_item.quantity or 1):
            cart_item.delete()
            remaining_qty = 0
            removed_mode = 'removed'
        else:
            cart_item.quantity = max(0, int(cart_item.quantity or 0) - remove_qty)
            cart_item.save(update_fields=['quantity'])
            remaining_qty = int(cart_item.quantity or 0)
            removed_mode = 'updated'
        total_items = int(sum(int(ci.quantity or 0) for ci in cart.items.all()))
        total_price = float(cart.get_total_price() or 0)
        item = {
            'type': 'cart_item',
            'id': listing.id,
            'cart_item_id': getattr(cart_item, 'id', None),
            'title': listing.title,
            'price': str(listing.price),
            'quantity': remaining_qty,
            'url': listing.get_absolute_url(),
            'image': listing.image.url if listing.image else None,
            'cart_item_count': total_items,
            'cart_total': total_price,
            'reason': 'Removed from cart.' if removed_mode == 'removed' else 'Cart quantity reduced.',
        }
        if removed_mode == 'removed':
            text = f"Removed {listing.title} from your cart. You now have {total_items} item(s) in cart."
        else:
            text = f"Reduced {listing.title} in your cart to {remaining_qty}. You now have {total_items} item(s) in cart."
        return {'text': text, 'platform_items': [item]}
    except Exception:
        logger.debug('_handle_cart_action_intent failed', exc_info=True)
        return {'text': 'I could not update your cart right now. Please try again.', 'platform_items': []}

def assistant_reply(prompt: str, context=None, user_id=None):
    """
    General Baysoko Assistant reply. Returns a dict with:
        - text: plain text answer
        - platform_items: list of rich objects (listings, stores, orders, subscriptions)
    """
    sys_prompt = (
        "You are the Baysoko Assistant. Help users with buying, selling, creating stores, "
        "listings, editing, deleting, subscriptions, orders, favorites, affiliate program, seller dashboard, and general platform tasks. "
        "Answer concisely and provide actionable steps; when appropriate, offer next actions (e.g., 'Add to cart', 'Create listing'). "
        "When user account context is available, answer account-specific questions strictly using the signed-in user's data only. "
        "Interpret first-person references (I/my/me) as the currently signed-in user unless the prompt is clearly general. "
        "If the user asks about anything outside the platform, politely redirect to platform‑related topics."
    )
    prompt_text = str(prompt or '')
    low_prompt = prompt_text.strip().lower()
    full_prompt = sys_prompt + "\n\nUser: " + prompt_text
    platform_items = []
    user = None

    # 1. Gather user‑specific platform data (if logged in)
    try:
        if user_id is not None:
            from django.contrib.auth import get_user_model
            from listings.models import Listing, Favorite, RecentlyViewed, Cart, Order, OrderItem
            from storefront.models import Store, Subscription
            User = get_user_model()
            user = User.objects.filter(pk=user_id).first()
            platform_lines = []
            fav_limit = getattr(settings, 'ASSISTANT_FAVORITES_LIMIT', 3)
            rec_limit = getattr(settings, 'ASSISTANT_RECENTLY_VIEWED_LIMIT', 3)
            cart_limit = getattr(settings, 'ASSISTANT_CART_LIMIT', 5)
            order_limit = getattr(settings, 'ASSISTANT_RECENT_ORDERS_LIMIT', 2)
            store_limit = getattr(settings, 'ASSISTANT_STORES_LIMIT', 3)
            max_prompt_items = getattr(settings, 'ASSISTANT_PROMPT_MAX_ITEMS', 8)

            if user:
                # Signed-in identity snapshot for strict account-grounded responses.
                display_identity = _format_user_identity_label(user)
                platform_lines.append('Signed-in user profile:')
                platform_lines.append(f"- identity: {display_identity}")
                platform_lines.append(f"- username: {getattr(user, 'username', '')}")
                if getattr(user, 'email', None):
                    platform_lines.append(f"- email: {user.email}")
                if getattr(user, 'first_name', None):
                    platform_lines.append(f"- first_name: {user.first_name}")
                if getattr(user, 'last_name', None):
                    platform_lines.append(f"- last_name: {user.last_name}")
                # Favorites
                favs = Favorite.objects.filter(user=user).select_related('listing')[:fav_limit]
                if favs:
                    platform_lines.append('User favorites:')
                    for f in favs:
                        l = f.listing
                        platform_items.append({
                            'type': 'listing',
                            'id': l.id,
                            'title': l.title,
                            'price': str(l.price),
                            'url': l.get_absolute_url(),
                            'image': l.image.url if l.image else None,
                            'location': l.location,
                            'seller': l.seller.username if l.seller else None,
                        })
                        platform_lines.append(f"- {l.title} | {l.price} | {l.get_absolute_url()}")
                # Cart
                cart = Cart.objects.filter(user=user).first()
                if cart:
                    cart_items = cart.items.select_related('listing')[:cart_limit]
                    if cart_items:
                        platform_lines.append('Cart contents:')
                        for ci in cart_items:
                            l = ci.listing
                            platform_items.append({
                                'type': 'cart_item',
                                'id': l.id,
                                'title': l.title,
                                'price': str(l.price),
                                'url': l.get_absolute_url(),
                                'quantity': ci.quantity,
                                'image': l.image.url if l.image else None,
                            })
                            platform_lines.append(f"- {l.title} x{ci.quantity} | {l.price}")
                # Recent orders
                recent_orders = Order.objects.filter(user=user).order_by('-id')[:order_limit]
                if recent_orders:
                    platform_lines.append('Recent orders:')
                    for o in recent_orders:
                        items = o.order_items.select_related('listing')[:3]
                        item_str = ', '.join([f"{it.listing.title} x{it.quantity}" for it in items])
                        platform_items.append({
                            'type': 'order',
                            'id': o.id,
                            'status': o.status,
                            'total': str(o.total_price),
                            'items_preview': item_str,
                            'url': o.get_absolute_url() if hasattr(o, 'get_absolute_url') else None,
                        })
                        platform_lines.append(f"- Order #{o.id} | {o.status} | {o.total_price} | {item_str}")
                # Recently viewed
                rec = RecentlyViewed.objects.filter(user=user).select_related('listing').order_by('-viewed_at')[:rec_limit]
                if rec:
                    platform_lines.append('Recently viewed:')
                    for r in rec:
                        l = r.listing
                        platform_items.append({
                            'type': 'listing',
                            'id': l.id,
                            'title': l.title,
                            'price': str(l.price),
                            'url': l.get_absolute_url(),
                            'image': l.image.url if l.image else None,
                        })
                        platform_lines.append(f"- {l.title} | {l.price} | {l.get_absolute_url()}")
                # User's stores
                stores = Store.objects.filter(owner=user)[:store_limit]
                if stores:
                    platform_lines.append('Your stores:')
                    for s in stores:
                        platform_items.append({
                            'type': 'store',
                            'id': s.id,
                            'name': s.name,
                            'slug': s.slug,
                            'url': f"/store/{s.slug}/",
                            'image': s.logo.url if s.logo else None,
                        })
                        platform_lines.append(f"- {s.name} | {s.get_absolute_url()}")
                # User subscriptions (strictly user-scoped)
                subs = Subscription.objects.filter(store__owner=user).select_related('store').order_by('-created_at')[:store_limit]
                if subs:
                    platform_lines.append('Your subscriptions:')
                    for sub in subs:
                        sub_url = f"/storefront/dashboard/store/{sub.store.slug}/subscription/manage/"
                        platform_items.append({
                            'type': 'subscription',
                            'id': sub.id,
                            'store_id': sub.store.id,
                            'store_name': sub.store.name,
                            'plan': sub.plan,
                            'status': sub.status,
                            'price': str(getattr(sub, 'amount', '')),
                            'expires': sub.current_period_end.isoformat() if sub.current_period_end else None,
                            'url': sub_url,
                        })
                        platform_lines.append(
                            f"- {sub.store.name} | plan={sub.plan} | status={sub.status} | manage={sub_url}"
                        )
                # Affiliate snapshot
                try:
                    from affiliates.models import AffiliateProfile, AffiliateClick, AffiliateAttribution, AffiliateCommission, AffiliatePayout
                    affiliate_profile = AffiliateProfile.objects.filter(user=user).first()
                    if affiliate_profile:
                        link_base = getattr(settings, 'SITE_URL', '').rstrip('/')
                        query_key = getattr(settings, 'AFFILIATE_QUERY_PARAM', 'aid')
                        affiliate_link = f"{link_base}/?{query_key}={affiliate_profile.code}" if link_base else f"/?{query_key}={affiliate_profile.code}"
                        clicks = AffiliateClick.objects.filter(affiliate=affiliate_profile).count()
                        referrals = AffiliateAttribution.objects.filter(affiliate=affiliate_profile).count()
                        commissions = AffiliateCommission.objects.filter(affiliate=affiliate_profile)
                        total_commissions = commissions.aggregate(total=Sum('amount')).get('total') or 0
                        paid_commissions = commissions.filter(status='paid').aggregate(total=Sum('amount')).get('total') or 0
                        pending_commissions = commissions.filter(status='pending').aggregate(total=Sum('amount')).get('total') or 0
                        platform_lines.append('Affiliate profile:')
                        platform_lines.append(f"- code: {affiliate_profile.code} | active={affiliate_profile.is_active}")
                        platform_lines.append(f"- link: {affiliate_link}")
                        platform_lines.append(
                            f"- clicks={clicks} | referrals={referrals} | total_commissions={total_commissions} | paid={paid_commissions} | pending={pending_commissions}"
                        )
                        platform_items.append({
                            'type': 'affiliate',
                            'id': affiliate_profile.id,
                            'title': 'Affiliate dashboard',
                            'code': affiliate_profile.code,
                            'link': affiliate_link,
                            'url': '/affiliates/',
                        })
                except Exception:
                    logger.debug('Affiliate snapshot failed', exc_info=True)
                # Seller dashboard snapshot (listings + sales)
                try:
                    listings_qs = Listing.objects.filter(Q(seller=user) | Q(store__owner=user))
                    if listings_qs.exists():
                        total_listings = listings_qs.count()
                        active_listings = listings_qs.filter(is_active=True, is_sold=False).count()
                        inactive_listings = listings_qs.filter(is_active=False).count()
                        sold_listings = listings_qs.filter(is_sold=True).count()
                        order_items = OrderItem.objects.filter(Q(listing__seller=user) | Q(listing__store__owner=user))
                        seller_orders = order_items.values('order_id').distinct().count()
                        revenue = order_items.aggregate(
                            total=Sum(F('price') * F('quantity'), output_field=DecimalField(max_digits=12, decimal_places=2))
                        ).get('total') or 0
                        platform_lines.append('Seller dashboard snapshot:')
                        platform_lines.append(
                            f"- listings total={total_listings} | active={active_listings} | inactive={inactive_listings} | sold={sold_listings}"
                        )
                        platform_lines.append(f"- seller orders={seller_orders} | revenue={revenue}")
                except Exception:
                    logger.debug('Seller dashboard snapshot failed', exc_info=True)
            # Global lowest priced item
            should_include_market_listing = bool(re.search(r"\b(arrival|arrivals|new|featured|listing|listings|item|items|product|products|cheapest|lowest)\b", low_prompt))
            lowest = Listing.objects.filter(is_active=True, is_sold=False).order_by('price').first()
            if lowest and should_include_market_listing:
                platform_items.append({
                    'type': 'listing',
                    'id': lowest.id,
                    'title': lowest.title,
                    'price': str(lowest.price),
                    'url': lowest.get_absolute_url(),
                    'image': lowest.image.url if lowest.image else None,
                })
                platform_lines.append('Lowest priced item on platform:')
                platform_lines.append(f"- {lowest.title} | {lowest.price} | {lowest.get_absolute_url()}")
            if platform_lines:
                effective_max_prompt_items = max(int(max_prompt_items or 0), 24)
                if len(platform_lines) > effective_max_prompt_items:
                    platform_lines = platform_lines[:effective_max_prompt_items]
                base = sys_prompt + '\nPlatform data (user‑scoped):\n' + '\n'.join(platform_lines) + '\n\n'
                if context and isinstance(context, list):
                    hist = '\n'.join([(h.get('role','user')+': '+h.get('content','')) if isinstance(h, dict) else str(h) for h in context])
                    full_prompt = base + 'Conversation history:\n' + hist + '\n\nUser: ' + prompt_text
                else:
                    full_prompt = base + 'User: ' + prompt_text
    except Exception as e:
        logger.debug('Error building platform summary', exc_info=True)

    retrieval_text = None
    retrieval_items = []

    # 2. Intent-first retrieval; final user text is always model-rendered.
    cart_action_answer = _handle_cart_action_intent(prompt_text, context=context, user_id=user_id)
    if cart_action_answer:
        retrieval_text = cart_action_answer.get('text')
        retrieval_items = cart_action_answer.get('platform_items', [])

    if retrieval_text is None:
        compare_answer = _handle_listing_comparison_intent(prompt_text, user_id=user_id)
        if compare_answer:
            retrieval_text = compare_answer.get('text')
            retrieval_items = compare_answer.get('platform_items', [])

    if retrieval_text is None:
        decision_answer = _handle_decision_support_intent(prompt_text, user_id=user_id)
        if decision_answer:
            retrieval_text = decision_answer.get('text')
            retrieval_items = decision_answer.get('platform_items', [])

    if retrieval_text is None:
        store_rec_answer = _recommend_stores_for_request(prompt_text, user_id=user_id)
        if store_rec_answer:
            retrieval_text = store_rec_answer.get('text')
            retrieval_items = store_rec_answer.get('platform_items', [])

    if retrieval_text is None and user_id and re.search(r"\b(what stores are in my account|what stores do i own|stores i own|my stores|stores in my account)\b", low_prompt):
        my_stores = _query_user_stores(user_id=user_id, limit=10)
        retrieval_text = f"Found {len(my_stores)} store(s)." if my_stores else "No stores found in your account."
        retrieval_items = my_stores

    if retrieval_text is None and user_id and user and re.search(r"\b(who am i|who am i signed in as|which account (am i|i am) signed in|what account (am i|i am) signed in|my current account|current account)\b", low_prompt):
        identity = _format_user_identity_label(user)
        retrieval_text = f"You are currently signed in as {identity}."
        retrieval_items = [
            {
                'type': 'action_suggestion',
                'id': 'open-profile',
                'title': 'Open my profile',
                'url': f"/profile/{user.id}/",
                'reason': 'Review your current account details.',
            },
            {
                'type': 'action_suggestion',
                'id': 'open-orders',
                'title': 'Open my inbox',
                'url': '/chats/',
                'reason': 'Continue account support in chat.',
            },
        ]

    if retrieval_text is None and user_id and _is_followup_for_owned_listings(prompt_text, context=context):
        my_listings = _query_user_owned_listings(user_id=user_id, limit=20)
        retrieval_text = f"Found {len(my_listings)} listing(s) in your account." if my_listings else "You do not have active listings yet."
        retrieval_items = my_listings

    if retrieval_text is None:
        db_answer = _answer_from_db(prompt, user_id=user_id)
        if db_answer:
            retrieval_text = db_answer.get('text')
            retrieval_items = db_answer.get('platform_items', [])

    # 3. Quick retrieval handling.
    low = prompt.strip().lower()
    if retrieval_text is None and re.search(r"\b(store|stores|store info|store details)\b", low):
        try:
            items = _query_stores(prompt=prompt, user_id=user_id)
            retrieval_text = f"Found {len(items)} store(s)." if items else "No stores found."
            retrieval_items = items
        except Exception:
            logger.debug('Store query retrieval failed', exc_info=True)

    if retrieval_text is None and (
        re.search(r"\b(listings|find listings|show me listings|search listings|find items|show items)\b", low)
        or re.search(r"\b(find|show)\b.*\blisting|items\b", low)
    ):
        try:
            filters = _parse_listing_filters_from_text(prompt)
            items = _query_listings(filters=filters, limit=5, user_id=user_id)
            retrieval_text = f"Found {len(items)} listing(s) matching your criteria." if items else "No listings found."
            retrieval_items = items
        except Exception:
            logger.debug('Listing query retrieval failed', exc_info=True)

    if retrieval_text is None and re.search(r"\b(lowest|cheapest|cheapest item|lowest priced|cheapest price)\b", low):
        try:
            item = _get_lowest_priced_listing()
            retrieval_text = f"Lowest priced item: {item.get('title','Unnamed')} - {item.get('price','')}." if item else 'No active listings found.'
            retrieval_items = [item] if item else []
        except Exception:
            logger.debug('Lowest-priced retrieval failed', exc_info=True)

    if retrieval_text is None:
        m_owner = re.search(r"who is the owner of ([\w\s'\-]+)\??", low)
        if m_owner:
            try:
                store_name = m_owner.group(1).strip()
                stores = _query_stores(filters={'name': store_name}, limit=5, user_id=user_id)
                if stores:
                    s = stores[0]
                    owner = s.get('owner') or 'unknown'
                    retrieval_text = f"{s.get('name')} is owned by {owner}."
                    retrieval_items = [s]
                else:
                    retrieval_text = f'No store named "{store_name}" found.'
                    retrieval_items = []
            except Exception:
                logger.debug('Store owner retrieval failed', exc_info=True)

    if retrieval_text is None and re.search(r"\b(subscription|subscriptions|subscribe|cancel subscription|renew subscription|my subscription|plan|plans|billing)\b", low):
        try:
            res_text, items = _handle_subscription_intent(prompt, user_id)
            retrieval_text = res_text
            retrieval_items = items or []
        except Exception:
            logger.debug('Subscription retrieval failed', exc_info=True)

    if retrieval_text is None and re.search(r"\b(affiliate|affiliates|referral|referrals|commission|commissions|payout|payouts)\b", low):
        try:
            res_text, items = _handle_affiliate_intent(prompt, user_id)
            if res_text:
                retrieval_text = res_text
                retrieval_items = items or []
        except Exception:
            logger.debug('Affiliate retrieval failed', exc_info=True)

    if retrieval_text is None and re.search(r"\b(seller dashboard|storefront dashboard|dashboard|analytics|performance|inventory|sales overview|seller analytics)\b", low):
        try:
            res_text, items = _handle_seller_dashboard_intent(prompt, user_id)
            if res_text:
                retrieval_text = res_text
                retrieval_items = items or []
        except Exception:
            logger.debug('Seller dashboard retrieval failed', exc_info=True)

    if retrieval_text is None and re.search(r"\b(order|orders|my orders|track order)\b", low):
        try:
            res_text, items = _handle_order_intent(prompt, user_id)
            if res_text:
                retrieval_text = res_text
                retrieval_items = items or []
        except Exception:
            logger.debug('Order retrieval failed', exc_info=True)

    # 3.5 Registry-based retrieval.
    if retrieval_text is None:
        try:
            db_result = try_database_query(prompt, user_id)
            if db_result:
                retrieval_text = db_result.get('text')
                retrieval_items = db_result.get('data') or []
        except Exception:
            logger.debug('Registry retrieval failed', exc_info=True)

    if context and 'Platform data (userscoped):' not in full_prompt and 'Platform data (user-scoped):' not in full_prompt:
        try:
            if isinstance(context, list):
                hist = '\n'.join([(h.get('role', 'user') + ': ' + h.get('content', '')) if isinstance(h, dict) else str(h) for h in context])
                full_prompt = sys_prompt + '\nConversation history:\n' + hist + '\n\nUser: ' + prompt_text
            else:
                full_prompt = sys_prompt + '\nContext:\n' + str(context) + '\n\nUser: ' + prompt_text
        except Exception:
            logger.debug('Failed to append conversation context to prompt', exc_info=True)

    adaptation_notes = _build_feedback_adaptation_notes(user_id=user_id, context=context)
    if adaptation_notes:
        full_prompt = f"{full_prompt}\n\nAdaptation notes:\n{adaptation_notes}\n"

    # 4. Always route final response through Gemini.
    response = _respond_with_gemini_final(
        user_prompt=prompt_text,
        base_prompt=full_prompt,
        retrieval_text=retrieval_text,
        retrieval_items=(retrieval_items or platform_items),
        fallback_text=retrieval_text or 'Assistant is temporarily unavailable.',
    )
    if isinstance(response, dict):
        response['text'] = _replace_account_placeholders(response.get('text', ''), user=user)
    return response

# ========== QUERY HELPERS ==========
def _query_stores(prompt=None, filters=None, limit=5, user_id=None):
    """Return a list of store dicts with rich fields."""
    try:
        from storefront.models import Store, Subscription
        qs = Store.objects.all()
        low_prompt = str(prompt or '').lower()
        if user_id and re.search(r"\b(my stores|stores in my account|stores i own|what stores do i own|what stores are in my account)\b", low_prompt):
            qs = qs.filter(owner_id=user_id)
        if filters:
            name = filters.get('name')
            if name:
                qs = qs.filter(name__icontains=name)
            slug = filters.get('slug')
            if slug:
                qs = qs.filter(slug__iexact=slug)
            owner = filters.get('owner')
            if owner:
                qs = qs.filter(owner__username__icontains=owner)
            owner_id = filters.get('owner_id')
            if owner_id:
                qs = qs.filter(owner_id=owner_id)
        qs = qs.order_by('-created_at')
        stores = qs[:limit]
        out = []
        for s in stores:
            sub = Subscription.objects.filter(store=s).order_by('-created_at').first()
            status = sub.status if sub else 'none'
            out.append({
                'type': 'store',
                'id': s.id,
                'name': s.name,
                'slug': s.slug,
                'owner': getattr(s.owner, 'username', None),
                'is_premium': getattr(s, 'is_premium', False),
                'subscription_status': status,
                'url': f"/store/{s.slug}/",
                'image': s.logo.url if s.logo else None,
                'reason': 'Relevant store for your request.',
            })
        return out
    except Exception as e:
        logger.debug('_query_stores error: %s', e)
        return []

def _parse_listing_filters_from_text(text: str):
    """Extract simple listing filters from user text."""
    f = {}
    try:
        m = re.search(r"price\s*(?:<=|<|less than)\s*([0-9,\.]+)", text.lower())
        if m:
            f['price_max'] = float(m.group(1).replace(',', ''))
        m = re.search(r"price\s*(?:>=|>|more than|at least)\s*([0-9,\.]+)", text.lower())
        if m:
            f['price_min'] = float(m.group(1).replace(',', ''))
        m = re.search(r"category[:\s]+([a-zA-Z0-9\s\-]+)", text, re.I)
        if m:
            f['category'] = m.group(1).strip()
        m = re.search(r"location[:\s]+([a-zA-Z0-9\s\-]+)", text, re.I)
        if m:
            f['location'] = m.group(1).strip()
        m = re.search(r"keywords?:\s*([\w\s,]+)", text, re.I)
        if m:
            f['q'] = m.group(1).strip()
    except Exception:
        pass
    return f

def _query_listings(filters=None, limit=10, order_by='-date_created', user_id=None):
    """Return a list of listing dicts with rich fields."""
    try:
        from listings.models import Listing, Category
        qs = Listing.objects.filter(is_active=True, is_sold=False)
        if filters:
            if 'price_min' in filters:
                qs = qs.filter(price__gte=filters['price_min'])
            if 'price_max' in filters:
                qs = qs.filter(price__lte=filters['price_max'])
            if 'location' in filters:
                qs = qs.filter(location__icontains=filters['location'])
            if 'category' in filters:
                cat = Category.objects.filter(name__icontains=filters['category']).first()
                if cat:
                    qs = qs.filter(category=cat)
            if 'q' in filters:
                q = filters['q']
                qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q))
        if order_by:
            qs = qs.order_by(order_by)
        if limit:
            qs = qs[:limit]
        out = []
        for l in qs:
            out.append({
                'type': 'listing',
                'id': l.id,
                'title': l.title,
                'price': str(l.price),
                'stock': int(getattr(l, 'stock', 0) or 0),
                'url': l.get_absolute_url(),
                'location': getattr(l, 'location', ''),
                'seller': getattr(l.seller, 'username', None),
                'image': l.image.url if l.image else None,
            })
        return out
    except Exception as e:
        logger.debug('_query_listings error: %s', e)
        return []

def _get_lowest_priced_listing(user_id=None, store_id=None):
    """Return a single listing dict for the lowest priced active listing."""
    try:
        from listings.models import Listing
        qs = Listing.objects.filter(is_active=True, is_sold=False)
        if store_id:
            qs = qs.filter(store_id=store_id)
        low = qs.order_by('price').first()
        if not low:
            return None
        return {
            'type': 'listing',
            'id': low.id,
            'title': low.title,
            'price': str(low.price),
            'url': low.get_absolute_url(),
            'location': getattr(low, 'location', ''),
            'seller': getattr(low.seller, 'username', None),
            'image': low.image.url if low.image else None,
        }
    except Exception as e:
        logger.debug('_get_lowest_priced_listing error: %s', e)
        return None


def _get_most_expensive_listing(user_id=None, store_id=None):
    """Return a single listing dict for the highest priced active listing."""
    try:
        from listings.models import Listing
        qs = Listing.objects.filter(is_active=True, is_sold=False)
        if store_id:
            qs = qs.filter(store_id=store_id)
        hi = qs.order_by('-price').first()
        if not hi:
            return None
        return {
            'type': 'listing',
            'id': hi.id,
            'title': hi.title,
            'price': str(hi.price),
            'url': hi.get_absolute_url(),
            'location': getattr(hi, 'location', ''),
            'seller': getattr(hi.seller, 'username', None),
            'image': hi.image.url if hi.image else None,
            'reason': 'Highest price among active listings.',
        }
    except Exception as e:
        logger.debug('_get_most_expensive_listing error: %s', e)
        return None


def _query_user_stores(user_id, limit=10):
    try:
        from storefront.models import Store, Subscription
        stores = Store.objects.filter(owner_id=user_id).order_by('-created_at')[:limit]
        out = []
        for s in stores:
            sub = Subscription.objects.filter(store=s).order_by('-created_at').first()
            out.append({
                'type': 'store',
                'id': s.id,
                'name': s.name,
                'slug': s.slug,
                'owner': getattr(s.owner, 'username', None),
                'is_premium': getattr(s, 'is_premium', False),
                'subscription_status': sub.status if sub else 'none',
                'url': f"/store/{s.slug}/",
                'image': s.logo.url if getattr(s, 'logo', None) else None,
                'reason': 'Owned by your account.',
            })
        return out
    except Exception:
        logger.debug('_query_user_stores failed', exc_info=True)
        return []


def _query_user_owned_listings(user_id, limit=20):
    try:
        from listings.models import Listing
        qs = Listing.objects.filter(
            Q(seller_id=user_id) | Q(store__owner_id=user_id),
            is_active=True,
            is_sold=False
        ).distinct().order_by('-date_created')[:limit]
        out = []
        for l in qs:
            out.append({
                'type': 'listing',
                'id': l.id,
                'title': l.title,
                'price': str(l.price),
                'stock': int(getattr(l, 'stock', 0) or 0),
                'url': l.get_absolute_url(),
                'location': getattr(l, 'location', ''),
                'image': l.image.url if l.image else None,
                'reason': 'Listing owned by your account.',
            })
        return out
    except Exception:
        logger.debug('_query_user_owned_listings failed', exc_info=True)
        return []


def _extract_last_user_prompt_from_context(context):
    if not isinstance(context, list):
        return ''
    for h in reversed(context):
        if not isinstance(h, dict):
            continue
        if str(h.get('role') or '').lower() == 'user':
            return str(h.get('content') or '')
    return ''


def _is_followup_for_owned_listings(prompt, context=None):
    low = str(prompt or '').strip().lower()
    if re.search(r"\b(show|list|display)\b.*\b(my account|my listings|listings i own|ones in my account|ones i own|the ones)\b", low):
        return True
    if re.search(r"\b(show|list|display)\b.*\b(the ones|them|those)\b", low):
        prev = _extract_last_user_prompt_from_context(context).lower()
        if re.search(r"\b(listings|items|how many listings|my listings)\b", prev):
            return True
    return False


def _build_listing_item(l, reason=None):
    return {
        'type': 'listing',
        'id': l.id,
        'title': l.title,
        'price': str(l.price),
        'stock': int(getattr(l, 'stock', 0) or 0),
        'url': l.get_absolute_url(),
        'location': getattr(l, 'location', ''),
        'seller': getattr(getattr(l, 'seller', None), 'username', None),
        'image': l.image.url if l.image else None,
        'reason': reason or 'Relevant listing for your request.',
    }


def _extract_compare_candidates(prompt):
    text = str(prompt or '').strip()
    patterns = [
        r"compare\s+(.+?)\s+(?:and|vs|versus)\s+(.+)",
        r"which\s+is\s+better\s+(.+?)\s+(?:or|vs|versus)\s+(.+)",
        r"(.+?)\s+(?:vs|versus)\s+(.+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            a = m.group(1).strip(" ?.,")
            b = m.group(2).strip(" ?.,")
            if a and b:
                return [a, b]
    return []


def _resolve_listing_by_name(name, user_id=None):
    try:
        from listings.models import Listing
        qs = Listing.objects.filter(is_active=True, is_sold=False)
        if user_id and re.search(r"\b(my|account|i own)\b", str(name or '').lower()):
            qs = qs.filter(Q(seller_id=user_id) | Q(store__owner_id=user_id)).distinct()
        hit = qs.filter(title__icontains=name).order_by('-date_created').first()
        if hit:
            return hit
        # Fuzzy fallback across a manageable subset
        cands = list(qs.order_by('-date_created')[:120])
        if not cands:
            return None
        n_query = _normalize_store_label(name)
        scored = []
        for l in cands:
            n_title = _normalize_store_label(l.title)
            ratio = difflib.SequenceMatcher(None, n_query, n_title).ratio() if (n_query and n_title) else 0
            if ratio >= 0.45:
                scored.append((ratio, l))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1] if scored else None
    except Exception:
        logger.debug('_resolve_listing_by_name failed', exc_info=True)
        return None


def _handle_listing_comparison_intent(prompt, user_id=None):
    low = str(prompt or '').lower()
    if not re.search(r"\b(compare|vs|versus|which is better)\b", low):
        return None
    names = _extract_compare_candidates(prompt)
    if len(names) < 2:
        return None
    a = _resolve_listing_by_name(names[0], user_id=user_id)
    b = _resolve_listing_by_name(names[1], user_id=user_id)
    if not a or not b:
        return {'text': 'I could not resolve both listing options to compare. Please share exact listing names.', 'platform_items': []}

    def _pick():
        wants_budget = bool(re.search(r"\b(cheap|cheaper|budget|affordable|low price)\b", low))
        wants_premium = bool(re.search(r"\b(premium|luxury|high end|best quality)\b", low))
        if wants_budget:
            return a if a.price <= b.price else b, 'Best for budget (lower price).'
        if wants_premium:
            return a if a.price >= b.price else b, 'Best premium pick (higher-priced option).'
        # default: balanced by lower price with in-stock preference
        a_score = (1 if (getattr(a, 'stock', 0) or 0) > 0 else 0) - float(a.price or 0) / 1_000_000
        b_score = (1 if (getattr(b, 'stock', 0) or 0) > 0 else 0) - float(b.price or 0) / 1_000_000
        return (a, 'Balanced pick based on price and availability.') if a_score >= b_score else (b, 'Balanced pick based on price and availability.')

    winner, winner_reason = _pick()
    text = (
        f"Comparison:\n"
        f"- {a.title}: KSh {a.price:,.2f}, stock {int(getattr(a, 'stock', 0) or 0):,}, location {getattr(a, 'location', 'N/A')}.\n"
        f"- {b.title}: KSh {b.price:,.2f}, stock {int(getattr(b, 'stock', 0) or 0):,}, location {getattr(b, 'location', 'N/A')}.\n\n"
        f"Recommendation: {winner.title}. {winner_reason}"
    )
    items = [
        _build_listing_item(a, reason='Comparison option A.'),
        _build_listing_item(b, reason='Comparison option B.'),
        _build_listing_item(winner, reason=winner_reason),
    ]
    return {'text': text, 'platform_items': items}


def _handle_decision_support_intent(prompt, user_id=None):
    low = str(prompt or '').lower()
    if not re.search(r"\b(help me choose|which should i buy|what should i buy|recommend (an |a )?(item|listing)|best option)\b", low):
        return None
    try:
        filters = _parse_listing_filters_from_text(prompt)
        items = _query_listings(filters=filters, limit=8, user_id=user_id)
        if not items:
            return {'text': 'I could not find listings matching your criteria. Try adding budget, category, or location.', 'platform_items': []}

        wants_budget = bool(re.search(r"\b(budget|cheap|affordable|low price)\b", low))
        wants_premium = bool(re.search(r"\b(premium|luxury|high end|best quality)\b", low))

        def score(it):
            price = float(str(it.get('price') or '0').replace(',', '') or 0)
            stock = int(it.get('stock') or 0)
            s = stock * 0.05
            if wants_budget:
                s -= price / 100000
            elif wants_premium:
                s += price / 100000
            else:
                s -= price / 300000
            return s

        ranked = sorted(items, key=score, reverse=True)
        top = ranked[:3]
        if top:
            top[0]['reason'] = 'Top match based on your stated preferences.'
            for i in top[1:]:
                i['reason'] = i.get('reason') or 'Alternative option to compare.'
        text = f"Based on your request, I recommend {top[0].get('title')} as the best starting option. I have also included alternatives to compare."
        return {'text': text, 'platform_items': top}
    except Exception:
        logger.debug('_handle_decision_support_intent failed', exc_info=True)
        return None


def _recommend_stores_for_request(prompt, user_id=None):
    low = str(prompt or '').lower()
    if not re.search(r"\b(recommend (a )?store|which store|best store|where can i buy|store for)\b", low):
        return None
    try:
        from storefront.models import Store
        from listings.models import Listing, Category
        terms = [t for t in _extract_terms(prompt) if t not in {'recommend', 'store', 'stores', 'best', 'where', 'buy'}]
        price_max = None
        m = re.search(r"(?:under|below|less than)\s*(?:ksh|kes|kshs)?\s*([0-9,]+(?:\.[0-9]+)?)", low, re.I)
        if m:
            price_max = float(m.group(1).replace(',', ''))

        category_obj = None
        m_cat = re.search(r"(?:for|in)\s+category\s+([\w\s'\-]+)", prompt, re.I)
        if m_cat:
            category_obj = Category.objects.filter(name__icontains=m_cat.group(1).strip()).first()

        qs = Listing.objects.filter(is_active=True, is_sold=False, store__isnull=False)
        if price_max is not None:
            qs = qs.filter(price__lte=price_max)
        if category_obj:
            qs = qs.filter(category=category_obj)
        if terms:
            q_obj = Q()
            for t in terms:
                q_obj |= Q(title__icontains=t) | Q(description__icontains=t) | Q(store__name__icontains=t)
            qs = qs.filter(q_obj)

        listing_samples = list(qs.select_related('store', 'store__owner').order_by('-date_created')[:200])
        if not listing_samples:
            return {'text': 'I could not find store matches for that request yet. Try adding category, location, or budget.', 'platform_items': []}

        store_scores = {}
        for l in listing_samples:
            st = l.store
            if not st:
                continue
            sid = st.id
            if sid not in store_scores:
                store_scores[sid] = {'store': st, 'count': 0, 'min_price': None, 'sample': l}
            entry = store_scores[sid]
            entry['count'] += 1
            entry['min_price'] = float(l.price) if entry['min_price'] is None else min(entry['min_price'], float(l.price))

        ranked = sorted(
            store_scores.values(),
            key=lambda e: (e['count'] + (0.5 if getattr(e['store'], 'is_premium', False) else 0), -(e['min_price'] or 0)),
            reverse=True
        )[:5]

        out = []
        for e in ranked:
            st = e['store']
            reason = f"Matched {e['count']} relevant listing(s)"
            if e['min_price'] is not None:
                reason += f"; from about KSh {e['min_price']:,.2f}"
            out.append({
                'type': 'store',
                'id': st.id,
                'name': st.name,
                'slug': st.slug,
                'owner': getattr(st.owner, 'username', None),
                'is_premium': getattr(st, 'is_premium', False),
                'url': f"/store/{st.slug}/",
                'image': st.logo.url if getattr(st, 'logo', None) else None,
                'reason': reason,
            })
        text = "Here are store recommendations based on what you're looking for."
        return {'text': text, 'platform_items': out}
    except Exception:
        logger.debug('_recommend_stores_for_request failed', exc_info=True)
        return None


def _extract_store_name_from_prompt(prompt):
    text = str(prompt or '').strip()
    patterns = [
        r"(?:in|for|of)\s+my\s+(.+?)(?:\s+(?:online\s+)?stores?\b|\s+storefront\b|\s+shop\b|$)",
        r"(?:store\s+named|store\s+called)\s+([\w\s'\-&]+)",
        r"(?:my\s+)([\w\s'\-&]+?)(?:\s+stores?\b)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            raw = m.group(1).strip()
            raw = re.split(r"\s+(?:and|with|plus)\s+", raw, maxsplit=1, flags=re.I)[0].strip()
            return raw
    return None


def _normalize_store_label(value):
    s = re.sub(r"[^a-z0-9\s]", " ", str(value or "").lower())
    tokens = [t for t in s.split() if t and t not in {"store", "stores", "online", "shop", "the", "my"}]
    return " ".join(tokens).strip()


def _find_user_store_by_name(user_id, store_query):
    try:
        from storefront.models import Store
        qs = Store.objects.filter(owner_id=user_id)
        if not store_query:
            stores = list(qs[:5])
            if len(stores) == 1:
                return stores[0]
            return None

        direct = qs.filter(name__icontains=store_query).order_by('-id').first()
        if direct:
            return direct

        n_query = _normalize_store_label(store_query)
        stores = list(qs[:50])
        if not stores:
            return None
        scored = []
        for st in stores:
            name_raw = st.name or ''
            n_name = _normalize_store_label(name_raw)
            score = 0.0
            if n_query and n_name:
                score = difflib.SequenceMatcher(None, n_query, n_name).ratio()
                q_terms = set(n_query.split())
                n_terms = set(n_name.split())
                if q_terms and n_terms:
                    overlap = len(q_terms & n_terms) / max(1, len(q_terms))
                    score = max(score, overlap)
            scored.append((score, st))
        scored.sort(key=lambda x: x[0], reverse=True)
        if scored and scored[0][0] >= 0.45:
            return scored[0][1]
    except Exception:
        logger.debug('_find_user_store_by_name failed', exc_info=True)
    return None


def _answer_store_inventory_summary(prompt, user_id=None):
    low = (prompt or '').strip().lower()
    inventory_asked = bool(re.search(r"\b(stock|inventory|worth|value|total worth|collective stock|stock count|total stock)\b", low))
    if not inventory_asked:
        return None
    mentions_store = bool(re.search(r"\bstore|online store|shop\b", low))
    if not mentions_store:
        return None

    try:
        from django.db.models import Sum, F, DecimalField, ExpressionWrapper, Count
        from storefront.models import Store
        from listings.models import Listing

        requires_user_scope = bool(re.search(r"\bmy\b", low))
        if requires_user_scope and not user_id:
            return {'text': 'Please sign in so I can calculate totals for your store.', 'platform_items': []}

        store_q = _extract_store_name_from_prompt(prompt)
        if requires_user_scope:
            store = _find_user_store_by_name(user_id, store_q)
        else:
            store_qs = Store.objects.all()
            if store_q:
                store_qs = store_qs.filter(name__icontains=store_q)
            store = store_qs.order_by('-id').first()
        if not store:
            if requires_user_scope:
                return {'text': 'I could not find that store in your account.', 'platform_items': []}
            return {'text': 'I could not find a matching store for that request.', 'platform_items': []}

        line_value_expr = ExpressionWrapper(F('stock') * F('price'), output_field=DecimalField(max_digits=20, decimal_places=2))
        listings = Listing.objects.filter(store=store, is_active=True, is_sold=False)
        agg = listings.aggregate(
            total_stock=Sum('stock'),
            total_worth=Sum(line_value_expr),
            listing_count=Count('id')
        )
        total_stock = int(agg.get('total_stock') or 0)
        total_worth = agg.get('total_worth') or 0
        listing_count = int(agg.get('listing_count') or 0)

        top_items = listings.annotate(line_value=line_value_expr).order_by('-line_value')[:3]
        platform_items = []
        for l in top_items:
            line_value = getattr(l, 'line_value', 0) or 0
            platform_items.append({
                'type': 'listing',
                'id': l.id,
                'title': l.title,
                'price': str(l.price),
                'stock': int(getattr(l, 'stock', 0) or 0),
                'line_value': str(line_value),
                'url': l.get_absolute_url(),
                'image': l.image.url if l.image else None,
                'reason': f"High inventory impact: {int(getattr(l, 'stock', 0) or 0)} units, est. KSh {line_value:,.2f}",
            })

        platform_items.append({
            'type': 'store',
            'id': store.id,
            'name': store.name,
            'url': f"/storefront/dashboard/store/{store.slug}/subscription/manage/",
            'reason': 'Store found and scoped for your inventory summary request.',
        })

        text = (
            f'{store.name}: {listing_count} active listing(s), total stock {total_stock:,} unit(s), '
            f'estimated total worth KSh {total_worth:,.2f}.'
        )
        return {'text': text, 'platform_items': platform_items}
    except Exception as e:
        logger.debug('_answer_store_inventory_summary error: %s', e)
        return None

def _answer_from_db(prompt: str, user_id=None):
    """Answer common factual queries directly from the database."""
    try:
        low = (prompt or '').strip().lower()
        inv_summary = _answer_store_inventory_summary(prompt, user_id=user_id)
        if inv_summary:
            return inv_summary
        # How many stores
        if re.search(r"\bhow many stores\b", low) or re.search(r"\bnumber of stores\b", low):
            from storefront.models import Store
            cnt = Store.objects.count()
            return {'text': f'There are {cnt} store(s) on Baysoko.', 'platform_items': []}
        # How many listings
        if re.search(r"\bhow many listings\b", low) or re.search(r"\bnumber of listings\b", low):
            from listings.models import Listing
            if user_id and re.search(r"\b(i|my|me|made|in my account)\b", low):
                cnt = Listing.objects.filter(
                    Q(seller_id=user_id) | Q(store__owner_id=user_id),
                    is_active=True,
                    is_sold=False
                ).distinct().count()
                return {'text': f'You have {cnt} active listing(s) in your account.', 'platform_items': _query_user_owned_listings(user_id, limit=10)}
            cnt = Listing.objects.filter(is_active=True, is_sold=False).count()
            return {'text': f'There are {cnt} active listing(s) on Baysoko.', 'platform_items': []}
        # Cheapest item
        if re.search(r"\b(cheapest|lowest priced|lowest price|cheapest item)\b", low):
            item = _get_lowest_priced_listing(user_id=user_id)
            if item:
                return {'text': f"Lowest priced item: {item.get('title')} — {item.get('price')}", 'platform_items': [item]}
            return {'text': 'No active listings found.', 'platform_items': []}
        # Most expensive item
        if re.search(r"\b(most expensive|highest priced|highest price|most costly|priciest)\b", low):
            item = _get_most_expensive_listing(user_id=user_id)
            if item:
                return {'text': f"Most expensive item: {item.get('title')} — {item.get('price')}", 'platform_items': [item]}
            return {'text': 'No active listings found.', 'platform_items': []}
        # Order lookup by number
        m = re.search(r"order\s*#?(\d+)", prompt)
        if m:
            try:
                if not user_id:
                    return {'text': 'Please sign in to track specific orders.', 'platform_items': []}
                oid = int(m.group(1))
                from listings.models import Order
                o = Order.objects.filter(pk=oid, user_id=user_id).first()
                if o:
                    items = o.order_items.select_related('listing')[:3]
                    item_str = ', '.join([f"{it.listing.title} x{it.quantity}" for it in items])
                    platform_items = [{
                        'type': 'order',
                        'id': o.id,
                        'status': o.status,
                        'total': str(o.total_price),
                        'items_preview': item_str,
                        'url': o.get_absolute_url() if hasattr(o, 'get_absolute_url') else None,
                    }]
                    return {'text': f'Order #{o.id} — status: {o.status}. Total: {o.total_price}.', 'platform_items': platform_items}
                return {'text': f'Order #{oid} was not found in your account.', 'platform_items': []}
            except Exception:
                pass
        # Search for listing by name
        m2 = re.search(r"(?:tell me about|show me|what can i get|what is|find)\s+(?:the\s+)?([\w\s'\-]{3,})", prompt, re.I)
        if m2:
            q = m2.group(1).strip()
            if len(q) > 2 and not q.lower().startswith(('how ', 'what ', 'where ', 'who ')):
                from listings.models import Listing
                qs = Listing.objects.filter(title__icontains=q, is_active=True, is_sold=False)[:5]
                items = [{
                    'type': 'listing',
                    'id': l.id,
                    'title': l.title,
                    'price': str(l.price),
                    'url': l.get_absolute_url(),
                    'image': l.image.url if l.image else None,
                } for l in qs]
                if items:
                    return {'text': f'Found {len(items)} listing(s) matching "{q}".', 'platform_items': items}
        # My cart
        if user_id and re.search(r"\b(my cart|what is in my cart|show my cart)\b", low):
            from listings.models import Cart
            cart = Cart.objects.filter(user_id=user_id).first()
            if not cart:
                return {'text': 'Your cart is empty.', 'platform_items': []}
            items = [{
                'type': 'cart_item',
                'id': ci.listing.id,
                'title': ci.listing.title,
                'price': str(ci.listing.price),
                'url': ci.listing.get_absolute_url(),
                'quantity': ci.quantity,
                'image': ci.listing.image.url if ci.listing.image else None,
            } for ci in cart.items.select_related('listing')]
            return {'text': f'You have {len(items)} item(s) in your cart.', 'platform_items': items}
        # My favorites
        if user_id and re.search(r"\b(my favorites|my favourite|show my favorites)\b", low):
            from listings.models import Favorite
            favs = Favorite.objects.filter(user_id=user_id).select_related('listing')[:10]
            items = [{
                'type': 'listing',
                'id': f.listing.id,
                'title': f.listing.title,
                'price': str(f.listing.price),
                'url': f.listing.get_absolute_url(),
                'image': f.listing.image.url if f.listing.image else None,
            } for f in favs]
            return {'text': f'You have {favs.count()} favorite(s).', 'platform_items': items}
        # Stores by owner
        m_owner = re.search(r"stores\s+(?:owned\s+by|by)\s+([\w\s'\-]+)", low)
        if not m_owner:
            m_owner = re.search(r"which\s+stores\s+does\s+([\w\s'\-]+)\s+own", low)
        if m_owner:
            owner_q = m_owner.group(1).strip()
            from django.contrib.auth import get_user_model
            from storefront.models import Store
            User = get_user_model()
            u = User.objects.filter(username__icontains=owner_q).first()
            if u:
                stores = Store.objects.filter(owner=u)
            else:
                stores = Store.objects.filter(name__icontains=owner_q)[:10]
            items = [{
                'type': 'store',
                'id': s.id,
                'name': s.name,
                'slug': s.slug,
                'owner': getattr(s.owner, 'username', None),
                'url': f"/store/{s.slug}/",
                'image': s.logo.url if s.logo else None,
            } for s in stores]
            if items:
                return {'text': f'Found {len(items)} store(s) for "{owner_q}".', 'platform_items': items}
            return {'text': f'No stores found for "{owner_q}".', 'platform_items': []}
        # Category stats
        m_cat = re.search(r"how many (?:listings|items) (?:in|under) category\s+([\w\s'\-]+)", low)
        if not m_cat:
            m_cat = re.search(r"(?:in|under) category\s+([\w\s'\-]+)\b", low)
        if m_cat:
            cat_q = m_cat.group(1).strip()
            from listings.models import Listing, Category
            cat = Category.objects.filter(name__icontains=cat_q).first()
            if cat:
                cnt = Listing.objects.filter(category=cat, is_active=True, is_sold=False).count()
                lowest = Listing.objects.filter(category=cat, is_active=True, is_sold=False).order_by('price').first()
                items = []
                if lowest:
                    items.append({
                        'type': 'listing',
                        'id': lowest.id,
                        'title': lowest.title,
                        'price': str(lowest.price),
                        'url': lowest.get_absolute_url(),
                        'image': lowest.image.url if lowest.image else None,
                    })
                return {'text': f'Category "{cat.name}" has {cnt} active listing(s).', 'platform_items': items}
            else:
                return {'text': f'No category named "{cat_q}" found.', 'platform_items': []}
        # Listings by seller/store
        m_seller = re.search(r"(?:listings|items)\s+(?:by|from)\s+([\w\s'\-]+)", prompt, re.I)
        if not m_seller:
            m_seller = re.search(r"what listings does\s+([\w\s'\-]+)\s+have", prompt, re.I)
        if m_seller:
            seller_q = m_seller.group(1).strip()
            from django.contrib.auth import get_user_model
            from listings.models import Listing
            from storefront.models import Store
            User = get_user_model()
            u = User.objects.filter(username__icontains=seller_q).first()
            if u:
                qs = Listing.objects.filter(seller=u, is_active=True, is_sold=False)[:10]
            else:
                st = Store.objects.filter(name__icontains=seller_q).first()
                if st:
                    qs = Listing.objects.filter(store=st, is_active=True, is_sold=False)[:10]
                else:
                    qs = Listing.objects.filter(title__icontains=seller_q, is_active=True, is_sold=False)[:10]
            items = [{
                'type': 'listing',
                'id': l.id,
                'title': l.title,
                'price': str(l.price),
                'url': l.get_absolute_url(),
                'image': l.image.url if l.image else None,
            } for l in qs]
            if items:
                return {'text': f'Found {len(items)} listing(s) for "{seller_q}".', 'platform_items': items}
            return {'text': f'No listings found for "{seller_q}".', 'platform_items': []}
    except Exception as e:
        logger.debug('_answer_from_db error: %s', e)
    return None


# ========== DATABASE QUERY REGISTRY (Retrieval functions for RAG) ==========
def _get_stores_by_name(name, user_id=None):
    try:
        from storefront.models import Store
        stores = Store.objects.filter(name__icontains=name)[:5]
        items = [{'type': 'store', 'id': s.id, 'name': s.name, 'url': getattr(s, 'get_absolute_url', lambda: f"/store/{s.slug}/")()} for s in stores]
        return {
            'text': f"Found {len(stores)} store(s) matching '{name}'.",
            'data': items,
            'context': '\n'.join([f"- {itm['name']} ({itm['url']})" for itm in items])
        }
    except Exception:
        return {'text': f"Error searching stores for '{name}'.", 'data': [], 'context': ''}


def _get_listings_by_category(category, user_id=None):
    try:
        from listings.models import Listing, Category
        cat = Category.objects.filter(name__icontains=category).first()
        if not cat:
            return {'text': f"No category '{category}' found.", 'data': [], 'context': ''}
        listings = Listing.objects.filter(category=cat, is_active=True, is_sold=False)[:5]
        items = [{'type': 'listing', 'id': l.id, 'title': l.title, 'price': str(getattr(l, 'price', ''))} for l in listings]
        return {
            'text': f"Found {len(listings)} listing(s) in category '{cat.name}'.",
            'data': items,
            'context': '\n'.join([f"- {itm['title']} ({itm['price']})" for itm in items])
        }
    except Exception:
        return {'text': f"Error searching listings for category '{category}'.", 'data': [], 'context': ''}


def _get_orders_for_user(user_id=None):
    try:
        if not user_id:
            return {'text': 'Sign in to view orders.', 'data': [], 'context': ''}
        from listings.models import Order
        orders = Order.objects.filter(user_id=user_id).order_by('-id')[:5]
        items = []
        for o in orders:
            items.append({'type': 'order', 'id': o.id, 'status': o.status, 'total': str(getattr(o, 'total_price', ''))})
        ctx = '\n'.join([f"- Order #{it['id']}: {it['status']} ({it['total']})" for it in items])
        return {'text': f'Found {len(items)} recent order(s).', 'data': items, 'context': ctx}
    except Exception:
        return {'text': 'Error retrieving orders.', 'data': [], 'context': ''}


def _get_cart_contents(user_id=None):
    try:
        if not user_id:
            return {'text': 'Sign in to view your cart.', 'data': [], 'context': ''}
        from listings.models import Cart
        cart = Cart.objects.filter(user_id=user_id).first()
        if not cart or not cart.items.exists():
            return {'text': 'Your cart is empty.', 'data': [], 'context': ''}
        items = []
        for ci in cart.items.select_related('listing'):
            l = ci.listing
            items.append({'type': 'cart', 'id': getattr(l, 'id', None), 'title': l.title, 'price': str(getattr(l, 'price', '')), 'quantity': ci.quantity, 'url': l.get_absolute_url()})
        ctx = '\n'.join([f"- {it['title']} x{it.get('quantity',1)} ({it.get('price')})" for it in items])
        return {'text': f'You have {len(items)} item(s) in your cart.', 'data': items, 'context': ctx}
    except Exception:
        return {'text': 'Error retrieving cart.', 'data': [], 'context': ''}


def _get_user_favorites(user_id=None):
    try:
        if not user_id:
            return {'text': 'Sign in to view favorites.', 'data': [], 'context': ''}
        from listings.models import Favorite
        favs = Favorite.objects.filter(user_id=user_id).select_related('listing')[:10]
        items = []
        for f in favs:
            l = f.listing
            items.append({'type': 'favorite', 'id': getattr(l, 'id', None), 'title': l.title, 'price': str(getattr(l, 'price', '')), 'url': l.get_absolute_url()})
        ctx = '\n'.join([f"- {it['title']} ({it.get('price')})" for it in items])
        return {'text': f'You have {favs.count()} favorite(s).', 'data': items, 'context': ctx}
    except Exception:
        return {'text': 'Error retrieving favorites.', 'data': [], 'context': ''}


def _get_stores_by_owner(owner_name, user_id=None):
    try:
        from django.contrib.auth import get_user_model
        from storefront.models import Store
        User = get_user_model()
        u = User.objects.filter(username__icontains=owner_name).first()
        if u:
            stores = Store.objects.filter(owner=u)[:10]
        else:
            stores = Store.objects.filter(name__icontains=owner_name)[:10]
        items = [{'type': 'store', 'id': s.id, 'name': s.name, 'url': getattr(s, 'get_absolute_url', lambda: f"/store/{s.slug}/")()} for s in stores]
        ctx = '\n'.join([f"- {it['name']} ({it['url']})" for it in items])
        return {'text': f'Found {len(items)} store(s) for "{owner_name}".', 'data': items, 'context': ctx}
    except Exception:
        return {'text': f'Error finding stores for "{owner_name}".', 'data': [], 'context': ''}


def _get_cheapest_in_category(category, user_id=None):
    try:
        from listings.models import Listing, Category
        cat = Category.objects.filter(name__icontains=category).first()
        if not cat:
            return {'text': f"No category '{category}' found.", 'data': [], 'context': ''}
        low = Listing.objects.filter(category=cat, is_active=True, is_sold=False).order_by('price').first()
        if not low:
            return {'text': f'No active listings in category "{cat.name}".', 'data': [], 'context': ''}
        item = {'type': 'listing', 'id': low.id, 'title': low.title, 'price': str(getattr(low, 'price', '')), 'url': low.get_absolute_url()}
        return {'text': f'Cheapest in category "{cat.name}": {low.title} ({getattr(low, "price", "")} )', 'data': [item], 'context': f"- {low.title} ({getattr(low, 'price', '')})"}
    except Exception:
        return {'text': 'Error retrieving cheapest item.', 'data': [], 'context': ''}


def _get_listings_by_seller(seller_name, user_id=None):
    try:
        from django.contrib.auth import get_user_model
        from listings.models import Listing
        from storefront.models import Store
        User = get_user_model()
        u = User.objects.filter(username__icontains=seller_name).first()
        if u:
            qs = Listing.objects.filter(seller=u, is_active=True, is_sold=False)[:10]
        else:
            st = Store.objects.filter(name__icontains=seller_name).first()
            if st:
                qs = Listing.objects.filter(store=st, is_active=True, is_sold=False)[:10]
            else:
                qs = Listing.objects.filter(title__icontains=seller_name, is_active=True, is_sold=False)[:10]
        items = [{'type': 'listing', 'id': l.id, 'title': l.title, 'price': str(getattr(l, 'price', '')), 'url': l.get_absolute_url()} for l in qs]
        ctx = '\n'.join([f"- {it['title']} ({it.get('price')})" for it in items])
        return {'text': f'Found {len(items)} listing(s) for "{seller_name}".', 'data': items, 'context': ctx}
    except Exception:
        return {'text': 'Error retrieving listings for seller.', 'data': [], 'context': ''}


def _get_listing_by_title(title, user_id=None):
    try:
        from listings.models import Listing
        l = Listing.objects.filter(title__icontains=title, is_active=True, is_sold=False).first()
        if not l:
            return {'text': f'No listing found matching "{title}".', 'data': [], 'context': ''}
        item = {'type': 'listing', 'id': l.id, 'title': l.title, 'price': str(getattr(l, 'price', '')), 'url': l.get_absolute_url()}
        return {'text': f'Found listing: {l.title}.', 'data': [item], 'context': f"- {l.title} ({getattr(l, 'price', '')})"}
    except Exception:
        return {'text': 'Error finding listing.', 'data': [], 'context': ''}


def _get_top_sellers(limit=5, user_id=None):
    try:
        from django.contrib.auth import get_user_model
        from django.db.models import Count
        User = get_user_model()
        top = User.objects.annotate(sales=Count('listing__orderitem')).filter(sales__gt=0).order_by('-sales')[:limit]
        items = [{'type': 'user', 'id': u.id, 'name': u.username, 'sales': getattr(u, 'sales', 0)} for u in top]
        ctx = '\n'.join([f"- {it['name']} ({it['sales']} sales)" for it in items])
        return {'text': f'Top {len(items)} sellers by items sold.', 'data': items, 'context': ctx}
    except Exception:
        return {'text': 'Error retrieving top sellers.', 'data': [], 'context': ''}


# Registry: list of pattern/function mappings
QUERY_REGISTRY = [
    {'patterns': [r"\b(store|stores)\b.*\b(name|called)\s+([\w\s]+)"], 'function': _get_stores_by_name, 'extract': lambda m: {'name': m.group(3).strip()}},
    {'patterns': [r"\b(listings|items)\b.*\b(in|under|category)\s+([\w\s]+)"], 'function': _get_listings_by_category, 'extract': lambda m: {'category': m.group(3).strip()}},
    {'patterns': [r"\border(s)?\b|\bmy orders\b"], 'function': _get_orders_for_user, 'extract': lambda m: {}},
    {'patterns': [r"\b(my cart|what is in my cart|show my cart)\b"], 'function': _get_cart_contents, 'extract': lambda m: {}},
    {'patterns': [r"\b(my favorites|my favourite|show my favorites)\b"], 'function': _get_user_favorites, 'extract': lambda m: {}},
    {'patterns': [r"stores\s+(?:owned\s+by|by)\s+([\w\s'\-]+)", r"which\s+stores\s+does\s+([\w\s'\-]+)\s+own"], 'function': _get_stores_by_owner, 'extract': lambda m: {'owner_name': m.group(1).strip()}},
    {'patterns': [r"cheapest\s+in\s+category\s+([\w\s]+)", r"cheapest\s+in\s+([\w\s]+)"], 'function': _get_cheapest_in_category, 'extract': lambda m: {'category': m.group(1).strip()}},
    {'patterns': [r"(listings|items)\s+(?:by|from)\s+([\w\s'\-]+)", r"what listings does\s+([\w\s'\-]+)\s+have"], 'function': _get_listings_by_seller, 'extract': lambda m: {'seller_name': (m.group(2) if m.lastindex and m.lastindex>=2 else m.group(1)).strip()}},
    {'patterns': [r"tell me about\s+([\w\s'\-]{3,})", r"show me\s+([\w\s'\-]{3,})"], 'function': _get_listing_by_title, 'extract': lambda m: {'title': m.group(1).strip()}},
    {'patterns': [r"\b(top|best|leading)\s+sellers?\b", r"\bwho\s+sells\s+the\s+most\b"], 'function': _get_top_sellers, 'extract': lambda m: {}}
]


def try_database_query(prompt: str, user_id=None):
    """Loop through QUERY_REGISTRY and return first matching result dict or None."""
    try:
        low = (prompt or '').strip()
        for entry in QUERY_REGISTRY:
            for pat in entry.get('patterns', []):
                m = re.search(pat, low, re.IGNORECASE)
                if m:
                    params = {}
                    try:
                        params = entry.get('extract', lambda mm: {})(m) or {}
                    except Exception:
                        params = {}
                    params['user_id'] = user_id
                    try:
                        return entry['function'](**params)
                    except Exception:
                        return {'text': 'Error executing query.', 'data': [], 'context': ''}
        return None
    except Exception:
        return None

def _handle_subscription_intent(prompt: str, user_id=None):
    """Handle subscription‑related intents: status, cancel, list plans, renew."""
    try:
        from storefront.subscription_service import SubscriptionService
        from storefront.models import Subscription, Store
        user = None
        if user_id:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            user = User.objects.filter(pk=user_id).first()
        low = prompt.strip().lower()
        store_q = _extract_store_name_from_prompt(prompt)
        target_sub = None
        if user:
            subs_qs = Subscription.objects.filter(store__owner=user).select_related('store').order_by('-created_at')
            if store_q:
                target_sub = subs_qs.filter(Q(store__name__icontains=store_q) | Q(store__slug__icontains=store_q)).first()
            if not target_sub:
                target_sub = subs_qs.first()

        asks_status = bool(re.search(r"\b(my subscription|subscription status|what is my subscription|what plan am i on|my plan|current plan|which plan)\b", low))
        asks_available = bool(re.search(r"\b(list plans|plans|what plans|available plans|available subscriptions|subscription plans|show plans)\b", low))

        if asks_status:
            if not user:
                return ('Please sign in to view your subscriptions.', [])
            summary = SubscriptionService.get_subscription_summary(user)
            text = (
                f"You have {summary.get('total_stores',0)} store(s). "
                f"Active subscriptions: {summary.get('active_subscriptions',0)}."
            )
            items = []
            active_subs = Subscription.objects.filter(store__owner=user).select_related('store').order_by('-created_at')[:8]
            for sub in active_subs:
                plan = (getattr(sub, 'plan', None) or getattr(sub, 'plan_name', None) or 'unknown')
                items.append({
                    'type': 'subscription',
                    'id': sub.id,
                    'store_name': sub.store.name,
                    'plan': plan,
                    'status': sub.status,
                    'price': str(getattr(sub, 'amount', '')),
                    'expires': sub.current_period_end.isoformat() if sub.current_period_end else None,
                    'url': f"/storefront/dashboard/store/{sub.store.slug}/subscription/manage/",
                })
            if not items:
                stores = Store.objects.filter(owner=user).order_by('-created_at')[:5]
                items = [{
                    'type': 'subscription',
                    'id': s.id,
                    'store_name': s.name,
                    'status': 'no_subscription',
                    'plan': 'none',
                    'url': f"/storefront/dashboard/store/{s.slug}/subscription/manage/",
                    'reason': 'Open this store subscription page.',
                } for s in stores]
                text += " No active subscription found yet."
            return (text, items)

        if re.search(r"\b(manage subscription|open subscription|subscription page|billing page)\b", low):
            if not user:
                return ('Please sign in to manage subscriptions.', [])
            stores = Store.objects.filter(owner=user).order_by('-created_at')[:8]
            items = [{
                'type': 'subscription',
                'id': s.id,
                'store_name': s.name,
                'status': 'manage',
                'url': f"/storefront/dashboard/store/{s.slug}/subscription/manage/",
                'reason': 'Manage subscription.',
            } for s in stores]
            return ('Open any subscription below to manage billing, plan, and renewal.', items)

        if re.search(r"\b(renew subscription|renew my subscription|reactivate subscription)\b", low):
            if not user:
                return ('Please sign in to renew subscriptions.', [])
            if not target_sub:
                return ('No subscription found to renew.', [])
            m_phone = re.search(r"(\+?\d[\d\-\s]{7,})", prompt)
            phone = m_phone.group(1).strip() if m_phone else None
            if not phone:
                return (
                    f"To renew {target_sub.store.name}, share your M-Pesa number in this chat or open manage subscription.",
                    [{
                        'type': 'subscription',
                        'id': target_sub.id,
                        'store_name': target_sub.store.name,
                        'status': target_sub.status,
                        'plan': target_sub.plan,
                        'url': f"/storefront/dashboard/store/{target_sub.store.slug}/subscription/manage/",
                    }]
                )
            ok, msg = SubscriptionService.renew_subscription(target_sub, phone_number=phone)
            return (
                (msg if isinstance(msg, str) else 'Renewal request processed.'),
                [{
                    'type': 'subscription',
                    'id': target_sub.id,
                    'store_name': target_sub.store.name,
                    'status': target_sub.status,
                    'plan': target_sub.plan,
                    'url': f"/storefront/dashboard/store/{target_sub.store.slug}/subscription/manage/",
                }]
            )

        if re.search(r"\b(upgrade|downgrade|change plan|switch plan)\b", low):
            if not user:
                return ('Please sign in to change subscription plans.', [])
            if not target_sub:
                return ('No subscription found to change plan.', [])
            m_plan = re.search(r"\b(basic|premium|enterprise|free)\b", low)
            if not m_plan:
                plans = SubscriptionService.get_display_plans()
                items = [{
                    'type': 'subscription_plan',
                    'id': k,
                    'name': f"{k.title()} plan",
                    'price': str(v.get('price', '')),
                    'features': v.get('features', []),
                    'reason': 'Available plan option.',
                } for k, v in plans.items()]
                return ('Specify the plan you want (basic, premium, or enterprise).', items)
            new_plan = m_plan.group(1).strip().lower()
            ok, msg = SubscriptionService.change_plan(target_sub.store, new_plan)
            out_msg = msg if isinstance(msg, str) else (f'Plan changed to {new_plan}.' if ok else 'Failed to change plan.')
            return (
                out_msg,
                [{
                    'type': 'subscription',
                    'id': target_sub.id,
                    'store_name': target_sub.store.name,
                    'status': target_sub.status,
                    'plan': new_plan,
                    'url': f"/storefront/dashboard/store/{target_sub.store.slug}/subscription/manage/",
                }]
            )

        if 'cancel subscription' in low or 'cancel my subscription' in low:
            if not user:
                return ('Please sign in to cancel subscriptions.', [])
            sub = target_sub or Subscription.objects.filter(store__owner=user).order_by('-created_at').first()
            if not sub:
                return ('No subscription found to cancel.', [])
            immediate = bool(re.search(r"\b(immediately|now|right away)\b", low))
            success = SubscriptionService.cancel_subscription(sub, cancel_at_period_end=not immediate)
            text = 'Subscription canceled immediately.' if immediate else 'Subscription cancellation scheduled at period end.'
            return (text if success else 'Failed to cancel subscription.', [{
                'type': 'subscription',
                'id': sub.id,
                'store_name': sub.store.name,
                'status': sub.status,
                'plan': sub.plan,
                'url': f"/storefront/dashboard/store/{sub.store.slug}/subscription/manage/",
            }])

        if asks_available:
            plans = SubscriptionService.get_display_plans()
            text = 'Available plans: ' + ', '.join([
                f"{k.title()} (KSh {float(v.get('price', 0) or 0):,.0f}/{v.get('period', 'month')})"
                for k, v in plans.items()
            ])
            items = []
            for k, v in plans.items():
                items.append({
                    'type': 'subscription_plan',
                    'id': k,
                    'name': f"{k.title()} plan",
                    'price': str(v.get('price', '')),
                    'features': v.get('features', []),
                })
            return (text, items)

        if user:
            summ = SubscriptionService.get_subscription_summary(user)
            text = (
                f"Subscription summary: {summ.get('total_stores',0)} stores; "
                f"{summ.get('active_subscriptions',0)} active subscriptions. "
                "Ask me to manage, renew, cancel, or change plan for a specific store."
            )
            stores = Store.objects.filter(owner=user)[:5]
            items = [{
                'type': 'subscription',
                'id': s.id,
                'store_name': s.name,
                'status': 'manage',
                'url': f"/storefront/dashboard/store/{s.slug}/subscription/manage/",
            } for s in stores]
            return (text, items)
        return ('Subscription help: ask about status, plans, renewals, plan changes, or cancellations.', [])
    except Exception as e:
        logger.debug('_handle_subscription_intent error: %s', e)
        return ('Subscription service unavailable.', [])

def _handle_affiliate_intent(prompt: str, user_id=None):
    """Handle affiliate-related intents: link, stats, commissions, payouts, terms."""
    try:
        low = (prompt or '').strip().lower()
        if not re.search(r"\b(affiliate|affiliates|referral|referrals|commission|commissions|payout|payouts|affiliate link|referral link)\b", low):
            return (None, [])
        if not user_id:
            return ('Please sign in to view your affiliate profile and commissions.', [])
        from affiliates.models import AffiliateProfile, AffiliateClick, AffiliateAttribution, AffiliateCommission, AffiliatePayout
        profile = AffiliateProfile.objects.filter(user_id=user_id).first()
        link_base = getattr(settings, 'SITE_URL', '').rstrip('/')
        query_key = getattr(settings, 'AFFILIATE_QUERY_PARAM', 'aid')
        affiliate_link = None
        if profile:
            affiliate_link = f"{link_base}/?{query_key}={profile.code}" if link_base else f"/?{query_key}={profile.code}"
        items = []
        if not profile:
            text = (
                "You are not enrolled in the Baysoko affiliate program yet. "
                "Open the affiliate dashboard to activate your profile and get your referral link."
            )
            items.append({
                'type': 'action_suggestion',
                'id': 'affiliate_activate',
                'title': 'Open affiliate dashboard',
                'reason': 'Activate your affiliate profile.',
                'url': '/affiliates/',
            })
            items.append({
                'type': 'action_suggestion',
                'id': 'affiliate_terms',
                'title': 'View affiliate terms',
                'reason': 'Review affiliate policies.',
                'url': '/affiliates/terms/',
            })
            return (text, items)

        clicks = AffiliateClick.objects.filter(affiliate=profile).count()
        referrals = AffiliateAttribution.objects.filter(affiliate=profile).count()
        commissions = AffiliateCommission.objects.filter(affiliate=profile)
        total_commissions = commissions.aggregate(total=Sum('amount')).get('total') or 0
        paid_commissions = commissions.filter(status='paid').aggregate(total=Sum('amount')).get('total') or 0
        pending_commissions = commissions.filter(status='pending').aggregate(total=Sum('amount')).get('total') or 0
        payout_count = AffiliatePayout.objects.filter(affiliate=profile).count()

        if re.search(r"\b(link|referral link|affiliate link)\b", low):
            text = f"Your affiliate link is ready. Share this link to earn commissions: {affiliate_link}"
        elif re.search(r"\b(commission|commissions|earnings|payout|payouts)\b", low):
            text = (
                f"Affiliate earnings summary: total commissions {total_commissions}, "
                f"paid {paid_commissions}, pending {pending_commissions}, payouts {payout_count}."
            )
        elif re.search(r"\b(click|clicks|referral|referrals)\b", low):
            text = f"Affiliate performance: {clicks} clicks and {referrals} referral(s) recorded."
        elif re.search(r"\b(terms|policy|rules)\b", low):
            text = "Here are the Baysoko affiliate terms and guidelines."
        else:
            text = (
                f"Affiliate overview: {clicks} clicks, {referrals} referrals, "
                f"total commissions {total_commissions}. Your link: {affiliate_link}"
            )

        items.append({
            'type': 'affiliate',
            'id': profile.id,
            'title': 'Affiliate dashboard',
            'code': profile.code,
            'link': affiliate_link,
            'url': '/affiliates/',
        })
        items.append({
            'type': 'action_suggestion',
            'id': 'affiliate_commissions',
            'title': 'View affiliate commissions',
            'reason': 'Review your earnings and payouts.',
            'url': '/affiliates/commissions/',
        })
        items.append({
            'type': 'action_suggestion',
            'id': 'affiliate_terms',
            'title': 'View affiliate terms',
            'reason': 'Review affiliate policies.',
            'url': '/affiliates/terms/',
        })
        return (text, items)
    except Exception as e:
        logger.debug('_handle_affiliate_intent error: %s', e)
        return ('Affiliate service unavailable.', [])

def _handle_seller_dashboard_intent(prompt: str, user_id=None):
    """Handle seller dashboard insights and navigation."""
    try:
        low = (prompt or '').strip().lower()
        if not re.search(r"\b(seller dashboard|storefront dashboard|dashboard|analytics|performance|inventory|sales overview|seller analytics)\b", low):
            return (None, [])
        if not user_id:
            return ('Please sign in to view your seller dashboard.', [])
        from listings.models import Listing, OrderItem
        from storefront.models import Store
        listings_qs = Listing.objects.filter(Q(seller_id=user_id) | Q(store__owner_id=user_id))
        total_listings = listings_qs.count()
        active_listings = listings_qs.filter(is_active=True, is_sold=False).count()
        inactive_listings = listings_qs.filter(is_active=False).count()
        sold_listings = listings_qs.filter(is_sold=True).count()
        order_items = OrderItem.objects.filter(Q(listing__seller_id=user_id) | Q(listing__store__owner_id=user_id))
        seller_orders = order_items.values('order_id').distinct().count()
        revenue = order_items.aggregate(
            total=Sum(F('price') * F('quantity'), output_field=DecimalField(max_digits=12, decimal_places=2))
        ).get('total') or 0

        text = (
            f"Seller dashboard summary: {total_listings} listing(s) "
            f"({active_listings} active, {inactive_listings} inactive, {sold_listings} sold). "
            f"{seller_orders} order(s) containing your listings. Total revenue recorded: {revenue}."
        )
        items = [{
            'type': 'action_suggestion',
            'id': 'seller_dashboard',
            'title': 'Open seller dashboard',
            'reason': 'Manage listings, orders, and payouts.',
            'url': '/storefront/dashboard/',
        }]
        stores = Store.objects.filter(owner_id=user_id).order_by('-created_at')[:5]
        for s in stores:
            items.append({
                'type': 'store',
                'id': s.id,
                'name': s.name,
                'url': f"/storefront/dashboard/store/{s.slug}/",
                'reason': 'Manage this store.',
            })
        return (text, items)
    except Exception as e:
        logger.debug('_handle_seller_dashboard_intent error: %s', e)
        return ('Seller dashboard service unavailable.', [])

def _handle_order_intent(prompt: str, user_id=None):
    """Handle order‑related intents: track, list recent orders."""
    try:
        from listings.models import Order
        low = prompt.strip().lower()
        # Track order by number
        m = re.search(r"track\s+order\s*#?(\d+)", low)
        if m:
            if not user_id:
                return ('Please sign in to track your order details.', [])
            oid = int(m.group(1))
            o = Order.objects.filter(pk=oid, user_id=user_id).first()
            if o:
                items = o.order_items.select_related('listing')[:3]
                item_str = ', '.join([f"{it.listing.title} x{it.quantity}" for it in items])
                platform_items = [{
                    'type': 'order',
                    'id': o.id,
                    'status': o.status,
                    'total': str(o.total_price),
                    'items_preview': item_str,
                    'url': o.get_absolute_url() if hasattr(o, 'get_absolute_url') else None,
                }]
                return (f'Order #{o.id} — status: {o.status}. Total: {o.total_price}.', platform_items)
            else:
                return (f'Order #{oid} not found.', [])
        # List recent orders
        if re.search(r"\b(my orders|recent orders)\b", low):
            if not user_id:
                return ('Please sign in to view your orders.', [])
            orders = Order.objects.filter(user_id=user_id).order_by('-id')[:5]
            items = []
            for o in orders:
                items_preview = ', '.join([f"{it.listing.title} x{it.quantity}" for it in o.order_items.select_related('listing')[:3]])
                items.append({
                    'type': 'order',
                    'id': o.id,
                    'status': o.status,
                    'total': str(o.total_price),
                    'items_preview': items_preview,
                    'url': o.get_absolute_url() if hasattr(o, 'get_absolute_url') else None,
                })
            text = f'You have {len(orders)} recent order(s).' if orders else 'No orders found.'
            return (text, items)
    except Exception as e:
        logger.debug('_handle_order_intent error: %s', e)
    return (None, [])
