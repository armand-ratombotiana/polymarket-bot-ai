# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Playwright E2E tests (38 tests: dashboard, navigation, API health)
- Backend caching layer (TTLCache: 6 caches, stats + clear endpoints)
- OpenAPI/Swagger enhancement (21 tags, 11 Pydantic response models)
- WebSocket real-time hooks (useWebSocket, useRealtimeData)
- ML probability calibration (Platt scaling, isotonic regression)
- OWASP security audit (constant-time comparison, security headers)
- PWA support (manifest, service worker, offline indicator)
- Database indexes optimization (6 modules)
- Feature flags system (13 default flags, runtime toggle)
- Backup/restore scripts (backup.sh, restore.sh, db-maintenance.sh)
- Load testing (locust + performance benchmarks)
- Bundle analyzer (@next/bundle-analyzer)
- Health check scripts (health_check.py, monitor.py)
- Structured JSON logging (JSONFormatter, ColoredFormatter)
- Storybook component stories
- Typed API client SDK (17 namespaces, 60+ methods)
- Comprehensive maintenance documentation
- **Prometheus metrics endpoint** (`GET /metrics`) + Grafana dashboard
  (provisioned datasource + dashboard JSON under `grafana/`)
- **External-API circuit breaker** (`core/circuit_breaker.py`) wrapping every
  outbound Polymarket / Gamma / CLOB call; opens on N consecutive failures
  + half-opens for a probe request
- **API versioning** (`core/api_versioning.py`) — `/api/v1/...` prefix with
  a version negotiator + deprecation header
- **Dark/light theme switcher** (next-themes + ThemeToggle component in
  the top status bar, persisted to localStorage)
- **Command palette (Cmd+K)** — shadcn/ui Command (cmdk) dialog with 25+
  navigation entries + 6 page-level actions
- **Browser push notifications** — `src/lib/notifications.ts` Web
  Notifications primitives + `useNotifications` hook (30s visibility-aware
  alert polling, deduplication, severity-tagged `requireInteraction` for
  critical alerts)
- **DB migration system** — `core/db/migration_runner.py` + idempotent
  `001_initial_schema.sql` and `001_initial_enterprise_schemas.sql` applied
  on app startup; `scripts/migrate.py` for ad-hoc runs
- **Advanced backtest** (walk-forward + Monte Carlo) —
  `backtesting/advanced.py` returns confidence intervals on Sharpe / max
  drawdown / win rate from N resampled runs
- **Recharts visualizations** — `src/components/charts/` (EquityCurveChart,
  PnLBarChart, Sparkline, GaugeChart, ReliabilityDiagram, theme.ts barrel)
  integrated into 5 panels (EquityCurve, Attribution, Observability,
  CapitalAllocator, MLValidation)
- **WebSocket broadcast layer** (`core/ws_broadcast.py`) — multiplexes 5
  channels (book, orders, trades, events, alerts) to all connected clients
- **i18n (English + French)** — next-intl + `useTranslation` hook with full
  nav / group / status / positions / analytics message catalogs;
  LocaleSwitcher in the top status bar
- **CLI tool** (`mini-services/polymarket-bot/cli.py`) — 14 typer commands
  (status, balance, positions, orders, trades, health, retrain,
  kill-switch, flags, flag, alerts, metrics, circuit-breakers, cache)
- **Audit log viewer** (`src/components/AuditLogPanel.tsx`) — severity-
  inferred table with category / severity / date / text-search filters,
  CSV + JSON export of the filtered set, 15s visibility-aware auto-refresh
- **A/B testing framework** (`ml/ab_testing.py`) — multi-variant
  experiments comparing model variants, strategy parameters, or
  capital-allocation curves; statistical significance tracked via Brier /
  ROC-AUC deltas
- **Backup verification + rotation** — `scripts/verify_backup.py`
  (round-trip integrity), `scripts/backup_rotation.py` (GFS: 7 daily +
  4 weekly + 12 monthly + 90d hard cap), `scripts/check_integrity.py`
  (per-DB PRAGMA + orphan checks), `scripts/test_restore.py` (full
  restore round-trip)
- **Rate limit dashboard** — `core/rate_limit_tracker.py` (in-memory
  thread-safe tracker) + `GET /api/rate-limit/stats` route + RateLimitPanel
  (4 KPI cards + endpoint bar chart + per-minute sparkline + 2 tables +
  policy reference, 30s visibility-aware polling)
- **Frontend error reporting** — `src/lib/errorReporter.ts` (captureError,
  captureMessage, flush, installErrorHandlers, getErrorStats) +
  ErrorReporterInit component installed in root layout + `POST /api/client-errors`
  endpoint with dedicated client_errors logger
- **User preferences system** — theme, locale, notification opt-in,
  sidebar collapse state, and audio mute persist across sessions via
  localStorage (with SSR-safe getters and stale-value fallback)
- **API contract tests** (`tests/test_openapi.py`, 33 tests) — verifies
  every OpenAPI-documented route matches the live response shape
- **Performance profiling** — in-process `cProfile` middleware + per-route
  timing histogram; `scripts/status_report.py` summarises p50/p95/p99
- **Security hardening pass** — penetration tests + OWASP Top 10 audit
  (constant-time compare, SSRF guard, fail-closed auth, sanitised 500s,
  locked-down `/docs` in live mode); see [docs/SECURITY.md](docs/SECURITY.md)
- **Documentation checker** — `scripts/check_docs.py` verifies all
  internal Markdown links resolve, every fenced code block declares a
  language, heading hierarchy has no level skips, and GFM tables have
  consistent column counts

### Changed
- Total tests: 1429+ (970+ backend + 459+ frontend + 38 E2E)
- All 90+ API routes now have rate limiting + OpenAPI documentation
- Frontend panels: 37 → 55+ (added Audit Log, Rate Limits, Theme Toggle,
  Locale Switcher, Command Palette, Error Reporter Init, plus 5 Recharts
  primitives)
- Backend route count: 77 → 90+ (added Prometheus `/metrics`,
  `/api/ab-test`, `/api/audit/logs`, `/api/rate-limit/stats`,
  `/api/client-errors`, plus expanded ML / flags surfaces)

## [1.0.0] - 2025-09-03

### Added
- **Core platform**: Next.js 16 dashboard + FastAPI backend (77 routes) + Caddy gateway
- **ML pipeline**: 4-model ensemble (RandomForest, GradientBoosting, SGD, LightGBM) + Level-2 LogisticRegression meta-learner
- **Walk-forward cross-validation**: Time-ordered train/test split (no lookahead bias)
- **Drift detection**: PSI (Population Stability Index), KS test, rolling Brier, EWMA Brier
- **Label backfill**: Historical resolved-market label backfill from Gamma API
- **Shadow inference**: Challenger model comparison + counterfactual trade recording
- **Decision ledger**: SQLite-backed correlation-ID system linking PREDICTION→SIGNAL→RISK→ORDER→FILL
- **Execution quality**: Slippage (bps), latency (ms), realized edge tracking per fill
- **7-dimension P&L attribution**: strategy, confidence, edge, probability, liquidity, holding period, direction
- **Observability**: 31 auto-collected metrics across 6 categories (data/bot/execution/ml/system)
- **Capital allocator**: Saturating edge curve (Michaelis-Menten) for position sizing
- **Risk management**: Kill switch, max drawdown circuit breaker, per-trade circuit breaker, MTM risk gate
- **10-check live safety gate**: Staged validation before enabling live trading
- **Paper trading**: Realistic slippage model (crossing + size + queue)
- **Marketable SL/TP**: Exits at best_bid (not mid) to ensure fills
- **Inventory flush**: Automatic position reduction when inventory exceeds limits
- **Data retention**: 7d observability, 30d decisions, 90d execution_quality pruning
- **37 UI panels**: Full dashboard with real-time polling, dark theme, responsive design
- **Rate limiting**: slowapi-based request throttling (120/min read, 30/min write, 5/min heavy)
- **Alerting system**: Threshold-based alerts with acknowledge/resolve workflow
- **Docker containerization**: Multi-stage builds, docker-compose, production Caddyfile
- **CI/CD**: GitHub Actions (frontend lint+test, backend test, production build)
- **542 tests**: 454 backend + 88 frontend, 0 failures
- **WCAG 2.1 AA accessibility**: Skip link, focus-visible, ARIA, focus trap, reduced-motion
- **Error boundaries**: Root + panel-level error boundaries with retry
- **Zod schemas**: Runtime API response validation
- **Framer Motion**: Panel transitions, animated lists, skeleton shimmer

### Changed
- ML train/test split from random permutation (AUC 0.97, lookahead bias) to time-ordered arange split (walk-forward AUC 0.57)
- PSI baseline from U-shaped market distribution (false PSI 3.9+) to model's own prediction distribution + threshold 0.25
- SL/TP exits from mid (never crosses best_bid) to marketable at best_bid
- MDD circuit breaker baseline from BANKROLL_CEILING ($200) to OPERATING_CAPITAL ($100)
- Signal trader scan from `gamma_client.extract_token_ids()` (returned empty) to `catalog.items()` direct iteration
- Meta-learner warmup from live-only to `warm_from_labeled_samples()` bootstrap from backfilled labels

### Fixed
- Settlement deadlock: nested asyncio.Lock caused permanent hang on first market resolution
- Liquidity type mismatch: capital allocator expected float, got dict
- CSS corruption: `:has()` selectors broke Tailwind v4 parsing
- ML predict returning 0.5 for all: was reading `store.market_info` (doesn't exist), fixed to read `market_discovery.catalog`
- Steamroller loss (−$1.18 avg loss): exits at mid never filled, fixed to marketable at best_bid

### Security
- Bearer token authentication (fail-closed, HMAC compare_digest)
- Input validation on all route params (Pydantic Query bounds)
- Global exception handler (sanitized 500s, no info leakage)
- Request logging middleware
- Upstream error detail sanitization

## [0.1.0] - 2025-09-01
### Added
- Initial project scaffold
- Basic FastAPI server
- Basic Next.js dashboard
