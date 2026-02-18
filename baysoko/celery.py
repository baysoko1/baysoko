import os
import sys
import importlib
from django.conf import settings

# Try to import Celery normally; if a local module named 'celery' shadows the
# installed package, attempt to load the real package from site-packages.
try:
    from celery import Celery
except Exception:
    # Attempt to locate and load the installed 'celery' package using PathFinder
    from importlib.machinery import PathFinder
    from importlib import util
    real_celery = None
    project_root = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
    for p in sys.path:
        try:
            if not p:
                continue
            p_abs = os.path.abspath(p)
            # Skip project directories to avoid local shadowing
            if p_abs.startswith(project_root):
                continue
            spec = PathFinder.find_spec('celery', [p_abs])
            if spec and spec.loader:
                module = util.module_from_spec(spec)
                spec.loader.exec_module(module)
                real_celery = module
                break
        except Exception:
            continue

    if real_celery is None:
        raise

    Celery = getattr(real_celery, 'Celery')

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'baysoko.settings')

app = Celery('baysoko')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks(lambda: settings.INSTALLED_APPS)

app.conf.update(
    broker_url=getattr(settings, 'CELERY_BROKER_URL', None),
    result_backend=getattr(settings, 'CELERY_RESULT_BACKEND', None),
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone=getattr(settings, 'TIME_ZONE', 'UTC'),
    enable_utc=True,
)

# Periodic tasks (Celery Beat)
try:
    from celery.schedules import crontab
    # Run subscription expiration check daily at 03:00 UTC
    app.conf.beat_schedule = getattr(app.conf, 'beat_schedule', {})
    app.conf.beat_schedule.update({
        'check-active-subscription-expirations-daily': {
            'task': 'storefront.tasks.check_active_subscription_expirations',
            'schedule': crontab(minute=0, hour=3),
            'options': {'queue': 'periodic'},
        }
    })
except Exception:
    # If celery.schedules isn't available at import time, skip schedule setup
    pass

__all__ = ('app',)
