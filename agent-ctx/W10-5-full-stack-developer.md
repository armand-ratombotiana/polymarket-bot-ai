# W10-5 — Zod schemas for API type safety

**Agent:** full-stack-developer
**Date:** 2026-09-04
**Task:** Add Zod runtime validation schemas for backend API responses consumed by the frontend.

## Context inputs read
- `worklog.md` (last ~200 lines) — Wave 9 (UI polish, a11y, memoization) complete; W10 is the type-safety wave.
- `src/lib/api.ts` — `apiFetch` injects auth + `XTransformPort=8080` but returns `Promise<Response>` (no runtime check).
- `src/hooks/useBot.ts` — `BotSnapshot`/`Position`/`Order`/`Trade`/`MLState` are hand-written interfaces; snapshot hook feeds raw JSON straight into React state.
- `src/components/PositionsPanel.tsx` — `Position` uses `realised_pnl` (British), optional `current_price?`/`unrealized_pnl?` (S1 mark-to-market additions).
- `src/components/AnalyticsPanel.tsx` — `Analytics` is wide (~28 fields; several nullable: `win_rate_ci_low`, `profit_factor: number | string | null`, `expectancy`, `sharpe_ratio`).
- `package.json` — confirmed `zod@^4.0.2` already in deps; ran `bun add zod` per task spec which bumped to 4.5.4.

## Files created (4 new files, 0 modifications to existing components)

### `src/lib/schemas.ts`
- 13 schemas: `PositionSchema`, `PositionsResponseSchema`, `OrderSchema`, `OrdersResponseSchema`, `TradeSchema`, `TradesResponseSchema`, `MarketSchema`, `MarketsResponseSchema`, `OrderBookSchema`, `AnalyticsSchema`, `HealthSchema`, `MLMetricsSchema`, `SnapshotSchema` (+ `EventsResponseSchema` and `OrderBooksResponseSchema` response wrappers).
- All schemas use `.passthrough()` so backend field additions (e.g. W11 whale alerts) don't break frontend parsing.
- Optional fields handled with `.optional()`; nullable numeric fields (`current_price`, `mid`, `paper_balance`, `win_rate_ci_low`) use `.nullable().optional()`.
- Enums pinned via `z.enum([...])` for `side` (LONG/SHORT, BUY/SELL), `status` (PENDING/FILLED/PARTIAL/CANCELLED/REJECTED/OPEN/CLOSED).
- Union types for `profit_factor: number | string | null` and `timestamp: string | number`.
- 9 inferred TypeScript types exported: `Position`, `Order`, `Trade`, `Market`, `OrderBook`, `Analytics`, `Health`, `MLMetrics`, `Snapshot`.

### `src/lib/safeFetch.ts`
- `safeFetch<T>(url, schema, init?)` returns discriminated union `{ success: true, data } | { success: false, error, raw }`.
- Wraps `apiFetch` (preserves auth + gateway port injection).
- Calls `logSchemaError` on parse failure to surface dev console noise.
- Synchronous `safeParse(value, schema)` variant for WebSocket message validation (same union return shape).
- Handles HTTP non-2xx, JSON parse errors, and network throws uniformly (no try/catch needed at call sites).

### `src/lib/validateDev.ts`
- `logSchemaError(url, raw, zodError)` — logs structured issue tree to `console.error` in dev (no-op in prod via `process.env.NODE_ENV !== 'production'` check). Each issue formatted with JSON path (e.g. `positions[0].avg_price`).
- `validateDev(value, schema)` — hot-path validator that skips schema parsing entirely in production (returns `{ success: true, data: value as T }` without running the schema).

### `src/lib/schemas.test.ts`
- 80 tests across 16 describe blocks.
- Covers: valid happy-path payloads, missing-required-field rejections, wrong-type rejections (string where number expected — no coercion so backend drift is caught), enum drift detection (unknown side/status values rejected), `.optional()` semantics (omitted vs. null vs. undefined), `.passthrough()` preservation (unknown fields survive), nullable numeric fields, union types, nested schema validation (malformed position inside snapshot array rejected), `safeFetch`/`safeParse`/`validateDev`/`logSchemaError` helpers (HTTP error path, JSON parse error path, network throw path, schema mismatch path, raw payload preservation, XTransformPort query injection).

## Verification
- `bun run lint` — 3 pre-existing warnings on other files (error.tsx, ErrorBoundary.tsx, PanelErrorBoundary.tsx — unused eslint-disable directives, none from my new files).
- `bun run test` — 168/168 pass (80 new + 88 pre-existing).

## Design decisions
- Used `ZodType<T>` instead of `ZodSchema<T>` because Zod v4 renamed `ZodSchema` to `ZodType` (the `ZodSchema` named export was removed as a runtime value).
- Used `.passthrough()` on every schema (rather than `.strict()`) because the backend is the source of truth and adds fields constantly — a strict schema would break the frontend every time the backend adds a non-validated field.
- Did NOT replace the existing hand-written TypeScript interfaces in `hooks/useBot.ts` or `components/AnalyticsPanel.tsx`. The inferred types from Zod are exported with the same names (`Position`, `Order`, etc.) but the existing interfaces remain authoritative for their respective components — there's no name collision because consumers import from one source or the other, not both.
- The `safeFetch` helper intentionally reuses `apiFetch` (not the global `fetch`) so the auth + gateway port injection stays consistent. Callers using `safeFetch` automatically get the `XTransformPort=8080` query param appended for `/api/*` URLs.
- Network errors are caught and returned as `{ success: false }` rather than re-thrown — callers don't have to wrap every call site in try/catch.
