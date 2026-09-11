# Platform Metrics Summary

A one-glance reference for the scale of the Polymarket Pro platform.
Numbers reflect the Wave 13–14 release (post-unreleased changes);
see [CHANGELOG.md](../CHANGELOG.md) for the version history and
[ARCHITECTURE.md](ARCHITECTURE.md) for the full system design.

## Code

- **Backend**: Python 3.12, FastAPI 0.111+, uvicorn[standard], Pydantic v2
- **Frontend**: Next.js 16, React 19, TypeScript 5, Tailwind CSS 4, shadcn/ui
- **Test framework**: pytest (backend), vitest (frontend), Playwright (E2E)
- **Observability**: Prometheus + Grafana + structured JSON logging
- **Gateway**: Caddy (port 81) with `?XTransformPort=` query-param routing
- **Containerization**: Docker multi-stage builds + docker-compose
- **CI/CD**: GitHub Actions (frontend lint+test, backend test, production build)

## Scale

| Metric                    | Count            |
| ------------------------- | ---------------- |
| Backend tests             | 970+             |
| Frontend tests            | 459+             |
| E2E tests                 | 38               |
| Total tests               | 1429+            |
| API routes                | 90+              |
| API route modules         | 13               |
| UI components             | 55+              |
| Recharts chart primitives | 5                |
| Backend Python files      | 160+             |
| Frontend TS / TSX files    | 150+             |
| Backend test files        | 71               |
| Frontend test files       | 20               |
| Documentation files       | 20+              |
| Operational scripts       | 17+              |
| Migration SQL files       | 2                |
| Feature flags             | 13               |
| i18n locales              | 2 (EN + FR)      |
| WebSocket channels        | 5                |
| ML ensemble models        | 4 (RF, GB, SGD, LightGBM) |
| Live safety checks       | 10               |
| P&L attribution buckets  | 7                |

## Features

### Trading

- Paper trading mode
- Marketable SL/TP (crosses spread at best_bid)
- Inventory flush (marketable SELL when over horizon)
- Per-trade circuit breaker (300s strategy cooldown)
- Smart order routing
- External-API circuit breaker (Polymarket / Gamma / CLOB)

### ML / AI

- 4-model ensemble + Level-2 meta-learner
- Walk-forward cross-validation
- Drift detection (PSI, KS, Brier)
- Probability calibration (Platt + isotonic)
- Label backfill from resolved markets
- Shadow inference (challenger models)
- A/B testing framework
- Advanced backtest (walk-forward + Monte Carlo)

### Risk

- Kill switch (file-backed + in-memory)
- Max drawdown circuit breaker
- MTM risk gate
- 10-check live safety gate (fail-closed)
- Capital allocator (saturating edge curve, $3 cap)
- Per-trade + external-API circuit breakers

### Observability

- Decision ledger (full chain traceability)
- Execution quality tracking (slippage, latency, realised edge)
- 7-dimension P&L attribution
- 31 auto-collected system metrics
- Prometheus `/metrics` endpoint
- Grafana dashboard (auto-provisioned)
- Alerting system (severity-tagged, ack/resolve workflow)
- Audit log viewer (severity filter, CSV + JSON export)
- Rate limit tracker (per-IP / per-endpoint, last 1h)
- Performance profiling (cProfile + p50/p95/p99)
- Frontend error reporting (Sentry-like client-side crash reporter)

### Infrastructure

- Docker containerization
- CI/CD (GitHub Actions)
- Feature flags (13 default, runtime-toggleable)
- API versioning (`/api/v1/...` prefix + deprecation header)
- Rate limiting (slowapi, 6 policy tiers)
- Circuit breaker (external APIs)
- DB migration system (idempotent + applied on startup)
- Backup + verification + rotation (GFS: 7d / 4w / 12m / 90d cap)
- Backup integrity checker (PRAGMA + orphan checks)
- Restore round-trip test (per-DB content hashing)
- Structured JSON logging
- WebSocket broadcast layer (5 channels)

### Frontend

- 55+ React panels
- 5 Recharts chart primitives (EquityCurveChart, PnLBarChart, Sparkline,
  GaugeChart, ReliabilityDiagram)
- Dark / light theme switcher (next-themes, class-based, persisted)
- Command palette (Cmd+K, 25+ nav entries + 6 page actions)
- i18n (EN / FR, next-intl + useTranslation hook)
- PWA (offline support, service worker, installable)
- Browser push notifications (Web Notifications API + useNotifications hook)
- Framer Motion animations
- Recharts visualizations
- Error boundaries + client-side error reporting
- WebSocket real-time updates (5 channels, auto-reconnect)
- Visibility-aware polling (paused on hidden tab, refetch on regain)
- WCAG 2.1 AA accessibility (skip link, focus-visible, ARIA, focus trap)
- User preferences (theme, locale, notifications, sidebar, audio)
