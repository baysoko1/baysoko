#!/usr/bin/env bash
# render_start.sh - run migrations, collectstatic, then start Daphne
set -euo pipefail

echo "-> Running database migrations"
python manage.py migrate --noinput

echo "-> Collecting static files"
python manage.py collectstatic --noinput

# Optional: create superuser if env provided
if [ -n "${DJANGO_SUPERUSER_EMAIL:-}" ] && [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
  echo "-> Creating superuser if not exists"
  python manage.py shell -c "from django.contrib.auth import get_user_model;User=get_user_model();User.objects.filter(email='$DJANGO_SUPERUSER_EMAIL').exists() or User.objects.create_superuser(email='$DJANGO_SUPERUSER_EMAIL',username='$DJANGO_SUPERUSER_USERNAME',password='$DJANGO_SUPERUSER_PASSWORD')"
fi

# Start Daphne (Render provides $PORT)
PORT=${PORT:-8000}
exec daphne -b 0.0.0.0 -p $PORT baysoko.asgi:application
