import { test, expect } from '@playwright/test'

/**
 * Responsive layout E2E tests.
 *
 * The dashboard has three responsive breakpoints that drive the sidebar
 * layout:
 *
 *   1. Mobile  (< 768px)  — Sidebar is translated off-screen; the
 *      TopStatusBar renders a hamburger button (`aria-label='Open
 *      navigation'`, `md:hidden` class in TopStatusBar.tsx:128) that
 *      flips `mobileNavOpen` in page.tsx, sliding the sidebar in over
 *      a backdrop. The collapse-toggle button is hidden.
 *
 *   2. Tablet  (768–1024px) — Hamburger hidden; sidebar visible but
 *      auto-collapsed (the matchMedia check at Sidebar.tsx:166-172
 *      matches at `max-width: 1024px`). Group labels are hidden; only
 *      the icons + kbd hints show. The collapse toggle button flips
 *      to 'Expand sidebar'.
 *
 *   3. Desktop (> 1024px) — Sidebar fully expanded with group labels
 *      and the "Collapse sidebar" button. Default Desktop Chrome
 *      viewport (1280×720) falls in this band.
 *
 * These tests use Playwright's per-test `test.use({ viewport })` to
 * resize the browser per scenario, then assert the right sidebar
 * shape renders. They never assert specific data values — only layout
 * structure.
 */

// Reusable viewport constants — kept inside the file so they're easy
// to grep + don't collide with a future global constant.
const MOBILE = { width: 375, height: 812 } as const // iPhone X-ish
const TABLET = { width: 768, height: 1024 } as const // iPad portrait
const DESKTOP = { width: 1920, height: 1080 } as const // large monitor

test.describe('Mobile viewport (375×812)', () => {
  test.use({ viewport: MOBILE })

  test.beforeEach(async ({ page }) => {
    await page.goto('/')
    await expect(page.locator('.page-area')).toBeVisible({ timeout: 45000 })
  })

  test('hamburger menu button is visible', async ({ page }) => {
    // TopStatusBar.tsx:128 — the hamburger button has aria-label
    // 'Open navigation' and is only rendered below the `md` breakpoint
    // (768px). At 375px viewport it must be visible.
    const hamburger = page.getByRole('button', { name: 'Open navigation' })
    await expect(hamburger).toBeVisible()
  })

  test('sidebar is hidden off-screen until hamburger click', async ({ page }) => {
    // The sidebar nav has the `.sidebar` class and starts translated
    // off-screen (CSS `transform: translateX(-100%)` in globals.css).
    // It's still in the DOM (so React can mount + render it) but not
    // visible to the user.
    const nav = page.getByRole('navigation', { name: 'Primary navigation' })
    await expect(nav).toHaveAttribute('aria-label', 'Primary navigation')
    // The Positions button should not be visible before the hamburger
    // is clicked — it's inside the off-screen nav.
    const positionsBtn = page.getByRole('button', { name: /^Positions$/ }).first()
    // Use `not.toBeVisible()` with a brief settle so the layout settles.
    await expect(positionsBtn).not.toBeVisible()

    // Click the hamburger — the mobileOpen class should be added and
    // the nav should slide in.
    await page.getByRole('button', { name: 'Open navigation' }).click()
    await expect(positionsBtn).toBeVisible({ timeout: 5000 })
  })

  test('clicking hamburger then a nav item closes the sidebar', async ({ page }) => {
    await page.getByRole('button', { name: 'Open navigation' }).click()
    const positionsBtn = page.getByRole('button', { name: /^Positions$/ }).first()
    await expect(positionsBtn).toBeVisible({ timeout: 5000 })

    // Click a nav item — Sidebar.tsx:174 (handleSelect) calls
    // onMobileClose?.() which clears mobileNavOpen.
    await positionsBtn.click()
    await expect(positionsBtn).toHaveAttribute('aria-current', 'page')
    // After close, the nav slides back off-screen — proxy check via the
    // mobile backdrop disappearing.
    await expect(page.locator('.bg-black\\/60')).toHaveCount(0)
  })

  test('mobile backdrop dismisses the sidebar on click', async ({ page }) => {
    await page.getByRole('button', { name: 'Open navigation' }).click()
    // The backdrop (Sidebar.tsx:182) renders a fixed-position overlay
    // covering the screen behind the slid-in sidebar.
    const backdrop = page.locator('.bg-black\\/60').first()
    await expect(backdrop).toBeVisible()
    await backdrop.click()
    // After backdrop click — Sidebar's onClick clears mobileOpen.
    await expect(backdrop).toHaveCount(0)
  })

  test('Escape key closes the mobile sidebar', async ({ page }) => {
    await page.getByRole('button', { name: 'Open navigation' }).click()
    await expect(page.locator('.bg-black\\/60').first()).toBeVisible()
    // page.tsx:321 — Escape clears mobileNavOpen.
    await page.keyboard.press('Escape')
    await expect(page.locator('.bg-black\\/60')).toHaveCount(0)
  })
})

test.describe('Tablet viewport (768×1024)', () => {
  test.use({ viewport: TABLET })

  test.beforeEach(async ({ page }) => {
    await page.goto('/')
    await expect(page.locator('.page-area')).toBeVisible({ timeout: 45000 })
  })

  test('hamburger is hidden on tablet', async ({ page }) => {
    // At 768px exactly, the `md:hidden` class hides the hamburger
    // (Tailwind's `md` breakpoint is `min-width: 768px`, so 768 is
    // the first viewport where the hamburger disappears).
    await expect(page.getByRole('button', { name: 'Open navigation' })).toHaveCount(0)
  })

  test('sidebar is auto-collapsed at tablet width', async ({ page }) => {
    // Sidebar.tsx:166-172 — matchMedia('(max-width: 1024px)').matches
    // is true at 768px, so the collapse state initialises to `true`.
    // The collapse button's aria-label flips to 'Expand sidebar'.
    const expandBtn = page.getByRole('button', { name: 'Expand sidebar' })
    await expect(expandBtn).toBeVisible()
    // Group labels (e.g. "Main") should be hidden when collapsed.
    const mainLabel = page
      .getByRole('navigation', { name: 'Primary navigation' })
      .locator('div')
      .filter({ hasText: /^Main$/ })
    await expect(mainLabel).toHaveCount(0)
  })

  test('clicking Expand restores group labels', async ({ page }) => {
    await page.getByRole('button', { name: 'Expand sidebar' }).click()
    // After expand, the button label flips back + the group labels
    // re-render.
    await expect(page.getByRole('button', { name: 'Collapse sidebar' })).toBeVisible()
    await expect(page.locator('text=Main').first()).toBeVisible()
  })

  test('collapsed sidebar still allows click navigation', async ({ page }) => {
    // When collapsed, the sidebar-item buttons render with `title`
    // attribute (Sidebar.tsx:266 — `title={collapsed ? ... : undefined}`).
    // Use the title-based selector to find the Positions button.
    const positionsBtn = page.getByRole('button').filter({ hasTitle: 'Positions (4)' })
    await positionsBtn.click()
    await expect(positionsBtn).toHaveAttribute('aria-current', 'page')
  })
})

test.describe('Desktop viewport (1920×1080)', () => {
  test.use({ viewport: DESKTOP })

  test.beforeEach(async ({ page }) => {
    await page.goto('/')
    await expect(page.locator('.page-area')).toBeVisible({ timeout: 45000 })
  })

  test('sidebar is fully expanded at desktop width', async ({ page }) => {
    // At 1920px the matchMedia max-width:1024px check is false, so
    // the collapse state initialises to `false` — the sidebar shows
    // group labels + the "Collapse sidebar" button.
    await expect(page.getByRole('button', { name: 'Collapse sidebar' })).toBeVisible()
    await expect(page.locator('text=Main').first()).toBeVisible()
    await expect(page.locator('text=Markets').first()).toBeVisible()
    await expect(page.locator('text=System').first()).toBeVisible()
  })

  test('all eight nav group labels render at desktop', async ({ page }) => {
    // NAV_GROUPS in Sidebar.tsx:63 has 8 entries: main, markets,
    // portfolio, capital, strategies, intelligence, analytics, system.
    // Each renders its visible label when the sidebar is expanded.
    for (const label of [
      'Main',
      'Markets',
      'Portfolio',
      'Capital',
      'Strategies',
      'Intelligence',
      'Analytics',
      'System',
    ]) {
      await expect(page.locator(`text=${label}`).first()).toBeVisible()
    }
  })

  test('main content area is wider than sidebar at desktop', async ({ page }) => {
    // Sanity check — at 1920px the layout should give the main
    // content more horizontal room than the sidebar.
    const sidebar = page.getByRole('navigation', { name: 'Primary navigation' })
    const main = page.getByRole('main')
    const sidebarBox = await sidebar.boundingBox()
    const mainBox = await main.boundingBox()
    expect(sidebarBox).toBeTruthy()
    expect(mainBox).toBeTruthy()
    if (sidebarBox && mainBox) {
      expect(mainBox.width).toBeGreaterThan(sidebarBox.width)
    }
  })
})

test.describe('Viewport transitions', () => {
  // Reusing the default Desktop Chrome viewport (1280×720) for the
  // "before" state, then resizing — verifies the Sidebar's matchMedia
  // listener fires on viewport change (Sidebar.tsx:170 addEventListener).
  test('resizing from desktop to mobile re-hides the sidebar', async ({ page }) => {
    await page.goto('/')
    await expect(page.locator('.page-area')).toBeVisible({ timeout: 45000 })
    // Desktop state: hamburger hidden, collapse button = Collapse sidebar.
    await expect(page.getByRole('button', { name: 'Open navigation' })).toHaveCount(0)

    // Resize to mobile.
    await page.setViewportSize(MOBILE)
    // Brief settle so the matchMedia listener fires + React re-renders.
    await page.waitForTimeout(500)
    // Hamburger should now be visible.
    await expect(page.getByRole('button', { name: 'Open navigation' })).toBeVisible()
  })

  test('resizing from mobile to desktop re-shows the sidebar', async ({ page }) => {
    await page.setViewportSize(MOBILE)
    await page.goto('/')
    await expect(page.locator('.page-area')).toBeVisible({ timeout: 45000 })
    // Mobile state: hamburger visible.
    await expect(page.getByRole('button', { name: 'Open navigation' })).toBeVisible()

    // Resize to desktop.
    await page.setViewportSize(DESKTOP)
    await page.waitForTimeout(500)
    // Hamburger should disappear, collapse button = Collapse sidebar.
    await expect(page.getByRole('button', { name: 'Open navigation' })).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Collapse sidebar' })).toBeVisible()
  })
})
