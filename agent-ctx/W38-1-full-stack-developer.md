# W38-1 — UI/UX Remediation Backlog (read-only audit)

- **Task ID:** W38-1
- **Agent:** full-stack-developer
- **Date:** 2026-11-12
- **Status:** ✅ Complete
- **Duration:** Single session

## Work product

- **`docs/ui_ux/REMEDIATION_BACKLOG.md`** (1,700 lines, 45 findings
  grouped into 10 categories + severity summary + remediation order).
- **`worklog.md`** appended with the standard format entry
  (W38-1 section, ~120 lines).

## Headline findings (45 total)

- **P0 (7):** §1.1 backend-reachable guard missing; §1.2 lint fails
  with 2 test-file errors; §2.1 ArbitrageMatrixView execute button
  has no confirmation; §2.2 PositionsPanel close button has no
  confirmation; §4.1 DeepAnalysisView ML prediction shown without
  confidence interval / model version / Brier; §6.1 1,277 hardcoded
  Tailwind hex literals across 61 files bypass the design system.
- **P1 (18):** §1.3 MarketsPanel slug-substring category filter;
  §1.4 EventLog keyword severity parser; §1.5 synthetic-data caveat
  buried in footer; §1.6 disconnect-overlay condition too narrow;
  §2.3 StrategyMatrix toggle no confirmation; §2.4 AIMLCommandCenter
  retrain no confirmation; §2.7 hidden `k` keyboard shortcut;
  §3.1 IngestionHealthPanel no rate-limit cross-join; §3.2 ingestion
  gaps no severity annotation; §4.2 AICopilotPanel no AI-generated
  label; §4.3 MLPanel "Calibrated" badge not verifiable; §5.1 only
  8/32 sidebar items have keyboard shortcuts; §5.2 two `Perf`
  shortLabels collide; §5.3 three confusing Database/Data/Ingestion
  items; §6.4 Sidebar logo SVG hardcodes `#3b82f6`; §7.1 + §7.2
  row-click not keyboard accessible; §8.1 119 `setInterval` calls
  across 30+ files; §9.1 TopStatusBar right cluster overflows on
  tablet; §9.2 no mobile swipe-to-open; §10.1 strategy lifecycle
  UI missing; §10.2 WS alerts not surfaced as panel-level toasts.
- **P2 (18):** confirmation asymmetries, hardcoded thresholds,
  dead code (ShortcutsModal, CommandPalette not mounted, orphaned
  PortfolioRiskPanel), contrast failures, missing lazy-load
  migrations, mobile layout issues.
- **P3 (2):** CSV export feature parity, restart-bot-service button.

## Verification

- `bun run lint` → **exit 1, 2 errors** in test files
  (`ErrorBoundary.test.tsx:112` + `PanelErrorBoundary.test.tsx:114`
  — `react-hooks/globals` rule fires on `throwNext` reassignment
  during render). PRE-EXISTING errors, not introduced by this
  audit (no source code was modified). Documented as finding §1.2.
- The backlog is evidence-based: every finding cites at least one
  specific file + line range. Grep / wc / ls commands quoted inline
  in §0.

## Files added

1. `docs/ui_ux/REMEDIATION_BACKLOG.md` (1,700 lines).
2. `agent-ctx/W38-1-full-stack-developer.md` (this file).
3. `worklog.md` (appended W38-1 entry, ~120 lines).

## Notes for next-wave engineers

- The backlog's §12.1 lists a 7-sprint remediation order. Sprint 1
  (P0) should be done first: lint fix (30min) → backend-reachable
  guard (2h) → confirmation dialogs on arb-execute + close-position
  (3h) → ML prediction badge (2h) → design-system codemod start
  (mechanical, 1 sprint).
- The 1,277-occurrence hex-literal codemod (§6.1) is the single
  largest mechanical change. Suggest running it in batches per file
  to keep PRs reviewable. A `tailwind.config.ts` extension mapping
  the design tokens to Tailwind color utilities (`bg-card`,
  `text-primary`, etc.) should land FIRST so the codemod has
  target classes to migrate to.
- The strategy lifecycle UI (§10.1) is a feature gap, not a polish
  item — the backend ships a 9-state machine + audit trail + LIVE
  requirements checklist (added in W37-5 per worklog) that's
  currently invisible to operators. Surfacing it is a meaningful
  capability unlock.

## Cross-references

- `docs/assessment/UI_UX_ASSESSMENT.md` (W17-7 baseline assessment)
- `docs/reassessment/UI_UX_REASSESSMENT.md` (W17-10 wave-1→16 delta)
- `docs/ui_ux/DESIGN_SYSTEM.md` (existing design-system reference)
- The backlog is a strict superset + extension of those prior
  documents, not a re-do.
