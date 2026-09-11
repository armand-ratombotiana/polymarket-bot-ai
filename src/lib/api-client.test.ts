// lib/api-client.test.ts — Unit tests for the typed API client SDK.
//
// W12-8 — Typed API client tests.
//
// Test philosophy:
//   These tests target the SDK wrapper, NOT the backend contract. They
//   verify that:
//     1. Every namespace is exposed on the master `api` object and each
//        namespace exposes the documented methods (contract surface).
//     2. Each GET method calls the correct URL with the correct HTTP
//        method (no accidental POST-when-you-meant-GET bugs).
//     3. POST / PUT / DELETE methods send the correct body + headers.
//     4. `ApiError` is thrown on non-OK responses, with `status` and
//        parsed `body` exposed for callers.
//     5. Non-JSON error bodies (gateway 502 with HTML, empty 204) don't
//        crash the parser.
//
//   The actual wire-shape of the responses (Position fields, Order
//   status enum, etc.) is covered by `schemas.test.ts`. Here we only
//   verify the SDK plumbing — the URL, the HTTP method, the JSON body
//   payload, the auth header passthrough (delegated to `apiFetch`).
//
// Mock strategy:
//   `apiFetch` (the underlying transport) calls `fetch(withGatewayPort(input))`.
//   `withGatewayPort` appends `?XTransformPort=8080` (or `&XTransformPort=8080`
//   if the URL already has a query string). We mock `global.fetch` to a
//   `vi.fn()` returning a synthetic `Response` and inspect the recorded
//   call args via `vi.mocked(fetch).mock.calls[i]`. The URL is asserted
//   with `toContain(...)` so the test doesn't depend on the gateway-port
//   injection detail (which is covered by `api.test.ts`).

import { describe, it, expect, beforeEach, vi } from 'vitest'
import {
  api,
  ApiError,
  systemApi,
  tradingApi,
  marketsApi,
  mlApi,
  analysisApi,
  riskApi,
  strategiesApi,
  arbitrageApi,
  analyticsApi,
  observabilityApi,
  alertsApi,
  decisionsApi,
  safetyApi,
  configApi,
  cacheApi,
  flagsApi,
  backtestApi,
} from '@/lib/api-client'

// Helper — build a JSON Response the way the SDK expects to receive it.
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('api-client', () => {
  beforeEach(() => {
    // Reset fetch to a fresh vi.fn on every test so call history is isolated.
    // apiFetch's `withGatewayPort` is applied AFTER this mock, so the URL
    // passed to the mock will already carry `XTransformPort=8080`.
    global.fetch = vi.fn() as unknown as typeof fetch
    localStorage.clear()
  })

  // -------------------------------------------------------------------------
  // Namespace structure — verify every namespace + method exists so a
  // future refactor doesn't silently drop a method (and produce a runtime
  // `TypeError: api.foo.bar is not a function` in prod).
  // -------------------------------------------------------------------------
  describe('namespace structure', () => {
    it('exposes all 17 namespaces on the master api object', () => {
      const namespaces = Object.keys(api).sort()
      expect(namespaces).toEqual([
        'alerts', 'analysis', 'analytics', 'arbitrage', 'backtest',
        'cache', 'config', 'decisions', 'flags', 'markets',
        'ml', 'observability', 'risk', 'safety', 'strategies',
        'system', 'trading',
      ])
    })

    it('systemApi has 5 methods', () => {
      const methods = Object.keys(systemApi).sort()
      expect(methods).toEqual(['equityHistory', 'events', 'health', 'snapshot', 'status'])
    })

    it('tradingApi has 7 methods', () => {
      const methods = Object.keys(tradingApi).sort()
      expect(methods).toEqual([
        'cancelAllOrders', 'cancelOrder', 'closePosition',
        'getOrders', 'getPositions', 'getTrades', 'placeTrade',
      ])
    })

    it('marketsApi has 6 methods', () => {
      const methods = Object.keys(marketsApi).sort()
      expect(methods).toEqual([
        'getCatalog', 'getCoverage', 'getDepth', 'getMarkets',
        'getOhlcv', 'getOrderbooks',
      ])
    })

    it('mlApi has 6 methods', () => {
      const methods = Object.keys(mlApi).sort()
      expect(methods).toEqual([
        'getDrift', 'getInfo', 'getMetrics', 'getVersions',
        'learn', 'retrain',
      ])
    })

    it('analysisApi has 5 methods', () => {
      const methods = Object.keys(analysisApi).sort()
      expect(methods).toEqual([
        'analyzeMarket', 'deepAnalysis', 'getNews',
        'getNewsSources', 'getNewsStats',
      ])
    })

    it('riskApi has 6 methods', () => {
      const methods = Object.keys(riskApi).sort()
      expect(methods).toEqual([
        'activateKillSwitch', 'deactivateKillSwitch', 'getExposure',
        'getLeaderboard', 'reconcile', 'setObservationMode',
      ])
    })

    it('strategiesApi has 2 methods', () => {
      const methods = Object.keys(strategiesApi).sort()
      expect(methods).toEqual(['getCatalog', 'toggle'])
    })

    it('arbitrageApi has 2 methods', () => {
      const methods = Object.keys(arbitrageApi).sort()
      expect(methods).toEqual(['execute', 'getOpportunities'])
    })

    it('analyticsApi has 4 methods', () => {
      const methods = Object.keys(analyticsApi).sort()
      expect(methods).toEqual([
        'getAnalytics', 'getAttribution', 'getClosedPositions',
        'getExecutionQuality',
      ])
    })

    it('observabilityApi has 2 methods', () => {
      const methods = Object.keys(observabilityApi).sort()
      expect(methods).toEqual(['get', 'getHistory'])
    })

    it('alertsApi has 5 methods', () => {
      const methods = Object.keys(alertsApi).sort()
      expect(methods).toEqual([
        'acknowledge', 'acknowledgeAll', 'evaluate', 'get', 'getStats',
      ])
    })

    it('decisionsApi has 2 methods', () => {
      const methods = Object.keys(decisionsApi).sort()
      expect(methods).toEqual(['getByToken', 'getRejected'])
    })

    it('safetyApi has 2 methods', () => {
      const methods = Object.keys(safetyApi).sort()
      expect(methods).toEqual(['enable', 'getReadiness'])
    })

    it('configApi has 2 methods', () => {
      const methods = Object.keys(configApi).sort()
      expect(methods).toEqual(['get', 'update'])
    })

    it('cacheApi has 2 methods', () => {
      const methods = Object.keys(cacheApi).sort()
      expect(methods).toEqual(['clear', 'getStats'])
    })

    it('flagsApi has 4 methods', () => {
      const methods = Object.keys(flagsApi).sort()
      expect(methods).toEqual(['get', 'getAll', 'reset', 'set'])
    })

    it('backtestApi has 1 method', () => {
      const methods = Object.keys(backtestApi).sort()
      expect(methods).toEqual(['run'])
    })

    it('every namespace method is a function', () => {
      // Walk every namespace + every method and verify it's callable.
      // This catches accidental property shadowing (e.g. a method named
      // `get` overwriting Object.prototype.get).
      for (const nsName of Object.keys(api)) {
        const ns = (api as Record<string, Record<string, unknown>>)[nsName]
        for (const methodName of Object.keys(ns)) {
          expect(typeof ns[methodName]).toBe('function')
        }
      }
    })
  })

  // -------------------------------------------------------------------------
  // GET URL coverage — pick a representative endpoint from each namespace
  // and verify the URL is constructed correctly.
  // -------------------------------------------------------------------------
  describe('GET URL coverage', () => {
    it('systemApi.health calls GET /api/health', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ status: 'ok' }))
      const result = await systemApi.health()
      expect(result.status).toBe('ok')
      const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/health')
      expect(init?.method ?? 'GET').toBe('GET')
    })

    it('systemApi.events forwards the limit query', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse([]))
      await systemApi.events(25)
      const [url] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/events')
      expect(url).toContain('limit=25')
    })

    it('tradingApi.getPositions calls GET /api/positions', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ positions: [], count: 0 }))
      await tradingApi.getPositions()
      const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/positions')
      expect(init?.method ?? 'GET').toBe('GET')
    })

    it('marketsApi.getDepth interpolates the token_id', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
      await marketsApi.getDepth('0xdeadbeef')
      const [url] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/depth/0xdeadbeef')
    })

    it('analyticsApi.getAttribution forwards the range query', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
      await analyticsApi.getAttribution('7d')
      const [url] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/attribution')
      expect(url).toContain('range=7d')
    })

    it('analyticsApi.getAttribution defaults range to 24h', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
      await analyticsApi.getAttribution()
      const [url] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('range=24h')
    })

    it('observabilityApi.getHistory interpolates name + limit', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse([]))
      await observabilityApi.getHistory('latency', 200)
      const [url] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/observability/history/latency')
      expect(url).toContain('limit=200')
    })

    it('alertsApi.get forwards limit + unacknowledged_only flags', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ alerts: [], stats: {} }))
      await alertsApi.get(100, true)
      const [url] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/alerts')
      expect(url).toContain('limit=100')
      expect(url).toContain('unacknowledged_only=true')
    })

    it('decisionsApi.getByToken interpolates token_id + limit', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse([]))
      await decisionsApi.getByToken('tok-abc', 10)
      const [url] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/decision/tok-abc')
      expect(url).toContain('limit=10')
    })
  })

  // -------------------------------------------------------------------------
  // POST / PUT / DELETE coverage — verify method, Content-Type header,
  // and JSON body payload.
  // -------------------------------------------------------------------------
  describe('POST/PUT/DELETE methods', () => {
    it('tradingApi.placeTrade sends POST with JSON body', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ ok: true }))
      const payload = { token_id: '0xabc', side: 'BUY', size: 100, price: 0.55 }
      await tradingApi.placeTrade(payload)
      const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/trade')
      expect(init?.method).toBe('POST')
      const headers = new Headers(init?.headers)
      expect(headers.get('Content-Type')).toBe('application/json')
      expect(JSON.parse(init?.body as string)).toEqual(payload)
    })

    it('tradingApi.closePosition sends POST without body', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ ok: true }))
      await tradingApi.closePosition('0xpos1')
      const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/positions/0xpos1/close')
      expect(init?.method).toBe('POST')
    })

    it('tradingApi.cancelOrder sends DELETE', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ ok: true }))
      await tradingApi.cancelOrder('order-123')
      const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/orders/order-123')
      expect(init?.method).toBe('DELETE')
    })

    it('tradingApi.cancelAllOrders sends DELETE /api/orders', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ cancelled: 5 }))
      await tradingApi.cancelAllOrders()
      const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/orders')
      expect(init?.method).toBe('DELETE')
    })

    it('mlApi.retrain sends POST', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ started: true }))
      await mlApi.retrain()
      const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/ml/retrain')
      expect(init?.method).toBe('POST')
    })

    it('mlApi.learn sends POST with JSON body', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ learned: true }))
      const payload = { feature: 'x', label: 1 }
      await mlApi.learn(payload)
      const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/ml/learn')
      expect(init?.method).toBe('POST')
      const headers = new Headers(init?.headers)
      expect(headers.get('Content-Type')).toBe('application/json')
      expect(JSON.parse(init?.body as string)).toEqual(payload)
    })

    it('analysisApi.deepAnalysis sends POST with JSON body', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ score: 0.7 }))
      const payload = { token_id: '0xabc', horizon: '24h' }
      await analysisApi.deepAnalysis(payload)
      const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/analysis/deep')
      expect(init?.method).toBe('POST')
      expect(JSON.parse(init?.body as string)).toEqual(payload)
    })

    it('riskApi.activateKillSwitch sends POST', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ activated: true }))
      await riskApi.activateKillSwitch()
      const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/kill-switch/activate')
      expect(init?.method).toBe('POST')
    })

    it('riskApi.setObservationMode sends POST with {enabled} body', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ ok: true }))
      await riskApi.setObservationMode(true)
      const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/risk/observation-mode')
      expect(init?.method).toBe('POST')
      expect(JSON.parse(init?.body as string)).toEqual({ enabled: true })
    })

    it('strategiesApi.toggle sends POST with {name, enabled} body', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ ok: true }))
      await strategiesApi.toggle('market_maker', false)
      const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/strategies/toggle')
      expect(init?.method).toBe('POST')
      expect(JSON.parse(init?.body as string)).toEqual({ name: 'market_maker', enabled: false })
    })

    it('arbitrageApi.execute sends POST with JSON body', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ executed: true }))
      const payload = { opp_id: 'abc', size: 50 }
      await arbitrageApi.execute(payload)
      const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/arbitrage/execute')
      expect(init?.method).toBe('POST')
      expect(JSON.parse(init?.body as string)).toEqual(payload)
    })

    it('alertsApi.acknowledge sends POST with alertId in path', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ acknowledged: true }))
      await alertsApi.acknowledge('alert-9')
      const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/alerts/alert-9/acknowledge')
      expect(init?.method).toBe('POST')
    })

    it('safetyApi.enable sends POST with {confirm, reason} body', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ live: true }))
      await safetyApi.enable(true, 'all checks passed')
      const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/live/enable')
      expect(init?.method).toBe('POST')
      expect(JSON.parse(init?.body as string)).toEqual({
        confirm: true,
        reason: 'all checks passed',
      })
    })

    it('configApi.update sends PUT with JSON body', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ saved: true }))
      const payload = { kill_switch: false, observation_only: true }
      await configApi.update(payload)
      const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/config')
      expect(init?.method).toBe('PUT')
      expect(JSON.parse(init?.body as string)).toEqual(payload)
    })

    it('cacheApi.clear sends POST', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ cleared: true }))
      await cacheApi.clear()
      const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/cache/clear')
      expect(init?.method).toBe('POST')
    })

    it('flagsApi.set sends POST with {enabled, config?} body', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ ok: true }))
      await flagsApi.set('feature_x', true, { threshold: 0.5 })
      const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/flags/feature_x')
      expect(init?.method).toBe('POST')
      expect(JSON.parse(init?.body as string)).toEqual({
        enabled: true,
        config: { threshold: 0.5 },
      })
    })

    it('flagsApi.set omits config when not provided', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ ok: true }))
      await flagsApi.set('feature_y', false)
      const [, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(JSON.parse(init?.body as string)).toEqual({ enabled: false, config: undefined })
    })

    it('flagsApi.reset sends POST', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ reset: true }))
      await flagsApi.reset('feature_x')
      const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/flags/feature_x/reset')
      expect(init?.method).toBe('POST')
    })

    it('backtestApi.run sends POST with JSON body', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ trades: 100, pnl: 50 }))
      const payload = { start: '2025-01-01', end: '2025-02-01' }
      await backtestApi.run(payload)
      const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/backtest/run')
      expect(init?.method).toBe('POST')
      expect(JSON.parse(init?.body as string)).toEqual(payload)
    })
  })

  // -------------------------------------------------------------------------
  // Auth header passthrough — verify that the Authorization bearer
  // (injected by apiFetch) survives through to the underlying fetch call.
  // This guards against a regression where a future refactor drops the
  // `headers` argument when spreading `init`.
  // -------------------------------------------------------------------------
  describe('auth header passthrough', () => {
    it('forwards the Bearer token from localStorage on GET', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ status: 'ok' }))
      localStorage.setItem('polymarket_api_token', 'tok-xyz')
      await systemApi.health()
      const [, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      const headers = new Headers(init?.headers)
      expect(headers.get('Authorization')).toBe('Bearer tok-xyz')
    })

    it('forwards the Bearer token from localStorage alongside POST Content-Type', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ ok: true }))
      localStorage.setItem('polymarket_api_token', 'tok-xyz')
      await tradingApi.placeTrade({ token_id: '0xabc', side: 'BUY', size: 1, price: 0.5 })
      const [, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      const headers = new Headers(init?.headers)
      expect(headers.get('Authorization')).toBe('Bearer tok-xyz')
      expect(headers.get('Content-Type')).toBe('application/json')
    })
  })

  // -------------------------------------------------------------------------
  // ApiError handling — verify the error path produces a typed ApiError
  // with `status` and parsed `body`, plus the edge case where the body
  // isn't JSON (gateway 5xx with HTML, empty 204, etc.).
  // -------------------------------------------------------------------------
  describe('ApiError handling', () => {
    it('throws ApiError on 400 with a JSON detail body', async () => {
      // mockImplementation returns a FRESH Response per call — Response bodies
      // are single-use, so reusing one (mockResolvedValue) makes the second
      // `res.json()` resolve to null. This test makes TWO calls (one in the
      // rejects.toThrow assertion, one in the try/catch) so we need a factory.
      vi.mocked(fetch).mockImplementation(async () =>
        jsonResponse({ detail: 'bad token_id' }, 400) as Response,
      )
      await expect(systemApi.health()).rejects.toThrow(ApiError)
      try {
        await systemApi.health()
      } catch (err) {
        expect(err).toBeInstanceOf(ApiError)
        const apiErr = err as ApiError
        expect(apiErr.status).toBe(400)
        expect(apiErr.body).toEqual({ detail: 'bad token_id' })
        expect(apiErr.message).toContain('400')
        expect(apiErr.message).toContain('bad token_id')
      }
    })

    it('throws ApiError on 500 with a JSON detail body', async () => {
      vi.mocked(fetch).mockImplementation(async () =>
        jsonResponse({ detail: 'internal error' }, 500) as Response,
      )
      await expect(tradingApi.getPositions()).rejects.toThrow(ApiError)
      try {
        await tradingApi.getPositions()
      } catch (err) {
        expect(err).toBeInstanceOf(ApiError)
        const apiErr = err as ApiError
        expect(apiErr.status).toBe(500)
        expect(apiErr.body.detail).toBe('internal error')
      }
    })

    it('exposes status + body as public fields', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ detail: 'nope', code: 'X' }, 422))
      try {
        await mlApi.getMetrics()
        throw new Error('should have thrown')
      } catch (err) {
        const apiErr = err as ApiError
        expect(apiErr.status).toBe(422)
        expect(apiErr.body).toEqual({ detail: 'nope', code: 'X' })
      }
    })

    it('uses "Unknown error" in message when detail is absent', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse({}, 503))
      try {
        await systemApi.health()
        throw new Error('should have thrown')
      } catch (err) {
        const apiErr = err as ApiError
        expect(apiErr.message).toContain('Unknown error')
      }
    })

    it('handles non-JSON error bodies (gateway 502 HTML)', async () => {
      // Gateway can return HTML 502 (e.g. when the upstream backend is
      // restarting). `res.json()` throws, the catch sets body=null, and
      // ApiError is still thrown with status 502 + body=null.
      vi.mocked(fetch).mockResolvedValue(
        new Response('<html>Bad Gateway</html>', {
          status: 502,
          headers: { 'Content-Type': 'text/html' },
        }),
      )
      try {
        await systemApi.health()
        throw new Error('should have thrown')
      } catch (err) {
        const apiErr = err as ApiError
        expect(apiErr.status).toBe(502)
        expect(apiErr.body).toBeNull()
      }
    })

    it('handles empty error bodies (204 / 205)', async () => {
      vi.mocked(fetch).mockResolvedValue(new Response('', { status: 404 }))
      try {
        await systemApi.events(10)
        throw new Error('should have thrown')
      } catch (err) {
        const apiErr = err as ApiError
        expect(apiErr.status).toBe(404)
        expect(apiErr.body).toBeNull()
      }
    })

    it('instanceof check works after re-throw (prototype chain)', async () => {
      // Regression for the ES5 emit + extending-built-ins gotcha. The
      // `Object.setPrototypeOf(this, ApiError.prototype)` line in the
      // ApiError constructor exists specifically to make this assertion
      // pass under TypeScript's down-leveled output.
      vi.mocked(fetch).mockResolvedValue(jsonResponse({ detail: 'x' }, 500))
      let caught: unknown
      try {
        await systemApi.health()
      } catch (err) {
        caught = err
      }
      expect(caught instanceof ApiError).toBe(true)
    })
  })

  // -------------------------------------------------------------------------
  // Return type contract — verify the JSON body returned by `res.json()`
  // is propagated to the caller (i.e. `request<T>` doesn't accidentally
  // wrap, unwrap, or mutate the response).
  // -------------------------------------------------------------------------
  describe('response propagation', () => {
    it('returns the parsed JSON body as-is', async () => {
      const payload = {
        positions: [{ token_id: '0xabc', slug: 'will-x', size: 100 }],
        count: 1,
        daily_pnl: 12.34,
      }
      vi.mocked(fetch).mockResolvedValue(jsonResponse(payload))
      const result = await tradingApi.getPositions()
      expect(result).toEqual(payload)
      expect(result.positions[0].token_id).toBe('0xabc')
      expect(result.count).toBe(1)
      expect(result.daily_pnl).toBe(12.34)
    })

    it('returns primitive arrays correctly', async () => {
      vi.mocked(fetch).mockResolvedValue(jsonResponse(['evt-1', 'evt-2', 'evt-3']))
      const result = await systemApi.events(3)
      expect(result).toEqual(['evt-1', 'evt-2', 'evt-3'])
    })

    it('returns null fields correctly (does not coerce)', async () => {
      vi.mocked(fetch).mockResolvedValue(
        jsonResponse({ equity: 100, avg_win: null, sharpe_ratio: null }),
      )
      const result = await analyticsApi.getAnalytics()
      expect(result.equity).toBe(100)
      expect(result.avg_win).toBeNull()
      expect(result.sharpe_ratio).toBeNull()
    })
  })
})
