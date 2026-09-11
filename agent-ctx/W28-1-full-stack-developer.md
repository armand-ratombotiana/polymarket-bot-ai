# W28-1 — TypeScript Zero-Error Pass (27 errors → 0)
- **Date:** 2026-09-04
- **Scope:** All TS6133 / TS6196 / TS2739 / TS2345 / TS2352 errors across
  the `src/**` tree.
- **Agent:** full-stack-developer
- **Task ID:** W28-1

## Summary

`bunx tsc --noEmit --skipLibCheck` exited non-zero with **27 distinct
errors** across **17 files**. The brief listed 21 of them (the original
W28-1 list); 6 additional errors in untracked test files
(`CapitalAllocatorPanel.test.tsx`, `LiveSafetyGatePanel.test.tsx`,
`MLValidationPanel.test.tsx`, `OrderFlowPanel.test.tsx`) were also
visible to `tsc` and had to be cleared to hit zero. All 27 fixed;
`bunx tsc --noEmit --skipLibCheck 2>&1 | wc -l` now prints **0**.

## Files touched

### Tracked files (modified)
- `src/app/page.tsx` — Removed unused `ShortcutsModal` import.
- `src/components/AnalyticsPanel.tsx` — Removed unused `ciLowPct` /
  `ciHighPct` locals.
- `src/components/ArbitrageMatrixView.test.tsx` — Removed unused
  `mockFetchRouteByUrl` helper; renamed 3 unused `input` params →
  `_input`.
- `src/components/AuditLogPanel.tsx` — Removed unused `Inbox`,
  `Loader2` lucide imports; removed `_AuditRow` type alias + the
  `AuditRowProps` interface it referenced (both unexported).
- `src/components/BacktestLabView.test.tsx` — Removed unused `act`
  import.
- `src/components/DatabaseStatusPanel.test.tsx` — Renamed unused
  `input` param → `_input`.
- `src/components/KeyboardCheatSheet.tsx` — Removed unused
  `ReactKeyboardEvent` type import.
- `src/components/PortfolioRiskPanel.test.tsx` — Removed unused `vi`
  import.
- `src/components/StrategyPerformancePanel.test.tsx` — Renamed 2
  unused `input` params → `_input`.
- `src/components/charts/TradeTape.tsx` — Removed unused `FlowSide`
  type import.
- `src/components/ui/ConfidenceIntervalBadge.tsx` — Removed unused
  `import * as React from 'react'`.
- `src/components/ui/StatisticalSignificanceBadge.tsx` — Same as
  above.
- `src/components/ui/VirtualTable.tsx` — Removed unused `useCallback`
  import; fixed TS2739 by declaring `ariaAttributes` on the
  `RowComponent` prop type (lets react-window v2's `List` infer
  `RowProps = RowComponentProps` without `index` / `style` leaking
  in and tripping `ExcludeForbiddenKeys_2<RowProps>` on `rowProps`).

### Untracked test files (newly created, now also fixed)
- `src/components/CapitalAllocatorPanel.test.tsx` — Widened 2
  `input: string` param types → `string | URL | Request` (matches
  global `fetch` signature; TS2345 under `strictFunctionTypes`).
- `src/components/LiveSafetyGatePanel.test.tsx` — Removed unused
  `mockFetchReject` helper.
- `src/components/MLValidationPanel.test.tsx` — Removed unused
  `mockFetchOk` helper; widened 3 `input: string` param types →
  `string | URL | Request`.
- `src/components/OrderFlowPanel.test.tsx` — Rewrote the
  `sampleOrderBooks` fixture to match `OrderBook` interface exactly
  (added `updated_at`, removed `bids`/`asks` depth-ladder fields);
  dropped the now-unnecessary `as OrderBook` cast.

## Verification

- `bunx tsc --noEmit --skipLibCheck 2>&1 | wc -l` → **0**.
- `bun run lint` → exit 0, no output (clean across all files).
- `bun run test` → **1176 passed (1176)** across 59 test files in
  272s. No regressions — the suite was at 1115 passing in the prior
  W26-7 entry; the +61 delta is the new W27-W28 test files
  (CapitalAllocatorPanel, LiveSafetyGatePanel, MLValidationPanel,
  OrderFlowPanel, RetentionPanel, ShadowInferencePanel,
  PerformanceReportPanel, plus expansion of existing panels) — all
  now compiling AND passing.
- Dev server log (`dev.log`) — clean. Next.js 16.1.3 / Turbopack
  compiled without errors after the fixes landed.

## Key insights

1. **The brief was 6 errors short.** The task listed 21 errors but
   `tsc` actually reported 27. Six additional errors lived in
   untracked test files created by recent W27/W28 agents. They were
   all real blockers for `bun run build` so they were fixed alongside
   the listed ones. Lesson for future task authors: re-run `tsc`
   right before finalising the error list, especially when prior
   agents may have committed new files.
2. **react-window v2's `RowProps` inference is subtle.** The
   `List<RowProps>` generic is inferred from BOTH `rowComponent` and
   `rowProps`. If `rowComponent` is declared to take
   `{ index, style } & RowComponentProps` (no `ariaAttributes`),
   TypeScript can't tell that `index` and `style` are react-window's
   reserved props (to be stripped via `ExcludeForbiddenKeys`), so it
   includes them in `RowProps` and then rejects our `rowProps`
   (which lacks them) on the `rowProps` prop. The fix is to declare
   ALL THREE reserved props (`ariaAttributes`, `index`, `style`) on
   the `rowComponent` so TypeScript can correctly infer
   `RowProps = RowComponentProps`.
3. **`noUnusedParameters` is satisfied by `_` prefix.** Five test
   files now have `(_input: string) => ...` callbacks. This is the
   standard TypeScript convention for unused trailing params and
   avoids the noise of pulling in `string | URL | Request` unions
   that the test doesn't actually need.
