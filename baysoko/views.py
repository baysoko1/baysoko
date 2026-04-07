from django.shortcuts import render
from django.http import JsonResponse, HttpResponse
from django.contrib.staticfiles import finders
from django.views.decorators.csrf import csrf_exempt
import json
import os
from django.conf import settings

def custom_error_500(request):
    """Custom handler for server errors (500)"""
    response = render(request, '500.html', status=500)
    return response


def custom_error_403(request, exception=None):
    """Custom handler for permission errors (403)"""
    response = render(request, '403.html', status=403)
    return response


def custom_error_404(request, exception=None):
    """Custom handler for page not found errors (404)"""
    response = render(request, '404.html', status=404)
    return response


@csrf_exempt
def client_error_log(request):
    """Receive client-side error reports and persist them to a log file.

    Expected JSON POST payload: { message, filename, lineno, colno, stack }
    This is best-effort: returns 204 on success.
    """
    try:
        if request.method != 'POST':
            return JsonResponse({'ok': False, 'error': 'POST required'}, status=405)
        payload = json.loads(request.body.decode('utf-8') or '{}')
        log_dir = os.path.join(settings.BASE_DIR, 'logs')
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, 'client_errors.log')
        with open(log_file, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({
                'path': request.path,
                'payload': payload
            }, ensure_ascii=False) + '\n')
        return JsonResponse({'ok': True}, status=204)
    except Exception as e:
        try:
            return JsonResponse({'ok': False, 'error': str(e)}, status=500)
        except Exception:
            return JsonResponse({'ok': False}, status=500)


def health(request):
    """Simple health check used by load balancers and platform health checks."""
    return JsonResponse({'status': 'ok'}, status=200)


def service_worker(request):
    """Serve the PWA service worker at the root scope."""
    sw_path = finders.find('service-worker.js')
    if not sw_path:
        return HttpResponse('', content_type='application/javascript', status=404)
    try:
        with open(sw_path, 'rb') as fh:
            return HttpResponse(fh.read(), content_type='application/javascript')
    except Exception:
        return HttpResponse('', content_type='application/javascript', status=500)


def manifest(request):
    """Serve the PWA manifest at the root for installability."""
    manifest_path = finders.find('manifest.json')
    if not manifest_path:
        return HttpResponse('', content_type='application/manifest+json', status=404)
    try:
        with open(manifest_path, 'rb') as fh:
            return HttpResponse(fh.read(), content_type='application/manifest+json')
    except Exception:
        return HttpResponse('', content_type='application/manifest+json', status=500)


def assetlinks(request):
    """Serve Android Digital Asset Links for TWA / App Links verification."""
    package_name = (getattr(settings, 'ANDROID_APP_PACKAGE', '') or '').strip()
    fingerprints_raw = (getattr(settings, 'ANDROID_APP_SHA256', '') or '').strip()
    fingerprints = [fp.strip() for fp in fingerprints_raw.split(',') if fp.strip()]
    payload = []
    if package_name and fingerprints:
        payload.append({
            "relation": [
                "delegate_permission/common.handle_all_urls"
            ],
            "target": {
                "namespace": "android_app",
                "package_name": package_name,
                "sha256_cert_fingerprints": fingerprints,
            },
        })
    return JsonResponse(payload, safe=False)


@csrf_exempt
def pwa_install_event(request):
    """Record a best-effort PWA install event."""
    try:
        if request.method != 'POST':
            return JsonResponse({'ok': False}, status=405)
        payload = {
            'user_id': getattr(request.user, 'id', None) if getattr(request, 'user', None) and request.user.is_authenticated else None,
            'user_agent': request.META.get('HTTP_USER_AGENT', ''),
            'ip': request.META.get('REMOTE_ADDR', ''),
        }
        try:
            log_dir = os.path.join(settings.BASE_DIR, 'logs')
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, 'pwa_installs.log')
            with open(log_file, 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(payload, ensure_ascii=False) + '\n')
        except Exception:
            pass
        return JsonResponse({'ok': True})
    except Exception:
        return JsonResponse({'ok': False}, status=500)
