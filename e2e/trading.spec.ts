import { test, expect } from '@playwright/test'

/**
 * Trading-flow E2E tests.
 *
 * Covers the three Portfolio sub-panels — Positions, Orders, and Trades &
 * Fills — that together represent the "post-trade" view of the workstation.
 * Each test asserts that:
 *
 *   1. The sidebar item is clickable and becomes the active panel
 *      (aria-current="page" — set in `Sidebar.tsx:230`).
 *   2. The page-area container survives the swap (the panel didn't crash
 *      the app).
 *   3. The panel content is non-empty — either a `<table>` / `.data-table`
 *      (when the bot has live data) OR an explicit empty-state message
 *      (when the bot is idle / disconnected).
 *
 * IMPORTANT: the backend may or may not be running. Tests never assert on
 * specific data values (row counts, P&L digits) — they assert STRUCTURE
 * (table-or-empty-state) so they pass whether the bot is live or paper-
 * idle.
 */

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  // The dashboard renders `<div class="page-area">` only after the client
  // has hydrated. Wait for it (default 30s — the dev server takes ~8s for
  // the first request, then the React tree mounts).
  await expect(page.locator('.page-area')).toBeVisible({ timeout: 45000 })
})

test.describe('Trading Flows', () => {
  test('can navigate to Positions panel', async ({ page }) => {
    // Sidebar.tsx:154 — the Positions item has `label: 'Positions'` and
    // no explicit aria-label on the button, so Playwright's accessible-
    // name lookup falls back to the visible label text. Use `.first()`
    // because some sub-panels (Command Center's compact PositionsPanel)
    // also render "Positions" text inside the main content.
    const positionsBtn = page.getByRole('button', { name: /^Positions$/ }).first()
    await positionsBtn.click()
    // aria-current="page" is the canonical "active panel" marker — set
    // conditionally in Sidebar.tsx:230 (`active === item.id ? 'page' :
    // undefined`). Asserting this proves the click registered and the
    // activeSection state in page.tsx flipped.
    await expect(positionsBtn).toHaveAttribute('aria-current', 'page')
    // The page-area wrapper persists across panel swaps — only its child
    // changes. Asserting it's still visible proves the layout didn't break.
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('can navigate to Orders panel', async ({ page }) => {
    // Orders is in the Portfolio group, no kbd shortcut (Sidebar.tsx:87).
    // Match the whole-word label to avoid colliding with the Command
    // Center's compact OrdersPanel "Orders" header.
    const ordersBtn = page.getByRole('button', { name: /^Orders$/ }).first()
    await ordersBtn.click()
    await expect(ordersBtn).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('can navigate to Trades panel', async ({ page }) => {
    // "Trades & Fills" is the full sidebar label (Sidebar.tsx:88). Match
    // on the prefix to be resilient against small wording changes.
    const tradesBtn = page.getByRole('button', { name: /Trades/ }).first()
    await tradesBtn.click()
    await expect(tradesBtn).toHaveAttribute('aria-current', 'page')
    await expect(page.locator('.page-area')).toBeVisible()
  })

  test('positions panel shows table or empty state', async ({ page }) => {
    // The PositionsPanel (src/components/PositionsPanel.tsx) renders a
    // `<table>` when `positions.length > 0`, otherwise an explicit empty-
    // state message ("No open positions"). One of those two MUST be
    // visible after navigation — proving the panel hydrated and the
    // empty-state branch works.
    await page.getByRole('button', { name: /^Positions$/ }).first().click()
    await expect(
      page.getByRole('button', { name: /^Positions$/ }).first(),
    ).toHaveAttribute('aria-current', 'page')

    // Use Promise.race to assert "table OR empty-state" without a fixed
    // sleep. Either resolves within the per-locator 30s timeout (more
    // than enough for the lazy-loaded panel to mount).
    const tableLocator = page.locator('table').first()
    const emptyLocator = page.getByText(/no open positions|no positions/i).first()
    await expect
      .poll(async () => (await tableLocator.count()) + (await emptyLocator.count()), {
        timeout: 15000,
        message: 'Positions panel neither rendered a table nor an empty state',
      })
      .toBeGreaterThan(0)
  })

  test('orders panel shows table or empty state', async ({ page }) => {
    // Same shape as the Positions test, but against OrdersPanel.tsx —
    // which renders a table when open_orders.length > 0, otherwise the
    // "No open orders" empty state.
    await page.getByRole('button', { name: /^Orders$/ }).first().click()
    await expect(
      page.getByRole('button', { name: /^Orders$/ }).first(),
    ).toHaveAttribute('aria-current', 'page')

    const tableLocator = page.locator('table').first()
    const emptyLocator = page.getByText(/no open orders|no orders/i).first()
    await expect
      .poll(async () => (await tableLocator.count()) + (await emptyLocator.count()), {
        timeout: 15000,
        message: 'Orders panel neither rendered a table nor an empty state',
      })
      .toBeGreaterThan(0)
  })

  test('trades panel shows table or empty state', async ({ page }) => {
    // TradesPanel.tsx renders a table when recent_trades.length > 0,
    // otherwise the "No trades yet" empty state.
    await page.getByRole('button', { name: /Trades/ }).first().click()
    await expect(
      page.getByRole('button', { name: /Trades/ }).first(),
    ).toHaveAttribute('aria-current', 'page')

    const tableLocator = page.locator('table').first()
    const emptyLocator = page.getByText(/no trades|no fills/i).first()
    await expect
      .poll(async () => (await tableLocator.count()) + (await emptyLocator.count()), {
        timeout: 15000,
        message: 'Trades panel neither rendered a table nor an empty state',
      })
      .toBeGreaterThan(0)
  })

  test('no uncaught page errors during panel navigation', async ({ page }) => {
    // Capture any uncaught JS exceptions during the trading-panel swap
    // sequence. A failed `fetch` in `useBot` is NOT a page error (it's
    // caught) — only genuine render / event-handler exceptions surface
    // here. This is the structural regression guard for the trading flow.
    const errors: string[] = []
    page.on('pageerror', (err) => errors.push(err.message))

    await page.getByRole('button', { name: /^Positions$/ }).first().click()
    await page.getByRole('button', { name: /^Orders$/ }).first().click()
    await page.getByRole('button', { name: /Trades/ }).first().click()
    // Land back on Positions.
    await page.getByRole('button', { name: /^Positions$/ }).first().click()

    // No uncaught errors should have accumulated. The lazy-loaded panel
    // chunks + the snapshot polling both happen async; give them a beat.
    await page.waitForTimeout(500)
    expect(errors, `Uncaught page errors: ${errors.join(', ')}`).toEqual([])
  })
})
