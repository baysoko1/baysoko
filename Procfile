release: bash deploy/railway_release.sh
web: gunicorn baysoko.asgi:application -k uvicorn.workers.UvicornWorker -w 2 -b 0.0.0.0:$PORT
