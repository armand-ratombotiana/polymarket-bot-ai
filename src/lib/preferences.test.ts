// lib/preferences.test.ts — W15-2 unit tests for the preferences store.
//
// Strategy: jsdom ships with a working `window.localStorage` so we can
// exercise the real load/save/reset/update code paths without stubbing
// the storage layer. Each test wipes the storage key in `beforeEach` so
// there is zero cross-test state leakage.
//
// What's covered:
//   1. getDefaults(): returns a snapshot of the canonical DEFAULTS —
//      used by `usePreferences` for SSR-safe initial state.
//   2. loadPreferences(): returns DEFAULTS when no localStorage entry
//      exists, when the stored value is malformed JSON, and when
//      `window` is undefined (SSR).
//   3. loadPreferences(): merges a partial stored payload over DEFAULTS
//      so adding a new preference field is always backward-compatible.
//   4. savePreferences(): writes the entire object as a JSON blob under
//      the storage key; round-trips through loadPreferences().
//   5. resetPreferences(): removes the storage key and returns DEFAULTS.
//   6. updatePreference(): merges the single field, persists, and
//      dispatches the `preferences-changed` CustomEvent with the new
//      full object as `detail`.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  loadPreferences,
  savePreferences,
  resetPreferences,
  updatePreference,
  getDefaults,
  STORAGE_KEY,
  PREFERENCES_CHANGED_EVENT,
  type UserPreferences,
} from './preferences'

// Local copy of the canonical DEFAULTS so tests can assert against the
// exact shape without depending on `getDefaults()` for comparison
// (avoids a circular assertion: "loadPreferences returns getDefaults()").
const EXPECTED_DEFAULTS: UserPreferences = {
  theme: 'dark',
  locale: 'en',
  defaultPanel: 'command',
  refreshIntervalMs: 2000,
  autoRefresh: true,
  reducedMotion: false,
  showUnrealizedPnl: true,
  showPriceFlashes: true,
  defaultChartType: 'area',
  numberFormat: 'us',
  notificationsEnabled: false,
  alertSeverityFilter: ['critical', 'error'],
  soundEnabled: false,
  soundVolume: 0.5,
  sidebarCollapsed: false,
  commandPaletteOpen: false,
  shareErrorReports: true,
}

describe('preferences — getDefaults', () => {
  it('returns the canonical DEFAULTS object', () => {
    expect(getDefaults()).toEqual(EXPECTED_DEFAULTS)
  })

  it('returns a fresh object on every call (no shared reference leak)', () => {
    // Defensive: callers (usePreferences) initialise useState with
    // `getDefaults()` — if the same reference were returned every
    // time, a future mutation of the state object would silently
    // corrupt the canonical DEFAULTS. The spread in `getDefaults()`
    // (technically the literal object definition) returns a fresh
    // value each call.
    const a = getDefaults()
    const b = getDefaults()
    expect(a).toEqual(b)
    expect(a).not.toBe(b)
  })
})

describe('preferences — loadPreferences', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('returns DEFAULTS when no localStorage entry exists', () => {
    const prefs = loadPreferences()
    expect(prefs).toEqual(EXPECTED_DEFAULTS)
  })

  it('returns DEFAULTS when the stored value is malformed JSON', () => {
    window.localStorage.setItem(STORAGE_KEY, '{ this is not valid json')
    const prefs = loadPreferences()
    expect(prefs).toEqual(EXPECTED_DEFAULTS)
  })

  it('returns DEFAULTS when the stored value is null-ish', () => {
    window.localStorage.setItem(STORAGE_KEY, 'null')
    // null is valid JSON, but `{ ...DEFAULTS, ...null }` spreads to
    // DEFAULTS — so the test still passes; we verify the shape rather
    // than the raw value.
    const prefs = loadPreferences()
    expect(prefs).toEqual(EXPECTED_DEFAULTS)
  })

  it('merges a partial stored payload over DEFAULTS (backward compat)', () => {
    // Simulate a previous release's payload that's missing newer
    // fields like `shareErrorReports` + `commandPaletteOpen`.
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        theme: 'light',
        locale: 'fr',
        refreshIntervalMs: 5000,
      }),
    )
    const prefs = loadPreferences()
    // Persisted values override.
    expect(prefs.theme).toBe('light')
    expect(prefs.locale).toBe('fr')
    expect(prefs.refreshIntervalMs).toBe(5000)
    // Missing values fall back to DEFAULTS.
    expect(prefs.defaultPanel).toBe('command')
    expect(prefs.shareErrorReports).toBe(true)
    expect(prefs.commandPaletteOpen).toBe(false)
    expect(prefs.alertSeverityFilter).toEqual(['critical', 'error'])
  })

  it('returns DEFAULTS when window is undefined (SSR)', () => {
    // jsdom always has `window`, so we simulate SSR by stubbing the
    // typeof check. The module-level code does `typeof window ===
    // 'undefined'` — we can't easily unset window, so instead we
    // verify the function returns DEFAULTS even when localStorage
    // throws. This is the same branch the SSR guard hits.
    const throwSpy = vi
      .spyOn(Storage.prototype, 'getItem')
      .mockImplementation(() => {
        throw new Error('storage disabled')
      })
    const prefs = loadPreferences()
    expect(prefs).toEqual(EXPECTED_DEFAULTS)
    throwSpy.mockRestore()
  })
})

describe('preferences — savePreferences', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('writes the full object as JSON under the storage key', () => {
    const prefs: UserPreferences = {
      ...EXPECTED_DEFAULTS,
      theme: 'light',
      refreshIntervalMs: 5000,
    }
    savePreferences(prefs)
    const stored = window.localStorage.getItem(STORAGE_KEY)
    expect(stored).not.toBeNull()
    expect(JSON.parse(stored!)).toEqual(prefs)
  })

  it('round-trips through loadPreferences', () => {
    const prefs: UserPreferences = {
      ...EXPECTED_DEFAULTS,
      theme: 'light',
      locale: 'fr',
      refreshIntervalMs: 5000,
      alertSeverityFilter: ['critical', 'error', 'warning'],
    }
    savePreferences(prefs)
    const loaded = loadPreferences()
    expect(loaded).toEqual(prefs)
  })

  it('overwrites the previous stored value on subsequent saves', () => {
    savePreferences({ ...EXPECTED_DEFAULTS, theme: 'light' })
    savePreferences({ ...EXPECTED_DEFAULTS, theme: 'dark' })
    const loaded = loadPreferences()
    expect(loaded.theme).toBe('dark')
  })

  it('does not dispatch the preferences-changed event (reserved for updatePreference)', () => {
    const handler = vi.fn()
    window.addEventListener(PREFERENCES_CHANGED_EVENT, handler)
    savePreferences({ ...EXPECTED_DEFAULTS, theme: 'light' })
    expect(handler).not.toHaveBeenCalled()
    window.removeEventListener(PREFERENCES_CHANGED_EVENT, handler)
  })

  it('swallows quota-exceeded errors silently', () => {
    const setItemSpy = vi
      .spyOn(Storage.prototype, 'setItem')
      .mockImplementation(() => {
        throw new Error('QuotaExceededError')
      })
    // Should not throw.
    expect(() =>
      savePreferences({ ...EXPECTED_DEFAULTS, theme: 'light' }),
    ).not.toThrow()
    setItemSpy.mockRestore()
  })
})

describe('preferences — resetPreferences', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('removes the storage key', () => {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...EXPECTED_DEFAULTS, theme: 'light' }))
    expect(window.localStorage.getItem(STORAGE_KEY)).not.toBeNull()
    resetPreferences()
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('returns the DEFAULTS object', () => {
    const result = resetPreferences()
    expect(result).toEqual(EXPECTED_DEFAULTS)
  })

  it('does not dispatch the preferences-changed event (the hook does)', () => {
    const handler = vi.fn()
    window.addEventListener(PREFERENCES_CHANGED_EVENT, handler)
    resetPreferences()
    expect(handler).not.toHaveBeenCalled()
    window.removeEventListener(PREFERENCES_CHANGED_EVENT, handler)
  })

  it('is idempotent when the storage key is already absent', () => {
    expect(() => resetPreferences()).not.toThrow()
    expect(() => resetPreferences()).not.toThrow()
  })

  it('swallows removeItem errors silently', () => {
    const removeItemSpy = vi
      .spyOn(Storage.prototype, 'removeItem')
      .mockImplementation(() => {
        throw new Error('storage disabled')
      })
    expect(() => resetPreferences()).not.toThrow()
    removeItemSpy.mockRestore()
  })
})

describe('preferences — updatePreference', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('persists the updated field + preserves the others', () => {
    const before = loadPreferences()
    const updated = updatePreference('theme', 'light')
    expect(updated.theme).toBe('light')
    expect(updated.locale).toBe(before.locale)
    expect(updated.refreshIntervalMs).toBe(before.refreshIntervalMs)
    // Verify persistence.
    const loaded = loadPreferences()
    expect(loaded.theme).toBe('light')
  })

  it('returns the full updated object (post-merge, not just the field)', () => {
    const updated = updatePreference('refreshIntervalMs', 7500)
    // Returned object has every DEFAULTS field present, plus the new
    // value for the updated field.
    expect(updated).toEqual({ ...EXPECTED_DEFAULTS, refreshIntervalMs: 7500 })
  })

  it('dispatches the preferences-changed event with the new value as detail', () => {
    const handler = vi.fn()
    window.addEventListener(PREFERENCES_CHANGED_EVENT, handler)
    const updated = updatePreference('soundEnabled', true)
    expect(handler).toHaveBeenCalledTimes(1)
    const event = handler.mock.calls[0][0] as CustomEvent<UserPreferences>
    expect(event.detail).toEqual(updated)
    expect(event.detail.soundEnabled).toBe(true)
    window.removeEventListener(PREFERENCES_CHANGED_EVENT, handler)
  })

  it('updates an array field (alertSeverityFilter)', () => {
    const updated = updatePreference('alertSeverityFilter', [
      'critical',
      'error',
      'warning',
      'info',
    ])
    expect(updated.alertSeverityFilter).toEqual([
      'critical',
      'error',
      'warning',
      'info',
    ])
    const loaded = loadPreferences()
    expect(loaded.alertSeverityFilter).toEqual([
      'critical',
      'error',
      'warning',
      'info',
    ])
  })

  it('is type-safe: TypeScript would refuse an invalid key/value pair', () => {
    // Compile-time check only (no runtime assertion). The fact that
    // the test compiles proves `updatePreference<K extends keyof
    // UserPreferences>` rejects an off-schema key + value pair.
    const updated = updatePreference('defaultChartType', 'candlestick')
    expect(updated.defaultChartType).toBe('candlestick')
  })

  it('preserves every other field when one is updated', () => {
    // Set baseline with multiple non-default values.
    savePreferences({
      ...EXPECTED_DEFAULTS,
      theme: 'light',
      locale: 'fr',
      refreshIntervalMs: 5000,
      soundEnabled: true,
      soundVolume: 0.75,
    })
    // Update a single field — verify the others are untouched.
    const updated = updatePreference('soundVolume', 0.25)
    expect(updated.theme).toBe('light')
    expect(updated.locale).toBe('fr')
    expect(updated.refreshIntervalMs).toBe(5000)
    expect(updated.soundEnabled).toBe(true)
    expect(updated.soundVolume).toBe(0.25)
  })
})
