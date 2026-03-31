#!/usr/bin/env bash
set -euo pipefail

echo "-> Repairing known migration-history issue if present"
python manage.py repair_migration_history || true

echo "-> Running database migrations"
python manage.py migrate --noinput

echo "-> Collecting static files"
python manage.py collectstatic --noinput
