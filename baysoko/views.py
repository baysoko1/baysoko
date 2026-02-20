from django.shortcuts import render
from django.http import JsonResponse
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
