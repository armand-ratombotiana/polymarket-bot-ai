import { test, expect } from '@playwright/test'

/**
 * Analytics flow E2E tests (expanded coverage).
 *
 * Sibling file to `analytics.spec.ts` — that file covers the basic
 * "panel becomes active + page-area survives" smoke for the 5
 * Analytics-group panels. THIS file goes deeper into each panel's
 * visible structure (KPI tiles, attribution breakdown, execution
 * metrics, closed-positions ledger table) so a regression that
 * leaves the panel mounted but with empty content is caught.
 *
 * The four panels covered:
 *   - Performance       (AnalyticsPanel + EquityCurve + Leaderboard)
 *   - Attribution       (AttributionPanel — 7-dim breakdown)
 *   - Execution Quality (ExecutionQualityPanel — per-fill audit)
 *   - Closed Positions  (ClosedPositionsPanel — realized P&L journal)
 *
 * All four load via `lazyPanel` (page.tsx:130-139) and fetch their
 * own data on mount. The backend may be up or down; tests assert
 * STRUCTURE — panel mounted + a stable header label is visible +
 * no PanelErrorBoundary fallback — never specific data values.
 */

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await expect(page.locator('.page-area')).toBeVisible({ timeout: 45000 })
})

test.describe('Performance panel flow', () => {
  test('navigates to the Performance panel and renders header', async ({
    page,
  }) => {
    // Sidebar.tsx:128 — `label: 'Performance'`.
    const perfBtn = page.getByRole('button', { name: /^Performance$/ }).first()
    await perfBtn.click()
    await expect(perfBtn).toHaveAttribute('aria-current', 'page')
    // The AnalyticsPanel header is "📊 Performance Analytics"
    // (AnalyticsPanel.tsx:132). It renders in every state (loading /
    // data / unavailable), making it the stable "panel mounted"
    // marker.
    await expect(
      page.getByText(/Performance Analytics/i).first(),
    ).toBeVisible({ timeout: 15000 })
  })

  test('Performance KPI cards render (P&L, Win Rate, Sharpe)', async ({
    page,
    request,
  }) => {
    // Conditional: the AnalyticsPanel renders KPI tiles only when
    // /api/analytics returns a payload. When the endpoint is down,
    // the panel shows the "Analytics data unavailable" notice (still
    // a valid mount). Probe the endpoint first to decide whether to
    // assert on KPI labels.
    const probe = await request
      .get('/api/analytics?XTransformPort=8080', {
        failOnStatusCode: false,
        timeout: 10000,
      })
      .catch(() => null)
    if (!probe || probe.status() !== 200) {
      test.skip(true, 'Backend /api/analytics not reachable — skipping KPI assertion')
      return
    }

    const perfBtn = page.getByRole('button', { name: /^Performance$/ }).first()
    await perfBtn.click()
    await expect(perfBtn).toHaveAttribute('aria-current', 'page')

    // The KPI grid (AnalyticsPanel.tsx:176) renders 8 kpi-card tiles:
    // Win Rate (Wilson 95% CI), Profit Factor, Trades / Volume,
    // Max Drawdown, Realized P&L, Unrealized P&L, Expectancy / Trade,
    // Avg Win / Avg Loss, Sharpe Ratio. The task spec calls out three
    // specifically — P&L, win rate, Sharpe — so assert each label
    // is present.
    await expect(
      page.getByText(/Win Rate \(Wilson/i).first(),
    ).toBeVisible({ timeout: 15000 })
    await expect(
      page.getByText(/Realized P&L|Realized P&amp;L/i).first(),
    ).toBeVisible()
    await expect(
      page.getByText(/Sharpe Ratio/i).first(),
    ).toBeVisible()
  })

  test('Performance panel renders EquityCurve chart card', async ({ page }) => {
    // The Performance panel composes EquityCurve + AnalyticsPanel +
    // LeaderboardPanel (page.tsx:863-873). The EquityCurve component
    // renders its own header; assert the page-area has the expected
    // child structure (no PanelErrorBoundary crash).
    const perfBtn = page.getByRole('button', { name: /^Performance$/ }).first()
    await perfBtn.click()
    await expect(perfBtn).toHaveAttribute('aria-current', 'page')
    await page.waitForTimeout(1500)
    await expect(page.locator('.panel-error-boundary')).toHaveCount(0)
    await expect(page.locator('.error-boundary-fallback')).toHaveCount(0)
    await expect(page.locator('.page-area')).toBeVisible()
  })
})

test.describe('Attribution panel flow', () => {
  test('navigates to the Attribution panel and renders header', async ({
    page,
  }) => {
    // Sidebar.tsx:130 — `label: 'Attribution'`.
    const attribBtn = page.getByRole('button', { name: /^Attribution$/ }).first()
    await attribBtn.click()
    await expect(attribBtn).toHaveAttribute('aria-current', 'page')
    // AttributionPanel.tsx:447 — "Performance Attribution" card-title.
    // It's always rendered (loading / data / error states all show
    // the header).
    await expect(
      page.getByText(/Performance Attribution/i).first(),
    ).toBeVisible({ timeout: 15000 })
  })

  test('Attribution renders the 7-dimension badge', async ({ page }) => {
    // AttributionPanel.tsx:450 — the "7-DIMENSION" badge sits next
    // to the card title. It's a constant render — always visible
    // once the panel mounts.
    const attribBtn = page.getByRole('button', { name: /^Attribution$/ }).first()
    await attribBtn.click()
    await expect(attribBtn).toHaveAttribute('aria-current', 'page')
    await expect(
      page.getByText(/7-DIMENSION/i).first(),
    ).toBeVisible({ timeout: 15000 })
  })

  test('Attribution renders summary KPI cards', async ({ page }) => {
    // AttributionPanel.tsx:507 — 4-column KPI grid: "Total P&L",
    // "Attributed", "Unattributed Residual", "Coverage". Always
    // rendered when the panel mounts (the values fall back to 0
    // when the fetch fails).
    const attribBtn = page.getByRole('button', { name: /^Attribution$/ }).first()
    await attribBtn.click()
    await expect(attribBtn).toHaveAttribute('aria-current', 'page')
    await expect(
      page.getByText(/^Total P&L$|^Total P&amp;L$/i).first(),
    ).toBeVisible({ timeout: 15000 })
    await expect(
      page.getByText(/^Attributed$/i).first(),
    ).toBeVisible()
    await expect(
      page.getByText(/Unattributed Residual/i).first(),
    ).toBeVisible()
    await expect(
      page.getByText(/^Coverage$/i).first(),
    ).toBeVisible()
  })

  test('Attribution renders the Dimensions / Waterfall / Strategies tabs', async ({
    page,
  }) => {
    // AttributionPanel.tsx:548 — the Tabs component renders three
    // TabsTrigger buttons ("Dimensions", "Waterfall", "Strategies").
    // They're always present (constant JSX), so they double as the
    // "panel mounted" stability check.
    const attribBtn = page.getByRole('button', { name: /^Attribution$/ }).first()
    await attribBtn.click()
    await expect(attribBtn).toHaveAttribute('aria-current', 'page')
    await expect(
      page.getByRole('tab', { name: /Dimensions/i }).first(),
    ).toBeVisible({ timeout: 15000 })
    await expect(
      page.getByRole('tab', { name: /Waterfall/i }).first(),
    ).toBeVisible()
    await expect(
      page.getByRole('tab', { name: /Strategies/i }).first(),
    ).toBeVisible()
  })

  test('Attribution breakdown renders (dimensions tab default)', async ({
    page,
  }) => {
    // The Dimensions tab is the default (defaultValue="dimensions"
    // in AttributionPanel.tsx:548). When the backend is up + has
    // attributed rows, it renders 7 attribution dimension cards
    // (Confidence, Edge, Probability, Liquidity, Holding Period,
    // Trade Direction, Strategy). When the backend is down or empty,
    // each card renders its own empty-state. Either way, the tab
    // panel content area should be non-empty.
    const attribBtn = page.getByRole('button', { name: /^Attribution$/ }).first()
    await attribBtn.click()
    await expect(attribBtn).toHaveAttribute('aria-current', 'page')

    // The Dimensions tab panel wraps the per-dimension cards. Each
    // card has a `role="region"` with `aria-label="X attribution"`
    // (AttributionPanel.tsx:591). At least the Confidence dimension
    // should be present in the rendered tree (it's the first
    // dimension in DIMENSIONS list and renders regardless of
    // backend state — empty-state is rendered when no rows).
    await expect
      .poll(
        async () =>
          await page
            .locator('[role="region"][aria-label$="attribution"]')
            .count(),
        {
          timeout: 15000,
          message: 'No attribution dimension regions rendered',
        },
      )
      .toBeGreaterThan(0)
  })
})

test.describe('Execution Quality panel flow', () => {
  test('navigates to Execution Quality and renders header', async ({ page }) => {
    // Sidebar.tsx:131 — `label: 'Execution Quality'`.
    const execBtn = page.getByRole('button', { name: /Execution Quality/i }).first()
    await execBtn.click()
    await expect(execBtn).toHaveAttribute('aria-current', 'page')
    // ExecutionQualityPanel.tsx:333 — "⚡ Execution Quality" card-title.
    await expect(
      page.getByText(/Execution Quality/i).first(),
    ).toBeVisible({ timeout: 15000 })
  })

  test('Execution Quality renders the per-fill audit KPI strip', async ({
    page,
  }) => {
    // ExecutionQualityPanel.tsx:409 — 5-column KPI grid:
    // "Avg Slippage", "Median Latency", "Realized Edge", "Fill Rate",
    // "Total Fills". All are always rendered (values fall back to
    // 0 / '—' when fetch fails).
    const execBtn = page.getByRole('button', { name: /Execution Quality/i }).first()
    await execBtn.click()
    await expect(execBtn).toHaveAttribute('aria-current', 'page')
    await expect(
      page.getByText(/^Avg Slippage$|Avg Slippage/i).first(),
    ).toBeVisible({ timeout: 15000 })
    await expect(
      page.getByText(/Median Latency/i).first(),
    ).toBeVisible()
    await expect(
      page.getByText(/Realized Edge/i).first(),
    ).toBeVisible()
    await expect(
      page.getByText(/Fill Rate/i).first(),
    ).toBeVisible()
  })

  test('Execution Quality renders the slippage distribution histogram', async ({
    page,
  }) => {
    // ExecutionQualityPanel.tsx:461 — the histogram has
    // role="img" + aria-label="Slippage distribution by bucket".
    // It's always rendered (the buckets are pre-computed; empty
    // data → all-zero bars).
    const execBtn = page.getByRole('button', { name: /Execution Quality/i }).first()
    await execBtn.click()
    await expect(execBtn).toHaveAttribute('aria-current', 'page')
    await expect(
      page.getByRole('img', { name: /Slippage distribution by bucket/i }).first(),
    ).toBeVisible({ timeout: 15000 })
  })

  test('Execution Quality renders the per-fill execution log table', async ({
    page,
  }) => {
    // ExecutionQualityPanel.tsx:658 — `<table role="table"
    // aria-label="Per-fill execution quality log">`. It's rendered
    // only when fills.length > 0; otherwise the empty-state
    // ("Slippage, latency, and realized-edge metrics will appear
    // here…") is shown at line 742.
    const execBtn = page.getByRole('button', { name: /Execution Quality/i }).first()
    await execBtn.click()
    await expect(execBtn).toHaveAttribute('aria-current', 'page')

    const tableLocator = page.getByRole('table', {
      name: /Per-fill execution quality log/i,
    })
    const emptyLocator = page.getByText(
      /Slippage, latency, and realized-edge metrics will appear here/i,
    )

    await expect
      .poll(
        async () => (await tableLocator.count()) + (await emptyLocator.count()),
        {
          timeout: 15000,
          message:
            'Execution Quality panel neither rendered the per-fill table nor its empty-state',
        },
      )
      .toBeGreaterThan(0)
  })
})

test.describe('Closed Positions panel flow', () => {
  test('navigates to Closed Positions and renders header', async ({ page }) => {
    // Sidebar.tsx:132 — `label: 'Closed Positions'`.
    const closedBtn = page.getByRole('button', { name: /Closed Positions/i }).first()
    await closedBtn.click()
    await expect(closedBtn).toHaveAttribute('aria-current', 'page')
    // ClosedPositionsPanel.tsx:366 / 408 — "📕 Closed Positions Ledger"
    // card-title. Rendered in both loading and main states.
    await expect(
      page.getByText(/Closed Positions Ledger/i).first(),
    ).toBeVisible({ timeout: 15000 })
  })

  test('Closed Positions renders the KPI summary strip', async ({ page }) => {
    // ClosedPositionsPanel.tsx:447 — 6-column KPI grid:
    // "Total Realized", "Win Rate", "Avg Win / Avg Loss",
    // "Avg Hold Time", "Best Trade", "Worst Trade" (the 6 KpiCard
    // labels). All rendered when the panel mounts; values fall back
    // to 0 / '—' when fetch fails.
    const closedBtn = page.getByRole('button', { name: /Closed Positions/i }).first()
    await closedBtn.click()
    await expect(closedBtn).toHaveAttribute('aria-current', 'page')
    await expect(
      page.getByText(/Total Realized/i).first(),
    ).toBeVisible({ timeout: 15000 })
    await expect(
      page.getByText(/^Win Rate$|Win Rate/i).first(),
    ).toBeVisible()
  })

  test('Closed Positions renders the ledger table or empty-state', async ({
    page,
  }) => {
    // ClosedPositionsPanel.tsx:575 — `<table role="table"
    // aria-label="Closed positions ledger">`. Rendered when
    // positions.length > 0; otherwise the empty-state
    // ("Closed positions will appear here once trades round-trip…")
    // is shown at line 571.
    const closedBtn = page.getByRole('button', { name: /Closed Positions/i }).first()
    await closedBtn.click()
    await expect(closedBtn).toHaveAttribute('aria-current', 'page')

    const tableLocator = page.getByRole('table', {
      name: /Closed positions ledger/i,
    })
    const emptyLocator = page.getByText(
      /Closed positions will appear here once trades round-trip/i,
    )

    await expect
      .poll(
        async () => (await tableLocator.count()) + (await emptyLocator.count()),
        {
          timeout: 15000,
          message:
            'Closed Positions panel neither rendered the ledger table nor its empty-state',
        },
      )
      .toBeGreaterThan(0)
  })

  test('Closed Positions exposes the Refresh + CSV export controls', async ({
    page,
  }) => {
    // ClosedPositionsPanel.tsx:431 — Refresh button (aria-label
    // "Refresh closed positions"). ClosedPositionsPanel.tsx:437 —
    // CSV export button (title="Export CSV"). Both are always
    // rendered in the header once the panel mounts (post-loading).
    const closedBtn = page.getByRole('button', { name: /Closed Positions/i }).first()
    await closedBtn.click()
    await expect(closedBtn).toHaveAttribute('aria-current', 'page')

    await expect(
      page.getByRole('button', { name: /Refresh closed positions/i }).first(),
    ).toBeVisible({ timeout: 15000 })
    // The CSV export button has the title attribute "Export CSV" —
    // accessible name resolves to the icon's text + label. Use the
    // title-based selector via the `CSV` text fragment.
    await expect(
      page.getByTitle('Export CSV').first(),
    ).toBeVisible()
  })
})

test.describe('Cross-panel Analytics flow', () => {
  test('every Analytics panel swaps without uncaught errors', async ({
    page,
  }) => {
    // Walk every Analytics-group sidebar item in NAV_GROUPS order and
    // capture any uncaught page errors. Failed fetches are caught by
    // per-panel try/catch handlers; only genuine JS exceptions
    // surface here.
    const errors: string[] = []
    page.on('pageerror', (err) => errors.push(err.message))

    const panelSelectors = [
      { name: /^Performance$/ },
      { name: /Backtest Lab/i },
      { name: /^Attribution$/ },
      { name: /Execution Quality/i },
      { name: /Closed Positions/i },
    ]
    for (const sel of panelSelectors) {
      await page.getByRole('button', sel).first().click()
      // Brief settle so the lazy chunk resolves before the next swap.
      await page.waitForTimeout(400)
    }
    expect(errors, `Uncaught page errors: ${errors.join(', ')}`).toEqual([])
  })
})
