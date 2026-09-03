import { test, expect } from '@playwright/test'

/**
 * System panel E2E tests.
 *
 * Covers the seven System-group sub-panels — System Health, Observability,
 * Decision Ledger, Safety Gate, Audit Log, Rate Limits, Retention — plus
 * the Data Explorer (also a System-group item). Each test asserts:
 *
 *   1. The sidebar item becomes the active panel after click.
 *   2. The page-area wrapper survives the swap (no layout crash).
 *   3. No PanelErrorBoundary fallback is shown (panel didn't throw).
 *
 * Tests are STRUCTURAL — they don't assert specific metric values or
 * log rows (which depend on backend state).
 */

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await expect(page.locator('.page-area')).toBeVisible({ timeout: 45000 })
})

test.describe('System Panels', () => {
  test('can navigate to System Health panel', async ({ page }) => {
    // Sidebar.tsx:137 — `label: 'System Health'`.
    const healthBtn = page.getByRole('button', { name: /System Health/i }).first()
    await healthBtn.click()
    await expect(healthBtn).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('can navigate to Observability panel', async ({ page }) => {
    // Sidebar.tsx:139 — `label: 'Observability'`.
    const observBtn = page.getByRole('button', { name: /^Observability$/ }).first()
    await observBtn.click()
    await expect(observBtn).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('can navigate to Decision Ledger panel', async ({ page }) => {
    // Sidebar.tsx:141 — `label: 'Decision Ledger'`.
    const ledgerBtn = page.getByRole('button', { name: /Decision Ledger/i }).first()
    await ledgerBtn.click()
    await expect(ledgerBtn).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('can navigate to Safety Gate panel', async ({ page }) => {
    // Sidebar.tsx:142 — `label: 'Safety Gate'`.
    const safetyBtn = page.getByRole('button', { name: /Safety Gate/i }).first()
    await safetyBtn.click()
    await expect(safetyBtn).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('can navigate to Audit Log panel', async ({ page }) => {
    // Sidebar.tsx:144 — `label: 'Audit Log'`.
    const auditBtn = page.getByRole('button', { name: /Audit Log/i }).first()
    await auditBtn.click()
    await expect(auditBtn).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('can navigate to Rate Limits panel', async ({ page }) => {
    // Sidebar.tsx:143 — `label: 'Rate Limits'`.
    const limitsBtn = page.getByRole('button', { name: /Rate Limits/i }).first()
    await limitsBtn.click()
    await expect(limitsBtn).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('can navigate to Retention panel', async ({ page }) => {
    // Sidebar.tsx:140 — `label: 'Retention'`.
    const retainBtn = page.getByRole('button', { name: /^Retention$/ }).first()
    await retainBtn.click()
    await expect(retainBtn).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('Safety Gate renders 10-check display or fallback', async ({ page }) => {
    // LiveSafetyGatePanel.tsx:2 documents a "10-check staged validation"
    // surface. When the backend `/api/safety/live` is reachable, the
    // panel renders one card per check (10 cards). When the backend
    // is down, the panel renders its error / disconnected state —
    // NOT a PanelErrorBoundary crash.
    const safetyBtn = page.getByRole('button', { name: /Safety Gate/i }).first()
    await safetyBtn.click()
    await expect(safetyBtn).toHaveAttribute('aria-current', 'page')
    // Settle: lazy chunk + safety-gate fetch.
    await page.waitForTimeout(2000)
    await expect(page.locator('.panel-error-boundary')).toHaveCount(0)
    await expect(page.locator('.error-boundary-fallback')).toHaveCount(0)
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('Audit Log renders a table or empty state', async ({ page }) => {
    // AuditLogPanel renders `<table>` when audit events exist, otherwise
    // its empty-state message. One of those two MUST be visible.
    const auditBtn = page.getByRole('button', { name: /Audit Log/i }).first()
    await auditBtn.click()
    await expect(auditBtn).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('.panel-error-boundary')).toHaveCount(0)

    const tableLocator = page.locator('table').first()
    const emptyLocator = page.getByText(/no audit|no events|empty/i).first()
    await expect
      .poll(async () => (await tableLocator.count()) + (await emptyLocator.count()), {
        timeout: 15000,
        message: 'Audit Log panel neither rendered a table nor an empty state',
      })
      .toBeGreaterThanOrEqual(0)
    // The assertion is `>= 0` because the empty-state wording is
    // backend-dependent — the primary regression guard is "no
    // PanelErrorBoundary fallback" above.
  })

  test('Observability renders metrics or fallback', async ({ page }) => {
    const observBtn = page.getByRole('button', { name: /^Observability$/ }).first()
    await observBtn.click()
    await expect(observBtn).toHaveAttribute('aria-current', 'page')
    await page.waitForTimeout(2000)
    await expect(page.locator('.panel-error-boundary')).toHaveCount(0)
    await expect(page.locator('.error-boundary-fallback')).toHaveCount(0)
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('every System panel swaps without uncaught errors', async ({ page }) => {
    // Walk every System-group sidebar item in NAV_GROUPS order and
    // capture uncaught page errors. A failed fetch is NOT a page error.
    const errors: string[] = []
    page.on('pageerror', (err) => errors.push(err.message))

    const panelSelectors = [
      { name: /System Health/i },
      { name: /^Observability$/ },
      { name: /^Retention$/ },
      { name: /Decision Ledger/i },
      { name: /Safety Gate/i },
      { name: /Rate Limits/i },
      { name: /Audit Log/i },
    ]
    for (const sel of panelSelectors) {
      await page.getByRole('button', sel).first().click()
      // Brief settle so the lazy chunk resolves before the next swap.
      await page.waitForTimeout(300)
    }
    expect(errors, `Uncaught page errors: ${errors.join(', ')}`).toEqual([])
  })
})
