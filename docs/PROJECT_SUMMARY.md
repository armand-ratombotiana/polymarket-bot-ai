# Polymarket Pro — Project Summary

**One-page snapshot of the entire platform.** For depth, follow the links in
the [Documentation Index](README.md).

## What it is

Polymarket Pro is an institutional-grade algorithmic trading bot for
[Polymarket](https://polymarket.com) prediction markets. It pairs a 4-model ML
ensemble with a Level-2 meta-learner, a 10-check live safety gate, full
decision auditability, paper-trading-by-default semantics, and a 37-panel React
workstation — so every PREDICTION → SIGNAL → RISK → ORDER → FILL chain can be
reconstructed, attributed, and stress-tested end-to-end.

## Key metrics

| Metric                | Value                                                                |
| --------------------- | -------------------------------------------------------------------- |
| Total tests           | 916+ (709 backend pytest + 207 frontend vitest + 38 Playwright E2E) |
| API routes            | 77+, all rate-limited, OpenAPI-documented (21 tags, 11 response models) |
| UI panels             | 37 React panels (WCAG 2.1 AA, dark theme, responsive)               |
| ML models             | 4-model ensemble + Level-2 meta-learner (RF / GB / SGD / LightGBM)   |
| Real-time channels    | 5 WebSocket channels + REST polling fallback                        |
| SQLite stores         | 7 (observability, decisions, execution-quality, audit, positions, market intel, shadow trades) |
| Caches                | 6 TTLCache instances (hot-path API responses)                        |
| Documentation files   | 13 (architecture, API, security, deployment, maintenance, …)        |

## Architecture at a glance

```
┌─────────────────┐     ┌──────────────┐     ┌──────────────────────────────┐
│  Browser (PWA)  │────▶│  Caddy :81   │────▶│  Next.js 16 (standalone) :3000│
│  37 panels + SW │     │  TLS + auth  │     │  React 19 + Tailwind v4      │
└─────────────────┘     └──────┬───────┘     └──────────────────────────────┘
                               │ ?XTransformPort=8080
                               ▼
                    ┌──────────────────────────────┐
                    │  FastAPI :8080               │
                    │  77 routes · rate-limited     │
                    │  OpenAPI · WebSocket /ws     │
                    │  TTLCache · feature flags    │
                    └──────┬───────────────────────┘
                           │
        ┌──────────┬───────┴────────┬──────────┬───────────────┐
        ▼          ▼                ▼          ▼               ▼
   ┌─────────┐ ┌─────────┐  ┌──────────────┐ ┌─────────┐ ┌───────────┐
   │ ML      │ │ Risk    │  │ Strategies   │ │ Capital │ │ Decision  │
   │ ensemble│ │ manager │  │ signal/mm/arb│ │ alloc.  │ │ ledger    │
   │ + meta  │ │ + 10-   │  │ + paper sim  │ │ + MTM   │ │ (SQLite)  │
   │ + calib │ │ check   │  │ + smart route│ │         │ │           │
   └─────────┘ └─────────┘  └──────────────┘ └─────────┘ └───────────┘
        │                                                       │
        ▼                                                       ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  7 SQLite stores + model.pkl + vector_store.npz (data/)         │
   └──────────────────────────────────────────────────────────────────┘
                           ▲
                           │  Polymarket Gamma + CLOB APIs (paper / live)
```

## Feature list

### Trading
- Paper-trading-by-default; live trading gated behind a 10-check safety gate
- Marketable SL/TP (crosses spread to best bid), inventory flush, per-trade circuit breaker
- Smart router, paper simulator with realistic slippage model

### ML / AI
- 4-model ensemble (RF, GB, SGD, LightGBM) + Level-2 LogisticRegression meta-learner
- Platt scaling + isotonic regression probability calibration
- Walk-forward CV, PSI/KS/Brier drift detection, label backfill
- Shadow inference (challenger comparison), model registry + promotion gate
- AI copilot (natural-language market Q&A)

### Risk
- Kill switch (file-backed + in-memory), max-drawdown circuit breaker
- MTM risk gate, per-trade circuit breaker, 10-check live safety gate
- 7-dimension P&L attribution

### Real-time
- WebSocket multiplexed push (positions / orders / trades / metrics / alerts)
- `useWebSocket` (low-level) + `useRealtimeData` (hybrid REST+WS) hooks
- 10s polling fallback, tab-hidden suppression, auto-reconnect

### Observability
- 31 auto-collected metrics across 6 categories
- Decision-ledger audit chain (correlation-ID linked PREDICTION → … → FILL)
- Structured JSON logging + colored dev formatter
- Bounded retention (7d / 30d / 90d) across the SQLite stores

### Frontend
- 37 React panels, dark theme, responsive, WCAG 2.1 AA
- PWA (manifest, service worker, offline indicator)
- Error boundaries (root + panel), skeleton loading, Framer Motion
- Zod runtime response validation, typed API client utilities

### Operations
- Docker multi-stage builds, docker-compose, Caddy gateway, supervisord
- Database optimisation script (`scripts/optimize_db.py`)
- Bundle analyzer (`@next/bundle-analyzer` + `scripts/analyze-bundle.sh`)
- Health endpoints, cache stats, OpenAPI/Swagger UI

### Security
- Bearer-token auth (HMAC constant-time compare, fail-closed)
- OWASP Top 10 hardening (SSRF guard, token-strength validator, security headers)
- Public-path allowlist, live-mode doc lockdown

## Getting started

See the [main README](../README.md) → **Quick Start** section. In short:

```bash
# Backend
cd /home/z/my-project/mini-services/polymarket-bot
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
set -a && . ./.env && set +a
python3 -m uvicorn api.server:app --port 8080 --reload

# Frontend (separate shell)
cd /home/z/my-project
bun install
bun run dev          # http://localhost:3000
```

## Documentation map

See [`README.md`](README.md) (this directory) for the full doc index.

## Status

Production-ready for paper trading. Live trading is gated behind the 10-check
safety gate and is **not** enabled by default. Use at your own risk — see the
[Disclaimer](../README.md#disclaimer).
