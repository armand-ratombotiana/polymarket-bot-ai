// hooks/useKeyboardShortcuts.ts — W17-6 React binding for the
// keyboard-shortcut catalog declared in `lib/keyboardShortcuts.ts`.
//
// Responsibilities:
//   * Attach a single `keydown` listener to `window` on mount and
//     detach on unmount (no per-shortcut listeners).
//   * Skip keypresses whose target is an editable element (<input>,
//     <textarea>, contenteditable) UNLESS the matching shortcut
//     declares `global: true` — those fire regardless of focus, so
//     `Escape` can close a modal even when the user is mid-typing.
//   * Call `preventDefault()` on the event when a shortcut matches
//     so the browser doesn't ALSO react (e.g. `/` triggering Firefox's
//     Quick Find, or `Cmd+K` jumping to the URL bar).
//   * Expose `enableShortcuts` / `disableShortcuts` so modal contexts
//     can pause the global handler (e.g. while a modal that uses
//     single-letter shortcuts for its own navigation is open).
//
// Why a ref for the shortcuts list:
//   The `shortcuts` prop array is rebuilt on every parent render
//   (each entry's `action` closure captures live state). Re-attaching
//   the window listener on every render would (a) thrash the listener
//   cache and (b) briefly miss any keypress that lands between the
//   detach and re-attach. Instead the hook keeps a ref to the LATEST
//   shortcuts array and the listener reads through the ref — so the
//   window listener is attached exactly once per mount, but always
//   sees the freshest actions.
//
// Why the listener is attached on the bubble phase (not capture):
//   The legacy page.tsx handler used bubble phase, and other workstation
//   components (CommandPalette, ConfirmationDialog, ShortcutsModal)
//   attach their own scoped Escape handlers at the same phase. Sticking
//   with bubble means the modal-scoped handler runs FIRST (it can
//   `stopPropagation` to keep the global handler from also firing),
//   mirroring the existing UX where Escape inside a modal closes the
//   modal but doesn't ALSO trigger the page-level "clear selection"
//   action.

'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  matchesShortcut,
  type Shortcut,
} from '@/lib/keyboardShortcuts'

/** Editable-element selectors — when the event target matches one of
 *  these, only `global: true` shortcuts fire. */
const EDITABLE_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT'])

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  if (EDITABLE_TAGS.has(target.tagName)) return true
  // contenteditable spans / divs — used by the markdown editor +
  // rich-text inputs in the W14-8 settings modal.
  if (target.isContentEditable) return true
  return false
}

export interface UseKeyboardShortcutsResult {
  /** Re-enable the global keydown listener after a `disableShortcuts`
   *  call. Safe to call when already enabled (no-op). */
  enableShortcuts: () => void
  /** Pause the global keydown listener. Used by modal contexts that
   *  take over the keyboard (e.g. the CommandPalette's arrow-key
   *  navigation). Safe to call when already disabled (no-op). */
  disableShortcuts: () => void
  /** Reflects the current enabled state. Mirrored in a ref so the
   *  listener (which closes over the ref, not the state) always
   *  sees the latest value without re-subscribing. */
  isEnabled: boolean
  /** Imperatively toggle. Useful for a "pause shortcuts" button in
   *  the cheat sheet's practice mode. */
  setEnabled: (next: boolean) => void
}

/**
 * Wire a list of shortcuts into the global keydown pipeline.
 *
 * Usage:
 *   const { disableShortcuts, enableShortcuts } = useKeyboardShortcuts([
 *     { key: '?', modifiers: [], action: () => setCheatOpen(true), ... },
 *     { key: 'Escape', modifiers: [], action: closeAll, global: true },
 *   ])
 *
 *   // When a modal takes over the keyboard:
 *   useEffect(() => {
 *     disableShortcuts()
 *     return () => enableShortcuts()
 *   }, [disableShortcuts, enableShortcuts])
 *
 * The hook intentionally accepts a flat list of `Shortcut` objects
 * (with the runtime `action` attached) rather than reading from
 * `SHORTCUT_DEFINITIONS` directly — the catalog is declarative
 * (JSON-able, used by the cheat sheet for display) while the hook
 * needs live callback closures. The call site composes the two by
 * `map`-ing `SHORTCUT_DEFINITIONS` and attaching the action per
 * entry (see `app/page.tsx`).
 */
export function useKeyboardShortcuts(
  shortcuts: Shortcut[],
  initialEnabled = true,
): UseKeyboardShortcutsResult {
  const [enabled, setEnabledState] = useState(initialEnabled)

  // Refs so the listener — attached once on mount — always reads the
  // freshest state without re-subscribing on every render.
  const shortcutsRef = useRef<Shortcut[]>(shortcuts)
  const enabledRef = useRef<boolean>(enabled)
  // Keep the refs in sync on every render. The listener reads through
  // these refs at event-fire time, so this synchronous assignment
  // guarantees the latest values are visible.
  shortcutsRef.current = shortcuts
  enabledRef.current = enabled

  useEffect(() => {
    // SSR guard — `window` is undefined during Next.js server render.
    if (typeof window === 'undefined') return

    const handler = (event: KeyboardEvent) => {
      if (!enabledRef.current) return

      const editable = isEditableTarget(event.target)
      const list = shortcutsRef.current

      // Iterate in declaration order — the FIRST matching shortcut
      // wins. This lets the call site order the list so a more
      // specific shortcut (e.g. Cmd+K) is checked before a less
      // specific one (e.g. plain `k`).
      for (const shortcut of list) {
        // Skip non-global shortcuts when the user is editing text —
        // otherwise pressing `1` while typing in the strategy-config
        // modal would yank the user to the Command Center.
        if (editable && !shortcut.global) continue

        if (matchesShortcut(event, shortcut)) {
          // preventDefault so the browser doesn't ALSO react:
          //   * `/` would open Firefox Quick Find
          //   * `'` would open Firefox Quick Find (history)
          //   * Cmd+K would focus the URL bar in some browsers
          //   * Backspace would navigate back (we don't register it
          //     today, but the principle holds for future additions)
          event.preventDefault()
          shortcut.action()
          return
        }
      }
    }

    // Bubble phase — see module docstring for the rationale.
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  const enableShortcuts = useCallback(() => setEnabledState(true), [])
  const disableShortcuts = useCallback(() => setEnabledState(false), [])
  const setEnabled = useCallback((next: boolean) => setEnabledState(next), [])

  return {
    enableShortcuts,
    disableShortcuts,
    isEnabled: enabled,
    setEnabled,
  }
}
