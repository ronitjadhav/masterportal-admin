# Backend image. Migrations run on startup (init_db → alembic upgrade head) and
# an empty DB auto-seeds the bundled `basic` portal, so the container is useful
# on first boot. Admin sessions are DB-backed, so this scales to multiple
# workers/replicas safely; the only remaining in-process state is the rate-limit
# window, which simply becomes per-worker (approximate, still bounds abuse) until
# it moves to a shared store. Default one worker; add `--workers N` to scale.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
