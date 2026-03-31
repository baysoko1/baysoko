#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$APP_DIR"

echo "-> Railway startup: running migrations"
python manage.py migrate --noinput

echo "-> Railway startup: collecting static files if supported"
if python manage.py help collectstatic >/dev/null 2>&1; then
  python manage.py collectstatic --noinput || echo "-> collectstatic failed; continuing with WhiteNoise finder-based static serving"
else
  echo "-> collectstatic command unavailable; continuing with WhiteNoise finder-based static serving"
fi

echo "-> Railway startup: configuring OAuth providers if supported"
if python manage.py help configure_oauth >/dev/null 2>&1; then
  python manage.py configure_oauth
else
  echo "-> configure_oauth command unavailable; continuing because runtime OAuth callback uses SITE_URL directly"
fi

if [ -n "${DJANGO_SUPERUSER_EMAIL:-}" ] && [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
  echo "-> Railway startup: ensuring Django superuser exists"
  python create_superuser.py
fi

exec gunicorn baysoko.asgi:application -k uvicorn.workers.UvicornWorker -w 2 -b 0.0.0.0:${PORT}
