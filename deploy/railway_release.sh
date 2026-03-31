#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$APP_DIR"

echo "-> Running database migrations"
if python manage.py migrate --noinput; then
  echo "-> Collecting static files"
  python manage.py collectstatic --noinput

  echo "-> Configuring OAuth providers"
  python manage.py configure_oauth

  if [ -n "${DJANGO_SUPERUSER_EMAIL:-}" ] && [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
    echo "-> Ensuring Django superuser exists"
    python create_superuser.py
  else
    echo "-> Skipping superuser creation (DJANGO_SUPERUSER_EMAIL / DJANGO_SUPERUSER_PASSWORD not set)"
  fi
else
  echo "-> Release-phase database setup unavailable; deferring migrations/static/oauth setup to runtime startup"
fi
