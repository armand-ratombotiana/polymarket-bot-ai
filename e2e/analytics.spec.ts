import { test, expect } from '@playwright/test'

/**
 * Analytics panel E2E tests.
 *
 * Covers the five Analytics sub-panels:
 *   - Performance       (EquityCurve + AnalyticsPanel + LeaderboardPanel)
 *   - Backtest Lab      (BacktestLabView)
 *   - Attribution       (AttributionPanel)
 *   - Execution Quality (ExecutionQualityPanel)
 *   - Closed Positions  (ClosedPositionsPanel)
 *
 * All five load via `lazyPanel` (page.tsx:130-146) and fetch their own
 * data on mount. Tests assert STRUCTURE — "the panel became active and
 * the page-area survived the swap" — never specific data values.
 */

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await expect(page.locator('.page-area')).toBeVisible({ timeout: 45000 })
})

test.describe('Analytics Panels', () => {
  test('can navigate to Performance panel', async ({ page }) => {
    // Sidebar.tsx:125 — `label: 'Performance'`. Use `.first()` because
    // some sub-panel cards (the AnalyticsPanel embeds KPI tiles that
    // include "performance" text in their aria-descriptions).
    const perfBtn = page.getByRole('button', { name: /^Performance$/ }).first()
    await perfBtn.click()
    await expect(perfBtn).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('can navigate to Backtest Lab panel', async ({ page }) => {
    // Sidebar.tsx:126 — `label: 'Backtest Lab'`.
    const backtestBtn = page.getByRole('button', { name: /Backtest Lab/i }).first()
    await backtestBtn.click()
    await expect(backtestBtn).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('can navigate to Attribution panel', async ({ page }) => {
    // Sidebar.tsx:127 — `label: 'Attribution'`.
    const attribBtn = page.getByRole('button', { name: /^Attribution$/ }).first()
    await attribBtn.click()
    await expect(attribBtn).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('can navigate to Execution Quality panel', async ({ page }) => {
    // Sidebar.tsx:128 — `label: 'Execution Quality'`.
    const execBtn = page.getByRole('button', { name: /Execution Quality/i }).first()
    await execBtn.click()
    await expect(execBtn).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('can navigate to Closed Positions panel', async ({ page }) => {
    // Sidebar.tsx:129 — `label: 'Closed Positions'`.
    const closedBtn = page.getByRole('button', { name: /Closed Positions/i }).first()
    await closedBtn.click()
    await expect(closedBtn).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('Performance panel renders KPIs without crashing', async ({ page }) => {
    // The Performance panel is a composite: EquityCurve (chart) +
    // AnalyticsPanel (KPI tiles) + LeaderboardPanel (strategy table).
    // All three fetch independently on mount. When the backend is down,
    // each lands in its own error / empty state — the panel must NOT
    // surface as a PanelErrorBoundary.
    const perfBtn = page.getByRole('button', { name: /^Performance$/ }).first()
    await perfBtn.click()
    await expect(perfBtn).toHaveAttribute('aria-current', 'page')
    // Give the lazy chunk + the 3 independent fetches a beat.
    await page.waitForTimeout(2000)
    await expect(page.locator('.panel-error-boundary')).toHaveCount(0)
    await expect(page.locator('.error-boundary-fallback')).toHaveCount(0)
    // The page-area must still be visible.
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('Backtest Lab panel renders without crashing', async ({ page }) => {
    const backtestBtn = page.getByRole('button', { name: /Backtest Lab/i }).first()
    await backtestBtn.click()
    await expect(backtestBtn).toHaveAttribute('aria-current', 'page')
    await page.waitForTimeout(2000)
    await expect(page.locator('.panel-error-boundary')).toHaveCount(0)
    await expect(page.locator('.error-boundary-fallback')).toHaveCount(0)
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('every Analytics panel swaps without uncaught errors', async ({ page }) => {
    // Walk every Analytics sub-panel in sidebar order and capture any
    // uncaught page errors. A failed fetch is NOT a page error (it's
    // caught by `useBot` / per-panel fetch hooks) — only genuine JS
    // exceptions appear here.
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
      // Brief settle so the lazy chunk + initial fetch resolve before
      // the next swap. 300ms is enough for the React commit + the
      // microtask queue to drain; fixed sleeps here are intentional —
      // Playwright's auto-wait doesn't cover "fetch settled" without
      // asserting on data we explicitly don't want to assert on.
      await page.waitForTimeout(300)
    }
    expect(errors, `Uncaught page errors: ${errors.join(', ')}`).toEqual([])
  })
})
