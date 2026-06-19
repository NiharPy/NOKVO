# syntax=docker/dockerfile:1
#
# NOKVO API — production image for Azure Container Apps.
#
# Build for linux/amd64 (Container Apps runs amd64; dev machines are arm64):
#   docker buildx build --platform linux/amd64 -t nokvo-api:local .
#
# The same image runs the migration job (command override: `alembic upgrade head`)
# and the web app, so migrations always match the deployed code.

# ---------- Stage 1: builder ----------
FROM python:3.13-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# build-essential + libffi-dev cover any sdist that lacks a manylinux wheel
# (argon2-cffi, cffi). Most deps ship wheels, so this is belt-and-suspenders.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# ---------- Stage 2: runtime ----------
FROM python:3.13-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:$PATH"

# libsndfile1: insurance for soundfile (pulled in transitively). pyrnnoise's
# native lib is bundled in its wheel; nothing shells out to ffmpeg at runtime,
# so we deliberately skip the heavy ffmpeg binary to keep the image small.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user.
RUN groupadd -r app && useradd -r -g app -d /app app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=app:app app ./app
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app alembic.ini ./alembic.ini

USER app

EXPOSE 8000

# 1 worker/replica — scale via Container Apps replicas, not workers (keeps the
# DB-pool math and the concurrency scaler clean). --proxy-headers so the
# Container Apps ingress's X-Forwarded-Proto/Host drive public_url.py + HTTPS.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
