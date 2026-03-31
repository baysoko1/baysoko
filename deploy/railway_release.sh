#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$APP_DIR"

echo "-> Running database migrations"
if python manage.py migrate --noinput; then
  echo "-> Collecting static files if supported"
  if python manage.py help collectstatic >/dev/null 2>&1; then
    python manage.py collectstatic --noinput || echo "-> collectstatic failed; continuing with WhiteNoise finder-based static serving"
  else
    echo "-> collectstatic command unavailable; continuing with WhiteNoise finder-based static serving"
  fi

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
