#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$APP_DIR"

echo "-> Railway startup: running migrations"
python manage.py migrate --noinput

echo "-> Railway startup: collecting static files"
python manage.py collectstatic --noinput

echo "-> Railway startup: configuring OAuth providers"
python manage.py configure_oauth

if [ -n "${DJANGO_SUPERUSER_EMAIL:-}" ] && [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
  echo "-> Railway startup: ensuring Django superuser exists"
  python create_superuser.py
fi

exec gunicorn baysoko.asgi:application -k uvicorn.workers.UvicornWorker -w 2 -b 0.0.0.0:${PORT}
