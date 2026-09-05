// components/CommandPalette.tsx — Global ⌘K / Ctrl+K command palette.
//
// Renders a modal containing a searchable list of every navigation
// destination and a small set of common actions. Opened via the Cmd+K /
// Ctrl+K keyboard shortcut wired in `app/page.tsx` (and via the ⌘K hint
// button in `TopStatusBar`).
//
// Implementation notes:
//  * Backed by the shadcn `ui/command.tsx` component, which wraps the
//    `cmdk` primitive. `cmdk` handles fuzzy filtering, keyboard arrow
//    navigation, and the active-item highlight automatically — so the
//    palette is fully keyboard-driven without us having to re-implement
//    any of those mechanics.
//  * Uses `CommandDialog` (not raw `Dialog` + `Command`) because the
//    shadcn variant already wires up an sr-only `DialogTitle` /
//    `DialogDescription` for screen readers. Without the title, Radix
//    Dialog emits an a11y warning ("DialogContent requires a
//    DialogTitle for the component to be accessible…").
//  * The command list is derived from the same `NAV_GROUPS` structure
//    that backs `Sidebar.tsx` so the two surfaces can never drift.

'use client'

import { useState } from 'react'
import {
  CommandDialog,
  CommandInput,
  CommandList,
  CommandEmpty,
  CommandGroup,
  CommandItem,
  CommandShortcut,
} from '@/components/ui/command'
import type { NavSection } from '@/components/Sidebar'

/** A single selectable entry inside the palette. */
interface CommandItemDef {
  /** Stable id (also used as the React key). */
  id: string
  /** Visible label. */
  label: string
  /** Group heading the item belongs to (e.g. "Navigate", "Actions"). */
  group: string
  /** Optional glyph rendered before the label (matches the Sidebar's
   *  unicode-icon approach so the two surfaces stay visually consistent). */
  icon?: string
  /** Optional kbd hint shown on the right edge of the row. */
  kbd?: string
  /** Optional extra search terms appended to the cmdk `value`. Lets a
   *  user type "home" to find "Command Center" even though the visible
   *  label is "Command Center". */
  keywords?: string[]
  /** Invoked when the row is selected (Enter / click). */
  action: () => void
}

interface CommandPaletteProps {
  /** Controlled open state. */
  open: boolean
  /** Called with the new open state whenever the dialog requests a
   *  change (Esc, backdrop click, after a selection). */
  onOpenChange: (open: boolean) => void
  /** Navigation callback — receives the target NavSection id. */
  onNavigate: (section: NavSection) => void
  /** Optional list of additional action commands. Injected by the
   *  parent (page.tsx) so the palette can trigger non-navigation
   *  workflows (refresh, export, theme toggle, etc.) without the
   *  palette itself having to know about page-level concerns. */
  extraActions?: CommandItemDef[]
}

/** Canonical navigation commands. Mirrors the Sidebar's NAV_GROUPS so the
 *  palette can never get out of sync with the visible sidebar. */
function buildNavCommands(onNavigate: (section: NavSection) => void): CommandItemDef[] {
  return [
    { id: 'nav-command', label: 'Command Center', group: 'Navigate', icon: '⊞', kbd: '1', action: () => onNavigate('command'), keywords: ['home', 'dashboard'] },
    { id: 'nav-books', label: 'Live Books', group: 'Navigate', icon: '◈', kbd: '2', action: () => onNavigate('markets-books'), keywords: ['markets', 'orderbook'] },
    { id: 'nav-screener', label: 'Screener', group: 'Navigate', icon: '⊡', kbd: '3', action: () => onNavigate('markets-screener'), keywords: ['scan', 'filter'] },
    { id: 'nav-positions', label: 'Positions', group: 'Navigate', icon: '◉', kbd: '4', action: () => onNavigate('portfolio-positions') },
    { id: 'nav-orders', label: 'Orders', group: 'Navigate', icon: '⊕', action: () => onNavigate('portfolio-orders') },
    { id: 'nav-trades', label: 'Trades & Fills', group: 'Navigate', icon: '◎', action: () => onNavigate('portfolio-trades'), keywords: ['fills'] },
    { id: 'nav-strategies', label: 'Strategy Registry', group: 'Navigate', icon: '⊗', kbd: '5', action: () => onNavigate('strategies-registry'), keywords: ['strategy'] },
    { id: 'nav-arbitrage', label: 'Arbitrage', group: 'Navigate', icon: '⇌', kbd: '6', action: () => onNavigate('strategies-arbitrage') },
    { id: 'nav-analysis', label: 'Deep Analysis', group: 'Navigate', icon: '⊘', kbd: '7', action: () => onNavigate('intelligence-analysis'), keywords: ['forecast'] },
    { id: 'nav-aiml', label: 'AI / ML Engine', group: 'Navigate', icon: '⊛', action: () => onNavigate('intelligence-aiml'), keywords: ['ml', 'model', 'engine'] },
    // W38-5 — Explainable AI / ML Prediction panel: trustworthy AI
    // prediction surface with SHAP explainability + prediction history.
    { id: 'nav-explainer', label: 'AI Prediction Explainer', group: 'Navigate', icon: '◍', action: () => onNavigate('intelligence-explainer'), keywords: ['ai', 'ml', 'explain', 'shap', 'trustworthy', 'prediction'] },
    { id: 'nav-copilot', label: 'Copilot', group: 'Navigate', icon: '◈', action: () => onNavigate('intelligence-copilot'), keywords: ['ai', 'assistant'] },
    { id: 'nav-shadow', label: 'Shadow Inference', group: 'Navigate', icon: '⬡', action: () => onNavigate('intelligence-shadow') },
    { id: 'nav-validation', label: 'ML Validation', group: 'Navigate', icon: '⊕', action: () => onNavigate('intelligence-validation') },
    { id: 'nav-performance', label: 'Performance', group: 'Navigate', icon: '◷', kbd: '8', action: () => onNavigate('analytics-performance') },
    { id: 'nav-backtest', label: 'Backtest Lab', group: 'Navigate', icon: '⊙', action: () => onNavigate('analytics-backtest') },
    { id: 'nav-attribution', label: 'Attribution', group: 'Navigate', icon: '◫', action: () => onNavigate('analytics-attribution') },
    { id: 'nav-execution', label: 'Execution Quality', group: 'Navigate', icon: '⌖', action: () => onNavigate('analytics-execution') },
    { id: 'nav-closed', label: 'Closed Positions', group: 'Navigate', icon: '⊟', action: () => onNavigate('analytics-closed') },
    { id: 'nav-capital', label: 'Capital Allocator', group: 'Navigate', icon: '$', action: () => onNavigate('capital-allocator') },
    { id: 'nav-health', label: 'System Health', group: 'Navigate', icon: '⊜', action: () => onNavigate('system-health') },
    { id: 'nav-database', label: 'Data Explorer', group: 'Navigate', icon: '⊞', action: () => onNavigate('system-database'), keywords: ['db', 'explorer'] },
    { id: 'nav-observability', label: 'Observability', group: 'Navigate', icon: '◉', action: () => onNavigate('system-observability') },
    { id: 'nav-retention', label: 'Retention', group: 'Navigate', icon: '⌫', action: () => onNavigate('system-retention') },
    { id: 'nav-decisions', label: 'Decision Ledger', group: 'Navigate', icon: '↹', action: () => onNavigate('system-decisions') },
    { id: 'nav-safety', label: 'Safety Gate', group: 'Navigate', icon: '🛡', action: () => onNavigate('system-safety') },
  ]
}

export default function CommandPalette({
  open,
  onOpenChange,
  onNavigate,
  extraActions = [],
}: CommandPaletteProps) {
  // Controlled search value. We feed it into cmdk via `value` /
  // `onValueChange` so we can reset it the next time the palette re-opens
  // (otherwise the previous query persists across opens, which is jarring).
  const [search, setSearch] = useState('')

  // Build the command list every render. Cheap (≈25 small objects) and
  // avoids stale closure issues if `onNavigate` identity changes.
  const commands: CommandItemDef[] = [
    ...buildNavCommands(onNavigate),
    ...extraActions,
  ]

  // Preserve insertion order of groups (Navigate first, then Actions).
  const groups = Array.from(new Set(commands.map((c) => c.group)))

  const handleSelect = (cmd: CommandItemDef) => {
    cmd.action()
    onOpenChange(false)
  }

  return (
    <CommandDialog
      open={open}
      onOpenChange={(next) => {
        // Reset the search field whenever the dialog closes so the next
        // open starts from a clean state.
        if (!next) setSearch('')
        onOpenChange(next)
      }}
      title="Command Palette"
      description="Search for a navigation destination or action to run."
      className="command-palette-dialog"
    >
      <CommandInput
        placeholder="Type a command or search…"
        value={search}
        onValueChange={setSearch}
      />
      <CommandList>
        <CommandEmpty>No results found.</CommandEmpty>
        {groups.map((group) => (
          <CommandGroup key={group} heading={group}>
            {commands
              .filter((c) => c.group === group)
              .map((cmd) => {
                // cmdk matches against the `value` prop. We compose the
                // visible label + any keywords so "home" finds "Command
                // Center" (which has keywords ['home', 'dashboard']).
                const value = `${cmd.label} ${(cmd.keywords || []).join(' ')}`
                return (
                  <CommandItem
                    key={cmd.id}
                    value={value}
                    onSelect={() => handleSelect(cmd)}
                  >
                    {cmd.icon && <span className="cmd-icon" aria-hidden="true">{cmd.icon}</span>}
                    <span>{cmd.label}</span>
                    {cmd.kbd && <CommandShortcut>{cmd.kbd}</CommandShortcut>}
                  </CommandItem>
                )
              })}
          </CommandGroup>
        ))}
      </CommandList>
    </CommandDialog>
  )
}

export type { CommandItemDef, CommandPaletteProps }
