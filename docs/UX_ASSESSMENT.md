# UI/UX Assessment — Polymarket Pro Workstation

Source: live audit of `webui/src` (all 25 components + `useBot` hook + `page.tsx`), backend `api/server.py`, and running containers. Scope: master-prompt mandate (UI/UX redesign, market readability, command center, feature completeness). Currency scale: **USD 100 operating capital / USD 200 ceiling** (see §8).

Severity: **CRITICAL** = financial integrity / data fabrication; **HIGH** = blocks correct use or misleads; **MEDIUM** = usability/accessibility; **LOW** = polish.

---

## A. Cross-cutting findings

| # | Feature | Current state | UX problem | Functional problem | Severity | Required change | Validation |
|---|---------|---------------|------------|--------------------|----------|-----------------|------------|
| A1 | Fake-value fallbacks | Many components render fabricated figures when the API is down or a field is missing: `EquityCurve || 10000.0`, `useBot paper_balance ?? 10000`, `AIML PSI '0.042'`, `drift 'HEALTHY'`, `version 'v1.0.0'`, `DeepAnalysis brier_score \|\| 0.175`, `compute \|\| 1.2ms`, `net_edge '0.0%'`, `liquidity '0'`, `EMA(8/21)` polyline was just a close-price line, `Max $500` caps, `$10,000 Bankroll` badges, dead `sortBy`, 2s dead polling | Users cannot distinguish real data from defaults; fabricated "model" metrics presented as truth | Misleads risk decisions; contradicts mandate "never present fake features as operational" | **CRITICAL** | Replace every fake fallback with `null`/`—`/explicit "unavailable"; real EMA(21); cap all trade inputs to $3/market; poll every 30s | Fixed & deployed (Phase 2a). Spot-check: kill API → UI shows `—`, never a number |
| A2 | Color-only indicators | Green/red badges and text colors carry the only meaning (PnL, kill state, drift status, OFI sign) | Red/green colorblind users cannot read state; no text alternative | WCAG 2.2 AA requirement: "don't rely on color alone" | **MEDIUM** | Add icons/arrows/text (▲▼, "HALTED", "DRIFT") alongside every color signal | Component matrix validation per component |
| A3 | Mode / currency / period labels | Panels show numbers with no "which mode (backtest/paper/live)", "which currency (USDC)", "which period", or calculation definition | Users cannot tell what a number means or whether modes are mixed | Mandate: "never mix backtesting/paper/shadow/live"; "define every metric" | **HIGH** | Every KPI card gets: value + mode + currency + period + last-updated + definition tooltip | Audit all KPI cards against label checklist |
| A4 | No loading/error states | MarketsPanel, PositionsPanel, OrdersPanel, TradesPanel, Screener render empty tables silently on fetch failure | No distinction between "no data" and "broken"; users think system is idle | Delays incident detection | **MEDIUM** | Standardized skeleton loader + inline error + "last updated Xs ago" | Screener empty state added; extend pattern |
| A5 | Dead / duplicate routes | `DepthChartModal` was unreachable (dead UI); two trade modals overlap in purpose | Duplicate, conflicting trading UIs | Mandate: "eliminate dead routes / duplicate pages" | **HIGH** | Route Screener "Trade/Depth" → depth+quick-trade; MarketsPanel "Trade" → chart+quick-trade | Fixed & deployed; both paths verified via UI |

---

## B. Terminal / Command center

| # | Feature | Current state | UX problem | Functional problem | Severity | Required change | Validation |
|---|---------|---------------|------------|--------------------|----------|-----------------|------------|
| B1 | Risk status panel | Shows ceiling, deployable, total exposure, remaining loss, capital invested, reserved, cash, exposure-dollar-days, correlated group | No labels for mode/currency/recon status per metric; missing many mandated metrics | Mandate command-center metrics list (win rate+CI, profit factor, MDD, risk utilization, active positions, pending orders, bot mode, strategy/model health, data freshness, ingestion, system health, kill state) | **HIGH** | Expand panel to full command-center set, each metric labeled + defined; add risk-utilization gauges | Phase 3; validate each mandated metric renders with definition |
| B2 | Equity curve | Now renders `—` when no data; badge shows PnL | No baseline annotation ($100 operating / $200 ceiling); single series mixes realized+unrealized without definition | Users can't see where the $100 operating line sits | **MEDIUM** | Add operating-capital reference line + legend defining equity & PnL basis | Phase 3 |
| B3 | Analytics panel | Win rate %, total trades, volume, realised pnl, open exposure | Win rate has no sample size or CI; "realised" spelling vs "realized" | Mandate: win rate must carry sample size + CI | **MEDIUM** | Show n and Wilson CI next to win rate; unify terminology | Phase 3 |
| B4 | Positions panel | Badge now "$100 Operating Capital" (was "$10,000 Bankroll"); shows yes_shares, avg entry, total invested, PnL | No strategy tag, exposure, cost basis, or staleness flag on rows; long titles truncate with no tooltip | Stale positions (3 found in reconciliation) invisible; "unknown" strategy attribution | **MEDIUM** | Add strategy/exposure/staleness columns, title tooltips, per-position PnL % | Phase 3/4 |
| B5 | Orders panel | Cancel via DELETE `/api/orders/{id}`; shows price/size/type | No order-state machine display (CREATED→…→FILLED), no partial-fill, no cancel-all confirmation | Mandate: order-state machine must be observable and persisted | **HIGH** | Render order status + transition history; confirm before cancel-all | Phase 5 |
| B6 | Trades panel | `.slice(0, 50)` with no "truncated" indicator; no filters | Users can't tell the list is capped; no way to isolate a strategy | Silent data loss in the feed | **MEDIUM** | Show "showing 50 of N" + strategy/side filters; honor sort | Phase 3/4 |
| B7 | Markets panel | Sortable by mid/spread/age; question title truncated; no loading/error | No market status (open/closed) or freshness; no outcome (YES/NO) hierarchy | Market readability mandate | **MEDIUM** | Add status + freshness; structure Event → Market → Outcome | Phase 4 |
| B8 | Header / kill switch | Kill + observation banners; Cancel-All without confirmation | Observation reason shown only in banner | Risk of accidental cancel-all | **LOW** | Add confirmation dialog; show observation_reason in panel too | Phase 3 |

---

## C. Arb matrix / Strategies / AI-ML / Deep analysis

| # | Feature | Current state | UX problem | Functional problem | Severity | Required change | Validation |
|---|---------|---------------|------------|--------------------|----------|-----------------|------------|
| C1 | Arb execute | Now posts to real `/api/arbitrage/execute`, capped at $3, with leg-status + error feedback (was dead endpoint, $500 cap, silent failure) | Legs show per-leg status string only | Legs route through risk; live skips synthetic `_no` tokens | **HIGH** | ✅ Fixed & deployed. Add per-leg expandable result rows | Verified HTTP 422 (route live) |
| C2 | Strategy matrix | Start/stop toggles; registry lists only 3 implemented strategies; no reason column | Status taxonomy (RESEARCH…BLOCKED) not shown | Mandate: hide unfinished strategies, show status reason | **MEDIUM** | Show full status + reason; gate start on status | Phase 6 |
| C3 | Leaderboard | Score, win rate, total pnl per strategy | Win rate without sample size/CI; historical "unknown" strategy | Same as B3 | **MEDIUM** | Add n + CI; group "unknown" explicitly | Phase 3 |
| C4 | AI/ML command center | Now renders `—` for missing PSI/version/brier (was fake 0.042/HEALTHY/v1.0.0); shows brier, ROC-AUC, updates | No champion/challenger, no walk-forward evidence, no out-of-sample badge | Mandate: ML lifecycle with no-lookahead; don't claim validation | **HIGH** | Add lifecycle section (walk-forward, champion/challenger, oos badge) | Phase 6 |
| C5 | Deep analysis | Now renders `—` for missing brier/version/compute/bounds (was 0.175/1.0/1.2ms/0.0%); "Estimated Slippage ($100 block)" label | Slippage label implies $100 sizing (scale violation); forecast/edge definitions not shown | Mandate: every metric defined; market-implied vs forecast distinction | **MEDIUM** | Relabel slippage block; add definition footnotes | Phase 4/6 |
| C6 | Backtest lab | Runs fixed backtests; shows pnl/sharpe/sortino/MDD/win-rate | No walk-forward mode; no "this is a backtest, not live" watermark | Mandate: never mix backtest with live | **HIGH** | Add mode watermark + walk-forward toggle | Phase 6 |
| C7 | Copilot | Heuristic recommendations with confidence "match %" | Risk of being read as validated AI edge | Mandate: feature completeness / honesty | **MEDIUM** | Label as experimental/heuristic; never present as validated | Phase 6 |

---

## D. Screener / Database / Health / Modals

| # | Feature | Current state | UX problem | Functional problem | Severity | Required change | Validation |
|---|---------|---------------|------------|--------------------|----------|-----------------|------------|
| D1 | Market screener | Empty state + 30s polling added; dead `sortBy` removed | No error state; no pagination/length indicator; no refresh countdown | Silent stale data | **MEDIUM** | Add error state + "N markets, refreshed Xs ago" | ✅ Fixed & deployed; extend error state |
| D2 | Database explorer | Read-only table browser | No query explain / freshness | None critical | **LOW** | Add row counts + last-update per table | Phase 7 |
| D3 | System health | Now "$100 Operating / $200 Ceiling" (was "$10,000 Max Capital"); shows psi drift, uptime, poller stats | No ingestion freshness, reconciliation status, or model health link | Mandate: system health must include ingestion + reconciliation | **MEDIUM** | Add ingestion lag, reconciliation badge, model health | Phase 3/7 |
| D4 | Chart modal | Real EMA(21) now; default size $1.50; cap $3; trade feedback + error surfaced (was $100 default, $500 cap, fake EMA, silent failures) | — | — | **HIGH** | ✅ Fixed & deployed. Add scale badge ($100 op. / $3 cap) | Verified in UI |
| D5 | Depth modal | Default $1.50, cap $3; reachable from Screener; good error handling already | — | — | **HIGH** | ✅ Fixed & deployed | Verified in UI |
| D6 | Shortcuts modal | Now lists only real shortcuts (was fake `/`, `B`, `S` actions) | `1–8` maps 10 tabs (9/10 unreachable by shortcut) | Minor | **LOW** | Map remaining tabs or cap shortcut hint to 8 | ✅ Fixed & deployed |

---

## E. Functional-completeness audit (feature flags)

| # | Area | Finding | Required action |
|---|------|---------|-----------------|
| E1 | Strategy registry | Only 3 strategies implemented; UI lists others as if usable | Gate each strategy by status; render BLOCKED/NOT-IMPLEMENTED honestly |
| E2 | Live execution | `live_trading_enabled=false`; every live order path must first pass risk + reconciliation gates | Keep default-off; expose toggle only with explicit authorization |
| E3 | ML validation | No out-of-sample / walk-forward evidence surfaced in UI | Add lifecycle status badge; block "validated" claims |
| E4 | Order states | API returns simple order records; no persisted state machine | Phase 5 state machine + UI rendering |

---

## Status legend
- **Fixed & deployed** — change is live in containers (Phase 2a deploy).
- **Phase N** — scheduled in the execution plan; not yet implemented.