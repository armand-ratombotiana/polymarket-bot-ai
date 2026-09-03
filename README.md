# Polymarket Pro — AI-Powered Prediction Market Trading Workstation

![Next.js](https://img.shields.io/badge/Next.js-16-black)
![Python](https://img.shields.io/badge/Python-3.12-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-0.111%2B-009688)
![TypeScript](https://img.shields.io/badge/TypeScript-5-3178C6)
![Tests](https://img.shields.io/badge/tests-340%20passing-brightgreen)
![Trading Mode](https://img.shields.io/badge/trading-paper-orange)

Polymarket Pro is an institutional-grade algorithmic trading bot for
[Polymarket](https://polymarket.com) prediction markets. It pairs a 4-model ML
ensemble with a Level-2 meta-learner, a 10-check live safety gate, full decision
auditability, paper-trading-by-default semantics, and a 37-panel React
workstation — so every PREDICTION → SIGNAL → RISK → ORDER → FILL chain can be
reconstructed, attributed, and stress-tested end-to-end.

---

## Table of Contents

1. [Key Features](#key-features)
2. [Architecture Overview](#architecture-overview)
3. [Tech Stack](#tech-stack)
4. [Quick Start](#quick-start)
5. [Configuration](#configuration)
6. [Project Structure](#project-structure)
7. [Trading Strategies](#trading-strategies)
8. [ML Pipeline](#ml-pipeline)
9. [Safety Systems](#safety-systems)
10. [Testing](#testing)
11. [API Overview](#api-overview)
12. [Deployment](#deployment)
13. [Security](#security)
14. [Disclaimer](#disclaimer)
15. [License](#license)

---

## Key Features

### Trading

- **Paper trading mode** — default execution mode; no live capital at risk until
  the 10-check gate is satisfied.
- **Marketable SL/TP** — stop-loss / take-profit logic that crosses the spread
  when triggered so risk is actually realised, not just nominally posted.
- **Inventory flush** — the market maker dumps any YES inventory held longer
  than the configured horizon via a marketable SELL at best bid.
- **Per-trade circuit breaker** — a single trade that loses more than the
  configured threshold triggers a 300s strategy cooldown so the strategy cannot
  keep losing while the rest of the book recovers.

### ML / AI

- **4-model ensemble** — RandomForest (isotonic-calibrated), GradientBoosting
  (isotonic-calibrated), SGDClassifier (online incremental), and LightGBM
  (optional, graceful 3-member fallback when the package is unavailable).
- **Level-2 meta-learner** — stacks the four base learner probabilities and
  blends them with adaptive per-model Brier-score weighting when cold.
- **Walk-forward CV** — drift detector history captures the last 10 PSI / KS /
  rolling-Brier snapshots, treated as walk-forward folds.
- **Drift detection** — Population Stability Index (PSI), KS-statistic, rolling
  and EWMA Brier scores; status escalates `HEALTHY → MODERATE_SHIFT →
  SIGNIFICANT_DRIFT` against documented thresholds.
- **Label backfill** — settled Polymarket outcomes are streamed back into the
  feature store so the model can be retrained against resolved markets rather
  than synthetic-only labels.
- **Shadow inference** — challenger models run in shadow mode against live
  predictions; the comparison view surfaces when a challenger is ready to
  promote to production.

### Risk

- **Kill switch** — file-backed global halt (`data/kill_switch`) plus an in-memory
  flag; any subsystem can trip it and every order submission reads it.
- **Max drawdown circuit breaker** — trips the kill switch when the rolling
  peak-to-trough drawdown breaches the configured dollar limit.
- **MTM risk gate** — mark-to-market exposure decomposition is consulted before
  new orders so unrealised losses cannot silently grow.
- **10-check live safety gate** — the gate must pass all ten independent checks
  (paper mode disabled, positive expectancy, drawdown within limit, win rate
  above floor, closed trade sample size, real ML data, drift healthy, kill
  switch tested, risk limits sane, API credentials present) before live trading
  can be enabled in-memory.
- **Capital allocator** — sizes new entries with a saturating Michaelis-Menten
  edge curve, modulated by confidence, calibration, drawdown, and liquidity.

### Observability

- **Decision ledger** — SQLite-backed unified ledger linking every stage of the
  pipeline via a single `decision_id`:
  `PREDICTION → SIGNAL → RISK_APPROVED | RISK_REJECTED → ORDER → FILL`.
- **Execution quality tracking** — slippage, latency, and realised-edge metrics
  per fill, persisted in `data/execution_quality.db`.
- **7-dimension P&L attribution** — strategy, confidence, edge, probability,
  liquidity, holding-period, and direction buckets, each with count / win
  rate / gross profit / gross loss / net P&L.
- **31 auto-collected system metrics** — collected across 6 categories
  (`system`, `data_source`, `execution`, `ml`, `risk`, `bot`) by the
  observability collector loop and persisted to `data/observability.db`.

### UI

- **37 React panels** — Command Center, Live Books, Screener, Positions,
  Orders, Trades, Strategy Registry, Arbitrage, Deep Analysis, AI/ML Engine,
  Copilot, Shadow Inference, ML Validation, Performance, Backtest Lab,
  Attribution, Execution Quality, Closed Positions, Capital Allocator, System
  Health, Data Explorer, Observability, Retention, Decision Ledger, Safety
  Gate, and supporting modals.
- **Dark dashboard** — bespoke design system (`#0e1015` surface, `#13161e`
  cards, `#1f2335` borders) implemented on Tailwind v4 + shadcn/ui primitives.
- **Responsive** — auto-collapsing sidebar at ≤1024px, mobile drawer, and
  keyboard shortcuts (1–8) for the most-used panels.
- **Real-time polling** — 500 ms dashboard refresh by default, paused while the
  document is hidden and re-fired immediately on tab return.

---

## Architecture Overview

Polymarket Pro is a **3-tier architecture** with a Next.js frontend, a Python
FastAPI backend, and a Caddy gateway that fronts both.

```text
┌──────────────────────────────────────────────────────────┐
│                     Browser (user)                       │
└──────────────────────────────────────────────────────────┘
                          │  HTTPS (port 81)
                          ▼
┌──────────────────────────────────────────────────────────┐
│  Caddy Gateway  (port 81)                                │
│  Routes via ?XTransformPort= query param                 │
│   • port=3000 → Next.js (frontend)                       │
│   • port=8080 → FastAPI (backend)                        │
└──────────────────────────────────────────────────────────┘
              │                              │
              ▼                              ▼
┌──────────────────────────┐   ┌──────────────────────────┐
│ Frontend  (port 3000)    │   │ Backend  (port 8080)     │
│ Next.js 16 + React 19    │   │ FastAPI + uvicorn        │
│ • 37 React panels        │◄──┤ • 77 REST routes         │
│ • /api/bot (spawns       │   │ • WebSocket broadcast    │
│   backend)               │   │ • 13 route modules       │
│ • 500 ms polling         │   │                          │
└──────────────────────────┘   └────────────┬─────────────┘
                                            │
                                            ▼
                          ┌────────────────────────────────┐
                          │  Core / ML / Risk layers       │
                          │  • Decision ledger (SQLite)    │
                          │  • Risk manager + kill switch  │
                          │  • ML ensemble + meta-learner  │
                          │  • Paper simulator            │
                          │  • 3 trading strategies       │
                          └────────────────────────────────┘
```

- **Frontend** — Next.js 16 (Turbopack) on port 3000, React 19, Tailwind v4, and
  shadcn/ui. The `/api/bot` route bootstraps the Python backend as a child
  process of the next-server so the two lifecycles are coupled.
- **Backend** — FastAPI on port 8080 with `uvicorn[standard]`. Exposes 77 REST
  routes (55 declared inline in `api/server.py` plus 22 registered by 13
  feature modules via the `register_routes(app)` pattern) and a WebSocket
  broadcast manager.
- **Gateway** — Caddy on port 81 routes inbound requests to either port using
  the `?XTransformPort=` query parameter. The frontend's `apiFetch` helper
  injects the param transparently.
- **Database** — SQLite with multiple specialised DBs: market intelligence,
  audit trail, decision ledger, observability, execution quality, closed
  positions, shadow trades, plus a JSON store-state file and a pickled model.
- **ML pipeline** — feature extraction → 4-model ensemble → Level-2 meta-learner
  → drift detector (PSI/KS/Brier) → label backfill from resolved markets →
  training-orchestrator retrain trigger when drift escalates.

---

## Tech Stack

| Category        | Technology                                                            |
| --------------- | --------------------------------------------------------------------- |
| Frontend        | Next.js 16, React 19, TypeScript 5, Tailwind CSS 4, shadcn/ui          |
| State / data    | Zustand, TanStack Query, TanStack Table, react-hook-form, Zod          |
| Visualisation   | Recharts, framer-motion, lucide-react                                 |
| Backend         | Python 3.12, FastAPI 0.111+, uvicorn[standard], Pydantic v2           |
| HTTP / WS       | httpx, websockets                                                     |
| Database        | SQLite (stdlib `sqlite3`), asyncpg (optional TimescaleDB sink)        |
| ML              | scikit-learn 1.4+, NumPy 1.26+, LightGBM 4.3+ (optional 4th member)  |
| Auth            | HMAC-compared Bearer token (fail-closed when unconfigured)            |
| Tooling         | Bun, ESLint 9, Ruff, pytest 8+, Prisma ORM (frontend-only schemas)    |
| Gateway         | Caddy (port 81) with `?XTransformPort=` query-param routing           |
| Process mgmt    | Next.js child-process spawn (dev), supervisord (optional)              |

---

## Quick Start

### Prerequisites

- **Node.js 20+** and **Bun** (frontend package manager + runtime)
- **Python 3.12+** with `pip` (or `uv`/`poetry` if you prefer)
- ~4 GB RAM (the dev server runs Next.js + FastAPI + SQLite concurrently)

### Install frontend dependencies

```bash
cd /home/z/my-project
bun install
```

### Install backend dependencies

```bash
cd /home/z/my-project/mini-services/polymarket-bot
pip install -r requirements.txt
```

### Configure environment

The backend reads its config from `mini-services/polymarket-bot/.env`. A
minimal starting point (paper trading, all safety defaults) is shipped with the
repo. To override any value, edit the file directly or export the same keys in
your shell. See [Configuration](#configuration) for the full list.

### Start the dev server

```bash
cd /home/z/my-project
bun run dev
```

This boots Next.js on port 3000. The first request to `/api/bot?action=start`
spawns the FastAPI backend as a child of the next-server process; the backend
binds to port 8080.

### (Alternative) Start the backend manually

If you want to run the backend standalone (e.g. for debugging without the
frontend):

```bash
cd /home/z/my-project/mini-services/polymarket-bot
python -m uvicorn api.server:app --host 0.0.0.0 --port 8080 --log-level info
```

### Access the dashboard

Use the **Preview Panel** in your hosting environment (the gateway is on port
81). Do **not** open `http://localhost:3000` directly — the Caddy gateway
performs the auth + port transform that lets the frontend call the backend
transparently.

---

## Configuration

All configuration is environment-variable driven. The canonical source is
`mini-services/polymarket-bot/.env`.

### Trading mode

| Variable                | Description                                            | Default |
| ----------------------- | ------------------------------------------------------ | ------- |
| `TRADING_MODE`           | Trading mode — `paper` (default) or `live`.           | `paper` |
| `PAPER_TRADE`            | Boolean paper-trade flag (mirrors `TRADING_MODE`).    | `true`  |
| `LIVE_TRADING_ENABLED`   | Hard switch; must be `true` for live orders to pass. | `false` |
| `API_TOKEN`              | Bearer token required on every non-public route.     | (set)   |
| `CORS_ORIGINS`           | Comma-separated allowed origins, or `*`.             | `*`     |
| `LOG_LEVEL`              | Python logging level.                                  | `INFO`  |
| `DASHBOARD_REFRESH_MS`   | Frontend polling interval in milliseconds.            | `500`   |

### Signal trader

| Variable                    | Description                                       | Default |
| --------------------------- | ------------------------------------------------- | ------- |
| `SIGNAL_ENABLED`            | Enable the ML-driven signal-trader strategy.     | `false` |
| `SIGNAL_MIN_CONFIDENCE`     | Minimum model probability to act on a signal.   | `0.50`  |
| `MAX_OPEN_ORDERS`           | Concurrent open orders across all strategies.    | `8`     |
| `MAX_POSITION_PER_MARKET_USDC` | Max USDC deployed per market token.          | `3.0`   |
| `MAX_TOTAL_EXPOSURE_USDC`   | Max total USDC exposure across all positions.    | `25.0`  |
| `DAILY_LOSS_LIMIT_USDC`     | Daily realised-loss limit; trips kill switch.    | `2.0`   |

### Market maker

| Variable                   | Description                                    | Default |
| -------------------------- | ---------------------------------------------- | ------- |
| `MM_ENABLED`               | Enable the market-maker strategy.            | `true`  |
| `MM_SPREAD_BPS`            | Base quote spread in basis points.            | `200`   |
| `MM_QUOTE_SIZE_USDC`        | USDC size per resting quote.                  | `1.5`   |
| `MM_MAX_INVENTORY_USDC`     | Max USDC of inventory before flush triggers.  | `15.0`  |

### Arbitrage scanner

| Variable                       | Description                                  | Default |
| ------------------------------ | -------------------------------------------- | ------- |
| `ARB_ENABLED`                  | Enable the cross-market arb scanner.        | `true`  |
| `ARB_MIN_PROFIT_BPS`           | Minimum profit (bps) to execute an arb.     | `50`    |
| `ARB_SCAN_INTERVAL_SECONDS`    | Seconds between arb scans.                 | `15`    |
| `ARB_ORDER_SIZE_USDC`          | USDC size per arb leg.                      | `1.5`   |

### Storage paths

| Variable                    | Description                                                |
| --------------------------- | ---------------------------------------------------------- |
| `MARKET_DB_PATH`            | Market intelligence SQLite (snapshots, ticks, news).      |
| `AUDIT_DB_PATH`             | Append-only audit trail SQLite.                            |
| `STORE_STATE_PATH`          | JSON snapshot of the in-memory data store (orders/pos).   |
| `KILL_SWITCH_PATH`          | File-based kill switch sentinel.                          |
| `KILL_SWITCH_REASON_PATH`   | Text file recording the last kill-switch trip reason.     |
| `MODEL_REGISTRY_PATH`       | JSON model registry (versions, promotion gate, metrics). |
| `VECTOR_STORE_PATH`         | Vector index for market similarity search.                |
| `MODEL_PATH`                | Pickle of the trained ensemble + meta-learner.            |
| `DECISION_LEDGER_DB_PATH`   | SQLite backing the unified decision ledger.              |

---

## Project Structure

```text
/home/z/my-project/
├── src/                              # Next.js 16 frontend (Turbopack)
│   ├── app/
│   │   ├── page.tsx                  # Workstation shell — 37 panel render blocks
│   │   ├── layout.tsx               # Root layout + theme provider
│   │   ├── globals.css              # Dark design-system tokens + utility classes
│   │   └── api/
│   │       └── bot/route.ts         # Next.js API route — spawns FastAPI on :8080
│   ├── components/                  # 37 React panels + shadcn/ui primitives
│   │   ├── Sidebar.tsx              # 7 nav groups, 37 NavSections, kbd shortcuts
│   │   ├── TopStatusPanel.tsx       # Bot status banner (P&L, mode, exposure)
│   │   ├── MLPanel.tsx              # AI/ML engine view (Brier / AUC / ECE / drift)
│   │   ├── EquityCurve.tsx          # Realised equity chart
│   │   ├── DecisionLedgerPanel.tsx  # W8 — PREDICTION→SIGNAL→RISK→ORDER→FILL chain
│   │   ├── AttributionPanel.tsx     # W8 — 7-dimension P&L attribution
│   │   ├── ExecutionQualityPanel.tsx# W8 — slippage / latency / realised edge
│   │   ├── ClosedPositionsPanel.tsx # W8 — settled positions + MTM P&L
│   │   ├── CapitalAllocatorPanel.tsx# W8 — saturating edge-curve sizing
│   │   ├── ShadowInferencePanel.tsx # W8 — challenger vs production comparison
│   │   ├── MLValidationPanel.tsx    # W8 — calibration plot + drift sparkline
│   │   ├── ObservabilityPanel.tsx   # W8 — 31 metrics across 6 categories
│   │   ├── RetentionPanel.tsx       # W8 — retention policy + manual prune
│   │   └── LiveSafetyGatePanel.tsx  # W8 — 10-check live trading gate
│   ├── hooks/                       # useBot, useAudio, etc.
│   └── lib/                         # apiFetch wrapper, formatters, types
│
├── mini-services/polymarket-bot/    # Python 3.12 backend
│   ├── api/
│   │   └── server.py                # FastAPI app — 55 inline routes + lifespan
│   ├── core/                        # Core trading logic
│   │   ├── data_store.py            # In-memory store (orders/positions/events)
│   │   ├── decision_ledger.py       # Unified SQLite decision ledger
│   │   ├── audit_logger.py          # Append-only audit trail
│   │   ├── book_poller.py           # Order-book polling loop
│   │   ├── ws_client.py             # Polymarket WebSocket client
│   │   ├── clob_client.py           # CLOB REST client (order submission)
│   │   ├── gamma_client.py          # Gamma markets REST client
│   │   ├── position_manager.py      # Position lifecycle (open/MTM/close)
│   │   ├── portfolio.py             # Exposure / reconciliation / leaderboard
│   │   ├── portfolio_mark_to_market.py # MTM risk gate
│   │   ├── settlement.py            # Market resolution + settlement
│   │   ├── capital_allocator.py     # Michaelis-Menten saturating edge curve
│   │   ├── attribution.py           # 7-dimension P&L attribution
│   │   ├── execution_quality.py     # Slippage / latency tracking
│   │   ├── closed_positions.py      # Settled positions store
│   │   ├── observability.py         # Generic metric store (SQLite)
│   │   ├── observability_collector.py # 31-metric auto-collection loop
│   │   ├── live_safety_gate.py      # 10-check live trading gate
│   │   ├── shadow_trading.py        # Shadow / challenger trade tracking
│   │   ├── retention.py             # Retention policy + manual prune
│   │   ├── safety.py                # Kill switch file I/O
│   │   ├── market_db.py             # Market intelligence SQLite schema
│   │   ├── market_discovery.py      # Catalogue / coverage scanner
│   │   ├── reconciliation.py        # Store vs on-chain reconciliation
│   │   ├── deep_analysis.py        # Per-market analytics engine
│   │   ├── analysis_engine.py       # Feature extraction + edge calc
│   │   ├── fundamental_ingest.py    # News / fundamentals ingestion
│   │   ├── label_backfill.py       # Settled-market label backfill
│   │   ├── watchdog.py              # Liveness watchdog
│   │   └── db/                      # SQL migrations + migration runner
│   ├── ml/                          # ML pipeline
│   │   ├── features.py              # Feature engineering
│   │   ├── model.py                 # 4-model ensemble (RF / GB / SGD / LightGBM)
│   │   ├── ensemble_meta_learner.py # Level-2 stacking meta-learner
│   │   ├── drift_detector.py        # PSI / KS / Brier drift detection
│   │   ├── model_registry.py        # Versioned model registry + promotion gate
│   │   ├── training_orchestrator.py # Drift-triggered retrain orchestrator
│   │   ├── validation.py            # Walk-forward CV + calibration report
│   │   ├── shadow_inference.py      # Challenger model shadow scoring
│   │   ├── vector_store.py          # Market similarity vector store
│   │   ├── copilot.py               # Natural-language market Q&A
│   │   └── routes.py                # ML version / rollback routes
│   ├── strategies/                  # Trading strategies
│   │   ├── base.py                  # Abstract strategy + risk gate
│   │   ├── signal_trader.py         # ML-driven directional trades
│   │   ├── market_maker.py          # A-S skew + inventory flush
│   │   ├── arb_scanner.py           # Cross-market arbitrage
│   │   └── registry.py              # Strategy registry / toggle
│   ├── risk/                        # Risk layer
│   │   ├── manager.py               # Kill switch, drawdown, per-trade breaker
│   │   └── routes.py                # Risk routes
│   ├── paper/
│   │   └── simulator.py             # Paper-trade fill simulator
│   ├── backtesting/
│   │   └── engine.py                # Vectorised backtest engine
│   ├── tests/                       # 340 tests across 44 files
│   │   ├── conftest.py              # Shared fixtures
│   │   └── test_*.py                # Module-level test suites
│   ├── data/                        # SQLite DBs + model artifacts
│   ├── config.py                    # Pydantic settings (env-driven)
│   ├── main.py                      # Standalone entrypoint (no Next.js)
│   ├── requirements.txt             # Pinned Python dependencies
│   ├── pyproject.toml               # Ruff config
│   └── pytest.ini                   # pytest configuration
│
├── docs/                            # Generated docs + reassessments
├── package.json                     # Bun / Next.js scripts + frontend deps
├── next.config.ts                   # Next.js 16 config (output: standalone)
├── tsconfig.json                    # TypeScript project references
└── README.md                        # This file
```

---

## Trading Strategies

The strategy registry (`strategies/registry.py`) exposes three trading
strategies, each toggleable at runtime via `POST /api/strategies/toggle`.

### Signal trader (`strategies/signal_trader.py`)

ML-driven directional trader. Pulls the ensemble probability from
`ml.model.predict()`, applies a confidence gate (`SIGNAL_MIN_CONFIDENCE`),
computes edge against the live best quote, and submits a marketable order when
both gates clear. Records the full PREDICTION → SIGNAL → RISK → ORDER → FILL
chain in the decision ledger.

### Market maker (`strategies/market_maker.py`)

Two-sided quote strategy with adaptive skew. Resting bids/offers are sized
against `MM_QUOTE_SIZE_USDC` and priced at `MM_SPREAD_BPS` around mid. If YES
inventory is held longer than the configured horizon, the strategy **flushes**
it via a marketable SELL at best bid and cancels competing quotes so the flush
is the only resting SELL.

### Arbitrage scanner (`strategies/arb_scanner.py`)

Cross-market arbitrage scanner. Polls related markets at
`ARB_SCAN_INTERVAL_SECONDS`, computes the no-arb band, and submits paired
orders when the spread exceeds `ARB_MIN_PROFIT_BPS`. Each leg is sized at
`ARB_ORDER_SIZE_USDC`.

---

## ML Pipeline

```text
   ┌────────────────────┐
   │ Feature extraction │  ml/features.py
   │ (order-book,       │  ← market_db, book_poller,
   │  fundamentals,     │    fundamental_ingest
   │  technicals)       │
   └─────────┬──────────┘
             │
             ▼
   ┌──────────────────────────────────────┐
   │ 4-model ensemble                    │  ml/model.py
   │ • RandomForestClassifier (isotonic) │
   │ • GradientBoostingClassifier (iso)  │
   │ • SGDClassifier (online incremental)│
   │ • LightGBMClassifier (optional 4th)│
   └─────────┬────────────────────────────┘
             │ 4 base probabilities
             ▼
   ┌────────────────────────────────────┐
   │ Level-2 meta-learner              │  ml/ensemble_meta_learner.py
   │ (stacking blend, adaptive Brier    │
   │  weighting when cold)             │
   └─────────┬──────────────────────────┘
             │ blended probability + edge
             ▼
   ┌────────────────────────────────────┐
   │ Drift detector                    │  ml/drift_detector.py
   │ (PSI, KS, rolling & EWMA Brier)   │
   │ Status: HEALTHY / MODERATE_SHIFT │
   │         / SIGNIFICANT_DRIFT       │
   └─────────┬──────────────────────────┘
             │ if drift escalates
             ▼
   ┌────────────────────────────────────┐
   │ Label backfill                    │  core/label_backfill.py
   │ (settled Polymarket outcomes →    │
   │  training labels)                │
   └─────────┬──────────────────────────┘
             │
             ▼
   ┌────────────────────────────────────┐
   │ Training orchestrator             │  ml/training_orchestrator.py
   │ (drift-triggered retrain +       │
   │  model-registry promotion gate)   │
   └────────────────────────────────────┘
```

**Promotion gate** (`ml/model_registry.py`): a candidate version is promoted to
active only when `Brier ≤ 0.22` AND `ROC-AUC ≥ 0.70`. The version record
carries Brier, ROC-AUC, ECE, Sharpe, sample counts (real vs synthetic),
training source, and an `is_active` flag — exposed via `GET /api/ml/versions`
and rolled back via `POST /api/ml/rollback`.

**Shadow inference** (`ml/shadow_inference.py`): challenger models score the
same live features as production, persist predictions to
`data/shadow_trades.db`, and surface comparison metrics (Brier / AUC / ECE
deltas) on the Shadow Inference panel. A challenger that beats production on
the promotion gate for N consecutive folds becomes eligible for promotion.

---

## Safety Systems

### Kill switch

A file-backed sentinel at `KILL_SWITCH_PATH` plus an in-memory flag on the data
store. Any subsystem can trip it (max drawdown breach, daily loss stop,
per-trade circuit breaker, manual `POST /api/kill-switch/activate`). Every
order submission checks both before sending.

### Circuit breakers

| Breaker                   | Threshold (env var)                | Effect                              |
| ------------------------- | ---------------------------------- | ----------------------------------- |
| Daily loss stop           | `DAILY_LOSS_LIMIT_USDC`            | Trips kill switch for the day.     |
| Weekly loss stop          | (derived)                          | Trips kill switch for the week.    |
| Max drawdown              | `MAX_DRAWDOWN_LIMIT` (config.py)    | Trips kill switch immediately.     |
| Per-trade max loss        | `PER_TRADE_MAX_LOSS` (config.py)   | 300 s strategy cooldown.           |
| Strategy cooldown         | `STRATEGY_COOLDOWN` (300 s)         | Strategy pauses after per-trade hit. |

### 10-check live safety gate

`core/live_safety_gate.py` runs ten independent checks before the in-memory
`live_mode` flag can be flipped. The full payload is returned by
`GET /api/live/readiness` and visualised on the Safety Gate panel.

1. **paper_mode** — `TRADING_MODE` must be `live` (not `paper`).
2. **positive_expectancy** — historical expectancy must be `> 0`.
3. **max_drawdown** — current drawdown must be within the configured limit.
4. **win_rate** — historical win rate must be above the floor.
5. **closed_trades** — sufficient closed-trade sample size for statistics.
6. **ml_real_data** — model must have been trained on real (not synthetic-only)
   data.
7. **drift_healthy** — drift detector status must be `HEALTHY` (not
   `MODERATE_SHIFT` or `SIGNIFICANT_DRIFT`).
8. **kill_switch_tested** — kill switch must have been exercised in a test
   within the lookback window.
9. **risk_limits** — all risk limits (`MAX_OPEN_ORDERS`,
   `MAX_TOTAL_EXPOSURE_USDC`, etc.) must be sane and consistent.
10. **api_credentials** — Polymarket API credentials must be present and
    non-expired.

The gate is **fail-closed**: a single failing check blocks the
`POST /api/live/enable` call. The check order and per-check pass/fail payload
are returned alongside the aggregate `passed` boolean.

---

## Testing

```bash
cd /home/z/my-project/mini-services/polymarket-bot
python -m pytest tests/ -v
```

The backend ships with **340 tests across 44 files** (plus `conftest.py` and
`pytest.ini`). Coverage spans every core module, every ML component, every
strategy, the risk manager, the paper simulator, the live safety gate, the
decision ledger, the observability collector, and an end-to-end decision-chain
test that exercises PREDICTION → SIGNAL → RISK → ORDER → FILL.

Frontend lint / type-check:

```bash
cd /home/z/my-project
bun run lint           # ESLint — clean (0 errors, 0 warnings)
bunx tsc --noEmit      # TypeScript — clean on src/
```

---

## API Overview

The FastAPI server exposes **77 routes** across 13 feature modules. A full
OpenAPI spec is available at `/openapi.json` (paper mode) or `/docs` (Swagger
UI) when the server is running. A complete route reference lives in
`docs/API.md` (when generated).

| Category      | Count | Sample endpoints                                              |
| ------------- | ----- | ------------------------------------------------------------- |
| System        | 8     | `GET /api/health`, `GET /api/system/health`                  |
| Markets       | 6     | `GET /api/markets`, `GET /api/markets/coverage`              |
| Portfolio     | 6     | `GET /api/positions`, `GET /api/positions/closed`            |
| Strategies    | 4     | `GET /api/strategies/catalog`, `POST /api/strategies/toggle` |
| Trading       | 5     | `POST /api/order`, `POST /api/kill-switch/activate`         |
| ML / AI       | 12    | `GET /api/ml/metrics`, `POST /api/ml/retrain`                |
| Analysis      | 5     | `GET /api/attribution`, `GET /api/execution-quality`         |
| Risk          | 4     | `GET /api/risk/reconcile`, `GET /api/exposure`               |
| Capital       | 1     | `GET /api/capital/allocation`                                 |
| System / data | 4     | `GET /api/database/tables`, `POST /api/system/prune`         |
| Live safety   | 2     | `GET /api/live/readiness`, `POST /api/live/enable`           |
| Decisions     | 1     | `GET /api/decisions/rejected`                                |
| Shadow        | 2     | `GET /api/shadow/trades`, `GET /api/shadow/comparison`       |

Route prefixes follow the category name: `System` = `/api/health`,
`/api/status`, `/api/snapshot`; `Markets` = `/api/markets`, `/api/orderbooks`;
`Trading` = `/api/order`, `/api/trade`, `/api/kill-switch`; `ML / AI` =
`/api/ml/*`, `/api/ai/*`; `Analysis` = `/api/analysis/*`; `Risk` =
`/api/risk/*`, `/api/exposure`; `Capital` = `/api/capital/*`; `System / data` =
`/api/database/*`, `/api/system/*`; `Live safety` = `/api/live/*`; `Decisions`
= `/api/decisions/*`; `Shadow` = `/api/shadow/*`.

WebSocket: `WS /ws` — broadcast manager pushes live book snapshots, order
updates, and event-log entries to all connected clients.

See `docs/API.md` for the complete reference (request/response schemas,
auth requirements, error codes).

---

## Deployment

### Production build (Next.js standalone)

```bash
cd /home/z/my-project
bun run build     # next build + copy .next/static + public into standalone
bun run start     # NODE_ENV=production bun .next/standalone/server.js
```

The Next.js standalone output (`next.config.ts → output: 'standalone'`) bundles
only the required `node_modules`, so the production server can run from
`.next/standalone/` without the full `bun install` tree.

### Backend in production

```bash
cd /home/z/my-project/mini-services/polymarket-bot
set -a && . ./.env && set +a
exec python3 -m uvicorn api.server:app --host 0.0.0.0 --port 8080 --log-level info
```

### Caddy gateway

The Caddy gateway on port 81 fronts both Next.js (port 3000) and FastAPI
(port 8080). Frontend API calls include `?XTransformPort=8080` so the gateway
proxies them to the backend; the rest go to port 3000. The gateway terminates
TLS, injects the appropriate `Authorization` header, and rewrites paths as
needed.

### Process supervision

For long-running deployments, use `supervisord` (config shipped at
`mini-services/polymarket-bot/supervisord.conf`) or systemd to keep the
FastAPI process alive. The backend's `package.json` ships a `dev` script that
auto-restarts uvicorn on crash.

---

## Security

- **Bearer token auth** — every non-public route requires an
  `Authorization: Bearer <API_TOKEN>` header. Token comparison is
  HMAC-constant-time. If `API_TOKEN` is unset, the server returns `503` (fail
  closed) rather than allowing unauthenticated access.
- **Public paths** — only `/api/health`, `/docs`, `/redoc`, `/openapi.json` are
  unauthenticated. In `live` mode, `/docs`/`/redoc`/`/openapi.json` are
  additionally locked down.
- **Paper trading by default** — `TRADING_MODE=paper`,
  `PAPER_TRADE=true`, `LIVE_TRADING_ENABLED=false` are the shipped defaults.
  No live order can land until all three are flipped, the 10-check gate
  passes, and the in-memory `live_mode` flag is set via
  `POST /api/live/enable`.
- **10-check gate before live trading** — see
  [Safety Systems](#safety-systems). The gate is fail-closed: a single failing
  check blocks live mode.
- **Kill switch file** — a sentinel file (`data/kill_switch`) outside the
  database means a process crash cannot accidentally leave live trading
  enabled; the file outlives the process.
- **CORS** — `CORS_ORIGINS=*` is the shipped default for development; tighten
  this in production.
- **No secrets in the repo** — `.env` is git-ignored by convention; rotate the
  shipped `API_TOKEN` before deploying.

---

## Disclaimer

Polymarket Pro is **not financial advice**. The software is provided for
educational and research purposes only. Trading prediction markets involves
substantial risk of loss. The default mode is paper trading; live trading is
gated behind a 10-check safety gate and is not enabled by default. Past
performance (including the bot's own backtest and paper-trading results) is not
indicative of future returns. **Use at your own risk.** The authors and
contributors accept no liability for losses incurred through use of this
software.

---

## License

Released under the **MIT License**. See `LICENSE` in the repository root for the
full text. No `license` field is declared in `package.json` — if you fork this
repository, please add one explicitly.

Copyright (c) 2025 Polymarket Pro contributors.
