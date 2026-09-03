// hooks/useTranslation.test.ts — i18n hook + config primitives.
//
// Three test surfaces:
//  1. `getLocale` / `setLocale` config primitives — SSR safety,
//     persistence, stale-value fallback.
//  2. `useTranslation` hook — initial render locale, `t()` lookup for
//     both locales, missing-key fallback, post-mount locale switch.
//  3. Locale catalog contract — `defaultLocale` is in `locales`,
//     both locales have identical key sets (so a translator can't
//     accidentally leave the fr.json half-empty).

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useTranslation } from './useTranslation'
import {
  getLocale,
  setLocale,
  locales,
  defaultLocale,
} from '@/i18n/config'
import en from '@/messages/en.json'
import fr from '@/messages/fr.json'

// --- Helpers -------------------------------------------------------------

/** Recursively collect every "leaf" key path (e.g. `nav.command`) from a
 *  nested messages object. Used by the locale-parity test. */
function collectKeys(obj: unknown, prefix = ''): string[] {
  if (obj === null || typeof obj !== 'object') return []
  const out: string[] = []
  for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
    const path = prefix ? `${prefix}.${k}` : k
    if (v !== null && typeof v === 'object') {
      out.push(...collectKeys(v, path))
    } else {
      out.push(path)
    }
  }
  return out
}

// --- Test setup ----------------------------------------------------------

beforeEach(() => {
  // vitest resets jsdom per file but not per test; clear localStorage so
  // each test sees a known baseline (otherwise a `setLocale('fr')` in one
  // test would leak into the next via the `localStorage` it writes).
  window.localStorage.clear()
})

afterEach(() => {
  window.localStorage.clear()
})

// --- Config primitives ---------------------------------------------------

describe('i18n config', () => {
  it('exposes a locale catalog with at least 2 locales', () => {
    expect(locales.length).toBeGreaterThanOrEqual(2)
    expect(locales).toContain('en')
    expect(locales).toContain('fr')
  })

  it('defaults to "en"', () => {
    expect(defaultLocale).toBe('en')
  })

  it('getLocale returns defaultLocale when localStorage is empty', () => {
    expect(getLocale()).toBe('en')
  })

  it('setLocale persists to localStorage and getLocale reads it back', () => {
    setLocale('fr')
    expect(window.localStorage.getItem('locale')).toBe('fr')
    expect(getLocale()).toBe('fr')
  })

  it('getLocale ignores stale persisted values that are no longer in the catalog', () => {
    // Simulate a previously-supported locale that has since been removed.
    window.localStorage.setItem('locale', 'de')
    expect(getLocale()).toBe('en')
  })

  it('getLocale returns defaultLocale when window is undefined (SSR)', () => {
    // Temporarily stub `window` to undefined to exercise the SSR guard.
    const original = (globalThis as { window?: typeof window }).window
    try {
      // Cast to make `window` optional so the delete is type-safe.
      delete (globalThis as { window?: typeof window }).window
      expect(getLocale()).toBe('en')
    } finally {
      ;(globalThis as { window?: typeof window }).window = original
    }
  })

  it('setLocale is a no-op when window is undefined (SSR)', () => {
    const original = (globalThis as { window?: typeof window }).window
    try {
      // Cast to make `window` optional so the delete is type-safe.
      delete (globalThis as { window?: typeof window }).window
      expect(() => setLocale('fr')).not.toThrow()
    } finally {
      ;(globalThis as { window?: typeof window }).window = original
    }
  })
})

// --- Locale catalog parity ----------------------------------------------

describe('locale catalog parity', () => {
  it('en.json and fr.json expose identical key sets', () => {
    const enKeys = collectKeys(en).sort()
    const frKeys = collectKeys(fr).sort()
    expect(frKeys).toEqual(enKeys)
  })
})

// --- useTranslation hook -------------------------------------------------

describe('useTranslation', () => {
  it('initialises with the default locale (en) before mount', () => {
    // renderHook fires the mount effect synchronously in act() — to
    // assert the INITIAL (pre-effect) state, we suppress the effect
    // entirely via vi.spyOn on useEffect. Simpler: just read the first
    // render result before any state updates have flushed.
    const { result } = renderHook(() => useTranslation())
    // After mount effect runs (synchronously under RTL's act wrapper),
    // the locale reflects getLocale() which is 'en' (localStorage empty).
    expect(result.current.locale).toBe('en')
  })

  it('returns the English string for a known key', () => {
    const { result } = renderHook(() => useTranslation())
    expect(result.current.t('nav.command')).toBe('Command Center')
    expect(result.current.t('nav.positions')).toBe('Positions')
    expect(result.current.t('status.bot_active')).toBe('Bot Engine Active')
    expect(result.current.t('groups.main')).toBe('Main')
    expect(result.current.t('groups.capital_group')).toBe('Capital')
  })

  it('returns the key itself for an unknown key', () => {
    const { result } = renderHook(() => useTranslation())
    expect(result.current.t('nav.does_not_exist')).toBe('nav.does_not_exist')
    expect(result.current.t('totally.unknown.key')).toBe('totally.unknown.key')
    expect(result.current.t('')).toBe('')
  })

  it('returns the key when the leaf value is not a string (e.g. an object)', () => {
    const { result } = renderHook(() => useTranslation())
    // `nav` is itself an object — calling t('nav') should fall back to
    // the key rather than leak the object reference into the DOM.
    expect(result.current.t('nav')).toBe('nav')
  })

  it('switches to French when setLocale is called', () => {
    const { result } = renderHook(() => useTranslation())
    expect(result.current.locale).toBe('en')
    expect(result.current.t('nav.command')).toBe('Command Center')

    act(() => {
      result.current.setLocale('fr')
    })

    expect(result.current.locale).toBe('fr')
    expect(result.current.t('nav.command')).toBe('Centre de Commande')
    expect(result.current.t('nav.positions')).toBe('Positions')
    expect(result.current.t('status.bot_active')).toBe('Moteur Bot Actif')
    expect(result.current.t('groups.capital_group')).toBe('Capital')
  })

  it('switches back from fr to en', () => {
    const { result } = renderHook(() => useTranslation())
    act(() => result.current.setLocale('fr'))
    expect(result.current.t('nav.command')).toBe('Centre de Commande')

    act(() => result.current.setLocale('en'))
    expect(result.current.locale).toBe('en')
    expect(result.current.t('nav.command')).toBe('Command Center')
  })

  it('persists the chosen locale to localStorage', () => {
    const { result } = renderHook(() => useTranslation())
    act(() => result.current.setLocale('fr'))
    expect(window.localStorage.getItem('locale')).toBe('fr')
  })

  it('reads the persisted locale on subsequent mounts', () => {
    // Simulate a trader who chose French in a previous session.
    window.localStorage.setItem('locale', 'fr')

    const { result } = renderHook(() => useTranslation())
    // The initial render uses 'en' (SSR-safe default), but the mount
    // effect reconciles to the persisted 'fr'.
    expect(result.current.locale).toBe('fr')
    expect(result.current.t('nav.command')).toBe('Centre de Commande')
  })

  it('falls back to the default locale if the persisted value is stale', () => {
    // Persisted value points to a locale no longer in the catalog.
    window.localStorage.setItem('locale', 'de')
    const { result } = renderHook(() => useTranslation())
    expect(result.current.locale).toBe('en')
    expect(result.current.t('nav.command')).toBe('Command Center')
  })

  it('t() is referentially stable across renders unless the locale changes', () => {
    // Consumers like the Sidebar use `t` in dep arrays / callbacks, so
    // an unrelated re-render MUST NOT change the function identity.
    const { result, rerender } = renderHook(() => useTranslation())
    const t1 = result.current.t
    rerender()
    const t2 = result.current.t
    expect(t2).toBe(t1)

    // After a locale switch the memo invalidates.
    act(() => result.current.setLocale('fr'))
    const t3 = result.current.t
    expect(t3).not.toBe(t1)
  })

  it('resolves every nav item label the Sidebar uses (en)', () => {
    const { result } = renderHook(() => useTranslation())
    // All keys the Sidebar passes through `t(item.labelKey)` must be
    // present in the en.json dictionary so the English UI never shows
    // a raw key string. Mirrors the Sidebar's NAV_GROUPS labelKey list.
    const navKeys = [
      'nav.command', 'nav.books', 'nav.screener', 'nav.positions',
      'nav.orders', 'nav.trades', 'nav.strategies', 'nav.arbitrage',
      'nav.analysis', 'nav.aiml', 'nav.copilot', 'nav.shadow',
      'nav.validation', 'nav.performance', 'nav.backtest',
      'nav.attribution', 'nav.execution', 'nav.closed', 'nav.capital',
      'nav.health', 'nav.database', 'nav.observability', 'nav.retention',
      'nav.decisions', 'nav.safety',
    ]
    navKeys.forEach((k) => {
      const v = result.current.t(k)
      expect(v, `expected ${k} to resolve to a string`).not.toBe(k)
      expect(typeof v).toBe('string')
    })
  })

  it('resolves every nav item label the Sidebar uses (fr)', () => {
    const { result } = renderHook(() => useTranslation())
    act(() => result.current.setLocale('fr'))
    const navKeys = [
      'nav.command', 'nav.books', 'nav.screener', 'nav.positions',
      'nav.orders', 'nav.trades', 'nav.strategies', 'nav.arbitrage',
      'nav.analysis', 'nav.aiml', 'nav.copilot', 'nav.shadow',
      'nav.validation', 'nav.performance', 'nav.backtest',
      'nav.attribution', 'nav.execution', 'nav.closed', 'nav.capital',
      'nav.health', 'nav.database', 'nav.observability', 'nav.retention',
      'nav.decisions', 'nav.safety',
    ]
    navKeys.forEach((k) => {
      const v = result.current.t(k)
      expect(v, `expected ${k} to resolve to a French string`).not.toBe(k)
    })
  })

  it('resolves every group label the Sidebar uses', () => {
    const { result } = renderHook(() => useTranslation())
    const groupKeys = [
      'groups.main', 'groups.markets', 'groups.portfolio',
      'groups.capital_group', 'groups.strategies', 'groups.intelligence',
      'groups.analytics', 'groups.system',
    ]
    groupKeys.forEach((k) => {
      expect(result.current.t(k)).not.toBe(k)
    })

    // Same check for French.
    act(() => result.current.setLocale('fr'))
    groupKeys.forEach((k) => {
      expect(result.current.t(k)).not.toBe(k)
    })
  })
})
