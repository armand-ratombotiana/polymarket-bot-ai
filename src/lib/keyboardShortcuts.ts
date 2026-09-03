// lib/keyboardShortcuts.ts — W17-6 Comprehensive keyboard shortcut system.
//
// This module is the single source of truth for the workstation's
// keyboard-shortcut *catalog*: the list of shortcuts the cheat sheet
// renders, the categories they belong to, and the predicate functions
// used both by the live `useKeyboardShortcuts` hook (to dispatch a
// keypress to the right action callback) and by the cheat sheet UI
// (to render a styled kbd badge).
//
// Design notes:
//   * The catalog (`SHORTCUT_DEFINITIONS`) is *declarative* — it does
//     not hold action callbacks. Actions are wired at the call site
//     (page.tsx) where the React state setters + handler closures
//     live. This keeps the catalog JSON-serialisable (so the cheat
//     sheet can export it) and lets the same catalog be reused in
//     unit tests without dragging in the entire workstation tree.
//   * The `matchesShortcut` predicate treats `meta` and `ctrl` as
//     the same modifier chord — matching the cross-platform convention
//     where ⌘K on macOS == Ctrl+K on Windows / Linux. The visible
//     glyph (`⌘` vs `Ctrl`) is chosen at format-time based on the
//     active platform so the cheat sheet shows the chord the user's
//     muscle memory expects.
//   * `formatShortcut` is SSR-safe: it guards `navigator.platform`
//     (which is undefined during Next.js server render) so the
//     function never throws when called from a server component.
//     The default — non-Mac — branch is used on the server; the
//     client re-paints with the correct glyph after hydration.

/** Modifier keys a shortcut can require. Treated as a set (order is
 *  normalised by `formatShortcut` for display, not by the matcher). */
export type ShortcutModifier = 'ctrl' | 'shift' | 'alt' | 'meta'

/** The four top-level groupings shown in the cheat sheet. Each
 *  category renders under its own header so a trader can scan the
 *  sheet by intent (am I navigating? trading? changing the view?). */
export type ShortcutCategory = 'navigation' | 'trading' | 'view' | 'system'

/** Full shortcut record — the catalog without the action plus the
 *  runtime action callback. The split keeps the catalog JSON-able. */
export interface Shortcut {
  /** KeyboardEvent.key value (case-insensitive at match time). */
  key: string
  /** Modifier chords that must accompany `key` for the shortcut to
   *  fire. Empty array = unmodified keypress. */
  modifiers: ShortcutModifier[]
  /** Action invoked when the shortcut matches. Wired at the call site. */
  action: () => void
  /** Human-readable description shown in the cheat sheet. */
  description: string
  /** Cheat-sheet grouping for the shortcut. */
  category: ShortcutCategory
  /** When true the shortcut fires even when the user is typing in an
   *  <input> / <textarea> / contenteditable. Reserved for shortcuts
   *  that must ALWAYS work (e.g. Escape, ⌘K). */
  global?: boolean
}

/** Catalog entry — everything except the runtime `action` callback.
 *  This is the shape stored in `SHORTCUT_DEFINITIONS` and exported
 *  for the cheat sheet to render. */
export type ShortcutDefinition = Omit<Shortcut, 'action'>

/** Display metadata for each shortcut category. The `icon` is a
 *  single emoji rendered next to the category header so the four
 *  groups are visually distinguishable at a glance. */
export const SHORTCUT_CATEGORIES = {
  navigation: { label: 'Navigation', icon: '🧭' },
  trading: { label: 'Trading', icon: '💰' },
  view: { label: 'View', icon: '👁️' },
  system: { label: 'System', icon: '⚙️' },
} as const

/** Type guard for the category union — used by the cheat sheet to
 *  validate user-supplied filter values and by tests to assert the
 *  catalog stays within the four known categories. */
export const SHORTCUT_CATEGORY_KEYS = Object.keys(
  SHORTCUT_CATEGORIES,
) as ShortcutCategory[]

/** Canonical shortcut catalog. The cheat sheet renders every entry
 *  in this list grouped by `category`. The live `useKeyboardShortcuts`
 *  hook in `page.tsx` consumes the same list, attaches an `action`
 *  callback to each, and dispatches keypresses to the first matching
 *  entry.
 *
 *  Order within a category matters — it's the order the cheat sheet
 *  renders. Keep related shortcuts together (e.g. buy/sell/cancel
 *  cluster in `trading`). */
export const SHORTCUT_DEFINITIONS: ShortcutDefinition[] = [
  // ── Navigation ────────────────────────────────────────────────────
  // Digits 1-8 — quick-flip to the eight top-level nav sections.
  // Matches the Sidebar kbd hints (`Sidebar.tsx` NAV_GROUPS) and the
  // legacy `KB_MAP` in page.tsx so existing muscle memory carries
  // over.
  { key: '1', modifiers: [], description: 'Command Center', category: 'navigation' },
  { key: '2', modifiers: [], description: 'Live Books', category: 'navigation' },
  { key: '3', modifiers: [], description: 'Screener', category: 'navigation' },
  { key: '4', modifiers: [], description: 'Positions', category: 'navigation' },
  { key: '5', modifiers: [], description: 'Strategy Registry', category: 'navigation' },
  { key: '6', modifiers: [], description: 'Arbitrage', category: 'navigation' },
  { key: '7', modifiers: [], description: 'Deep Analysis', category: 'navigation' },
  { key: '8', modifiers: [], description: 'Performance', category: 'navigation' },

  // ── View ──────────────────────────────────────────────────────────
  // View shortcuts change what the trader sees (palette / search /
  // full-screen / theme) without mutating trading state.
  { key: 'k', modifiers: ['meta'], description: 'Open Command Palette', category: 'view' },
  { key: '/', modifiers: [], description: 'Focus search', category: 'view' },
  { key: 'f', modifiers: [], description: 'Toggle full screen', category: 'view' },
  { key: 't', modifiers: [], description: 'Toggle theme (dark/light)', category: 'view' },

  // ── Trading ───────────────────────────────────────────────────────
  // Single-letter trading shortcuts — deliberately the home row so
  // they're fast to press while watching a book. Each is a no-op
  // without a selection (toast / silent) so an accidental press
  // doesn't fire a live order.
  { key: 'b', modifiers: [], description: 'Quick buy (selected market)', category: 'trading' },
  { key: 's', modifiers: [], description: 'Quick sell (selected market)', category: 'trading' },
  { key: 'c', modifiers: [], description: 'Close selected position', category: 'trading' },
  { key: 'x', modifiers: [], description: 'Cancel all orders', category: 'trading' },

  // ── System ────────────────────────────────────────────────────────
  // Refresh / cheat-sheet / Escape — housekeeping shortcuts that
  // don't fit the three categories above. Escape is `global:true`
  // so it closes modals even when the user is mid-typing in an input.
  { key: 'r', modifiers: [], description: 'Refresh all data', category: 'system' },
  { key: '?', modifiers: [], description: 'Show this cheat sheet', category: 'system' },
  {
    key: 'Escape',
    modifiers: [],
    description: 'Close modal / Clear selection',
    category: 'system',
    global: true,
  },
]

/**
 * Format a shortcut definition as a human-readable chord string for
 * display in the cheat sheet (e.g. `"⌘ + K"`, `"Shift + ?"`,
 * `"Escape"`).
 *
 * The leading modifier glyph (`⌘` vs `Ctrl`) is chosen based on the
 * active platform so macOS traders see the muscle-memory symbol
 * while Windows / Linux traders see the literal key name.
 *
 * SSR-safe: `navigator.platform` is undefined during Next.js server
 * render; the function falls back to the non-Mac glyph in that case
 * (the client re-paints with the correct glyph after hydration via
 * the cheat sheet's `mounted` guard).
 */
export function formatShortcut(shortcut: ShortcutDefinition): string {
  const parts: string[] = []
  // `meta` and `ctrl` are treated as the same modifier chord at
  // match-time (see `matchesShortcut`). For DISPLAY we prefer the
  // platform-appropriate glyph when `meta` is requested, and fall
  // back to the literal "Ctrl" when only `ctrl` is requested.
  if (shortcut.modifiers.includes('meta')) {
    parts.push(isMacPlatform() ? '⌘' : 'Ctrl')
  }
  if (shortcut.modifiers.includes('ctrl')) {
    parts.push('Ctrl')
  }
  if (shortcut.modifiers.includes('shift')) parts.push('Shift')
  if (shortcut.modifiers.includes('alt')) parts.push(isMacPlatform() ? 'Option' : 'Alt')

  // The terminal key — uppercased for single letters (`b` → `B`),
  // left as-is for symbolic keys (`?`, `/`) and named keys (`Escape`).
  const keyLabel = formatKey(shortcut.key)
  parts.push(keyLabel)

  return parts.join(' + ')
}

/**
 * Returns true when the given KeyboardEvent matches the shortcut
 * definition. The comparison is case-insensitive on the key value
 * and treats `metaKey` + `ctrlKey` as interchangeable (so ⌘K on Mac
 * matches a shortcut declared with either `meta` or `ctrl` in its
 * modifiers list).
 *
 * `shiftKey` / `altKey` must match exactly: a shortcut that declares
 * `shift` only matches when the user is holding Shift (and a
 * shortcut that DOESN'T declare shift doesn't fire when the user
 * happens to be holding Shift).
 */
export function matchesShortcut(
  event: KeyboardEvent,
  shortcut: ShortcutDefinition,
): boolean {
  const key = event.key.toLowerCase()
  const expectedKey = shortcut.key.toLowerCase()
  if (key !== expectedKey) return false

  // meta / ctrl — interchangeable. A shortcut with EITHER modifier
  // in its `modifiers` list matches when the user holds EITHER the
  // ⌘ key (Mac) OR the Ctrl key (Win/Linux).
  const hasMeta = event.metaKey || event.ctrlKey
  const needsMeta =
    shortcut.modifiers.includes('meta') || shortcut.modifiers.includes('ctrl')
  if (hasMeta !== needsMeta) return false

  // shift — exact match.
  const needsShift = shortcut.modifiers.includes('shift')
  if (needsShift !== event.shiftKey) return false

  // alt — exact match.
  const needsAlt = shortcut.modifiers.includes('alt')
  if (needsAlt !== event.altKey) return false

  return true
}

/**
 * Returns the catalog entries for a single category, preserving the
 * declaration order. Used by the cheat sheet to render each group
 * without having to filter at render time.
 */
export function shortcutsByCategory(category: ShortcutCategory): ShortcutDefinition[] {
  return SHORTCUT_DEFINITIONS.filter((s) => s.category === category)
}

// ── Internals ────────────────────────────────────────────────────────

/**
 * Detects macOS / iOS so the cheat sheet shows the ⌘ glyph and the
 * `Option` label (instead of `Ctrl` / `Alt`). Reads `navigator.platform`
 * with an SSR guard so the function is safe to call from a server
 * component.
 *
 * `navigator.platform` is deprecated in favour of
 * `navigator.userAgentData.platform`, but the newer API is still
 * behind a feature flag on Firefox / Safari (2025-Q3). The legacy
 * API is universally supported and is what every shortcut-display
 * library in the React ecosystem still uses today.
 */
function isMacPlatform(): boolean {
  if (typeof navigator === 'undefined') return false
  const platform =
    (navigator.platform as string | undefined) ||
    (typeof navigator.userAgent === 'string' ? navigator.userAgent : '')
  return /Mac|iPod|iPhone|iPad/i.test(platform)
}

/**
 * Formats the terminal key for display. Single lowercase letters are
 * uppercased (`b` → `B`); everything else passes through unchanged so
 * named keys (`Escape`, `Enter`) and symbolic keys (`?`, `/`) keep
 * their canonical form.
 */
function formatKey(key: string): string {
  // Single-character letter — uppercase for visual weight in the
  // kbd badge.
  if (key.length === 1 && /[a-z]/i.test(key)) return key.toUpperCase()
  return key
}
