import { test, expect } from '@playwright/test'

/**
 * Sidebar navigation tests.
 *
 * Covers four interaction surfaces:
 *  1. Click navigation — every sidebar item, when clicked, becomes the
 *     active panel. Asserted via `aria-current="page"` on the button (set
 *     in `Sidebar.tsx:230`) — NOT via panel content, because some panels
 *     load via `next/dynamic` and their inner text depends on backend
 *     data that may be empty when the bot is idle.
 *  2. Keyboard shortcuts — `KB_MAP` in `page.tsx:146` binds digits 1-8
 *     to nav sections. The handler at `page.tsx:247` ignores keypresses
 *     originating in input/textarea elements and respects modifier keys.
 *  3. Collapse/expand — the sidebar has a collapse toggle button with
 *     `aria-label` 'Collapse sidebar' / 'Expand sidebar' (Sidebar.tsx:197).
 *     Collapsing hides the group labels (`!collapsed &&` block at line 212).
 *  4. Mobile sidebar — the TopStatusBar renders an "Open navigation"
 *     button (aria-label, TopStatusBar.tsx:131) visible only below the
 *     `md` breakpoint (`md:hidden` class). Clicking it sets `mobileOpen`
 *     which adds the `mobile-open` class to the `<nav>` (Sidebar.tsx:160).
 *
 * The shared `beforeEach` waits for the `.page-area` container — proxy for
 * "the React tree has hydrated and at least the default panel mounted".
 */

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await expect(page.locator('.page-area')).toBeVisible({ timeout: 45000 })
})

// Sidebar items that have a keyboard shortcut (digits 1-8). These are the
// subset of nav items where `KB_MAP` in page.tsx:146 binds a keypress.
// Items WITHOUT a kbd (Orders, Trades, Capital Allocator, AI/ML, Copilot,
// Shadow, ML Validation, Backtest Lab, Attribution, Execution Quality,
// Closed Positions, Data Explorer, Observability, Retention, Decision
// Ledger, Safety Gate) are still clickable but have no shortcut.
const SHORTCUT_ITEMS: Array<{ key: string; label: string }> = [
  { key: '1', label: 'Command Center' },
  { key: '2', label: 'Live Books' },
  { key: '3', label: 'Screener' },
  { key: '4', label: 'Positions' },
  { key: '5', label: 'Strategy Registry' },
  { key: '6', label: 'Arbitrage' },
  { key: '7', label: 'Deep Analysis' },
  { key: '8', label: 'Performance' },
]

test.describe('Sidebar click navigation', () => {
  for (const item of SHORTCUT_ITEMS) {
    test(`clicking "${item.label}" makes it the active panel`, async ({ page }) => {
      await page.goto('/')
      const navButton = page.getByRole('button', { name: new RegExp(item.label) }).first()
      await navButton.click()
      // aria-current="page" is set on the active sidebar button
      // (Sidebar.tsx:230 — `aria-current={active === item.id ? 'page' : undefined}`).
      await expect(navButton).toHaveAttribute('aria-current', 'page')
    })
  }

  test('clicking a non-shortcut item (Orders) still activates it', async ({ page }) => {
    await page.goto('/')
    // "Orders" is in the Portfolio group, no kbd shortcut — but clicking
    // should still switch the panel and mark itself active.
    const ordersBtn = page.getByRole('button', { name: /^Orders$/ }).first()
    await ordersBtn.click()
    await expect(ordersBtn).toHaveAttribute('aria-current', 'page')
  })

  test('clicking Safety Gate (last item in System group) activates it', async ({ page }) => {
    await page.goto('/')
    const safetyBtn = page.getByRole('button', { name: /Safety Gate/ }).first()
    await safetyBtn.click()
    await expect(safetyBtn).toHaveAttribute('aria-current', 'page')
  })
})

test.describe('Keyboard shortcuts', () => {
  for (const item of SHORTCUT_ITEMS) {
    test(`pressing "${item.key}" activates "${item.label}"`, async ({ page }) => {
      await page.goto('/')
      // Focus the body so the keydown lands on `window` (the handler in
      // page.tsx:247 is attached to `window`). Playwright's `keyboard.press`
      // already routes to the page's focused element; if nothing is
      // focused, the document body receives the event and the window-level
      // listener picks it up.
      await page.body().click()
      await page.keyboard.press(item.key)
      // The corresponding sidebar button should be marked active.
      const navButton = page
        .getByRole('button', { name: new RegExp(item.label) })
        .first()
      await expect(navButton).toHaveAttribute('aria-current', 'page')
    })
  }

  test('shortcut is ignored when typing in an input', async ({ page }) => {
    await page.goto('/')
    // Open the strategy config modal — it contains a text input.
    // The shortcut handler at page.tsx:249 bails when `e.target` is an
    // HTMLInputElement or HTMLTextAreaElement. Pressing 'c' opens the
    // config modal; we then focus an input and verify '1' does NOT
    // switch to the Command Center (the previously-active section
    // remains active).
    await page.keyboard.press('4') // Positions
    await expect(
      page.getByRole('button', { name: /Positions/ }).first(),
    ).toHaveAttribute('aria-current', 'page')

    // Open config modal (pressing 'c' is bound to toggling configOpen).
    await page.keyboard.press('c')
    // The config modal renders a StrategyConfigModal. Find any visible
    // text input inside it. If the modal didn't open, this test would
    // time out — that's acceptable: it surfaces a real regression.
    const input = page.locator('input[type="text"], input[type="number"], textarea').first()
    await expect(input).toBeVisible({ timeout: 10000 })
    await input.fill('1') // type the digit '1' INTO the input
    // Positions should STILL be active — the '1' keypress was consumed
    // by the input, not routed to the window-level nav handler.
    await expect(
      page.getByRole('button', { name: /Positions/ }).first(),
    ).toHaveAttribute('aria-current', 'page')
    // Clean up.
    await page.keyboard.press('Escape')
  })

  test('modifier-prefixed shortcuts do not navigate (Ctrl+1)', async ({ page }) => {
    await page.goto('/')
    await page.keyboard.press('4') // Positions — establish a non-default active section
    await expect(
      page.getByRole('button', { name: /Positions/ }).first(),
    ).toHaveAttribute('aria-current', 'page')
    // The handler at page.tsx:250 bails on `e.metaKey || e.ctrlKey || e.altKey`.
    // Ctrl+1 should NOT navigate to Command Center.
    await page.keyboard.press('Control+1')
    await expect(
      page.getByRole('button', { name: /Positions/ }).first(),
    ).toHaveAttribute('aria-current', 'page')
  })
})

test.describe('Sidebar collapse / expand', () => {
  test('collapse button hides group labels', async ({ page }) => {
    await page.goto('/')
    // Initially expanded on Desktop Chrome (1280px viewport > 1024px
    // breakpoint in the matchMedia check at Sidebar.tsx:136).
    const collapseBtn = page.getByRole('button', { name: 'Collapse sidebar' })
    await expect(collapseBtn).toBeVisible()
    // Group label "Main" should be visible while expanded.
    await expect(page.locator('text=Main').first()).toBeVisible()

    await collapseBtn.click()

    // After collapse, the button's aria-label flips to 'Expand sidebar'
    // (Sidebar.tsx:197 — `aria-label={collapsed ? 'Expand sidebar' :
    // 'Collapse sidebar'}`).
    await expect(page.getByRole('button', { name: 'Expand sidebar' })).toBeVisible()
    // Group labels are conditionally rendered (`!collapsed && <div>Main...`)
    // so they should disappear from the DOM when collapsed.
    // Use a filtered locator to avoid the skip-link / app-name false
    // positives — the group label is uppercase-styled text in a div
    // with role=listitem's first child.
    const mainGroupLabel = page
      .getByRole('navigation', { name: 'Primary navigation' })
      .locator('div')
      .filter({ hasText: /^Main$/ })
    await expect(mainGroupLabel).toHaveCount(0)
  })

  test('expand button restores group labels', async ({ page }) => {
    await page.goto('/')
    // Collapse, then expand, verify labels return.
    await page.getByRole('button', { name: 'Collapse sidebar' }).click()
    await expect(page.getByRole('button', { name: 'Expand sidebar' })).toBeVisible()

    await page.getByRole('button', { name: 'Expand sidebar' }).click()
    await expect(page.getByRole('button', { name: 'Collapse sidebar' })).toBeVisible()
    await expect(page.locator('text=Main').first()).toBeVisible()
  })

  test('collapsed sidebar still allows click navigation', async ({ page }) => {
    await page.goto('/')
    // Collapse the sidebar.
    await page.getByRole('button', { name: 'Collapse sidebar' }).click()
    // When collapsed, the sidebar-item buttons still render — they just
    // don't show the text label. The button's `title` attribute is set
    // to the full label when collapsed (Sidebar.tsx:231 — `title={collapsed
    // ? \`${item.label}${item.kbd ? ` (${item.kbd})` : ''}\` : undefined}`).
    // Use the title-based selector to find the Positions button.
    const positionsBtn = page
      .getByRole('button')
      .filter({ hasTitle: 'Positions (4)' })
    await positionsBtn.click()
    await expect(positionsBtn).toHaveAttribute('aria-current', 'page')
  })
})

test.describe('Mobile sidebar (hamburger menu)', () => {
  // Use a small viewport so the `md:hidden` mobile-only nav button in
  // TopStatusBar is visible.
  test.use({ viewport: { width: 375, height: 812 } }) // iPhone X-ish

  test('hamburger button opens the mobile sidebar', async ({ page }) => {
    await page.goto('/')
    // The mobile nav button (TopStatusBar.tsx:128) has aria-label
    // 'Open navigation' and is only rendered below the `md` breakpoint.
    const hamburger = page.getByRole('button', { name: 'Open navigation' })
    await expect(hamburger).toBeVisible()

    // Click it — should set `mobileNavOpen=true` in page.tsx:165, which
    // passes `mobileOpen` to Sidebar, which adds the `mobile-open` class
    // to the `<nav>` element (Sidebar.tsx:160).
    await hamburger.click()
    // The nav element should now be visible (it was translated off-screen
    // when closed). Wait for the Positions button to be visible as the
    // signal that the sidebar slide-in animation has completed.
    await expect(
      page.getByRole('button', { name: /Positions/ }).first(),
    ).toBeVisible({ timeout: 5000 })
  })

  test('clicking a sidebar item closes the mobile sidebar', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('button', { name: 'Open navigation' }).click()
    // Sidebar should be visible now.
    await expect(
      page.getByRole('navigation', { name: 'Primary navigation' }),
    ).toBeVisible()
    // Click an item — `handleSelect` in Sidebar.tsx:143 calls
    // `onChange(id)` then `onMobileClose?.()` which clears `mobileNavOpen`.
    await page.getByRole('button', { name: /Positions/ }).first().click()
    // After selection, the active item is Positions, and the mobile
    // sidebar should auto-close (translate off-screen).
    await expect(
      page.getByRole('button', { name: /Positions/ }).first(),
    ).toHaveAttribute('aria-current', 'page')
    // The mobile backdrop (Sidebar.tsx:151) is rendered only when
    // `mobileOpen` is true. After close it should disappear.
    await expect(page.locator('.bg-black\\/60')).toHaveCount(0)
  })

  test('Escape key closes the mobile sidebar', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('button', { name: 'Open navigation' }).click()
    await expect(
      page.getByRole('navigation', { name: 'Primary navigation' }),
    ).toBeVisible()
    // The keyboard handler at page.tsx:264 clears `mobileNavOpen` on
    // Escape.
    await page.keyboard.press('Escape')
    // Backdrop should be gone — proxy signal that mobileOpen=false.
    await expect(page.locator('.bg-black\\/60')).toHaveCount(0)
  })
})
