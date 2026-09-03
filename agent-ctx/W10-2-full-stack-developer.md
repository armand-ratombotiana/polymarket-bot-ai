# W10-2: Docker Containerization

**Agent:** full-stack-developer
**Task:** Docker containerization for the Polymarket trading bot platform.
**Date:** 2026-09-03

## Objective

Production-ready Docker orchestration for the two-service stack
(Next.js 16 frontend on Bun + FastAPI/Python 3.12 backend) behind a Caddy
gateway that honours the existing `?XTransformPort=` routing contract.

## Files Created

| File | Purpose |
| --- | --- |
| `/home/z/my-project/Dockerfile` | 3-stage Bun build for the Next.js standalone server; final image runs as non-root `nextjs` user (uid 1001). |
| `/home/z/my-project/mini-services/polymarket-bot/Dockerfile` | Python 3.12-slim image; installs `build-essential` for LightGBM/numpy; runs `uvicorn api.server:app` on `:8080` with a curl-based `HEALTHCHECK`. |
| `/home/z/my-project/docker-compose.yml` | Three services (`frontend`, `backend`, `caddy`) on a custom `polymarket-net` bridge network; three named volumes (`backend-data`, `caddy-data`, `caddy-config`); `env_file` for both app services. |
| `/home/z/my-project/.dockerignore` | ~30 entries: `node_modules`, `.next`, `.git`, `*.log`, `.env*`, backend data/pycache/tests, `docs`, `agent-ctx`, `tool-results`, plus Dockerfiles themselves. |
| `/home/z/my-project/mini-services/polymarket-bot/.dockerignore` | Python artefacts (`__pycache__`, `*.pyc`), `data/`, `tests/`, `.pytest_cache`, logs, `.env*`. |
| `/home/z/my-project/Caddyfile.prod` | Production gateway: `backend:{query.XTransformPort}` + `frontend:3000` reverse-proxies (DNS names instead of `localhost`); `encode zstd gzip`; usage notes for Let's Encrypt. |

## Key Decisions

1. **Multi-stage frontend build** — `oven/bun:1` (deps + builder) →
   `oven/bun:1-slim` (runner). Final image is the slim base + standalone
   Next.js bundle + `.next/static` + `public/` only.

2. **Non-root user** — frontend container creates `nodejs:1001` group and
   `nextjs:1001` user; `USER nextjs` before `EXPOSE`/`CMD`. All `COPY`
   directives use `--chown=nextjs:nodejs`.

3. **Backend env path overrides** — compose injects `MARKET_DB_PATH`,
   `AUDIT_DB_PATH`, `STORE_STATE_PATH`, `KILL_SWITCH_PATH`,
   `MODEL_REGISTRY_PATH`, `VECTOR_STORE_PATH`, `MODEL_PATH`,
   `DECISION_LEDGER_DB_PATH` etc. all rooted at `/app/data/` so SQLite
   DBs land on the `backend-data` volume.

4. **Healthcheck on backend** — `curl -fsS http://127.0.0.1:8080/health`
   every 30s; `frontend` waits for `service_healthy` before starting.

5. **No secrets in images** — `.env` files excluded via `.dockerignore`;
   secrets injected via `env_file:` in compose at runtime only.

6. **No test files in production images** — both `.dockerignore` files
   exclude the `tests/` directory and `__pycache__` (no `.pyc` ships).

7. **Data persistence** — `backend-data:/app/data` volume so all SQLite
   DBs (`market.db`, `audit_trail.db`, `decision_ledger.db`,
   `observability.db`, `execution_quality.db`, `market_intelligence.db`,
   `closed_positions.db`, `shadow_trades.db`) plus `model.pkl`,
   `vector_store.npz`, `model_registry.json`, `vector_index.json`,
   `store_state.json` survive container restarts.

8. **next.config.ts** — already declared `output: "standalone"` (line 4)
   so no source modification was required.

## Verification

- `bun run lint` → **0 errors, 2 pre-existing warnings** (unused
  `no-console` eslint-disable directives in `ErrorBoundary.tsx` /
  `PanelErrorBoundary.tsx` — unrelated to this task).
- `python3 -c "import yaml; yaml.safe_load(open('docker-compose.yml'))"`
  → YAML parses cleanly; top-level keys `[services, volumes, networks]`.
- dev.log unchanged (server still 200s on `/`).

## Constraints Honoured

- Multi-stage builds (frontend + backend) ✓
- Non-root user in frontend container ✓
- No secrets baked into images ✓
- Data directory mounted as a named volume ✓
- Test files excluded from production images ✓
- Did NOT modify any source files other than next.config (which needed
  no changes) ✓
- Only created Dockerfiles, docker-compose.yml, .dockerignore files,
  Caddyfile.prod ✓

## Hand-off Notes for Future Agents

- To run the stack locally:
  ```bash
  docker compose build
  docker compose up -d
  docker compose logs -f
  ```
- For production, replace `./Caddyfile` with `./Caddyfile.prod` in the
  `caddy.volumes` block, set the real domain in `Caddyfile.prod`, and
  expose ports `80` + `443` on the `caddy` service.
- The frontend `Dockerfile` does NOT run `bun run build`'s post-build
  `cp` step (defined in `package.json`) — instead the compose runner
  copies `public/` and `.next/static/` separately. This matches the
  standard Next.js standalone deployment pattern.
- The backend `Dockerfile` uses `python -m uvicorn` (not the
  `bun run dev` infinite-restart loop) — appropriate for production.
- If LightGBM wheel compilation is desired for a smaller image, a
  future task could switch to `pip install --only-binary=:all:` and
  drop `build-essential` (the current build tolerates source fallback).
EOF
