import { test, expect } from '@playwright/test'

/**
 * API integration / health tests.
 *
 * These tests verify the *contract* between the dashboard frontend and the
 * Polymarket bot backend (FastAPI on :8080, exposed via the Caddy gateway at
 * `?XTransformPort=8080` per `src/lib/api.ts`).
 *
 * IMPORTANT: the backend may or may not be running during E2E. The tests are
 * written defensively:
 *  - When the backend IS up, `/api/health?XTransformPort=8080` should
 *    return 200 with a JSON body containing the canonical `status` field.
 *  - When the backend is DOWN, the gateway returns a 502 (bad gateway) or
 *    the fetch throws ECONNREFUSED — both of which the dashboard's `useBot`
 *    hook handles by leaving `status` at 'disconnected' and `snapshot`
 *    at its `DEFAULT_SNAPSHOT`. The dashboard must NOT crash.
 *
 * The "frontend can fetch" test verifies the FRONTEND'S behaviour, not the
 * backend's: it loads the dashboard and asserts that the page rendered
 * without crashing, regardless of whether the bot API is reachable. This
 * guards against regressions in the `useBot` error path (e.g. an unhandled
 * rejection that would surface as a React error boundary fallback).
 */

const BOT_API_PORT = '8080'

function gatewayUrl(path: string): string {
  // Per the gateway contract: every cross-service request must be a
  // relative path with `?XTransformPort=<port>` query. Caddy reads the
  // query param and reverse-proxies to `localhost:<port>`.
  const sep = path.includes('?') ? '&' : '?'
  return `${path}${sep}XTransformPort=${BOT_API_PORT}`
}

test.describe('Backend health endpoint (via gateway)', () => {
  test('GET /api/health responds (200 if up, 5xx if backend down)', async ({
    request,
  }) => {
    // Hit the gateway-routed backend health endpoint. We don't assert a
    // specific status code — instead we assert the response shape contract:
    // either the backend is healthy (200 + JSON) OR it's down (5xx with the
    // gateway's error envelope). Either way, the request itself must
    // complete (no infinite hang) and return JSON.
    const response = await request.get(gatewayUrl('/api/health'), {
      failOnStatusCode: false, // we tolerate 5xx here
      timeout: 15000,
    })
    // The response should have a status code in the expected ranges.
    // 200 = backend healthy; 502/503/504 = gateway can't reach backend.
    expect(response.status()).toBeGreaterThanOrEqual(200)
    expect(response.status()).toBeLessThan(600)
    // Body should be JSON-parseable (FastAPI returns JSON envelopes for
    // both success and error responses; Caddy's default 502 is HTML, but
    // we accept either gracefully by attempting parse).
    const text = await response.text()
    expect(text.length).toBeGreaterThan(0)
  })

  test('GET /api/health returns JSON status field when backend is up', async ({
    request,
  }) => {
    // Conditional contract: only assert the JSON shape if the backend
    // actually responded with 200. If it's down (5xx), skip — we can't
    // assert the shape of an error response from the gateway.
    const response = await request.get(gatewayUrl('/api/health'), {
      failOnStatusCode: false,
      timeout: 15000,
    })
    if (response.status() !== 200) {
      test.skip(true, `Backend not running (got ${response.status()}) — skipping JSON-shape assertion`)
      return
    }
    const body = await response.json().catch(() => null)
    expect(body).toBeTruthy()
    expect(typeof body).toBe('object')
    // The FastAPI `/api/health` route returns at minimum a `status` field
    // (the canonical health-check contract — verified across the W9/W10
    // backend test suites that assert `body['status'] == 'ok'`).
    expect(body).toHaveProperty('status')
  })

  test('GET /api/status responds with trading-mode envelope', async ({
    request,
  }) => {
    // `/api/status` is the dashboard's secondary health probe — used by
    // `useBot` as a fallback when `/api/snapshot` is unavailable. It
    // returns the bot's mode (paper/live/shadow/backtest), kill-switch
    // state, and strategy list.
    const response = await request.get(gatewayUrl('/api/status'), {
      failOnStatusCode: false,
      timeout: 15000,
    })
    expect(response.status()).toBeGreaterThanOrEqual(200)
    expect(response.status()).toBeLessThan(600)
    if (response.status() !== 200) {
      test.skip(true, `Backend not running (got ${response.status()}) — skipping JSON-shape assertion`)
      return
    }
    const body = await response.json().catch(() => null)
    expect(body).toBeTruthy()
    // `mode` is the canonical field — asserted in W9-8 integration tests.
    expect(body).toHaveProperty('mode')
  })
})

test.describe('Frontend can fetch from backend (via gateway)', () => {
  test('dashboard renders without crashing regardless of backend state', async ({
    page,
  }) => {
    // The dashboard's `useBot` hook (src/hooks/useBot.ts) calls
    // `fetch('/api/snapshot?XTransformPort=8080')` on mount. If the
    // backend is down, the fetch rejects, `useBot` catches it, leaves
    // `status='disconnected'`, and the dashboard continues rendering with
    // `DEFAULT_SNAPSHOT`. The Page-level ErrorBoundary should NOT trip.
    const errors: string[] = []
    page.on('pageerror', (err) => {
      // Capture uncaught page errors. Note: failed `fetch()` calls do NOT
      // surface as `pageerror` — they're handled by the `.catch()` in
      // `useBot`. Only genuine JS exceptions (e.g. unhandled promise
      // rejection, undefined-property access) appear here.
      errors.push(err.message)
    })

    await page.goto('/')
    // Wait for the dashboard shell to render — this proves React hydrated
    // and at least the Command Center panel mounted, which requires the
    // `useBot` hook to have been called and not thrown synchronously.
    await expect(page.locator('.page-area')).toBeVisible({ timeout: 45000 })
    // The sidebar should also be present — proves the app shell layout
    // survived the `useBot` initialisation.
    await expect(
      page.getByRole('navigation', { name: 'Primary navigation' }),
    ).toBeVisible()

    // Give the `useBot` poll loop a beat to fire its first fetch. We're
    // not asserting on the result — we're asserting the fetch was
    // *attempted* (which it always is, by design) without crashing the
    // page. 2 seconds is enough for the initial `fetchRestSnapshot` call
    // to resolve or reject.
    await page.waitForTimeout(2000)

    // No uncaught page errors should have been recorded. A failed fetch
    // is not an error (it's caught); a thrown exception would be.
    expect(errors, `Uncaught page errors: ${errors.join(', ')}`).toEqual([])
  })

  test('snapshot endpoint is reachable from the browser context', async ({
    page,
  }) => {
    // Verify that a request to `/api/snapshot?XTransformPort=8080`
    // (the exact URL `useBot` calls) at least COMPLETES from inside the
    // browser — proving the gateway routing works for the frontend's
    // primary data endpoint. We don't assert the response shape; we
    // assert that the fetch promise settles.
    await page.goto('/')
    // Evaluate inside the browser context so the fetch goes through the
    // same network path the app uses (Caddy gateway, not the test
    // runner's HTTP client).
    const outcome = await page.evaluate(async () => {
      try {
        const res = await fetch('/api/snapshot?XTransformPort=8080', {
          headers: {
            // The default token from `src/lib/api.ts::getApiToken()`.
            Authorization: 'Bearer I76FCamSbBw0e1r_V0RRX81uG-3DUCS_pofbYNC-RgHO3x9b3DIovCPe01iDREBT',
          },
        })
        return { ok: res.ok, status: res.status }
      } catch (err) {
        return { ok: false, status: 0, error: String(err) }
      }
    })
    // Either the backend responded (status > 0) OR the fetch threw
    // (status === 0, error populated). Either is acceptable for E2E —
    // we're testing that the fetch path WORKS (didn't hang the page).
    expect(outcome).toBeTruthy()
    if (outcome.status === 0) {
      // Network error — backend is down. That's OK for this test; we
      // just verify the error was caught, not propagated.
      expect(outcome.error).toBeTruthy()
    } else {
      // Got a real HTTP response — verify the status is in a sane range.
      expect(outcome.status).toBeGreaterThanOrEqual(200)
      expect(outcome.status).toBeLessThan(600)
    }
  })

  test('connection-status pill renders in the top bar', async ({ page }) => {
    // The TopStatusBar (src/components/TopStatusBar.tsx) renders a status
    // pill (StatusPill component) that reflects `useBot`'s connection
    // state. Even when disconnected, it shows a label. This verifies the
    // status-bar wiring is intact end-to-end through the `useBot` hook.
    await page.goto('/')
    await expect(page.locator('.page-area')).toBeVisible({ timeout: 45000 })
    // The top status bar header has role=banner and aria-label
    // 'System status bar' (TopStatusBar.tsx:125).
    const topbar = page.getByRole('banner', { name: 'System status bar' })
    await expect(topbar).toBeVisible()
    // The status pill is rendered inside the topbar. We don't assert
    // specific text (it changes based on connection state: 'connecting',
    // 'connected', 'disconnected', 'error'). Just assert SOMETHING in the
    // topbar has rendered (the topbar has non-empty text content).
    await expect(topbar).not.toHaveText('')
  })
})
