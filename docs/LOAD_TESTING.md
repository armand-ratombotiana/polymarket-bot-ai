# Load Testing & Performance

Polymarket Pro's quality bar is enforced by a four-layer test pyramid. This
document maps each layer, the reference baselines, and the load-testing
strategy for the live HTTP surface.

## Test pyramid

| Layer      | Tooling                 | Count  | Location                                                  |
| ---------- | ----------------------- | ------ | --------------------------------------------------------- |
| Backend    | pytest                  | 709    | `mini-services/polymarket-bot/tests/`                     |
| Frontend   | vitest + Testing Library| 207    | `src/**/*.test.ts(x)`                                     |
| E2E        | Playwright              | 38     | `e2e/*.spec.ts`                                           |
| **Total**  |                         | **916+** |                                                            |

### Backend (pytest)

```bash
cd /home/z/my-project/mini-services/polymarket-bot
python -m pytest tests/ -v
```

Includes contract tests (`test_openapi.py`, 33 tests asserting the OpenAPI
schema, response models, and route summaries), cache tests (`test_cache.py`),
calibration tests (`test_calibration.py`), security tests (`test_security.py`),
and an end-to-end decision-chain test (`test_e2e_decision_chain.py`).

### Frontend (vitest)

```bash
cd /home/z/my-project
bun run test
```

Component tests (`*.test.tsx`), hook tests (`useWebSocket.test.ts`,
`useRealtimeData.test.ts`), and lib tests (`api.test.ts`, `schemas.test.ts`).

### E2E (Playwright)

```bash
cd /home/z/my-project
bun run e2e            # headless
bun run e2e:headed     # visible browser
bun run e2e:ui         # interactive UI mode
```

Three spec files cover the critical user paths:

- `e2e/dashboard.spec.ts` — dashboard renders, all 37 panels mount, polling
  doesn't crash on empty data.
- `e2e/navigation.spec.ts` — sidebar routing, deep-link refresh, back/forward.
- `e2e/api-health.spec.ts` — every public API endpoint returns expected shape.

## Performance baselines

Collected on the dev sandbox (single uvicorn worker, paper mode, cold cache).
Treat as **regression-detection** baselines, not SLAs.

| Metric                                  | Baseline          |
| --------------------------------------- | ----------------- |
| `GET /api/health` p50                   | < 5 ms            |
| `GET /api/positions` p50 (cached)       | < 15 ms           |
| `GET /api/ml/metrics` p50 (cached)      | < 25 ms          |
| `GET /api/analytics` p50 (cache miss)   | ~120 ms           |
| Frontend First Load JS (dashboard route)| ~180 KB (gzipped) |
| Playwright full suite (3 specs)         | ~25 s             |
| Backend full pytest suite               | ~60 s             |

## Load testing the HTTP surface

A checked-in [Locust](https://locust.io) load profile lives at
[`tests/load/locustfile.py`](../mini-services/polymarket-bot/tests/load/locustfile.py)
(W12-3), simulating a dashboard user polling the read-heavy endpoints with
realistic weights. A companion in-process benchmark module,
[`tests/load/test_benchmarks.py`](../mini-services/polymarket-bot/tests/load/test_benchmarks.py),
measures per-route p95 latency via `fastapi.testclient.TestClient` (isolating
route-handler cost from network/uvicorn overhead).

### Locust (end-to-end HTTP throughput)

```bash
pip install locust
cd /home/z/my-project/mini-services/polymarket-bot
locust -f tests/load/locustfile.py --host=http://localhost:8080
# Open http://localhost:8089, set spawn rate & peak users.
```

The shipped traffic profile (weights in the locustfile):

| Endpoint                  | Weight | Notes                                  |
| ------------------------- | ------ | -------------------------------------- |
| `GET /api/snapshot`       | 10     | polled every 0.5–2s by the dashboard  |
| `GET /api/positions`      | 8      | polled frequently                      |
| `GET /api/orders`         | 8      | polled frequently                      |
| `GET /api/markets`        | 6      | cached                                 |
| `GET /api/ml/metrics`     | 4      | cached                                 |
| `GET /api/analytics`      | 3      | cached                                 |
| `POST /api/order`         | 1      | paper mode — tests write path         |

### Benchmarks (in-process p95 latency)

```bash
cd /home/z/my-project/mini-services/polymarket-bot
python -m pytest tests/load/test_benchmarks.py -v
```

Each endpoint is hit 20× sequentially via `TestClient`; the p95 is gated at
`target_ms * 2` (2× headroom for the slower CI/test environment over the
production target).

Watch for:

- **429 responses** — rate limiter kicking in (120/min read, 30/min write,
  5/min heavy by default; tune in `api/rate_limit.py`).
- **p99 latency growth** — usually a cache-miss cascade or a missing index
  (run `scripts/optimize_db.py`, see [MAINTENANCE.md](MAINTENANCE.md)).
- **Connection exhaustion** — uvicorn default pool; raise `--workers` or
  front with Caddy connection reuse.

## Frontend performance

See [PERFORMANCE.md](PERFORMANCE.md) for the React-side patterns (memoization,
virtualisation, polling backoff, skeleton loading) and
[BUILD_OPTIMIZATION.md](BUILD_OPTIMIZATION.md) for bundle-analysis tooling.

## See also
- [BUILD_OPTIMIZATION.md](BUILD_OPTIMIZATION.md) — bundle analyzer
- [MAINTENANCE.md](MAINTENANCE.md) — DB optimisation & health checks
- [PERFORMANCE.md](PERFORMANCE.md) — frontend performance patterns
