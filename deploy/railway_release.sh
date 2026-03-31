#!/usr/bin/env bash
set -euo pipefail

echo "-> Running database migrations"
python manage.py migrate --noinput

echo "-> Collecting static files"
python manage.py collectstatic --noinput
