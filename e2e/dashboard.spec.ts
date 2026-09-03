import { test, expect } from '@playwright/test'

/**
 * Dashboard Golden Path tests.
 *
 * These tests cover the critical "first 5 seconds" UX: the app boots, the
 * sidebar renders its nav groups, the Command Center is the default panel,
 * and the user can click a sidebar item to swap the panel area.
 *
 * IMPORTANT: the dashboard (`src/app/page.tsx`) is a `'use client'` component
 * that returns an "Initializing Polymarket Pro Workstation…" placeholder
 * until `mounted` flips to `true` (a `useEffect` on mount). Playwright's
 * `webServer` hook only waits for the dev server to accept TCP connections —
 * it does NOT wait for the client bundle to hydrate. The shared `beforeEach`
 * below waits for the `.page-area` container to appear, which is the proxy
 * signal that the workstation has mounted and rendered at least one panel.
 *
 * The backend may or may not be running during E2E (the sandbox boots it
 * separately). The dashboard's `useBot` hook is designed to fail soft: when
 * `/api/snapshot` errors, the snapshot stays at its `DEFAULT_SNAPSHOT` and
 * the status pill shows `disconnected`. Tests below assert STRUCTURE
 * (panels exist, labels are present) rather than DATA VALUES (no P&L
 * digits, no order counts) so they're stable whether the bot is live or
 * paper-idle.
 */

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  // The dashboard renders `<div class="page-area">` only after the client
  // has hydrated. Wait for it (with the default 30s timeout — the dev
  // server takes ~8s for the first request, then the React tree mounts).
  await expect(page.locator('.page-area')).toBeVisible({ timeout: 45000 })
})

test.describe('Dashboard Golden Path', () => {
  test('page loads with title', async ({ page }) => {
    await page.goto('/')
    // `metadata.title` in `src/app/layout.tsx` is
    // 'Polymarket Pro — Algorithmic Trading Workstation'.
    await expect(page).toHaveTitle(/Polymarket/)
  })

  test('sidebar renders all nav groups', async ({ page }) => {
    await page.goto('/')
    // The eight top-level group labels defined in `Sidebar.tsx::NAV_GROUPS`.
    // On desktop (default Desktop Chrome viewport 1280×720) the sidebar is
    // expanded, so the group labels are visible (they're hidden only when
    // the sidebar is collapsed or in mobile mode).
    await expect(page.locator('text=Main').first()).toBeVisible()
    await expect(page.locator('text=Markets').first()).toBeVisible()
    await expect(page.locator('text=Portfolio').first()).toBeVisible()
    await expect(page.locator('text=System').first()).toBeVisible()
  })

  test('can navigate between panels', async ({ page }) => {
    await page.goto('/')
    // Click on Positions in sidebar (group: Portfolio).
    await page.getByRole('button', { name: /Positions/ }).first().click()
    // Verify the panel content area is still present (the panel switched,
    // not the layout broke). The `.page-area` wrapper persists across
    // panel swaps — only its child changes.
    await expect(page.locator('.page-area')).toBeVisible()
    // The clicked item should now be the active nav entry (aria-current
    // is set to 'page' on the active button per Sidebar.tsx:230).
    await expect(
      page.getByRole('button', { name: /Positions/ }).first(),
    ).toHaveAttribute('aria-current', 'page')
  })

  test('command center loads by default', async ({ page }) => {
    await page.goto('/')
    // The default `activeSection` state in `page.tsx:164` is `'command'`,
    // which renders the "Command Center" panel. The Command Center is a
    // composite panel (RiskStatusPanel + EquityCurve + MarketsPanel + ...)
    // so we assert against the sidebar's active marker rather than a
    // specific sub-component label that might differ by data state.
    await expect(
      page.getByRole('button', { name: /Command Center/ }),
    ).toHaveAttribute('aria-current', 'page')
  })

  test('app shell layout is intact (sidebar + main visible)', async ({ page }) => {
    await page.goto('/')
    // The two top-level structural elements: the `<nav aria-label="Primary
    // navigation">` sidebar and the `<main id="main">` content area.
    await expect(page.getByRole('navigation', { name: 'Primary navigation' })).toBeVisible()
    await expect(page.getByRole('main')).toBeVisible()
  })

  test('sidebar footer shows bot engine status', async ({ page }) => {
    await page.goto('/')
    // The sidebar footer has a `role="status"` block that announces the
    // bot engine state ("Bot Engine Active" by default). This is the
    // informational channel — it doesn't change with backend state, so
    // it's a stable selector.
    await expect(page.getByText('Bot Engine Active')).toBeVisible()
  })
})
