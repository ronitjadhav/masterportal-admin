# Backend image. Migrations run on startup (init_db → alembic upgrade head) and
# an empty DB auto-seeds the bundled `basic` portal, so the container is useful
# on first boot. Single worker on purpose: admin sessions and the rate-limit
# windows are in-process (see PLAN.md) — scale out only once those move to Redis.
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
