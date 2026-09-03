# Documentation Index

This is the map of all Polymarket Pro documentation. Every link below resolves
to a file in the repository — if a link is broken, it is a bug.

> Run `python3 scripts/check_docs.py` to verify all internal links, fenced
> code blocks, heading hierarchy, and GFM tables across the doc set.

## Getting Started
- [README](../README.md) — Project overview, quick start, architecture summary
- [CONTRIBUTING](../CONTRIBUTING.md) — How to contribute (style, tests, PR flow)
- [.env.example](../.env.example) — Environment variables reference
- [METRICS_SUMMARY](METRICS_SUMMARY.md) — One-glance platform metrics (tests, routes, features)

## Architecture
- [ARCHITECTURE.md](ARCHITECTURE.md) — System design (services, data flow, contracts, Wave 13–14 additions)
- [API.md](API.md) — All 90+ API routes (request/response, auth, error codes)
- [API_CLIENT.md](API_CLIENT.md) — Typed frontend API client utilities
- [WEBSOCKET.md](WEBSOCKET.md) — WebSocket real-time push (5 channels, hooks, fallback)

## Operations
- [DEPLOYMENT.md](DEPLOYMENT.md) — Production deployment (Docker, Caddy, supervisord)
- [MAINTENANCE.md](MAINTENANCE.md) — Backup, restore, DB maintenance, retention, verification
- [LOAD_TESTING.md](LOAD_TESTING.md) — Performance & load-testing strategy
- [BUILD_OPTIMIZATION.md](BUILD_OPTIMIZATION.md) — Bundle analysis & optimization

## Quality
- [PERFORMANCE.md](PERFORMANCE.md) — Frontend performance patterns
- [ACCESSIBILITY.md](ACCESSIBILITY.md) — WCAG 2.1 AA compliance
- [SECURITY.md](SECURITY.md) — OWASP Top 10 security hardening + penetration tests

## Reference
- [PROJECT_SUMMARY.md](PROJECT_SUMMARY.md) — One-page platform summary
- [CHANGELOG.md](../CHANGELOG.md) — Version history (Wave 1.0 → 13–14 unreleased)
- [LICENSE](../LICENSE) — MIT license

## Reassessments
- [reassessment/FINAL_SYSTEM_REASSESSMENT.md](reassessment/FINAL_SYSTEM_REASSESSMENT.md) — Historical system audit

## Operations tooling reference
- [systemd/README.md](systemd/README.md) — systemd unit files for production deployment
