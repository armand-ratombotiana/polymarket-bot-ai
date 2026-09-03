// hooks/usePreferences.ts — W15-2 React binding for the preferences store.
//
// The preferences module (`src/lib/preferences.ts`) is framework-agnostic
// (pure localStorage + CustomEvent). This hook is the React layer:
//
//   * `preferences`        — current value, initialised to DEFAULTS so
//                             the first paint matches SSR + hydration.
//   * `update(key, value)` — single-field update + persist + broadcast.
//   * `reset()`            — restore DEFAULTS (used by the SettingsModal).
//   * `loaded`             — flips true once the mount effect has loaded
//                             the persisted blob. Consumers that need to
//                             distinguish "default because no preference"
//                             from "default because preferences explicitly
//                             equal the defaults" can read this flag.
//
// Subscription strategy:
//   * The hook reads the persisted value ONCE on mount (via
//     `loadPreferences()`), then subscribes to the
//     `preferences-changed` CustomEvent so any subsequent
//     `updatePreference()` call (from this hook, from another
//     mounted consumer, or from a future "preferences" command in
//     the W13-5 Command Palette) re-renders every active consumer
//     synchronously in the same event-loop tick.
//   * No React Context provider is required: the event-bus pattern
//     keeps the surface area small and avoids a provider-level
//     re-render storm every time any preference changes.
//
// SSR safety:
//   * Initial state is `getDefaults()` so the server-rendered payload
//     and the very first client render agree (same shape, same values).
//     The mount effect then reconciles to the persisted value — a
//     single scheduled re-render that does NOT trigger a hydration
//     warning (the diff is applied post-mount).

'use client'

import { useState, useEffect, useCallback } from 'react'
import {
  loadPreferences,
  updatePreference,
  resetPreferences,
  getDefaults,
  PREFERENCES_CHANGED_EVENT,
  type UserPreferences,
  type PreferencesChangedEvent,
} from '@/lib/preferences'

export interface UsePreferencesResult {
  /** Current preferences — DEFAULTS until the mount effect loads
   *  the persisted blob. */
  preferences: UserPreferences
  /** True once the persisted value has been loaded (post-mount). */
  loaded: boolean
  /** Update a single field. Persists to localStorage + broadcasts
   *  the change so every mounted consumer re-renders. */
  update: <K extends keyof UserPreferences>(key: K, value: UserPreferences[K]) => UserPreferences
  /** Reset every preference to its DEFAULTS. Returns the new
   *  preferences object so the caller can synchronously assert
   *  on the post-reset state. */
  reset: () => UserPreferences
}

export function usePreferences(): UsePreferencesResult {
  // Initial state is DEFAULTS so SSR + first client render match.
  const [preferences, setPreferences] = useState<UserPreferences>(() => getDefaults())
  const [loaded, setLoaded] = useState(false)

  // On mount, read the persisted preferences and reconcile state.
  useEffect(() => {
    const persisted = loadPreferences()
    setPreferences(persisted)
    setLoaded(true)

    // Subscribe to subsequent changes (fired by `updatePreference`).
    // `event.detail` is the fully-updated object so we don't need
    // a separate `loadPreferences()` round-trip per change.
    const handler = (event: Event) => {
      const ce = event as PreferencesChangedEvent
      if (ce.detail) {
        setPreferences(ce.detail)
      } else {
        // Defensive: if the detail is missing for any reason, fall
        // back to a fresh read so the in-memory state never drifts
        // from what's in localStorage.
        setPreferences(loadPreferences())
      }
    }
    window.addEventListener(PREFERENCES_CHANGED_EVENT, handler)
    return () => window.removeEventListener(PREFERENCES_CHANGED_EVENT, handler)
  }, [])

  // `update` is a stable callback identity: it closes over the
  // module-scope `updatePreference` (which reads the latest state
  // from localStorage on every call) — no need to depend on
  // `preferences`. This means memoized children receiving `update`
  // as a prop don't bypass React.memo when `preferences` changes.
  const update = useCallback(
    <K extends keyof UserPreferences>(key: K, value: UserPreferences[K]) => {
      const updated = updatePreference(key, value)
      // The CustomEvent will also fire + flow through the listener
      // above, but setting state here too guarantees the calling
      // component sees the new value in the same React commit
      // cycle (the event listener is async with respect to React's
      // batched updates — explicit setState avoids a one-tick lag
      // for the caller).
      setPreferences(updated)
      return updated
    },
    [],
  )

  // `reset` is also stable — same rationale as `update`.
  const reset = useCallback(() => {
    const defaults = resetPreferences()
    // Manually dispatch a `preferences-changed` event so any OTHER
    // mounted consumer (not this hook instance) gets the reset
    // notification. `resetPreferences` only clears localStorage;
    // it does NOT dispatch the event by design (it's a primitive).
    if (typeof window !== 'undefined') {
      window.dispatchEvent(
        new CustomEvent<UserPreferences>(PREFERENCES_CHANGED_EVENT, {
          detail: defaults,
        }) as PreferencesChangedEvent,
      )
    }
    setPreferences(defaults)
    return defaults
  }, [])

  return { preferences, loaded, update, reset }
}
