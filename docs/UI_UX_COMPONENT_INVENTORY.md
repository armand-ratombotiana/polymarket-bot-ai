# Polymarket Workstation — Component Inventory & Disposition Matrix

## 1. Application Shell & Navigation

| Component | File Path | Status | Key Enhancements & Role |
| :--- | :--- | :--- | :--- |
| **Sidebar Navigation** | `src/components/Sidebar.tsx` | **NEW** | Grouped vertical navigation, auto-collapse on smaller viewports, keyboard hints (`1`–`8`), accessible ARIA tree. |
| **Top Status Bar** | `src/components/TopStatusBar.tsx` | **NEW** | Persistent real-time header: mode badge, connectivity dot, data freshness timer, capital/P&L monitor, kill switch. |
| **Confirmation Dialog** | `src/components/ConfirmationDialog.tsx` | **NEW** | Accessible modal with focus trap, Escape cancel, severity theming, and explicit action impact disclosures. |
| **Header** | `src/components/Header.tsx` | **REFACTORED** | Stripped fabricated badges ("50+ Strategies", "Calibrated", "1-Click"); retained token management. |
| **Main Page Shell** | `src/app/page.tsx` | **REFACTORED** | Restructured into sidebar grid layout with clean section routing, modals, and failure overlays. |

---

## 2. Command Center & Risk Monitoring

| Component | File Path | Status | Key Enhancements & Role |
| :--- | :--- | :--- | :--- |
| **Risk Status Panel** | `src/components/RiskStatusPanel.tsx` | **REFACTORED** | Full capital breakdown ($100 operating / $200 ceiling), exposure bar, daily loss tracking, reconciliation audits. |
| **Equity Curve** | `src/components/EquityCurve.tsx` | **REFACTORED** | Dynamic SVG curve with explicit $100 baseline reference line, mode labeling, and peak/trough metrics. |
| **Analytics Panel** | `src/components/AnalyticsPanel.tsx` | **REFACTORED** | Wilson 95% confidence intervals on win rate, sample size warnings (n < 10), profit factor, drawdown. |
| **ML Status Card** | `src/components/MLPanel.tsx` | **REFACTORED** | Compact feature importances, online update counter, model readiness indicators. |
| **Event Log** | `src/components/EventLog.tsx` | **REFACTORED** | Real-time audit log with categorized syntax highlighting, search filter, and clipboard copy. |

---

## 3. Markets & Execution

| Component | File Path | Status | Key Enhancements & Role |
| :--- | :--- | :--- | :--- |
| **Markets Panel** | `src/components/MarketsPanel.tsx` | **REFACTORED** | Multi-line hierarchical typography, implied probability gauges, data age indicators, stale flags (>30s). |
| **Market Screener** | `src/components/MarketScreener.tsx` | **REFACTORED** | Gamma API integration, fixed search closure in 30s auto-refresh, error handling, query clear button. |
| **Market Chart Modal** | `src/components/MarketChartModal.tsx` | **REFACTORED** | Candlestick timeline with synthetic data watermark notice, EMA(21) overlay, fast $1.50/$3 order ticket. |
| **Depth Chart Modal** | `src/components/DepthChartModal.tsx` | **REFACTORED** | Cumulative L2 order book depth visualization, click-to-fill limit price selection, trade ticket. |

---

## 4. Portfolio & Strategies

| Component | File Path | Status | Key Enhancements & Role |
| :--- | :--- | :--- | :--- |
| **Positions Panel** | `src/components/PositionsPanel.tsx` | **REFACTORED** | Open positions table with contract categorization, YES/NO outcome tags, cost basis, realized P&L. |
| **Orders Panel** | `src/components/OrdersPanel.tsx` | **REFACTORED** | Working order queue with filled/remaining size indicator, per-order cancel, and bulk Cancel All. |
| **Trades Panel** | `src/components/TradesPanel.tsx` | **REFACTORED** | Recent executions audit stream with strategy attribution, P&L markers, and explicit slice bounds. |
| **Strategy Matrix** | `src/components/StrategyMatrix.tsx` | **REFACTORED** | Truthful strategy gating (3 Implemented vs 47 Research Stubs), deploy/stop toggles, stub block notices. |
| **Arbitrage Matrix** | `src/components/ArbitrageMatrixView.tsx` | **REFACTORED** | Dutch-book spread scanner with synthetic NO-pricing disclosures and dual-leg execution feedback. |
| **Strategy Config** | `src/components/StrategyConfigModal.tsx` | **REFACTORED** | Runtime risk and strategy parameter tuning with realistic limits aligned to the $100 capital regime. |

---

## 5. Intelligence, Analytics & System

| Component | File Path | Status | Key Enhancements & Role |
| :--- | :--- | :--- | :--- |
| **Deep Analysis View** | `src/components/DeepAnalysisView.tsx` | **REFACTORED** | 9-factor probabilistic evaluation, OFI flow metrics, news sentiment, corrected ~$1.50 slippage scale. |
| **AI/ML Command Center** | `src/components/AIMLCommandCenter.tsx` | **REFACTORED** | 32-feature Gini rankings, calibration reliability curves, drift PSI index, experimental training disclosures. |
| **AI Copilot Panel** | `src/components/AICopilotPanel.tsx` | **REFACTORED** | Heuristic market intelligence assistant with TF-IDF lexical matches and clear capability boundaries. |
| **Backtest Lab** | `src/components/BacktestLabView.tsx` | **REFACTORED** | Monte Carlo parameter simulation lab with permanent simulation watermark notices. |
| **System Health** | `src/components/SystemHealthView.tsx` | **REFACTORED** | Microservices watchdog monitor, book poller success metrics, database storage stats. |
| **Database Explorer** | `src/components/DatabaseExplorerView.tsx` | **REFACTORED** | Hypertable data browser with table descriptions and in-memory buffering explanations. |
| **Shortcuts Modal** | `src/components/ShortcutsModal.tsx` | **REFACTORED** | Up-to-date cheatsheet for 1–8 section switching, emergency kill switch, and configuration drawers. |
