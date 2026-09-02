# ── Base ──────────────────────────────────────────────────────────────────────
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=300 \
    MODEL_PATH=/app/data/model.pkl

WORKDIR /app

# ── Python deps (with retry on network failure) ───────────────────────────────
FROM base AS deps
COPY requirements.txt .

# pip install with 10 retries and 300s timeout per operation
# (PyPI can be slow/flaky — retries absorb transient timeouts)
RUN pip install --upgrade pip --timeout 300 && \
    pip install --timeout 300 --retries 10 -r requirements.txt

# supervisor installed via pip — avoids apt-get entirely (Debian CDN port 80
# is blocked from docker bridge networks on some hosts; PyPI over HTTPS works).
RUN pip install --no-cache-dir --timeout 300 --retries 10 supervisor

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

HEALTHCHECK --interval=20s --timeout=5s --start-period=60s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"

LABEL version="2.0.0" description="Polymarket bot — 24/7 supervisord + ML + FastAPI"
