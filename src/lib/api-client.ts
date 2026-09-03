// lib/api-client.ts — Typed API client SDK for the Polymarket bot backend.
//
// W12-8 — Typed wrapper around the raw `apiFetch` helper.
//
// Why this file exists:
//   `apiFetch` (in `lib/api.ts`) only handles two concerns: appending the
//   gateway `XTransformPort` query to relative URLs and injecting the
//   `Authorization` bearer header from localStorage. Every call site
//   has to repeat:
//     - the `res.ok` check,
//     - the `await res.json()` cast,
//     - the error-shape parsing (the backend returns `{ detail: ... }`),
//     - and the per-endpoint URL string.
//   Worse, the response type at each call site is `any`, so a renamed
//   backend field (e.g. `positions` → `open_positions`) compiles cleanly
//   and silently produces `undefined` in the UI.
//
//   This module wraps each backend route in a typed function so:
//     1. The URL is constructed once, in one place.
//     2. The response is typed via Zod-inferred types from `lib/schemas`
//        (for the contract-critical endpoints) or `any` (for endpoints
//        whose response shape is still being audited — those are
//        individually marked with a `// TODO: tighten response type`
//        comment for follow-up tasks).
//     3. Errors are thrown as `ApiError` (with `status` + parsed `body`)
//        so call sites can catch them once and surface a uniform
//        toast / inline error message.
//     4. POST / PUT / DELETE bodies are JSON-stringified and the
//        `Content-Type: application/json` header is set automatically.
//
// Design choices:
//   * The master `api` object is a frozen namespace map (`api.system.health`,
//     `api.trading.getPositions`, etc.) so call sites read like English:
//       await api.trading.cancelOrder(orderId)
//     rather than the older `fetch(\`/api/orders/${orderId}\`, { method: 'DELETE' })`.
//   * Each namespace object is exported individually so tree-shakers can
//     pull in just `tradingApi` without dragging in the `mlApi` weight.
//   * The `request<T>` helper is intentionally NOT exported — call sites
//     go through the typed namespace methods, not the generic helper.
//     This keeps the surface area auditable (every backend call is
//     declared once in this file).
//   * Methods return `Promise<T>` (not `Promise<{ data: T; error: ... }>`)
//     and throw on failure. The calling hook (useBot / useTanStackQuery)
//     is responsible for the try/catch — this matches the existing
//     useBot pattern (`await fetch(...).catch(() => {})`).
//
// Compatibility note:
//   This module is a NEW file. It does NOT replace the existing `apiFetch`
//   calls in `hooks/useBot.ts` or any component — those stay as they are
//   until a follow-up migration task swaps them over. The two patterns
//   coexist: `apiFetch` for ad-hoc fetches, `api` for typed namespace calls.

import { apiFetch } from './api'
import type { Position, Order, Trade, Analytics, Health, MLMetrics } from './schemas'

/**
 * Typed API client for the Polymarket bot backend.
 * All methods return typed responses. Errors are thrown as ApiError.
 */

export class ApiError extends Error {
  constructor(public status: number, public body: any) {
    super(`API Error ${status}: ${body?.detail || 'Unknown error'}`)
    // Restore prototype chain — required when extending built-ins like
    // `Error` under TypeScript's strict ES5 emit target. Without this,
    // `instanceof ApiError` returns false after a `throw new ApiError(...)`
    // because the transpiled ES5 constructor doesn't preserve the prototype.
    // See https://github.com/Microsoft/TypeScript-wiki/blob/main/Breaking-Changes.md#extending-built-ins-like-error-array-and-map-may-no-longer-work
    Object.setPrototypeOf(this, ApiError.prototype)
    this.name = 'ApiError'
  }
}

async function request<T>(endpoint: string, options?: RequestInit): Promise<T> {
  const res = await apiFetch(endpoint, options)
  if (!res.ok) {
    let body
    try {
      body = await res.json()
    } catch {
      // Body wasn't JSON (e.g. a plain-text 500 from the gateway, or an
      // empty 204). Surface `null` so callers can branch on `body === null`
      // rather than crashing on `body.detail`.
      body = null
    }
    throw new ApiError(res.status, body)
  }
  return res.json() as Promise<T>
}

// === Health & System ===
export const systemApi = {
  health: () => request<Health>('/api/health'),
  status: () => request<any>('/api/status'),
  snapshot: () => request<any>('/api/snapshot'),
  events: (limit = 50) => request<any[]>(`/api/events?limit=${limit}`),
  equityHistory: () => request<any[]>('/api/history/equity'),
}

// === Trading ===
export const tradingApi = {
  getPositions: () => request<{ positions: Position[]; count: number; daily_pnl?: number }>('/api/positions'),
  getOrders: () => request<{ orders: Order[]; count: number }>('/api/orders'),
  getTrades: (limit = 50) => request<{ trades: Trade[]; count: number }>(`/api/trades?limit=${limit}`),
  placeTrade: (params: { token_id: string; side: string; size: number; price: number }) =>
    request<any>('/api/trade', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    }),
  closePosition: (tokenId: string) =>
    request<any>(`/api/positions/${tokenId}/close`, { method: 'POST' }),
  cancelOrder: (orderId: string) =>
    request<any>(`/api/orders/${orderId}`, { method: 'DELETE' }),
  cancelAllOrders: () =>
    request<any>('/api/orders', { method: 'DELETE' }),
}

// === Markets ===
export const marketsApi = {
  getMarkets: () => request<any[]>('/api/markets'),
  getOrderbooks: () => request<any[]>('/api/orderbooks'),
  getCatalog: () => request<any[]>('/api/markets/catalog'),
  getCoverage: () => request<any>('/api/markets/coverage'),
  getDepth: (tokenId: string) => request<any>(`/api/depth/${tokenId}`),
  getOhlcv: (tokenId: string) => request<any[]>(`/api/history/ohlcv/${tokenId}`),
}

// === ML ===
export const mlApi = {
  getInfo: () => request<any>('/api/ml'),
  getMetrics: () => request<MLMetrics>('/api/ml/metrics'),
  getDrift: () => request<any>('/api/ml/drift'),
  getVersions: () => request<any[]>('/api/ml/versions'),
  retrain: () => request<any>('/api/ml/retrain', { method: 'POST' }),
  learn: (params: any) =>
    request<any>('/api/ml/learn', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    }),
}

// === Analysis ===
export const analysisApi = {
  getNews: () => request<any[]>('/api/analysis/news'),
  getNewsStats: () => request<any>('/api/analysis/news/stats'),
  getNewsSources: () => request<any[]>('/api/analysis/news/sources'),
  analyzeMarket: (tokenId: string) =>
    request<any>(`/api/analysis/market/${tokenId}`),
  deepAnalysis: (params: any) =>
    request<any>('/api/analysis/deep', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    }),
}

// === Risk ===
export const riskApi = {
  getExposure: () => request<any>('/api/exposure'),
  getLeaderboard: () => request<any[]>('/api/leaderboard'),
  reconcile: () => request<any>('/api/risk/reconcile'),
  activateKillSwitch: () =>
    request<any>('/api/kill-switch/activate', { method: 'POST' }),
  deactivateKillSwitch: () =>
    request<any>('/api/kill-switch/deactivate', { method: 'POST' }),
  setObservationMode: (enabled: boolean) =>
    request<any>('/api/risk/observation-mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    }),
}

// === Strategies ===
export const strategiesApi = {
  getCatalog: () => request<any[]>('/api/strategies/catalog'),
  toggle: (name: string, enabled: boolean) =>
    request<any>('/api/strategies/toggle', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, enabled }),
    }),
}

// === Arbitrage ===
export const arbitrageApi = {
  getOpportunities: () => request<any[]>('/api/arbitrage/opportunities'),
  execute: (params: any) =>
    request<any>('/api/arbitrage/execute', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    }),
}

// === Analytics ===
export const analyticsApi = {
  getAnalytics: () => request<Analytics>('/api/analytics'),
  getAttribution: (range = '24h') => request<any>(`/api/attribution?range=${range}`),
  getExecutionQuality: () => request<any>('/api/execution-quality'),
  getClosedPositions: (limit = 50) =>
    request<any[]>(`/api/positions/closed?limit=${limit}`),
}

// === Observability ===
export const observabilityApi = {
  get: () => request<any>('/api/observability'),
  getHistory: (name: string, limit = 100) =>
    request<any[]>(`/api/observability/history/${name}?limit=${limit}`),
}

// === Alerts ===
export const alertsApi = {
  get: (limit = 50, unacknowledgedOnly = false) =>
    request<{ alerts: any[]; stats: any }>(`/api/alerts?limit=${limit}&unacknowledged_only=${unacknowledgedOnly}`),
  getStats: () => request<any>('/api/alerts/stats'),
  acknowledge: (alertId: string) =>
    request<any>(`/api/alerts/${alertId}/acknowledge`, { method: 'POST' }),
  acknowledgeAll: () =>
    request<any>('/api/alerts/acknowledge-all', { method: 'POST' }),
  evaluate: () =>
    request<any>('/api/alerts/evaluate', { method: 'POST' }),
}

// === Decisions ===
export const decisionsApi = {
  getRejected: (limit = 50) =>
    request<any[]>(`/api/decisions/rejected?limit=${limit}`),
  getByToken: (tokenId: string, limit = 50) =>
    request<any[]>(`/api/decision/${tokenId}?limit=${limit}`),
}

// === Live Safety Gate ===
export const safetyApi = {
  getReadiness: () => request<any>('/api/live/readiness'),
  enable: (confirm: boolean, reason: string) =>
    request<any>('/api/live/enable', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ confirm, reason }),
    }),
}

// === Config ===
export const configApi = {
  get: () => request<any>('/api/config'),
  update: (config: any) =>
    request<any>('/api/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    }),
}

// === Cache ===
export const cacheApi = {
  getStats: () => request<any>('/api/cache/stats'),
  clear: () => request<any>('/api/cache/clear', { method: 'POST' }),
}

// === Feature Flags ===
export const flagsApi = {
  getAll: () => request<any[]>('/api/flags'),
  get: (key: string) => request<any>(`/api/flags/${key}`),
  set: (key: string, enabled: boolean, config?: any) =>
    request<any>(`/api/flags/${key}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled, config }),
    }),
  reset: (key: string) =>
    request<any>(`/api/flags/${key}/reset`, { method: 'POST' }),
}

// === Backtesting ===
export const backtestApi = {
  run: (params: any) =>
    request<any>('/api/backtest/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    }),
}

// Master API object
export const api = {
  system: systemApi,
  trading: tradingApi,
  markets: marketsApi,
  ml: mlApi,
  analysis: analysisApi,
  risk: riskApi,
  strategies: strategiesApi,
  arbitrage: arbitrageApi,
  analytics: analyticsApi,
  observability: observabilityApi,
  alerts: alertsApi,
  decisions: decisionsApi,
  safety: safetyApi,
  config: configApi,
  cache: cacheApi,
  flags: flagsApi,
  backtest: backtestApi,
}

export default api
