// components/KeyboardCheatSheet.tsx — W17-6 Full-screen keyboard
// shortcut cheat sheet + practice mode.
//
// Replaces the legacy `ShortcutsModal.tsx` (still mounted by page.tsx
// for any consumer that hasn't migrated). The new cheat sheet:
//   * Pulls its catalog from the single source of truth in
//     `lib/keyboardShortcuts.ts` — the cheat sheet, the hook, and any
//     future surface (e.g. a settings-panel shortcut editor) all read
//     from the same `SHORTCUT_DEFINITIONS` array.
//   * Groups shortcuts by category (Navigation / Trading / View /
//     System) with a category-icon header so the trader can scan by
//     intent.
//   * Offers a fuzzy search box — typing "buy" filters to just the
//     Quick buy row; typing "nav" filters to the eight nav digits.
//   * Ships a "Practice mode" that picks a random shortcut, prompts
//     the user to press it, and confirms / corrects — so the trader
//     builds muscle memory without having to read the cheat sheet.
//   * Can export the catalog as JSON (download) or as a PNG-style
//     screenshot via the browser's clipboard API (best-effort).
//
// Mounting: rendered conditionally by `app/page.tsx` when
// `cheatOpen===true`. The cheat sheet owns its own focus trap +
// Escape handling so the parent's `useKeyboardShortcuts` hook can
// stay simple (it doesn't need to know about the cheat sheet's
// internal focus management).
//
// Accessibility:
//   * role="dialog" + aria-modal="true" so screen readers know it's
//     a modal context.
//   * aria-labelledby points at the visible title so the screen
//     reader announces "Workstation Keyboard Shortcuts" on open.
//   * Focus trap keeps Tab focus cycling within the dialog (mirrors
//     the ConfirmationDialog pattern).
//   * Escape closes the dialog (also stops propagation so the
//     parent's global Escape handler doesn't ALSO clear market
//     selection).

'use client'

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from 'react'
import {
  SHORTCUT_CATEGORIES,
  SHORTCUT_DEFINITIONS,
  formatShortcut,
  matchesShortcut,
  type ShortcutCategory,
  type ShortcutDefinition,
} from '@/lib/keyboardShortcuts'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Tabs,
  TabsList,
  TabsTrigger,
  TabsContent,
} from '@/components/ui/tabs'

export interface KeyboardCheatSheetProps {
  /** Controls visibility. When false the component renders null. */
  isOpen: boolean
  /** Close callback — invoked on Escape / backdrop-click / close-button. */
  onClose: () => void
}

// ── Helpers ────────────────────────────────────────────────────────────

/** Stable empty-array fallback for the "no results" branch — keeps
 *  `.map` from throwing on `undefined` and avoids a fresh `[]`
 *  allocation per render. */
const EMPTY_LIST: ShortcutDefinition[] = []

/** Picks a pseudo-random shortcut from the catalog. Used by practice
 *  mode to prompt the user with a fresh chord each round. Excludes
 *  `Escape` (the cheat sheet's own close key — pressing it would
 *  close the dialog instead of registering a practice hit) and the
 *  `?` shortcut (which would also close the dialog when cheat sheet
 *  re-opens). */
function pickRandomShortcut(exclude?: ShortcutDefinition): ShortcutDefinition {
  const pool = SHORTCUT_DEFINITIONS.filter(
    (s) =>
      s.key !== 'Escape' &&
      s.key !== '?' &&
      s !== exclude,
  )
  if (pool.length === 0) return SHORTCUT_DEFINITIONS[0]
  return pool[Math.floor(Math.random() * pool.length)]
}

/** Practice-mode state machine. */
type PracticeState =
  | { kind: 'idle' }
  | { kind: 'prompting'; shortcut: ShortcutDefinition; attempts: number }
  | { kind: 'success'; shortcut: ShortcutDefinition }
  | { kind: 'failure'; shortcut: ShortcutDefinition; pressedKey: string }

// ── Component ─────────────────────────────────────────────────────────

export default function KeyboardCheatSheet({
  isOpen,
  onClose,
}: KeyboardCheatSheetProps) {
  // Search query — filters the catalog by description (case-insensitive).
  const [query, setQuery] = useState('')
  // Active tab — lets the trader jump to a specific category, or "all"
  // to see everything at once. Defaults to "all" so the cheat sheet
  // shows the full catalog on first open.
  const [activeCategory, setActiveCategory] =
    useState<ShortcutCategory | 'all'>('all')
  // Practice-mode state. `idle` means practice is off; `prompting`
  // means a shortcut is on screen waiting for the user to press it.
  const [practice, setPractice] = useState<PracticeState>({ kind: 'idle' })
  // Toast / banner message for export feedback ("Saved JSON" / "Copy
  // failed — your browser blocked clipboard write").
  const [feedback, setFeedback] = useState<string | null>(null)

  // Focus management refs — mirrors the ConfirmationDialog pattern.
  const dialogRef = useRef<HTMLDivElement>(null)
  const closeBtnRef = useRef<HTMLButtonElement>(null)
  const searchInputRef = useRef<HTMLInputElement>(null)
  const lastActiveRef = useRef<HTMLElement | null>(null)
  // Stable timeout handle for the feedback banner auto-clear.
  const feedbackTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // ── Filtered catalog ──────────────────────────────────────────────
  // Recomputed only when the query OR active category changes. The
  // search is a case-insensitive substring match on the description
  // AND on the formatted chord (so typing "Cmd" surfaces every
  // modifier shortcut).
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q && activeCategory === 'all') return SHORTCUT_DEFINITIONS
    return SHORTCUT_DEFINITIONS.filter((s) => {
      if (activeCategory !== 'all' && s.category !== activeCategory) return false
      if (!q) return true
      const haystack = `${s.description} ${formatShortcut(s)} ${s.key} ${s.category}`.toLowerCase()
      return haystack.includes(q)
    })
  }, [query, activeCategory])

  // Group the filtered list by category so the render pass can map
  // over an ordered list of (category, items) pairs.
  const grouped = useMemo(() => {
    const groups: Array<{ category: ShortcutCategory; items: ShortcutDefinition[] }> = []
    for (const cat of Object.keys(SHORTCUT_CATEGORIES) as ShortcutCategory[]) {
      const items = filtered.filter((s) => s.category === cat)
      if (items.length > 0) groups.push({ category: cat, items })
    }
    return groups
  }, [filtered])

  // ── Open / close lifecycle ──────────────────────────────────────
  // On open: capture the previously-focused element (so we can restore
  // focus on close) and move focus into the search input so the user
  // can immediately start typing. On close: restore focus to the
  // trigger.
  useEffect(() => {
    if (!isOpen) return
    if (typeof document !== 'undefined' && document.activeElement instanceof HTMLElement) {
      lastActiveRef.current = document.activeElement
    }
    // Defer to allow the dialog to mount before we steal focus.
    const t = setTimeout(() => searchInputRef.current?.focus(), 50)
    return () => {
      clearTimeout(t)
    }
  }, [isOpen])

  // Restore focus on close.
  useEffect(() => {
    if (isOpen) return
    lastActiveRef.current?.focus?.()
    lastActiveRef.current = null
    // Reset transient state on close so the next open starts fresh.
    setQuery('')
    setActiveCategory('all')
    setPractice({ kind: 'idle' })
    setFeedback(null)
  }, [isOpen])

  // ── Escape + focus trap ──────────────────────────────────────────
  // The cheat sheet owns its own Escape handler so it can
  // stopPropagation before the parent's global Escape handler runs.
  // Without this, pressing Escape would close the cheat sheet AND
  // clear any market selection the parent tracks.
  useEffect(() => {
    if (!isOpen) return
    const handler = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.stopPropagation()
      e.preventDefault()
      onClose()
    }
    window.addEventListener('keydown', handler, true) // capture phase — runs BEFORE the parent's bubble-phase handler
    return () => window.removeEventListener('keydown', handler, true)
  }, [isOpen, onClose])

  // Focus trap — keep Tab focus cycling inside the dialog while open.
  useEffect(() => {
    if (!isOpen) return
    const el = dialogRef.current
    if (!el) return
    const selector =
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    const trap = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return
      const focusables = Array.from(el.querySelectorAll<HTMLElement>(selector))
      if (focusables.length === 0) return
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      if (e.shiftKey) {
        if (document.activeElement === first) {
          e.preventDefault()
          last?.focus()
        }
      } else {
        if (document.activeElement === last) {
          e.preventDefault()
          first?.focus()
        }
      }
    }
    el.addEventListener('keydown', trap)
    return () => el.removeEventListener('keydown', trap)
  }, [isOpen])

  // ── Practice mode ───────────────────────────────────────────────
  // Starts a practice round: pick a random shortcut, prompt the user
  // to press it, listen for the next keydown, and confirm / correct.
  const startPractice = useCallback(() => {
    const shortcut = pickRandomShortcut()
    setPractice({ kind: 'prompting', shortcut, attempts: 0 })
  }, [])

  const stopPractice = useCallback(() => {
    setPractice({ kind: 'idle' })
  }, [])

  // Practice-mode keydown handler — attached only when the practice
  // state is `prompting`. Captures the next keypress and compares it
  // to the prompt's shortcut definition.
  useEffect(() => {
    if (practice.kind !== 'prompting') return
    const handler = (e: KeyboardEvent) => {
      // Don't propagate to the parent — the practice round consumes
      // this keypress.
      e.stopPropagation()
      e.preventDefault()
      const target = practice.shortcut
      // Synthesize a KeyboardEvent-shape for the matcher (the matcher
      // reads e.key, e.metaKey, etc — all of which the real event
      // already provides).
      if (matchesShortcut(e, target)) {
        setPractice({ kind: 'success', shortcut: target })
        // Auto-advance to the next prompt after a brief celebratory
        // pause.
        setTimeout(() => {
          setPractice({ kind: 'prompting', shortcut: pickRandomShortcut(target), attempts: 0 })
        }, 900)
      } else {
        const nextAttempts = practice.attempts + 1
        // After 2 wrong attempts, show the answer + auto-advance.
        if (nextAttempts >= 2) {
          setPractice({
            kind: 'failure',
            shortcut: target,
            pressedKey: e.key,
          })
          setTimeout(() => {
            setPractice({
              kind: 'prompting',
              shortcut: pickRandomShortcut(target),
              attempts: 0,
            })
          }, 1500)
        } else {
          // Same prompt, increment attempts — give the user another try.
          setPractice({ kind: 'prompting', shortcut: target, attempts: nextAttempts })
        }
      }
    }
    // Capture phase so we run BEFORE the global hook (which would
    // otherwise dispatch the same keypress to its own action list).
    window.addEventListener('keydown', handler, true)
    return () => window.removeEventListener('keydown', handler, true)
  }, [practice])

  // ── Export ──────────────────────────────────────────────────────
  // JSON export — straightforward blob download. Image export is
  // best-effort: uses the async Clipboard API to copy a text snapshot
  // (the browser doesn't expose a "render element to PNG" API without
  // pulling in html2canvas; the text snapshot is a pragmatic fallback
  // that always works).
  const showFeedback = useCallback((message: string) => {
    setFeedback(message)
    if (feedbackTimerRef.current) clearTimeout(feedbackTimerRef.current)
    feedbackTimerRef.current = setTimeout(() => setFeedback(null), 2400)
  }, [])

  useEffect(() => {
    return () => {
      if (feedbackTimerRef.current) clearTimeout(feedbackTimerRef.current)
    }
  }, [])

  const exportJson = useCallback(() => {
    const payload = {
      generatedAt: new Date().toISOString(),
      shortcuts: SHORTCUT_DEFINITIONS.map((s) => ({
        key: s.key,
        modifiers: s.modifiers,
        description: s.description,
        category: s.category,
        global: s.global ?? false,
        formatted: formatShortcut(s),
      })),
    }
    const blob = new Blob([JSON.stringify(payload, null, 2)], {
      type: 'application/json',
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = 'keyboard-shortcuts.json'
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    showFeedback('Saved keyboard-shortcuts.json')
  }, [showFeedback])

  const exportImage = useCallback(async () => {
    // Best-effort text snapshot of the catalog — works in every
    // browser without pulling in html2canvas. The user can paste the
    // snapshot into a notes app for offline reference.
    const lines = SHORTCUT_DEFINITIONS.map(
      (s) => `${formatShortcut(s).padEnd(20)} ${s.description}`,
    ).join('\n')
    const text = `Polymarket Pro — Keyboard Shortcuts\n\n${lines}`
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text)
        showFeedback('Shortcut catalog copied to clipboard (text snapshot)')
      } else {
        showFeedback('Clipboard API unavailable — export JSON instead')
      }
    } catch {
      showFeedback('Clipboard write failed — export JSON instead')
    }
  }, [showFeedback])

  // ── Render ──────────────────────────────────────────────────────
  if (!isOpen) return null

  return (
    <div
      className="modal-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
      role="presentation"
      data-testid="cheat-sheet-backdrop"
    >
      <div
        ref={dialogRef}
        className="modal"
        style={{
          maxWidth: '780px',
          width: 'calc(100% - 32px)',
          maxHeight: '90vh',
        }}
        role="dialog"
        aria-modal="true"
        aria-labelledby="cheat-sheet-title"
        data-testid="cheat-sheet-dialog"
      >
        {/* Header ────────────────────────────────────────────────── */}
        <div className="modal-header">
          <div className="flex items-center gap-2">
            <span aria-hidden="true">⌨️</span>
            <h2
              id="cheat-sheet-title"
              className="text-sm font-bold text-[#dde1ed]"
            >
              Workstation Keyboard Cheat Sheet
            </h2>
          </div>
          <button
            ref={closeBtnRef}
            onClick={onClose}
            className="modal-close"
            aria-label="Close cheat sheet"
          >
            <span aria-hidden="true">✕</span>
          </button>
        </div>

        {/* Toolbar — search + export ─────────────────────────────── */}
        <div
          className="modal-body"
          style={{ paddingTop: 12, paddingBottom: 0 }}
        >
          <div className="flex flex-col sm:flex-row gap-2 sm:items-center">
            <Input
              ref={searchInputRef}
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search shortcuts…"
              aria-label="Filter shortcuts"
              className="flex-1"
              data-testid="cheat-sheet-search"
            />
            <div className="flex gap-1.5">
              <Button
                variant="outline"
                size="sm"
                onClick={exportJson}
                aria-label="Export shortcut catalog as JSON"
                title="Export as JSON"
              >
                ⬇ JSON
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={exportImage}
                aria-label="Copy shortcut catalog to clipboard"
                title="Copy to clipboard"
              >
                📋 Copy
              </Button>
              {practice.kind === 'idle' ? (
                <Button
                  variant="default"
                  size="sm"
                  onClick={startPractice}
                  aria-label="Start practice mode"
                  title="Practice pressing shortcuts"
                >
                  🎯 Practice
                </Button>
              ) : (
                <Button
                  variant="outline"
                  size="sm"
                  onClick={stopPractice}
                  aria-label="Stop practice mode"
                  title="Stop practice"
                >
                  ⏹ Stop
                </Button>
              )}
            </div>
          </div>

          {/* Feedback banner — auto-clears after ~2s ──────────────── */}
          {feedback && (
            <div
              role="status"
              aria-live="polite"
              className="mt-2 text-xs text-cyan-400 bg-[#0e1015] border border-[#1f2335] px-3 py-1.5 rounded"
              data-testid="cheat-sheet-feedback"
            >
              {feedback}
            </div>
          )}

          {/* Practice prompt ─────────────────────────────────────── */}
          {practice.kind !== 'idle' && (
            <PracticePanel practice={practice} />
          )}

          {/* Tabs — category filter ─────────────────────────────── */}
          <Tabs
            value={activeCategory}
            onValueChange={(v) => setActiveCategory(v as ShortcutCategory | 'all')}
            className="mt-3"
          >
            <TabsList className="flex-wrap h-auto">
              <TabsTrigger value="all">All</TabsTrigger>
              {(Object.keys(SHORTCUT_CATEGORIES) as ShortcutCategory[]).map(
                (cat) => (
                  <TabsTrigger key={cat} value={cat}>
                    <span aria-hidden="true" className="mr-1">
                      {SHORTCUT_CATEGORIES[cat].icon}
                    </span>
                    {SHORTCUT_CATEGORIES[cat].label}
                  </TabsTrigger>
                ),
              )}
            </TabsList>

            <TabsContent value={activeCategory} className="mt-3">
              {/* Body — grouped shortcuts ─────────────────────────── */}
              <div
                className="space-y-4 max-h-[55vh] overflow-y-auto scrollbar-thin pr-1"
                data-testid="cheat-sheet-list"
              >
                {grouped.length === 0 ? (
                  <div
                    className="text-center text-sm text-[#7e8aaa] py-8"
                    data-testid="cheat-sheet-empty"
                  >
                    No shortcuts match “{query}”.
                  </div>
                ) : (
                  grouped.map(({ category, items }) => (
                    <section
                      key={category}
                      aria-labelledby={`cat-${category}`}
                      data-testid={`cheat-sheet-category-${category}`}
                    >
                      <h3
                        id={`cat-${category}`}
                        className="text-[11px] font-bold uppercase tracking-wider text-[#7e8aaa] mb-1.5 flex items-center gap-1.5"
                      >
                        <span aria-hidden="true">
                          {SHORTCUT_CATEGORIES[category].icon}
                        </span>
                        {SHORTCUT_CATEGORIES[category].label}
                      </h3>
                      <ul className="space-y-1">
                        {items.map((s) => (
                          <li
                            key={`${s.category}-${s.key}-${s.modifiers.join('+')}`}
                            className="flex justify-between items-center bg-[#0e1015] px-3 py-2 rounded text-xs border border-[#1f2335] hover:border-[#2d3450] transition-colors"
                          >
                            <span className="text-[#dde1ed] pr-2">
                              {s.description}
                            </span>
                            <kbd
                              className="bg-[#13161e] text-cyan-400 border border-[#1f2335] px-2 py-0.5 rounded mono font-bold text-[11px] whitespace-nowrap"
                              aria-label={`Shortcut ${formatShortcut(s)}`}
                            >
                              {formatShortcut(s)}
                            </kbd>
                          </li>
                        ))}
                      </ul>
                    </section>
                  ))
                )}
              </div>
            </TabsContent>
          </Tabs>
        </div>

        {/* Footer ────────────────────────────────────────────────── */}
        <div className="modal-footer justify-between sm:justify-between">
          <span className="text-[11px] text-[#7e8aaa] hidden sm:inline">
            {filtered.length} of {SHORTCUT_DEFINITIONS.length} shortcuts
          </span>
          <Button onClick={onClose} size="sm">
            Got it (Esc)
          </Button>
        </div>
      </div>
    </div>
  )
}

// ── Practice panel ────────────────────────────────────────────────────

function PracticePanel({ practice }: { practice: PracticeState }) {
  if (practice.kind === 'idle') return null

  if (practice.kind === 'prompting') {
    return (
      <div
        className="mt-3 px-3 py-3 rounded border border-cyan-500/30 bg-cyan-500/5 text-center"
        data-testid="cheat-sheet-practice"
      >
        <div className="text-[11px] uppercase tracking-wider text-cyan-400 font-bold mb-1">
          Practice Mode
        </div>
        <div className="text-sm text-[#dde1ed]">
          Press: <kbd className="bg-[#13161e] text-cyan-400 border border-[#1f2335] px-2 py-0.5 rounded mono font-bold text-[12px] ml-1">
            {formatShortcut(practice.shortcut)}
          </kbd>
        </div>
        <div className="text-[11px] text-[#7e8aaa] mt-1">
          {practice.shortcut.description}
          {practice.attempts > 0 && (
            <span className="ml-2 text-amber-400">
              · attempt {practice.attempts + 1}
            </span>
          )}
        </div>
      </div>
    )
  }

  if (practice.kind === 'success') {
    return (
      <div
        className="mt-3 px-3 py-3 rounded border border-green-500/30 bg-green-500/5 text-center"
        data-testid="cheat-sheet-practice-success"
      >
        <div className="text-sm text-green-400 font-bold">
          ✓ Correct! {formatShortcut(practice.shortcut)}
        </div>
        <div className="text-[11px] text-[#7e8aaa] mt-1">
          Loading next shortcut…
        </div>
      </div>
    )
  }

  // failure
  return (
    <div
      className="mt-3 px-3 py-3 rounded border border-red-500/30 bg-red-500/5 text-center"
      data-testid="cheat-sheet-practice-failure"
    >
      <div className="text-sm text-red-400 font-bold">
        ✗ Expected <kbd className="bg-[#13161e] text-red-400 border border-[#1f2335] px-2 py-0.5 rounded mono font-bold text-[12px] mx-1">
          {formatShortcut(practice.shortcut)}
        </kbd>
      </div>
      <div className="text-[11px] text-[#7e8aaa] mt-1">
        You pressed: <span className="mono">{practice.pressedKey}</span> ·
        loading next…
      </div>
    </div>
  )
}

// Re-export for tests + consumers that want the catalog directly.
export { EMPTY_LIST as _EMPTY_LIST, pickRandomShortcut as _pickRandomShortcut }
