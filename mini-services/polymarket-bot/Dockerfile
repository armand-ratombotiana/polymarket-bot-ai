# ──────────────────────────────────────────────────────────────────────────────
# Polymarket Bot — Backend (FastAPI + uvicorn)
# Python 3.12-slim base; installs system deps for LightGBM/numpy compilation.
# ──────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim

WORKDIR /app

# Set deterministic build flags; keep build deps small.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# LightGBM + numpy wheels are prebuilt for manylinux, but keep build-essential
# available for any source-built fallback (e.g. eth-account C-extensions).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (better layer caching).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source. `.dockerignore` excludes tests/, __pycache__, data/.
COPY . .

# Ensure runtime data directory exists (mounted as a volume in compose).
RUN mkdir -p /app/data

# FastAPI server — uvicorn entrypoint matches `api/server.py:app`.
EXPOSE 8080

# Health probe (used by docker-compose `healthcheck`).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["python", "-m", "uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8080"]
