# ── Base ──────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    MODEL_PATH=/app/data/model.pkl

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libssl-dev libffi-dev build-essential supervisor \
    && rm -rf /var/lib/apt/lists/*

# ── Python deps (with retry on network failure) ───────────────────────────────
FROM base AS deps
COPY requirements.txt .

# pip install with 3 retries and 120s timeout per package
RUN pip install --upgrade pip --timeout 120 && \
    pip install --timeout 120 --retries 5 -r requirements.txt

# ── Runtime ───────────────────────────────────────────────────────────────────
FROM deps AS final

COPY . .

# Directories for logs and persisted ML model
RUN mkdir -p /app/logs /app/data \
    && addgroup --system bot \
    && adduser  --system --ingroup bot bot \
    && chown -R bot:bot /app

USER bot

EXPOSE 8000

# supervisord manages: bot API server + health watchdog (both auto-restart)
CMD ["supervisord", "-c", "/app/supervisord.conf"]

HEALTHCHECK --interval=20s --timeout=5s --start-period=35s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"

LABEL version="2.0.0" description="Polymarket bot — 24/7 supervisord + ML + FastAPI"
