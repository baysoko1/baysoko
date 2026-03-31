#!/usr/bin/env bash
set -euo pipefail

echo "-> Running database migrations"
python manage.py migrate --noinput

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
