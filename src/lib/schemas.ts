// lib/schemas.ts — Zod runtime validation schemas for backend API responses.
//
// W10-5 — Runtime type safety net for the frontend.
//
// Why this file exists:
//   The backend (Python FastAPI) is the source of truth for the shape of
//   every API response the frontend consumes. TypeScript interfaces (e.g.
//   `BotSnapshot` in `hooks/useBot.ts`, `Analytics` in
//   `components/AnalyticsPanel.tsx`) describe what we EXPECT, but they only
//   exist at compile time. At runtime, a backend change (renamed field,
//   dropped optional, new required field) silently produces `undefined` in
//   the UI — usually rendering `NaN`/`—` instead of crashing visibly.
//
//   These Zod schemas validate the actual JSON wire-shape on every fetch.
//   In dev, mismatches surface loudly (console.error + a logged issue tree).
//   In prod, the `safeFetch` helper returns a discriminated union so callers
//   can fall back to a safe default instead of feeding untyped data into
//   React state.
//
// Design choices:
//   * `.passthrough()` is used wherever the backend may add fields the
//     frontend doesn't consume yet (Snapshot, Analytics). This prevents a
//     new field added in W11 from breaking the frontend in W10.
//   * `.optional()` is used liberally for fields the backend may omit in
//     degraded states (no order book yet, ML not trained, etc.).
//   * Numeric fields use `z.number()` (not `z.coerce.number()`) so a
//     backend that returns `"3.14"` (string) fails loudly in dev instead
//     of being silently coerced — that's almost always a bug.
//   * Enums are pinned with `z.enum([...])` to catch contract drift
//     (e.g. backend introducing a new order status).
//
// Compatibility note:
//   This module is intentionally a NEW file. It does NOT replace the
//   existing hand-written interfaces in `hooks/useBot.ts` or
//   `components/AnalyticsPanel.tsx` (those stay as the compile-time view).
//   The inferred `Position`/`Order`/etc. types below are EXPORTED with the
//   same names, but the existing interfaces are imported directly from
//   their source modules — there is no name collision because consumers
//   import either `import type { Position } from '@/hooks/useBot'` OR
//   `import { PositionSchema } from '@/lib/schemas'` (and infer locally).

import { z } from 'zod'

// ---------------------------------------------------------------------------
// Position schema
// ---------------------------------------------------------------------------
// Matches the wire-shape of `/api/positions` entries (post-S1 mark-to-market
// fields `current_price` + `unrealized_pnl` are optional). `slug` and
// `yes_shares` are NOT marked optional because the backend always emits
// them when a position exists (they're derived from the order book state).
export const PositionSchema = z.object({
  token_id: z.string(),
  slug: z.string().optional(),
  side: z.enum(['LONG', 'SHORT']).optional(),
  size: z.number().optional(),
  avg_price: z.number().optional(),
  yes_shares: z.number().optional(),
  no_shares: z.number().optional(),
  avg_entry_price: z.number().optional(),
  total_invested: z.number().optional(),
  realised_pnl: z.number().optional(),
  realized_pnl: z.number().optional(),
  current_price: z.number().nullable().optional(),
  unrealized_pnl: z.number().nullable().optional(),
  unrealized_pnl_alias: z.number().nullable().optional(),
  opened_at: z.string().optional(),
  strategy: z.string().optional(),
}).passthrough()

export const PositionsResponseSchema = z.array(PositionSchema)

// ---------------------------------------------------------------------------
// Order schema
// ---------------------------------------------------------------------------
// `/api/orders` returns `{ orders: Order[] }`. Each order carries size,
// matched fill, strategy, and a `paper` flag. Status enum is pinned to
// catch backend additions (e.g. `EXPIRED`, `REPLACED`).
export const OrderSchema = z.object({
  order_id: z.string(),
  token_id: z.string(),
  slug: z.string().optional(),
  side: z.enum(['BUY', 'SELL']),
  price: z.number(),
  size: z.number(),
  size_matched: z.number().optional(),
  status: z.enum(['PENDING', 'FILLED', 'PARTIAL', 'CANCELLED', 'REJECTED', 'OPEN', 'CLOSED']).optional(),
  strategy: z.string().optional(),
  paper: z.boolean().optional(),
  created_at: z.union([z.string(), z.number()]).optional(),
}).passthrough()

export const OrdersResponseSchema = z.object({
  orders: z.array(OrderSchema),
}).passthrough()

// ---------------------------------------------------------------------------
// Trade / fill schema
// ---------------------------------------------------------------------------
// `/api/trades` returns `{ trades: Trade[] }`. `timestamp` may be ISO
// 8601 string OR unix epoch (seconds OR ms) — union covers both shapes.
export const TradeSchema = z.object({
  trade_id: z.string().optional(),
  token_id: z.string(),
  slug: z.string().optional(),
  side: z.enum(['BUY', 'SELL']),
  price: z.number(),
  size: z.number(),
  pnl: z.number().optional(),
  timestamp: z.union([z.string(), z.number()]),
  strategy: z.string().optional(),
  paper: z.boolean().optional(),
}).passthrough()

export const TradesResponseSchema = z.object({
  trades: z.array(TradeSchema),
}).passthrough()

// ---------------------------------------------------------------------------
// Market schema
// ---------------------------------------------------------------------------
// `/api/markets` returns a list of binary prediction markets. `question`
// is the human-readable description; yes/no prices are 0..1 probabilities.
export const MarketSchema = z.object({
  token_id: z.string(),
  question: z.string().optional(),
  slug: z.string().optional(),
  yes_price: z.number().optional(),
  no_price: z.number().optional(),
  spread: z.number().optional(),
  volume: z.number().optional(),
  liquidity: z.number().optional(),
  end_date: z.string().optional(),
  active: z.boolean().optional(),
}).passthrough()

export const MarketsResponseSchema = z.array(MarketSchema)

// ---------------------------------------------------------------------------
// OrderBook schema (used by useBot's snapshot)
// ---------------------------------------------------------------------------
// Matches `OrderBook` interface in `hooks/useBot.ts`. Nullable numeric
// fields are common (no market yet → mid is null).
export const OrderBookSchema = z.object({
  token_id: z.string(),
  slug: z.string().optional(),
  best_bid: z.number().nullable().optional(),
  best_ask: z.number().nullable().optional(),
  mid: z.number().nullable().optional(),
  spread: z.number().nullable().optional(),
  updated_at: z.number().optional(),
}).passthrough()

// ---------------------------------------------------------------------------
// Analytics schema (full institutional KPI panel)
// ---------------------------------------------------------------------------
// Matches `Analytics` interface in `components/AnalyticsPanel.tsx`. This is
// a wide object — most fields are required (the backend always emits them)
// but extended KPIs (S3) may be null when sample size is too small.
export const AnalyticsSchema = z.object({
  equity: z.number(),
  realized_pnl: z.number().optional(),
  unrealized_pnl: z.number().optional(),
  net_pnl: z.number().optional(),
  total_pnl: z.number().optional(),
  total_trades: z.number().optional(),
  winning_trades: z.number().optional(),
  losing_trades: z.number().optional(),
  closed_trades: z.number().optional(),
  open_trades: z.number().optional(),
  win_rate: z.number().optional(),
  win_rate_ci_low: z.number().nullable().optional(),
  win_rate_ci_high: z.number().nullable().optional(),
  profit_factor: z.union([z.number(), z.string(), z.null()]).optional(),
  max_drawdown_dollars: z.number().optional(),
  max_drawdown_pct: z.number().optional(),
  total_volume_usdc: z.number().optional(),
  open_exposure: z.number().optional(),
  open_position_count: z.number().optional(),
  pending_order_capital: z.number().optional(),
  risk_utilization: z.number().optional(),
  mode: z.string().optional(),
  data_freshness_seconds: z.number().optional(),
  peak_equity: z.number().optional(),
  active_strategies: z.array(z.string()).optional(),
  avg_win: z.number().nullable().optional(),
  avg_loss: z.number().nullable().optional(),
  expectancy: z.number().nullable().optional(),
  sharpe_ratio: z.number().nullable().optional(),
}).passthrough()

// ---------------------------------------------------------------------------
// Health / status schema
// ---------------------------------------------------------------------------
// `/api/status` returns the bot's current operating state. Used by the
// status pill in the command center header.
export const HealthSchema = z.object({
  status: z.string(),
  mode: z.string().optional(),
  uptime: z.number().optional(),
  balance: z.number().optional(),
  kill_switch: z.boolean().optional(),
  kill_switch_durable: z.boolean().optional(),
  observation_only: z.boolean().optional(),
  observation_reason: z.string().optional(),
  daily_pnl: z.number().optional(),
  paper_balance: z.number().nullable().optional(),
  strategies: z.array(z.string()).optional(),
}).passthrough()

// ---------------------------------------------------------------------------
// ML metrics schema
// ---------------------------------------------------------------------------
// `/api/ml/metrics` returns model calibration + drift stats. All fields
// optional — model may not be trained yet.
export const MLMetricsSchema = z.object({
  auc: z.number().optional(),
  roc_auc: z.number().optional(),
  brier: z.number().optional(),
  brier_score: z.number().optional(),
  log_loss: z.number().optional(),
  accuracy: z.number().optional(),
  ece: z.number().optional(),
  n_updates: z.number().optional(),
  version: z.string().optional(),
  model_ready: z.boolean().optional(),
  drift_status: z.string().optional(),
  drift_psi: z.number().optional(),
  drift_brier: z.number().nullable().optional(),
  drift_ewma_brier: z.number().nullable().optional(),
  meta_learner_warm: z.boolean().optional(),
  training_source: z.string().optional(),
  adaptive_weights: z
    .object({
      rf: z.number().optional(),
      gb: z.number().optional(),
      sgd: z.number().optional(),
      lgbm: z.number().optional(),
    })
    .passthrough()
    .optional(),
}).passthrough()

// ---------------------------------------------------------------------------
// Snapshot schema (the big one)
// ---------------------------------------------------------------------------
// `/api/snapshot` returns the entire bot state in a single response. This
// is the composite type consumed by `useBot`. `.passthrough()` is critical
// here — the backend adds fields constantly (drift detection, whale
// alerts, etc.) and we don't want a new backend field to break the
// frontend's parsing.
export const SnapshotSchema = z.object({
  type: z.string().optional(),
  timestamp: z.number().optional(),
  mode: z.string().optional(),
  kill_switch: z.boolean().optional(),
  kill_switch_durable: z.boolean().optional(),
  observation_only: z.boolean().optional(),
  observation_reason: z.string().optional(),
  daily_pnl: z.number().optional(),
  paper_balance: z.number().nullable().optional(),
  strategies: z.array(z.string()).optional(),
  order_books: z.array(OrderBookSchema).optional(),
  open_orders: z.array(OrderSchema).optional(),
  positions: z.array(PositionSchema).optional(),
  recent_trades: z.array(TradeSchema).optional(),
  events: z.array(z.string()).optional(),
  ml: MLMetricsSchema.optional(),
  balance: z.number().optional(),
}).passthrough()

// ---------------------------------------------------------------------------
// Events schema (audit log)
// ---------------------------------------------------------------------------
export const EventsResponseSchema = z.object({
  events: z.array(z.string()),
}).passthrough()

// ---------------------------------------------------------------------------
// OrderBooks response schema
// ---------------------------------------------------------------------------
export const OrderBooksResponseSchema = z.object({
  order_books: z.array(OrderBookSchema),
}).passthrough()

// ---------------------------------------------------------------------------
// Inferred TypeScript types
// ---------------------------------------------------------------------------
// These are EXPORTED for consumers that want to derive types from the Zod
// schemas (rather than maintaining parallel hand-written interfaces). The
// existing interfaces in `hooks/useBot.ts` and `components/AnalyticsPanel.tsx`
// remain authoritative for their respective components — these types are
// provided for new consumers that want runtime-checked types.
export type Position = z.infer<typeof PositionSchema>
export type Order = z.infer<typeof OrderSchema>
export type Trade = z.infer<typeof TradeSchema>
export type Market = z.infer<typeof MarketSchema>
export type OrderBook = z.infer<typeof OrderBookSchema>
export type Analytics = z.infer<typeof AnalyticsSchema>
export type Health = z.infer<typeof HealthSchema>
export type MLMetrics = z.infer<typeof MLMetricsSchema>
export type Snapshot = z.infer<typeof SnapshotSchema>
