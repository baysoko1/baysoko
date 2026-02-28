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
        ,
        'check-trial-expirations-daily': {
            'task': 'storefront.tasks.check_trial_expirations',
            'schedule': crontab(minute=10, hour=3),
            'options': {'queue': 'periodic'},
        },
        'send-weekly-reactivation-reminders': {
            'task': 'storefront.tasks.send_weekly_reactivation_reminders',
            # Every Monday at 04:00 UTC
            'schedule': crontab(minute=0, hour=4, day_of_week=1),
            'options': {'queue': 'periodic'},
        }
    })
except Exception:
    # If celery.schedules isn't available at import time, skip schedule setup
    pass

# Ensure Celery uses Django TIME_ZONE
try:
    from django.conf import settings as _dj_settings
    app.conf.timezone = getattr(_dj_settings, 'TIME_ZONE', 'UTC')
    # Keep enable_utc True to ensure consistent UTC handling internally
    app.conf.enable_utc = True
except Exception:
    pass

# Trigger a one-off startup reminders run when the worker is ready.
try:
    from celery.signals import worker_ready

    @worker_ready.connect
    def _on_worker_ready(sender, **kwargs):
        """Run startup reminders once when the first worker starts.

        Uses cache-based guard inside the task itself to avoid duplicates.
        """
        try:
            # Import and call the trigger task (delay to run in background)
            from storefront.tasks import trigger_startup_reminders
            try:
                trigger_startup_reminders.delay()
            except Exception:
                # As a fallback, call synchronously (best-effort)
                try:
                    trigger_startup_reminders()
                except Exception:
                    pass
        except Exception:
            # Do not raise on worker start errors
            pass
except Exception:
    pass

__all__ = ('app',)
