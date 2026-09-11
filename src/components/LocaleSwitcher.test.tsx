// components/LocaleSwitcher.test.tsx — W38-8 component tests.
//
// The switcher is a tiny native <select> bound to useTranslation. We
// mock the hook so the test stays focused on the rendered UI.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import LocaleSwitcher from './LocaleSwitcher'

// Mutable mock so each test can flip the locale without re-mocking.
const setLocale = vi.fn()
let currentLocale: 'en' | 'fr' = 'en'
vi.mock('@/hooks/useTranslation', () => ({
  useTranslation: () => ({
    locale: currentLocale,
    setLocale,
    t: (k: string) => k,
  }),
}))

beforeEach(() => {
  currentLocale = 'en'
  setLocale.mockReset()
})

afterEach(() => {
  cleanup()
})

describe('LocaleSwitcher', () => {
  it('renders a <select> with an accessible label', () => {
    render(<LocaleSwitcher />)
    const select = screen.getByLabelText('Select language')
    expect(select).toBeInTheDocument()
  })

  it('renders without crashing', () => {
    render(<LocaleSwitcher />)
    expect(screen.getByRole('combobox')).toBeInTheDocument()
  })

  it('renders the EN + FR options', () => {
    render(<LocaleSwitcher />)
    expect(screen.getByRole('option', { name: 'EN' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'FR' })).toBeInTheDocument()
  })

  it('reflects the current locale as the select value', () => {
    currentLocale = 'fr'
    render(<LocaleSwitcher />)
    const select = screen.getByLabelText('Select language') as HTMLSelectElement
    expect(select.value).toBe('fr')
  })

  it('calls setLocale when the user picks a different language', async () => {
    const user = userEvent.setup()
    render(<LocaleSwitcher />)
    await user.selectOptions(
      screen.getByLabelText('Select language'),
      'fr',
    )
    expect(setLocale).toHaveBeenCalledWith('fr')
  })
})
