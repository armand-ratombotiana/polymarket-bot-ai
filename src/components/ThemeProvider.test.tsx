// components/ThemeProvider.test.tsx — W42-2 component test.
//
// `ThemeProvider` is a thin wrapper around `next-themes`'s
// `NextThemesProvider` configured for the workstation (class attribute,
// dark default, no system, instant transitions). The wrapper itself
// adds no behaviour beyond forwarding `children`, so the minimal
// contract to assert is "renders children" (i.e. the provider does
// not swallow them and does not crash during mount in jsdom). Deeper
// theme-toggling assertions live in `ThemeToggle.test.tsx`.

import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import ThemeProvider from './ThemeProvider'

describe('ThemeProvider', () => {
  it('renders children', () => {
    render(
      <ThemeProvider>
        <div>Test Child</div>
      </ThemeProvider>,
    )
    expect(screen.getByText('Test Child')).toBeInTheDocument()
  })
})
