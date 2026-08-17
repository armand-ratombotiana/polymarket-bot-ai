# Polymarket Trading Workstation — UI/UX Redesign Specification

## 1. Executive Summary & Design Philosophy
The Polymarket Trading Workstation has undergone a comprehensive, institutional-grade UI/UX overhaul. Rather than applying a superficial theme, the entire information architecture, state presentation, financial risk communication, and component taxonomy were re-engineered from first principles.

### Foundational Tenets
1. **Truth Before Aesthetics:** No placeholder figures, synthetic quotes masquerading as real market data, or fabricated system metrics. Stubs are explicitly labeled; estimated pricing (e.g. synthetic Dutch-book NO-leg quotes) carries clear disclosure banners.
2. **Never Mix Operating Modes:** The interface maintains prominent visual badges across all panels for **PAPER**, **SHADOW**, **LIVE**, and **BACKTEST** execution regimes.
3. **Financial Safety Over Convenience:** Destructive or capital-allocating actions (e.g. Kill Switch, Cancel All Orders) require explicit, accessible, keyboard-trapped confirmation dialogs displaying the exact scope and impact before execution.
4. **Information Density Without Chaos:** Professional multi-factor layout organizing data hierarchically across 7 primary domains with clean monospaced financial value alignment (`tabular-nums`).

---

## 2. Information Architecture & Navigation

The flat 10-tab overflow header was replaced with a responsive **Collapsible Sidebar Navigation** paired with a persistent **Top Status Bar**.

```
Sidebar Navigation (Keyboard Shortcuts 1–8)
├── [1] Command Center          — Comprehensive Executive Desk (Risk Strip, Books, Positions, Orders, Events, Equity, Analytics, ML)
├── [2] Markets
│       ├── Live Books (2)      — Continuous L2 prediction market order books with hierarchical categorization
│       └── Screener (3)        — Global Polymarket Gamma scanner with keyword filtering and volume/liquidity metrics
├── [3] Portfolio
│       ├── Positions (4)       — Open prediction contracts with cost basis, share quantities, and realized P&L
│       ├── Orders              — Working limit orders queue with fill tracking and single/bulk cancellation
│       └── Trades & Fills      — Session execution audit feed with strategy attribution
├── [4] Strategies
│       ├── Strategy Registry (5)— 50-strategy matrix with truthful gating (3 Implemented vs 47 Research Stubs)
│       └── Arbitrage (6)       — Dutch-book spread scanner with estimated dual-leg pricing disclosures
├── [5] Intelligence
│       ├── Deep Analysis (7)   — 9-factor probabilistic forecasting, OFI order flow, and NLP sentiment signals
│       ├── AI / ML Engine      — Calibration diagrams, 32-feature importances, drift PSI index, and model lineage
│       └── Copilot             — Rule-based heuristic assistant with lexical TF-IDF market matching
├── [6] Analytics & Backtest
│       ├── Performance (8)     — Wilson 95% CI win rates, profit factor, drawdown metrics, and strategy leaderboard
│       └── Backtest Lab        — Monte Carlo statistical archetype simulation lab
└── [7] System
        ├── Platform Health     — Microservice process monitoring, book poller telemetry, and storage metrics
        └── Data Explorer       — TimescaleDB / SQLite hypertable browser and time-series inspector
```

---

## 3. Persistent Top Status Bar
Always visible across all views (`TopStatusBar.tsx`):
- **Operating Mode Badge:** `PAPER` (amber) / `SHADOW` (cyan) / `LIVE` (red) / `BACKTEST` (purple).
- **Kill Switch & Observation State:** Flashes prominent alerts if trading is halted or observation-only limits are triggered.
- **Connection Health & Latency:** Real-time WebSocket connectivity status (`Connected`, `Connecting`, `Disconnected`).
- **Data Freshness:** Elapsed time since newest order book tick with automated stale thresholds (>15s warning, >60s dead).
- **Financial Balance & Daily P&L:** Clean monospaced USDC figures (displays `—` when null; never defaults to arbitrary balances).
- **Fast Global Actions:** Audio toggle, Keyboard shortcut cheatsheet (`?`), Risk Configuration (`C`), Cancel All Orders, and Emergency Kill Switch (`K`).

---

## 4. Financial Risk & Capital Control System
- **Operating Regime:** Standardized to **$100 Operating Capital** with a **$200 Hard Bankroll Ceiling** and **$3.00 Maximum Order Sizing per Market**.
- **Realized vs Unrealized Decomposition:** Exposure decomposition marks positions to live mid-market books when present.
- **Reconciliation Transparency:** Displays automated reconciliation audits and specific findings if internal balances diverge from external limits.

---

## 5. Strategy & Machine Learning Integrity
- **Truthful Strategy Gating:** Distinguishes the 3 functional algorithmic execution strategies (`mm_avellaneda_stoikov`, `arb_binary_dutch_book`, `ml_random_forest_quant`) from the 47 architectural research stubs. The UI prevents toggling stubs to "Running" and explains their status.
- **Experimental ML Labeling:** Brier scores, ROC-AUC metrics, and calibration curves are clearly marked as evaluated against synthetic holdout distributions.
- **Lexical Copilot Transparency:** The Copilot is explicitly framed as a heuristic and template assistant rather than an unconstrained generative advisor.
