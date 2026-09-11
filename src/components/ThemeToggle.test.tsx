// src/components/ThemeToggle.test.tsx — W13-4 dark/light theme switcher tests.
//
// Behaviour under test:
//   1. SSR snapshot — the toggle renders `null` while `mounted === false`
//      (i.e. during server render, where useEffect never fires). This
//      is the hydration-mismatch guard mandated by next-themes docs:
//      rendering the icon before the provider has resolved the theme
//      would emit a `🌙` (or `☀️`) that may mismatch what the client
//      picks post-hydration, which React flags as an error.
//   2. Post-mount — the toggle renders a real <button> with the
//      current-theme icon (☀️ for dark, 🌙 for light).
//   3. Click — clicking the button flips the active theme on
//      `document.documentElement` (the class next-themes toggles).
//
// Test isolation: each test resets `document.documentElement.className`
// and clears localStorage so a previous test's theme can't leak into the
// next. The provider's `defaultTheme` prop lets us start each test in a
// known state without relying on localStorage persistence.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import { renderToStaticMarkup } from 'react-dom/server'
import userEvent from '@testing-library/user-event'
import { ThemeProvider as NextThemesProvider } from 'next-themes'
import ThemeToggle from './ThemeToggle'

// Helper: wrap the toggle in a real NextThemesProvider so `useTheme()`
// resolves against the actual next-themes context (rather than the
// no-op default context, which would always report `theme === undefined`
// and make the click test meaningless).
function renderWithProvider(initialTheme: 'dark' | 'light' = 'dark') {
  return render(
    <NextThemesProvider
      attribute="class"
      defaultTheme={initialTheme}
      enableSystem={false}
      disableTransitionOnChange
    >
      <ThemeToggle />
    </NextThemesProvider>,
  )
}

describe('ThemeToggle (W13-4)', () => {
  beforeEach(() => {
    // Reset the <html> class list so a previous test's "light" class
    // doesn't bleed into the next test's defaultTheme="dark" assertion.
    document.documentElement.className = ''
    // next-themes persists the chosen theme in localStorage under the
    // "theme" key. Clearing it lets `defaultTheme` win on every mount.
    window.localStorage.clear()
  })

  afterEach(() => {
    document.documentElement.className = ''
    window.localStorage.clear()
    vi.restoreAllMocks()
  })

  it('renders null before mount (SSR snapshot)', () => {
    // Use renderToStaticMarkup (server render) so useEffect never
    // fires — this is exactly what Next.js does during SSR. The
    // `mounted` flag stays false, so the component short-circuits to
    // null. We assert that no <button> element is emitted in the
    // server-rendered HTML (the NextThemesProvider itself may emit
    // an inline script tag for color-scheme detection, but the
    // ThemeToggle child should contribute zero markup).
    const html = renderToStaticMarkup(
      <NextThemesProvider
        attribute="class"
        defaultTheme="dark"
        enableSystem={false}
      >
        <ThemeToggle />
      </NextThemesProvider>,
    )
    expect(html).not.toContain('<button')
    expect(html).not.toContain('☀️')
    expect(html).not.toContain('🌙')
  })

  it('renders a button after mount', () => {
    renderWithProvider('dark')
    const btn = screen.getByRole('button')
    expect(btn).toBeInTheDocument()
    // Default theme is dark, so the button shows the sun icon
    // (clicking would switch to light) and announces the target
    // state, not the current state.
    expect(btn).toHaveAttribute('aria-label', 'Switch to light mode')
    expect(btn).toHaveTextContent('☀️')
    // aria-pressed reflects whether dark mode (the toggle's "on"
    // state) is currently active.
    expect(btn).toHaveAttribute('aria-pressed', 'true')
  })

  it('clicking the button toggles the theme from dark to light', async () => {
    const user = userEvent.setup()
    renderWithProvider('dark')

    // Pre-click: html class should contain "dark" (set by the provider
    // on mount when defaultTheme="dark").
    expect(document.documentElement.classList.contains('dark')).toBe(true)

    const btn = screen.getByRole('button', { name: /switch to light mode/i })
    await act(async () => {
      await user.click(btn)
    })

    // Post-click: provider should have flipped the class to "light".
    expect(document.documentElement.classList.contains('light')).toBe(true)
    expect(document.documentElement.classList.contains('dark')).toBe(false)

    // The button should now show the moon icon and announce the
    // inverse target (switch back to dark).
    const btnAfter = screen.getByRole('button')
    expect(btnAfter).toHaveTextContent('🌙')
    expect(btnAfter).toHaveAttribute('aria-label', 'Switch to dark mode')
    expect(btnAfter).toHaveAttribute('aria-pressed', 'false')
  })

  it('clicking again toggles back from light to dark', async () => {
    const user = userEvent.setup()
    renderWithProvider('light')

    // Pre-click: light is active.
    expect(document.documentElement.classList.contains('light')).toBe(true)

    const btn = screen.getByRole('button', { name: /switch to dark mode/i })
    await act(async () => {
      await user.click(btn)
    })

    expect(document.documentElement.classList.contains('dark')).toBe(true)
    expect(document.documentElement.classList.contains('light')).toBe(false)
  })

  it('persists the chosen theme to localStorage so reload keeps it', async () => {
    const user = userEvent.setup()
    renderWithProvider('dark')

    const btn = screen.getByRole('button', { name: /switch to light mode/i })
    await act(async () => {
      await user.click(btn)
    })

    // next-themes persists the active theme under the "theme" key.
    expect(window.localStorage.getItem('theme')).toBe('light')
  })
})
