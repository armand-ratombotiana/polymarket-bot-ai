// lib/preferences.ts — W15-2 User preferences store (localStorage-backed).
//
// The workstation exposes a single global "preferences" object whose
// lifetime outlives any single React tree: a trader flips the
// dashboard theme, tweaks the polling cadence, or mutes audio cues
// today, and expects the same choice to apply on tomorrow's reload.
//
// Storage layout:
//   * All preferences live under a single localStorage key
//     (`polymarket_preferences`) so a future "export / import my
//     setup" feature is one `JSON.parse` away — no key enumeration.
//   * Stored as a single JSON blob. The shape is fixed by the
//     `UserPreferences` interface; partial updates are merged over
//     the `DEFAULTS` so adding a new preference field in a future
//     release automatically back-fills it for existing users (the
//     `{ ...DEFAULTS, ...parsed }` spread handles missing fields).
//
// Cross-component reactivity:
//   * `updatePreference(key, value)` writes to localStorage AND
//     dispatches a `preferences-changed` CustomEvent on `window`.
//     The `usePreferences` hook subscribes to this event so every
//     mounted consumer re-renders synchronously in the same tick —
//     the trader toggles "show unrealized P&L" once and every panel
//     that respects the flag flips in the next React commit, without
//     requiring a state library like Zustand or a context provider.
//   * The event fires on the SAME window that wrote the value, so
//     multi-tab edits do NOT cross-pollinate by design (the
//     workstation is a single-window app; cross-tab sync would add
//     confusion more than it adds value).
//
// SSR safety:
//   * `loadPreferences()` returns `DEFAULTS` when `window` is
//     undefined — `next-themes`, `useTranslation`, and this module
//     all initialise client-only state via a mount effect, so the
//     first server render stays consistent and hydration matches
//     cleanly.
//
// Default values:
//   * Every default is chosen so a fresh install renders the same
//     dashboard a trader has been using since W8 (dark theme, 2s
//     REST poll, EN locale, US number formatting, audio muted).
//     Defaults are NOT aspirational: enabling sound on first launch
//     would surprise a trader in a quiet office, and flipping to
//     light theme would clash with the dark-first design system.

'use client'

export interface UserPreferences {
  // ── Display ────────────────────────────────────────────────────────
  /** Active colour palette. Drives `next-themes` via the SettingsModal
   *  + ThemeToggle button in TopStatusBar. */
  theme: 'dark' | 'light'
  /** UI language. Drives the `useTranslation` hook's locale. */
  locale: 'en' | 'fr'

  // ── Dashboard ──────────────────────────────────────────────────────
  /** Sidebar section active on first load (e.g. `command`,
   *  `markets-books`). Reads back as a `NavSection` string in
   *  page.tsx. */
  defaultPanel: string
  /** REST polling cadence for `useBot`'s 2s fallback poll. Lower =
   *  snappier updates, higher = less backend load. */
  refreshIntervalMs: number
  /** When false, the `useBot` REST fallback poll is fully paused
   *  (the WebSocket stays live). */
  autoRefresh: boolean
  /** When true, disables Framer Motion panel transitions + CSS
   *  animations (price flashes, spinner pulses). Mirrors the OS
   *  "Reduce motion" accessibility setting. */
  reducedMotion: boolean

  // ── Trading display ────────────────────────────────────────────────
  /** Show the Unrealized P&L column on the positions table. Traders
   *  who haven't reconciled exposure may want to hide this until
   *  the backend reliably publishes `current_price`. */
  showUnrealizedPnl: boolean
  /** Apply `.price-up` / `.price-down` CSS classes on tick moves
   *  (the green/red flash on the Mark column). */
  showPriceFlashes: boolean
  /** Default chart type when opening a market chart modal. */
  defaultChartType: 'line' | 'candlestick' | 'area'
  /** Number formatting: US = `1,234.56`; EU = `1.234,56`. */
  numberFormat: 'us' | 'eu'

  // ── Notifications ──────────────────────────────────────────────────
  /** Master switch for browser push notifications (W13-6). When
   *  false, the `useNotifications` hook does not poll the alerts
   *  endpoint. */
  notificationsEnabled: boolean
  /** Severities the trader wants surfaced as a desktop toast.
   *  `critical` and `error` are the only levels the existing
   *  `useNotifications` hook honours today; the filter is exposed
   *  so a future "info alerts" rollout can be opted in per-user. */
  alertSeverityFilter: ('critical' | 'error' | 'warning' | 'info')[]

  // ── Sound ──────────────────────────────────────────────────────────
  /** Master switch for the `useAudio` cue system (U13 trade fills,
   *  whale alerts, kill switch). Independent from the OS mute. */
  soundEnabled: boolean
  /** Cue volume 0..1. Applied to every Web Audio oscillator + the
   *  order-placed / kill-switch audio snippets. */
  soundVolume: number

  // ── Layout ────────────────────────────────────────────────────────
  /** Persisted sidebar collapsed state (W13 sidebar redesign). */
  sidebarCollapsed: boolean
  /** When true, the command palette (Cmd/Ctrl+K) auto-opens on
   *  mount. Off by default so a reload doesn't immediately capture
   *  keyboard focus. */
  commandPaletteOpen: boolean

  // ── Privacy ────────────────────────────────────────────────────────
  /** When true, the W13-3 ErrorReporter ships uncaught client-side
   *  exceptions to the backend `/api/errors` endpoint for triage.
   *  When false, errors stay local (console only). */
  shareErrorReports: boolean
}

const DEFAULTS: UserPreferences = {
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

const STORAGE_KEY = 'polymarket_preferences'

/** Event type dispatched by `updatePreference`. The `detail` carries
 *  the full updated preferences object so listeners don't need a
 *  separate `localStorage.getItem` round-trip. */
export const PREFERENCES_CHANGED_EVENT = 'preferences-changed'

export type PreferencesChangedEvent = CustomEvent<UserPreferences>

/**
 * loadPreferences — read the persisted preferences from localStorage.
 *
 * Returns `DEFAULTS` when:
 *   * `window` is undefined (SSR / non-browser environment).
 *   * localStorage has no entry under `STORAGE_KEY` (first launch).
 *   * The stored value is malformed JSON (corruption recovery).
 *
 * When the stored value parses but is missing fields (a previous
 * release's payload), the missing fields are back-filled from
 * `DEFAULTS` via the spread, so adding a new preference is always
 * backward-compatible.
 *
 * Always returns a fresh object (spread DEFAULTS) — never the module-
 * level DEFAULTS reference — so a caller mutating the returned value
 * cannot corrupt the canonical defaults for subsequent callers.
 */
export function loadPreferences(): UserPreferences {
  if (typeof window === 'undefined') return { ...DEFAULTS }
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (!stored) return { ...DEFAULTS }
    const parsed = JSON.parse(stored) as Partial<UserPreferences>
    return { ...DEFAULTS, ...parsed }
  } catch {
    return { ...DEFAULTS }
  }
}

/**
 * savePreferences — persist the entire preferences object.
 *
 * Writes synchronously to localStorage. Does NOT dispatch the
 * `preferences-changed` event — that's reserved for `updatePreference`
 * so the only re-render trigger is a single-call path (avoids a
 * duplicate dispatch if `savePreferences` is ever called from the
 * event listener itself).
 */
export function savePreferences(prefs: UserPreferences): void {
  if (typeof window === 'undefined') return
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs))
  } catch {
    // Quota exceeded / disabled storage — swallow so the trader
    // keeps working in-memory for this session even if persistence
    // is unavailable (private mode, disk full, etc.).
  }
}

/**
 * resetPreferences — clear the persisted blob and return DEFAULTS.
 *
 * Used by the SettingsModal "Reset to defaults" button. The returned
 * object is what the caller should set as the new state. Returns a
 * fresh spread so callers can't mutate the canonical DEFAULTS.
 */
export function resetPreferences(): UserPreferences {
  if (typeof window !== 'undefined') {
    try {
      localStorage.removeItem(STORAGE_KEY)
    } catch {
      // localStorage.removeItem can throw if storage is disabled;
      // a reset still flips the in-memory state back to DEFAULTS.
    }
  }
  return { ...DEFAULTS }
}

/**
 * updatePreference — atomically update a single field + persist +
 * broadcast the new state.
 *
 * @param key   Field name on `UserPreferences` to update.
 * @param value New value for the field.
 * @returns The fully-updated preferences object (post-merge).
 *
 * Dispatches a `preferences-changed` CustomEvent on `window` with the
 * updated object as `detail`. The `usePreferences` hook listens for
 * this event so every mounted consumer re-renders in the same React
 * commit cycle.
 */
export function updatePreference<K extends keyof UserPreferences>(
  key: K,
  value: UserPreferences[K]
): UserPreferences {
  const current = loadPreferences()
  const updated = { ...current, [key]: value }
  savePreferences(updated)
  if (typeof window !== 'undefined') {
    window.dispatchEvent(
      new CustomEvent<UserPreferences>(PREFERENCES_CHANGED_EVENT, {
        detail: updated,
      }) as PreferencesChangedEvent,
    )
  }
  return updated
}

/**
 * getDefaults — exported read-only accessor for the DEFAULTS object.
 *
 * Used by tests + the `usePreferences` hook to know the canonical
 * shape when initialising state before the mount effect has loaded
 * the persisted values. Returns a fresh spread so callers can't
 * mutate the canonical DEFAULTS via the returned reference.
 */
export function getDefaults(): UserPreferences {
  return { ...DEFAULTS }
}

export { STORAGE_KEY }
