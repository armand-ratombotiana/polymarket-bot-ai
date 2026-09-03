# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
