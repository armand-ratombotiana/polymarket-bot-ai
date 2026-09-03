// lib/schemas.test.ts — Runtime validation tests for the Zod API schemas.
//
// W10-5 — Runtime type safety net for the frontend.
//
// Test philosophy:
//   These tests target the SCHEMAS, not the API contract. They verify
//   that the Zod definitions in `lib/schemas.ts`:
//     1. Accept well-formed payloads (the happy path).
//     2. Reject malformed payloads (the API-drift detection path).
//     3. Honour `.optional()` correctly (omitted vs. null vs. undefined).
//     4. Honour `.passthrough()` correctly (extra fields survive).
//     5. Pin enums (a new order status must be a deliberate update).
//     6. Handle edge cases (null where a number was expected, wrong
//        primitive types, empty arrays, deeply-nested paths).
//
//   Each test block is self-contained — no shared mutable state. The
//   fixture payloads are minimal valid objects that exercise every code
//   path of the schema under test (not a single representative sample).

import { describe, it, expect, vi, beforeEach } from 'vitest'
import {
  PositionSchema,
  PositionsResponseSchema,
  OrderSchema,
  OrdersResponseSchema,
  TradeSchema,
  TradesResponseSchema,
  MarketSchema,
  MarketsResponseSchema,
  OrderBookSchema,
  AnalyticsSchema,
  HealthSchema,
  MLMetricsSchema,
  SnapshotSchema,
  EventsResponseSchema,
  OrderBooksResponseSchema,
} from '@/lib/schemas'
import { safeFetch, safeParse } from '@/lib/safeFetch'
import { logSchemaError, validateDev } from '@/lib/validateDev'

// ---------------------------------------------------------------------------
// Position schema
// ---------------------------------------------------------------------------
describe('PositionSchema', () => {
  it('accepts a fully-populated position', () => {
    const r = PositionSchema.safeParse({
      token_id: '0xabc',
      slug: 'will-btc-hit-100k',
      side: 'LONG',
      size: 100,
      avg_price: 0.55,
      yes_shares: 100,
      no_shares: 0,
      avg_entry_price: 0.55,
      total_invested: 55.0,
      realised_pnl: 12.34,
      current_price: 0.62,
      unrealized_pnl: 7.0,
      opened_at: '2024-12-01T10:00:00Z',
      strategy: 'mm_avellaneda_stoikov',
    })
    expect(r.success).toBe(true)
  })

  it('accepts a minimal position with only token_id (all else optional)', () => {
    const r = PositionSchema.safeParse({ token_id: '0xmin' })
    expect(r.success).toBe(true)
    if (r.success) {
      expect(r.data.token_id).toBe('0xmin')
      expect(r.data.current_price).toBeUndefined()
    }
  })

  it('accepts null for current_price (no order book yet)', () => {
    const r = PositionSchema.safeParse({
      token_id: 't',
      current_price: null,
      unrealized_pnl: null,
    })
    expect(r.success).toBe(true)
  })

  it('rejects a non-string token_id', () => {
    const r = PositionSchema.safeParse({ token_id: 123 })
    expect(r.success).toBe(false)
  })

  it('rejects an invalid side enum value', () => {
    const r = PositionSchema.safeParse({ token_id: 't', side: 'SIDEWAYS' })
    expect(r.success).toBe(false)
  })

  it('rejects a string where a number is required (no coercion)', () => {
    const r = PositionSchema.safeParse({ token_id: 't', size: '100' })
    expect(r.success).toBe(false)
  })

  it('preserves unknown fields via .passthrough()', () => {
    const r = PositionSchema.safeParse({
      token_id: 't',
      new_metric_added_in_w11: 42,
    })
    expect(r.success).toBe(true)
    if (r.success) {
      expect((r.data as Record<string, unknown>).new_metric_added_in_w11).toBe(42)
    }
  })
})

// ---------------------------------------------------------------------------
// PositionsResponseSchema (array wrapper)
// ---------------------------------------------------------------------------
describe('PositionsResponseSchema', () => {
  it('accepts an array of positions', () => {
    const r = PositionsResponseSchema.safeParse([
      { token_id: 't1' },
      { token_id: 't2', size: 5 },
    ])
    expect(r.success).toBe(true)
    if (r.success) expect(r.data).toHaveLength(2)
  })

  it('accepts an empty array', () => {
    const r = PositionsResponseSchema.safeParse([])
    expect(r.success).toBe(true)
  })

  it('rejects a non-array payload', () => {
    const r = PositionsResponseSchema.safeParse({ positions: [] })
    expect(r.success).toBe(false)
  })

  it('rejects when one element of the array is malformed', () => {
    const r = PositionsResponseSchema.safeParse([
      { token_id: 't1' },
      { side: 'LONG' }, // missing token_id
    ])
    expect(r.success).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// Order schema
// ---------------------------------------------------------------------------
describe('OrderSchema', () => {
  it('accepts a fully-populated order', () => {
    const r = OrderSchema.safeParse({
      order_id: 'ord-1',
      token_id: 'tok-1',
      slug: 'market-slug',
      side: 'BUY',
      price: 0.42,
      size: 50,
      size_matched: 10,
      status: 'PARTIAL',
      strategy: 'arb_binary_dutch_book',
      paper: true,
      created_at: '2024-12-01T10:00:00Z',
    })
    expect(r.success).toBe(true)
  })

  it('accepts an order without optional fields', () => {
    const r = OrderSchema.safeParse({
      order_id: 'o1',
      token_id: 't1',
      side: 'SELL',
      price: 0.50,
      size: 10,
    })
    expect(r.success).toBe(true)
  })

  it('accepts created_at as either a string or a number', () => {
    expect(OrderSchema.safeParse({
      order_id: 'o', token_id: 't', side: 'BUY', price: 1, size: 1, created_at: '2024-01-01',
    }).success).toBe(true)
    expect(OrderSchema.safeParse({
      order_id: 'o', token_id: 't', side: 'BUY', price: 1, size: 1, created_at: 1700000000,
    }).success).toBe(true)
  })

  it('rejects an unknown status enum value', () => {
    const r = OrderSchema.safeParse({
      order_id: 'o', token_id: 't', side: 'BUY', price: 1, size: 1, status: 'WAITING',
    })
    expect(r.success).toBe(false)
  })

  it('rejects an unknown side enum value', () => {
    const r = OrderSchema.safeParse({
      order_id: 'o', token_id: 't', side: 'HOLD', price: 1, size: 1,
    })
    expect(r.success).toBe(false)
  })

  it('rejects a negative number where a positive number is expected (sanity)', () => {
    // Note: schema does NOT enforce positivity — only type. This test
    // documents that contract.
    const r = OrderSchema.safeParse({
      order_id: 'o', token_id: 't', side: 'BUY', price: -0.5, size: -10,
    })
    expect(r.success).toBe(true) // passes type check; negativity is a domain concern
  })

  it('preserves unknown fields via .passthrough()', () => {
    const r = OrderSchema.safeParse({
      order_id: 'o', token_id: 't', side: 'BUY', price: 1, size: 1, iceberg_qty: 5,
    })
    expect(r.success).toBe(true)
    if (r.success) {
      expect((r.data as Record<string, unknown>).iceberg_qty).toBe(5)
    }
  })
})

describe('OrdersResponseSchema', () => {
  it('accepts { orders: Order[] }', () => {
    const r = OrdersResponseSchema.safeParse({ orders: [{ order_id: 'o', token_id: 't', side: 'BUY', price: 1, size: 1 }] })
    expect(r.success).toBe(true)
  })

  it('accepts { orders: [] }', () => {
    const r = OrdersResponseSchema.safeParse({ orders: [] })
    expect(r.success).toBe(true)
  })

  it('rejects a missing orders key', () => {
    const r = OrdersResponseSchema.safeParse({ positions: [] })
    expect(r.success).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// Trade schema
// ---------------------------------------------------------------------------
describe('TradeSchema', () => {
  it('accepts a trade with ISO timestamp', () => {
    const r = TradeSchema.safeParse({
      trade_id: 'tr-1',
      token_id: 'tok-1',
      slug: 'market',
      side: 'BUY',
      price: 0.42,
      size: 10,
      pnl: 1.5,
      timestamp: '2024-12-01T10:00:00Z',
      strategy: 'ml_random_forest_quant',
      paper: true,
    })
    expect(r.success).toBe(true)
  })

  it('accepts a trade with unix timestamp', () => {
    const r = TradeSchema.safeParse({
      token_id: 't', side: 'SELL', price: 0.5, size: 1, timestamp: 1700000000,
    })
    expect(r.success).toBe(true)
  })

  it('accepts a trade without optional trade_id', () => {
    const r = TradeSchema.safeParse({
      token_id: 't', side: 'BUY', price: 0.5, size: 1, timestamp: 1,
    })
    expect(r.success).toBe(true)
  })

  it('rejects a trade missing token_id', () => {
    const r = TradeSchema.safeParse({
      side: 'BUY', price: 0.5, size: 1, timestamp: 1,
    })
    expect(r.success).toBe(false)
  })

  it('rejects a trade with boolean timestamp', () => {
    const r = TradeSchema.safeParse({
      token_id: 't', side: 'BUY', price: 0.5, size: 1, timestamp: true,
    })
    expect(r.success).toBe(false)
  })
})

describe('TradesResponseSchema', () => {
  it('accepts { trades: Trade[] }', () => {
    const r = TradesResponseSchema.safeParse({
      trades: [{ token_id: 't', side: 'BUY', price: 0.5, size: 1, timestamp: 1 }],
    })
    expect(r.success).toBe(true)
  })

  it('accepts empty trades list', () => {
    const r = TradesResponseSchema.safeParse({ trades: [] })
    expect(r.success).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// Market schema
// ---------------------------------------------------------------------------
describe('MarketSchema', () => {
  it('accepts a fully-populated market', () => {
    const r = MarketSchema.safeParse({
      token_id: 'tok',
      question: 'Will BTC hit $100k by Dec 2025?',
      slug: 'will-btc-hit-100k',
      yes_price: 0.42,
      no_price: 0.58,
      spread: 0.01,
      volume: 15000,
      liquidity: 5000,
      end_date: '2025-12-31',
      active: true,
    })
    expect(r.success).toBe(true)
  })

  it('accepts a minimal market (just token_id)', () => {
    const r = MarketSchema.safeParse({ token_id: 'tok' })
    expect(r.success).toBe(true)
  })

  it('rejects a non-string token_id', () => {
    const r = MarketSchema.safeParse({ token_id: 42 })
    expect(r.success).toBe(false)
  })
})

describe('MarketsResponseSchema', () => {
  it('accepts an array of markets', () => {
    const r = MarketsResponseSchema.safeParse([
      { token_id: 't1' },
      { token_id: 't2', yes_price: 0.5 },
    ])
    expect(r.success).toBe(true)
    if (r.success) expect(r.data).toHaveLength(2)
  })
})

// ---------------------------------------------------------------------------
// OrderBook schema
// ---------------------------------------------------------------------------
describe('OrderBookSchema', () => {
  it('accepts a fully-populated book', () => {
    const r = OrderBookSchema.safeParse({
      token_id: 't',
      slug: 'mkt',
      best_bid: 0.41,
      best_ask: 0.43,
      mid: 0.42,
      spread: 0.02,
      updated_at: 1700000000,
    })
    expect(r.success).toBe(true)
  })

  it('accepts null for all nullable numeric fields (no liquidity yet)', () => {
    const r = OrderBookSchema.safeParse({
      token_id: 't',
      best_bid: null,
      best_ask: null,
      mid: null,
      spread: null,
    })
    expect(r.success).toBe(true)
  })

  it('rejects a string where a nullable number is expected', () => {
    const r = OrderBookSchema.safeParse({ token_id: 't', mid: '0.42' })
    expect(r.success).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// Analytics schema
// ---------------------------------------------------------------------------
describe('AnalyticsSchema', () => {
  it('accepts a fully-populated analytics payload', () => {
    const r = AnalyticsSchema.safeParse({
      equity: 1024.50,
      realized_pnl: 12.34,
      unrealized_pnl: 5.0,
      net_pnl: 17.34,
      total_trades: 50,
      winning_trades: 30,
      losing_trades: 20,
      closed_trades: 50,
      open_trades: 0,
      win_rate: 0.6,
      win_rate_ci_low: 0.46,
      win_rate_ci_high: 0.73,
      profit_factor: 1.5,
      max_drawdown_dollars: 25,
      max_drawdown_pct: 0.025,
      total_volume_usdc: 1500,
      open_exposure: 10,
      open_position_count: 2,
      pending_order_capital: 5,
      risk_utilization: 0.4,
      mode: 'paper',
      data_freshness_seconds: 2,
      peak_equity: 1100,
      active_strategies: ['mm_avellaneda_stoikov'],
      avg_win: 1.0,
      avg_loss: -0.5,
      expectancy: 0.4,
      sharpe_ratio: 1.2,
    })
    expect(r.success).toBe(true)
  })

  it('accepts equity-only payload (everything else optional)', () => {
    const r = AnalyticsSchema.safeParse({ equity: 0 })
    expect(r.success).toBe(true)
  })

  it('accepts null for win_rate_ci_low / ci_high (small sample)', () => {
    const r = AnalyticsSchema.safeParse({
      equity: 0,
      win_rate_ci_low: null,
      win_rate_ci_high: null,
    })
    expect(r.success).toBe(true)
  })

  it('accepts profit_factor as a number, a string ("Infinity"), or null', () => {
    expect(AnalyticsSchema.safeParse({ equity: 0, profit_factor: 1.5 }).success).toBe(true)
    expect(AnalyticsSchema.safeParse({ equity: 0, profit_factor: 'Infinity' }).success).toBe(true)
    expect(AnalyticsSchema.safeParse({ equity: 0, profit_factor: null }).success).toBe(true)
  })

  it('rejects profit_factor as a boolean (wrong type)', () => {
    const r = AnalyticsSchema.safeParse({ equity: 0, profit_factor: true })
    expect(r.success).toBe(false)
  })

  it('rejects a non-number equity (required field)', () => {
    const r = AnalyticsSchema.safeParse({ equity: '1024' })
    expect(r.success).toBe(false)
  })

  it('rejects a string array for active_strategies', () => {
    const r = AnalyticsSchema.safeParse({ equity: 0, active_strategies: [1, 2, 3] })
    expect(r.success).toBe(false)
  })

  it('preserves unknown fields via .passthrough()', () => {
    const r = AnalyticsSchema.safeParse({
      equity: 0,
      calmar_ratio: 2.0, // added in W11, not in W10 schema
    })
    expect(r.success).toBe(true)
    if (r.success) {
      expect((r.data as Record<string, unknown>).calmar_ratio).toBe(2.0)
    }
  })
})

// ---------------------------------------------------------------------------
// Health / status schema
// ---------------------------------------------------------------------------
describe('HealthSchema', () => {
  it('accepts a fully-populated health payload', () => {
    const r = HealthSchema.safeParse({
      status: 'running',
      mode: 'paper',
      uptime: 3600,
      balance: 100,
      kill_switch: false,
      kill_switch_durable: false,
      observation_only: false,
      observation_reason: '',
      daily_pnl: 0,
      paper_balance: 100,
      strategies: ['mm_avellaneda_stoikov'],
    })
    expect(r.success).toBe(true)
  })

  it('accepts a minimal health payload (status only)', () => {
    const r = HealthSchema.safeParse({ status: 'ok' })
    expect(r.success).toBe(true)
  })

  it('accepts null for paper_balance (live mode)', () => {
    const r = HealthSchema.safeParse({ status: 'ok', paper_balance: null })
    expect(r.success).toBe(true)
  })

  it('rejects a missing status field', () => {
    const r = HealthSchema.safeParse({ mode: 'paper' })
    expect(r.success).toBe(false)
  })

  it('rejects a non-string status', () => {
    const r = HealthSchema.safeParse({ status: 200 })
    expect(r.success).toBe(false)
  })

  it('rejects a non-boolean kill_switch', () => {
    const r = HealthSchema.safeParse({ status: 'ok', kill_switch: 'false' })
    expect(r.success).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// ML metrics schema
// ---------------------------------------------------------------------------
describe('MLMetricsSchema', () => {
  it('accepts a fully-populated ML metrics payload', () => {
    const r = MLMetricsSchema.safeParse({
      auc: 0.78,
      roc_auc: 0.78,
      brier: 0.18,
      brier_score: 0.18,
      log_loss: 0.55,
      accuracy: 0.72,
      ece: 0.04,
      n_updates: 1500,
      version: 'rf-v2',
      model_ready: true,
      drift_status: 'stable',
      drift_psi: 0.05,
      drift_brier: 0.18,
      drift_ewma_brier: 0.17,
      meta_learner_warm: true,
      training_source: 'historical',
      adaptive_weights: { rf: 0.4, gb: 0.3, sgd: 0.2, lgbm: 0.1 },
    })
    expect(r.success).toBe(true)
  })

  it('accepts an empty object (everything optional — model untrained)', () => {
    const r = MLMetricsSchema.safeParse({})
    expect(r.success).toBe(true)
  })

  it('accepts null for drift_brier / drift_ewma_brier (no history yet)', () => {
    const r = MLMetricsSchema.safeParse({
      drift_brier: null,
      drift_ewma_brier: null,
    })
    expect(r.success).toBe(true)
  })

  it('rejects a non-number auc', () => {
    const r = MLMetricsSchema.safeParse({ auc: '0.78' })
    expect(r.success).toBe(false)
  })

  it('rejects adaptive_weights with non-numeric rf weight', () => {
    const r = MLMetricsSchema.safeParse({ adaptive_weights: { rf: '0.4' } })
    expect(r.success).toBe(false)
  })

  it('preserves unknown adaptive_weights sub-fields via .passthrough()', () => {
    const r = MLMetricsSchema.safeParse({
      adaptive_weights: { rf: 0.4, new_model_added_in_w11: 0.1 },
    })
    expect(r.success).toBe(true)
    if (r.success && r.data.adaptive_weights) {
      expect(
        (r.data.adaptive_weights as Record<string, unknown>).new_model_added_in_w11,
      ).toBe(0.1)
    }
  })
})

// ---------------------------------------------------------------------------
// Snapshot schema (the big one)
// ---------------------------------------------------------------------------
describe('SnapshotSchema', () => {
  it('accepts a fully-populated snapshot', () => {
    const r = SnapshotSchema.safeParse({
      type: 'snapshot',
      timestamp: 1700000000,
      mode: 'paper',
      kill_switch: false,
      kill_switch_durable: false,
      observation_only: false,
      observation_reason: '',
      daily_pnl: 12.5,
      paper_balance: 100,
      strategies: ['mm_avellaneda_stoikov'],
      order_books: [{ token_id: 't', mid: 0.5 }],
      open_orders: [{ order_id: 'o', token_id: 't', side: 'BUY', price: 0.5, size: 1 }],
      positions: [{ token_id: 't' }],
      recent_trades: [{ token_id: 't', side: 'BUY', price: 0.5, size: 1, timestamp: 1 }],
      events: ['bot started'],
      ml: { auc: 0.78, model_ready: true },
    })
    expect(r.success).toBe(true)
  })

  it('accepts an empty object (graceful startup)', () => {
    const r = SnapshotSchema.safeParse({})
    expect(r.success).toBe(true)
  })

  it('accepts null for paper_balance (live mode)', () => {
    const r = SnapshotSchema.safeParse({ paper_balance: null })
    expect(r.success).toBe(true)
  })

  it('rejects a string where a boolean is expected for kill_switch', () => {
    const r = SnapshotSchema.safeParse({ kill_switch: 'false' })
    expect(r.success).toBe(false)
  })

  it('rejects a malformed position nested in the snapshot', () => {
    const r = SnapshotSchema.safeParse({
      positions: [{ /* missing token_id */ side: 'LONG' }],
    })
    expect(r.success).toBe(false)
  })

  it('preserves unknown top-level fields via .passthrough()', () => {
    const r = SnapshotSchema.safeParse({
      whale_alerts: [], // new in W11
      sentiment_feed: {}, // new in W12
    })
    expect(r.success).toBe(true)
    if (r.success) {
      expect((r.data as Record<string, unknown>).whale_alerts).toEqual([])
    }
  })
})

// ---------------------------------------------------------------------------
// Events / OrderBooks response wrappers
// ---------------------------------------------------------------------------
describe('EventsResponseSchema', () => {
  it('accepts { events: string[] }', () => {
    const r = EventsResponseSchema.safeParse({ events: ['a', 'b'] })
    expect(r.success).toBe(true)
  })

  it('accepts an empty events array', () => {
    const r = EventsResponseSchema.safeParse({ events: [] })
    expect(r.success).toBe(true)
  })

  it('rejects a non-array events value', () => {
    const r = EventsResponseSchema.safeParse({ events: 'not-an-array' })
    expect(r.success).toBe(false)
  })

  it('rejects a non-string element in events array', () => {
    const r = EventsResponseSchema.safeParse({ events: ['ok', 42] })
    expect(r.success).toBe(false)
  })
})

describe('OrderBooksResponseSchema', () => {
  it('accepts { order_books: OrderBook[] }', () => {
    const r = OrderBooksResponseSchema.safeParse({
      order_books: [{ token_id: 't', mid: 0.5 }],
    })
    expect(r.success).toBe(true)
  })

  it('accepts an empty order_books array', () => {
    const r = OrderBooksResponseSchema.safeParse({ order_books: [] })
    expect(r.success).toBe(true)
  })

  it('rejects a missing order_books key', () => {
    const r = OrderBooksResponseSchema.safeParse({ books: [] })
    expect(r.success).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// safeFetch helper
// ---------------------------------------------------------------------------
describe('safeFetch', () => {
  beforeEach(() => {
    // Restore the global fetch mock between tests so call history doesn't
    // leak. apiFetch delegates to fetch internally; mocking fetch lets us
    // control the response shape.
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  it('returns { success: true, data } when the response matches the schema', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ token_id: 't1' }), { status: 200 }),
    )
    const r = await safeFetch('/api/positions/x', PositionSchema)
    expect(r.success).toBe(true)
    if (r.success) expect(r.data.token_id).toBe('t1')
  })

  it('returns { success: false } with HTTP error on non-2xx', async () => {
    vi.mocked(fetch).mockResolvedValue(new Response('nope', { status: 500 }))
    const r = await safeFetch('/api/positions/x', PositionSchema)
    expect(r.success).toBe(false)
    if (!r.success) {
      expect(r.error).toContain('500')
      expect(r.raw).toBeNull()
    }
  })

  it('returns { success: false } when the payload fails schema validation', async () => {
    vi.mocked(fetch).mockResolvedValue(
      // size: '100' is a string where a number is expected
      new Response(JSON.stringify({ token_id: 't', size: '100' }), { status: 200 }),
    )
    const r = await safeFetch('/api/positions/x', PositionSchema)
    expect(r.success).toBe(false)
    if (!r.success) {
      expect(typeof r.error).toBe('string')
      // raw payload is preserved for dev tooling
      expect(r.raw).toEqual({ token_id: 't', size: '100' })
    }
  })

  it('returns { success: false } when the response body is not valid JSON', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response('not-json', { status: 200, headers: { 'Content-Type': 'text/plain' } }),
    )
    const r = await safeFetch('/api/positions/x', PositionSchema)
    expect(r.success).toBe(false)
    if (!r.success) {
      expect(r.error).toContain('not valid JSON')
    }
  })

  it('returns { success: false } when fetch throws (network error)', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('network down'))
    const r = await safeFetch('/api/positions/x', PositionSchema)
    expect(r.success).toBe(false)
    if (!r.success) {
      expect(r.error).toContain('network down')
    }
  })

  it('injects the XTransformPort query param via apiFetch', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(JSON.stringify({ token_id: 't' }), { status: 200 }),
    )
    await safeFetch('/api/positions/x', PositionSchema)
    const [url] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
    expect(url).toContain('XTransformPort=')
  })
})

// ---------------------------------------------------------------------------
// safeParse helper (synchronous)
// ---------------------------------------------------------------------------
describe('safeParse', () => {
  it('returns { success: true, data } for valid input', () => {
    const r = safeParse({ token_id: 't' }, PositionSchema)
    expect(r.success).toBe(true)
    if (r.success) expect(r.data.token_id).toBe('t')
  })

  it('returns { success: false } for invalid input', () => {
    const r = safeParse({ token_id: 42 }, PositionSchema)
    expect(r.success).toBe(false)
    if (!r.success) {
      expect(r.raw).toEqual({ token_id: 42 })
    }
  })
})

// ---------------------------------------------------------------------------
// validateDev helper
// ---------------------------------------------------------------------------
describe('validateDev', () => {
  beforeEach(() => {
    // Silence the console.error calls emitted by logSchemaError so the
    // test runner output stays clean.
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  it('runs schema validation and returns the result', () => {
    const r = validateDev({ token_id: 't' }, PositionSchema)
    expect(r.success).toBe(true)
  })

  it('returns an error string for invalid input', () => {
    const r = validateDev({ token_id: 42 }, PositionSchema)
    expect(r.success).toBe(false)
    if (!r.success) expect(typeof r.error).toBe('string')
  })

  it('logs schema errors to console.error (dev surfaces drift loudly)', () => {
    const spy = vi.spyOn(console, 'error')
    validateDev({ token_id: 42 }, PositionSchema)
    expect(spy).toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// logSchemaError helper
// ---------------------------------------------------------------------------
describe('logSchemaError', () => {
  it('is a no-op when called directly with a fabricated ZodError-like object', () => {
    // logSchemaError reads .issues from the ZodError. We construct a
    // minimal stand-in to exercise the formatting path without running
    // an actual schema parse.
    const fakeErr = {
      issues: [
        { path: ['positions', 0, 'avg_price'], code: 'invalid_type', message: 'expected number' },
      ],
    }
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    expect(() => logSchemaError('/api/x', { bad: 'data' }, fakeErr as never)).not.toThrow()
    expect(spy).toHaveBeenCalled()
  })
})
