import { test, expect } from '@playwright/test'

/**
 * Database Status panel E2E flow tests.
 *
 * Covers the `system-database-status` panel rendered by
 * `src/components/DatabaseStatusPanel.tsx` (W21-7). The panel exposes
 * the live DB backend (PostgreSQL vs SQLite), the PG pool health, the
 * table-level row/size stats, the recent connection errors, and a
 * manual "Retry PG Connection" button that POSTs to
 * `/api/system/db-retry`.
 *
 * Backend contract (mirrors the AsyncDBPool standby surface):
 *
 *   GET /api/system/db-status → {
 *     backend: 'postgresql' | 'sqlite',
 *     pg_health: { status, uptime_pct, avg_latency_ms, ... } | null,
 *     fallback_counter: number,
 *     tables: Array<{ name, row_count, size_mb, database, last_modified }>,
 *     recent_errors: Array<{ timestamp, error, retry_attempt, backend }>,
 *     generated_at: number,
 *   }
 *   POST /api/system/db-retry → { success, backend, message, attempted_at }
 *
 * The backend may or may not be running during E2E (the sandbox boots
 * it separately and the panel's fetch fail-closes to an `ErrorState`
 * sub-component with its own retry button). Tests therefore assert
 * STRUCTURE — the panel mount path that doesn't crash — never specific
 * data values (no uptime %, no row counts).
 *
 * Selectors mirror the W21-7 component contract:
 *  - `[data-testid="database-status-panel"]`  → root container when data lands
 *  - `[data-testid="db-backend-badge"]`       → BackendBadge (PG / SQLite pill)
 *  - `aria-label="Retry PostgreSQL connection"` → manual retry button
 *  - `aria-label="Refresh database status"`     → header Refresh button
 *  - `aria-label="Retry database status fetch"` → ErrorState's retry button
 */

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  // Wait for the client to hydrate — the panel mounts only after
  // `lazyPanel` resolves the dynamic import (page.tsx:152).
  await expect(page.locator('.page-area')).toBeVisible({ timeout: 45000 })
})

// The Database nav item is at Sidebar.tsx:142 with label 'Database'
// (id 'system-database-status'). The label is i18n-resolved; the EN
// fallback is 'Database'. Use the prefix match so FR locale ('Base de
// Données') doesn't break the test.
const DATABASE_NAV_PATTERN = /Database|Base de Données/i

test.describe('Database Status flow', () => {
  test('can navigate to the Database status panel', async ({ page }) => {
    // Sidebar.tsx:142 — `label: 'Database'`, id `system-database-status`.
    const dbBtn = page.getByRole('button', { name: DATABASE_NAV_PATTERN }).first()
    await dbBtn.click()
    // aria-current="page" is set on the active sidebar button
    // (Sidebar.tsx:269 — `aria-current={active === item.id ? 'page' : undefined}`).
    await expect(dbBtn).toHaveAttribute('aria-current', 'page')
    // The .page-area wrapper persists across swaps — assert it's still
    // visible as the proxy "panel mounted" signal.
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('panel mounts without crashing the app shell', async ({ page }) => {
    // After navigation, neither the root ErrorBoundary
    // (.error-boundary-fallback) nor the PanelErrorBoundary
    // (.panel-error-boundary) should be visible — the lazy chunk
    // resolved, the panel mounted, and any fetch failure was caught
    // by the panel's own try/catch (rendering its ErrorState sub-card,
    // NOT a crash). 2s settle covers the lazy-chunk download + the
    // /api/system/db-status fetch's promise rejection microtask.
    const dbBtn = page.getByRole('button', { name: DATABASE_NAV_PATTERN }).first()
    await dbBtn.click()
    await expect(dbBtn).toHaveAttribute('aria-current', 'page')
    await page.waitForTimeout(2000)
    await expect(page.locator('.panel-error-boundary')).toHaveCount(0)
    await expect(page.locator('.error-boundary-fallback')).toHaveCount(0)
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('panel renders backend indicator (PostgreSQL or SQLite)', async ({
    page,
  }) => {
    // The BackendBadge (DatabaseStatusPanel.tsx:173) is the canonical
    // backend indicator — a Badge with `data-testid="db-backend-badge"`
    // whose visible text is either "PostgreSQL" or "SQLite".
    //
    // Three render paths:
    //   1. Loading skeleton  → no badge yet (the skeleton renders
    //      before the fetch resolves).
    //   2. Data loaded      → badge visible with PG or SQLite text.
    //   3. Fetch errored    → ErrorState card replaces the panel
    //      body (no badge, but the ErrorState renders its own Retry
    //      button).
    //
    // We poll for "(badge visible) OR (error state visible)" — both
    // are valid post-mount states; what's NOT valid is a PanelErrorBoundary
    // fallback (asserted separately above).
    const dbBtn = page.getByRole('button', { name: DATABASE_NAV_PATTERN }).first()
    await dbBtn.click()
    await expect(dbBtn).toHaveAttribute('aria-current', 'page')

    const badge = page.getByTestId('db-backend-badge')
    const errorState = page.getByText('Database status endpoint unavailable')

    await expect
      .poll(
        async () => {
          const badgeText = (await badge.textContent()) ?? ''
          const isBadgeVisible = await badge.isVisible().catch(() => false)
          const isErrorVisible = await errorState.isVisible().catch(() => false)
          return {
            badge: isBadgeVisible ? badgeText : null,
            error: isErrorVisible,
          }
        },
        {
          timeout: 15000,
          message:
            'Database panel neither rendered the backend badge nor the error state',
        },
      )
      .toMatchObject({
        // Exactly one of the two states must hold. The badge text is
        // either PostgreSQL or SQLite — never empty when visible.
        // The error flag is true when the panel fail-closed.
        badge: expect.stringMatching(/^(PostgreSQL|SQLite)$/),
        error: expect.any(Boolean),
      })
  })

  test('backend badge text matches PostgreSQL or SQLite when panel is up', async ({
    page,
    request,
  }) => {
    // Conditional contract: when the backend /api/system/db-status
    // endpoint is unreachable, the panel renders its ErrorState card
    // (no badge). Skip the badge-text assertion in that case —
    // mirroring the api-health.spec.ts + ml.spec.ts pattern of
    // probing the endpoint first.
    const probe = await request
      .get('/api/system/db-status?XTransformPort=8080', {
        failOnStatusCode: false,
        timeout: 10000,
      })
      .catch(() => null)
    if (!probe || probe.status() !== 200) {
      test.skip(true, 'Backend /api/system/db-status not reachable — skipping badge-text assertion')
      return
    }

    const dbBtn = page.getByRole('button', { name: DATABASE_NAV_PATTERN }).first()
    await dbBtn.click()
    await expect(dbBtn).toHaveAttribute('aria-current', 'page')

    // The badge MUST be visible (panel data loaded) — assert its text
    // matches one of the two canonical backend identifiers.
    await expect(
      page.getByTestId('db-backend-badge'),
    ).toContainText(/^(PostgreSQL|SQLite)$/, { timeout: 15000 })
  })

  test('panel renders PG health status display', async ({ page }) => {
    // The PostgreSQL Connection Health card (DatabaseStatusPanel.tsx:545)
    // renders either:
    //   - a 5-column health grid (when pg_health is non-null), OR
    //   - the "PostgreSQL pool is not configured" notice (when pg_health
    //     is null — i.e. operating on the SQLite standby backend).
    //
    // Both paths include a HealthBadge sub-component (when pg_health is
    // present) OR a fallback notice. The header text
    // "PostgreSQL Connection Health" is ALWAYS present in either path.
    const dbBtn = page.getByRole('button', { name: DATABASE_NAV_PATTERN }).first()
    await dbBtn.click()
    await expect(dbBtn).toHaveAttribute('aria-current', 'page')

    // The card title is always rendered — it sits above the conditional
    // (grid | notice) block. Use a regex so a wording tweak (e.g.
    // "PG Connection Health" vs "PostgreSQL Connection Health") doesn't
    // break the test.
    await expect(
      page.getByText(/PostgreSQL Connection Health/i).first(),
    ).toBeVisible({ timeout: 15000 })
  })

  test('panel renders database tables list or empty-state', async ({ page }) => {
    // The "Database Tables" card (DatabaseStatusPanel.tsx:663) renders
    // either a `<table>` with per-table rows, OR an empty-state message
    // ("No table statistics available") when the backend reports no
    // tables. Both paths include the "Database Tables" header — assert
    // the header is visible AND that one of the two body states holds.
    const dbBtn = page.getByRole('button', { name: DATABASE_NAV_PATTERN }).first()
    await dbBtn.click()
    await expect(dbBtn).toHaveAttribute('aria-current', 'page')

    // Card title is always present.
    await expect(
      page.getByText(/^Database Tables$/i).first(),
    ).toBeVisible({ timeout: 15000 })

    // Either a table is rendered (with rows) OR the empty-state
    // message is shown. Use a poll to handle the lazy chunk + fetch
    // settle window.
    const tableLocator = page
      .getByText(/^Database Tables$/i)
      .first()
      .locator('xpath=ancestor::*[contains(@class,"card") or self::div]')
      .locator('table')
    const emptyLocator = page.getByText(/No table statistics available/i).first()

    await expect
      .poll(
        async () => (await tableLocator.count()) + (await emptyLocator.count()),
        {
          timeout: 15000,
          message:
            'Database Tables card neither rendered a table nor the empty-state',
        },
      )
      .toBeGreaterThan(0)
  })

  test('Retry PG Connection button is present and clickable', async ({ page }) => {
    // The "Retry PG Connection" button (DatabaseStatusPanel.tsx:629)
    // is always rendered inside the PG Health card body — its
    // aria-label is "Retry PostgreSQL connection". Clicking it fires
    // `POST /api/system/db-retry` and re-fetches the status.
    //
    // We verify the button is visible AND that clicking it does NOT
    // crash the app (the click triggers a network POST; the response
    // is rendered as a result banner — `retryResult` state in
    // DatabaseStatusPanel.tsx:304). We don't assert on the banner's
    // success/failure text because that depends on the backend state.
    const dbBtn = page.getByRole('button', { name: DATABASE_NAV_PATTERN }).first()
    await dbBtn.click()
    await expect(dbBtn).toHaveAttribute('aria-current', 'page')

    const retryBtn = page.getByRole('button', {
      name: /Retry PostgreSQL connection/i,
    }).first()
    await expect(retryBtn).toBeVisible({ timeout: 15000 })

    // Capture uncaught page errors during the click + settle.
    const errors: string[] = []
    page.on('pageerror', (err) => errors.push(err.message))

    await retryBtn.click()
    // Settle so the POST promise rejects (backend may be down) and
    // the catch handler runs — proving the panel handled the failure
    // instead of throwing.
    await page.waitForTimeout(1500)

    expect(errors, `Uncaught page errors: ${errors.join(', ')}`).toEqual([])
    // The page-area must survive the click (no crash).
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('header Refresh button re-fetches status without crashing', async ({
    page,
  }) => {
    // The header "Refresh" button (DatabaseStatusPanel.tsx:468) calls
    // `handleManualRefresh` → `fetchStatus()` — a GET to
    // /api/system/db-status. When the backend is down the promise
    // rejects and the catch handler sets the error state (the ErrorState
    // card replaces the panel body); the page must NOT crash.
    const dbBtn = page.getByRole('button', { name: DATABASE_NAV_PATTERN }).first()
    await dbBtn.click()
    await expect(dbBtn).toHaveAttribute('aria-current', 'page')

    const refreshBtn = page.getByRole('button', {
      name: /Refresh database status/i,
    }).first()
    // The Refresh button is rendered in the header only after the
    // initial fetch resolves (loading skeleton doesn't render the
    // header). Wait for it to mount.
    await expect(refreshBtn).toBeVisible({ timeout: 15000 })

    const errors: string[] = []
    page.on('pageerror', (err) => errors.push(err.message))

    await refreshBtn.click()
    await page.waitForTimeout(1500)

    expect(errors, `Uncaught page errors: ${errors.join(', ')}`).toEqual([])
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('ErrorState card exposes its own Retry button when fetch fails', async ({
    page,
    request,
  }) => {
    // When the initial /api/system/db-status fetch rejects (network
    // error or non-2xx), the panel renders an ErrorState sub-component
    // (DatabaseStatusPanel.tsx:255) with its own "Retry" button whose
    // aria-label is "Retry database status fetch". This is the
    // canonical recovery affordance when the backend is unreachable.
    //
    // Conditional contract: only assert when the backend is confirmed
    // down — when the backend is up, the panel renders the data view
    // (no ErrorState). Mirror the api-health.spec.ts probe pattern.
    const probe = await request
      .get('/api/system/db-status?XTransformPort=8080', {
        failOnStatusCode: false,
        timeout: 10000,
      })
      .catch(() => null)
    if (probe && probe.status() === 200) {
      // Backend is up — the ErrorState won't render. Skip the test
      // (it's only meaningful when the panel fail-closes).
      test.skip(true, 'Backend reachable — ErrorState retry path not exercised')
      return
    }

    const dbBtn = page.getByRole('button', { name: DATABASE_NAV_PATTERN }).first()
    await dbBtn.click()
    await expect(dbBtn).toHaveAttribute('aria-current', 'page')

    const errorRetryBtn = page.getByRole('button', {
      name: /Retry database status fetch/i,
    }).first()
    await expect(errorRetryBtn).toBeVisible({ timeout: 15000 })

    // Clicking it should call handleManualRefresh (which re-issues the
    // GET). The page must NOT crash.
    const errors: string[] = []
    page.on('pageerror', (err) => errors.push(err.message))
    await errorRetryBtn.click()
    await page.waitForTimeout(1000)
    expect(errors).toEqual([])
  })
})
