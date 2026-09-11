import { test, expect } from '@playwright/test'

/**
 * Theme toggle E2E tests.
 *
 * The ThemeToggle button (`src/components/ThemeToggle.tsx`) lives in the
 * TopStatusBar right-hand action cluster. It calls `setTheme(isDark ?
 * 'light' : 'dark')` from `next-themes`, which writes the new theme to
 * `localStorage` AND adds / removes the `dark` / `light` CSS class on
 * the `<html>` element.
 *
 * These tests verify:
 *   1. The toggle button renders (ThemeToggle returns null on SSR, so
 *      this implicitly proves client-side mount completed).
 *   2. Clicking the toggle flips the `<html>` class between dark / light.
 *   3. Clicking again restores the previous theme (round-trip).
 *
 * The default theme is `dark` (ThemeProvider.tsx `defaultTheme='dark'`),
 * so the first toggle goes dark → light, the second light → dark.
 */

test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await expect(page.locator('.page-area')).toBeVisible({ timeout: 45000 })
})

test.describe('Theme Toggle', () => {
  test('theme toggle button is present in the top status bar', async ({ page }) => {
    // ThemeToggle renders null during SSR (see ThemeToggle.tsx:47 —
    // `if (!mounted) return null`). The dashboard's `beforeEach`
    // already waits for `.page-area`, which guarantees client mount.
    // Use the accessible name — the button's aria-label is "Switch to
    // light mode" when dark is active (default) and "Switch to dark
    // mode" when light is active.
    const toggleDark = page.getByRole('button', { name: 'Switch to light mode' })
    const toggleLight = page.getByRole('button', { name: 'Switch to dark mode' })
    // Exactly one of the two labels should be present (depending on
    // the current theme).
    const darkCount = await toggleDark.count()
    const lightCount = await toggleLight.count()
    expect(darkCount + lightCount).toBeGreaterThan(0)
  })

  test('clicking the toggle flips the <html> theme class', async ({ page }) => {
    // next-themes writes the active theme as a CSS class on <html>
    // (default behaviour — `attribute="class"` in the ThemeProvider).
    // The `<html>` starts with either `dark` or `light` (after mount).
    const html = page.locator('html')

    // Snapshot the starting class so we can verify the flip.
    const initialClasses = await html.getAttribute('class')
    const initialIsDark = (initialClasses ?? '').includes('dark')

    // Click the appropriate toggle button — the aria-label tells us
    // which direction the click will go.
    const targetLabel = initialIsDark ? 'Switch to light mode' : 'Switch to dark mode'
    const toggle = page.getByRole('button', { name: targetLabel }).first()
    await expect(toggle).toBeVisible()
    await toggle.click()

    // The <html> class should now contain the OPPOSITE theme label.
    // Use expect.poll because next-themes writes the class via a
    // layout effect — it's synchronous in practice, but polling
    // avoids a race on slow CI runners.
    await expect
      .poll(async () => {
        const cls = (await html.getAttribute('class')) ?? ''
        return initialIsDark ? cls.includes('light') : cls.includes('dark')
      })
      .toBe(true)
  })

  test('toggling twice returns to the original theme', async ({ page }) => {
    // Round-trip: dark → light → dark (or light → dark → light,
    // depending on the persisted starting state).
    const html = page.locator('html')
    const initialClasses = (await html.getAttribute('class')) ?? ''

    const firstIsDark = initialClasses.includes('dark')
    const firstLabel = firstIsDark ? 'Switch to light mode' : 'Switch to dark mode'
    await page.getByRole('button', { name: firstLabel }).first().click()

    // Wait for the class flip to land before the second click.
    await expect
      .poll(async () => {
        const cls = (await html.getAttribute('class')) ?? ''
        return firstIsDark ? cls.includes('light') : cls.includes('dark')
      })
      .toBe(true)

    // After the flip, the aria-label inverts — the button now offers
    // to switch BACK to the original theme.
    const secondLabel = firstIsDark ? 'Switch to dark mode' : 'Switch to light mode'
    await page.getByRole('button', { name: secondLabel }).first().click()

    // Verify the class round-tripped.
    await expect
      .poll(async () => {
        const cls = (await html.getAttribute('class')) ?? ''
        return firstIsDark ? cls.includes('dark') : cls.includes('light')
      })
      .toBe(true)
  })

  test('theme persists across page reloads', async ({ page }) => {
    // next-themes persists the choice in localStorage under the
    // `theme` key (default). After a reload, the ThemeProvider reads
    // it and re-applies the class on the very first paint (via the
    // inline script injected before React hydrates).
    const html = page.locator('html')
    const firstIsDark = ((await html.getAttribute('class')) ?? '').includes('dark')

    // Flip the theme.
    const firstLabel = firstIsDark ? 'Switch to light mode' : 'Switch to dark mode'
    await page.getByRole('button', { name: firstLabel }).first().click()

    // Wait for the class flip to land + give next-themes a beat to
    // flush the localStorage write (it's synchronous, but the class
    // mutation is via a layout effect).
    await expect
      .poll(async () => {
        const cls = (await html.getAttribute('class')) ?? ''
        return firstIsDark ? cls.includes('light') : cls.includes('dark')
      })
      .toBe(true)

    // Reload.
    await page.reload()
    await expect(page.locator('.page-area')).toBeVisible({ timeout: 45000 })

    // The post-reload <html> class should match what we set BEFORE the
    // reload — proves persistence.
    await expect
      .poll(async () => {
        const cls = (await html.getAttribute('class')) ?? ''
        return firstIsDark ? cls.includes('light') : cls.includes('dark')
      })
      .toBe(true)

    // Clean up: flip back to the original so other tests start from
    // a deterministic state. (Playwright launches a fresh browser
    // context per test by default, so this is belt-and-suspenders.)
    const restoreLabel = firstIsDark ? 'Switch to dark mode' : 'Switch to light mode'
    const restoreBtn = page.getByRole('button', { name: restoreLabel }).first()
    if (await restoreBtn.isVisible().catch(() => false)) {
      await restoreBtn.click()
    }
  })

  test('toggling theme does not crash the page or any panel', async ({ page }) => {
    // Theme re-themes every CSS variable consumer (cards, pills,
    // charts). A regression in any one of them could throw. Capture
    // uncaught page errors during the toggle + a brief settle window.
    const errors: string[] = []
    page.on('pageerror', (err) => errors.push(err.message))

    const html = page.locator('html')
    const firstIsDark = ((await html.getAttribute('class')) ?? '').includes('dark')
    const label = firstIsDark ? 'Switch to light mode' : 'Switch to dark mode'
    await page.getByRole('button', { name: label }).first().click()
    // Brief settle so any themed chart re-render completes.
    await page.waitForTimeout(500)

    // Restore the original theme so other tests aren't affected.
    const restoreLabel = firstIsDark ? 'Switch to dark mode' : 'Switch to light mode'
    const restoreBtn = page.getByRole('button', { name: restoreLabel }).first()
    if (await restoreBtn.isVisible().catch(() => false)) {
      await restoreBtn.click()
    }
    await page.waitForTimeout(300)

    expect(errors, `Uncaught page errors: ${errors.join(', ')}`).toEqual([])
  })
})
