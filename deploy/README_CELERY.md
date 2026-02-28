Celery + Beat deployment (recommended)

This project includes example deployment artifacts to run Celery worker and Celery Beat.

Options:

1) Systemd units
- Edit files in `deploy/systemd/` to set `EnvironmentFile`, `WorkingDirectory`, and `ExecStart` paths.
- Copy units to `/etc/systemd/system/` then:
  ```bash
  sudo systemctl daemon-reload
  sudo systemctl enable --now celery-beat
  sudo systemctl enable --now celery-worker
  ```

2) Docker Compose
- Use `deploy/docker-compose.celery.yml` as an example to run services and Redis.
- Start with:
  ```bash
  docker compose -f deploy/docker-compose.celery.yml up -d --build
  ```

3) Development
- Run Redis locally (or use `REDIS_URL` to a hosted instance), then run:
  ```bash
  celery -A baysoko worker --loglevel=info
  celery -A baysoko beat --loglevel=info
  ```

Notes:
- Ensure `CELERY_BROKER_URL` and `CELERY_RESULT_BACKEND` are set in environment.
- On worker startup the app triggers `trigger_startup_reminders` via `worker_ready`, which uses cache to avoid multiple runs.
