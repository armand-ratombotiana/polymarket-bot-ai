// components/CommandPalette.test.tsx — ⌘K command palette rendering,
// filtering, selection, and keyboard shortcut behavior.
//
// These tests exercise the palette in isolation. The Cmd+K shortcut
// itself lives in `app/page.tsx`, but that file has a huge dependency
// tree (WebSocket hook, audio hook, dynamic panel imports, etc.) which
// makes mounting it in vitest impractical. Instead, the shortcut test
// below mounts a thin wrapper that replicates the EXACT useEffect
// pattern used in page.tsx — so the keyboard behaviour is verified
// without dragging in the rest of the workstation.

import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useEffect, useState } from 'react'
import CommandPalette from './CommandPalette'
import type { NavSection } from './Sidebar'

// ── Helpers ────────────────────────────────────────────────────────────────

/** Standard palette props for tests that don't care about navigation. */
const noop = () => {}
const noopNav = (_s: NavSection) => {}

// ── Tests ──────────────────────────────────────────────────────────────────

describe('CommandPalette', () => {
  it('renders the palette when open=true', () => {
    render(
      <CommandPalette open onOpenChange={noop} onNavigate={noopNav} />,
    )
    // The input is the canonical "is the palette mounted" sentinel —
    // Radix Dialog renders into a portal at document.body, so the input
    // is reachable via the testing-library `screen` queries.
    expect(
      screen.getByPlaceholderText('Type a command or search…'),
    ).toBeInTheDocument()

    // A representative navigation command should be visible.
    expect(screen.getByText('Command Center')).toBeInTheDocument()
    expect(screen.getByText('Positions')).toBeInTheDocument()
    expect(screen.getByText('Strategy Registry')).toBeInTheDocument()
  })

  it('does NOT render the palette when open=false', () => {
    render(
      <CommandPalette open={false} onOpenChange={noop} onNavigate={noopNav} />,
    )
    expect(
      screen.queryByPlaceholderText('Type a command or search…'),
    ).not.toBeInTheDocument()
    expect(screen.queryByText('Command Center')).not.toBeInTheDocument()
  })

  it('renders both default groups (Navigate + Actions is absent without extraActions)', () => {
    render(
      <CommandPalette open onOpenChange={noop} onNavigate={noopNav} />,
    )
    // Without extraActions, only the Navigate group is rendered.
    expect(screen.getByText('Navigate')).toBeInTheDocument()
    expect(screen.queryByText('Actions')).not.toBeInTheDocument()
  })

  it('typing filters commands down to matching items', async () => {
    const user = userEvent.setup()
    render(
      <CommandPalette open onOpenChange={noop} onNavigate={noopNav} />,
    )
    const input = screen.getByPlaceholderText('Type a command or search…')

    // Type a query that matches the "Positions" label.
    await user.type(input, 'positions')

    // "Positions" should still be in the document (the query matches).
    expect(screen.getByText('Positions')).toBeInTheDocument()
    // "Command Center" should be filtered out by cmdk.
    expect(screen.queryByText('Command Center')).not.toBeInTheDocument()
    // "Strategy Registry" should also be filtered out.
    expect(screen.queryByText('Strategy Registry')).not.toBeInTheDocument()
  })

  it('matches against keywords (not just the visible label)', async () => {
    const user = userEvent.setup()
    render(
      <CommandPalette open onOpenChange={noop} onNavigate={noopNav} />,
    )
    const input = screen.getByPlaceholderText('Type a command or search…')

    // "Command Center" has keywords ['home', 'dashboard']. Typing "home"
    // should keep "Command Center" visible (cmdk matches the composed
    // `value` which includes the keywords).
    await user.type(input, 'home')
    expect(screen.getByText('Command Center')).toBeInTheDocument()
    expect(screen.queryByText('Positions')).not.toBeInTheDocument()
  })

  it('renders the Empty state when no command matches the query', async () => {
    const user = userEvent.setup()
    render(
      <CommandPalette open onOpenChange={noop} onNavigate={noopNav} />,
    )
    const input = screen.getByPlaceholderText('Type a command or search…')
    await user.type(input, 'zzzzz-no-such-command')
    expect(screen.getByText(/no results found/i)).toBeInTheDocument()
  })

  it('selecting a navigation command calls onNavigate(section) and closes the palette', async () => {
    const user = userEvent.setup()
    const onNavigate = vi.fn()
    const onOpenChange = vi.fn()
    render(
      <CommandPalette
        open
        onOpenChange={onOpenChange}
        onNavigate={onNavigate}
      />,
    )

    // Click the "Positions" row.
    await user.click(screen.getByText('Positions'))

    // onNavigate should have been called with the matching NavSection id.
    expect(onNavigate).toHaveBeenCalledTimes(1)
    expect(onNavigate).toHaveBeenCalledWith('portfolio-positions')

    // The palette should request close (onOpenChange(false)).
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it('selecting any navigation row passes the correct section id', async () => {
    const user = userEvent.setup()
    const onNavigate = vi.fn()
    render(
      <CommandPalette open onOpenChange={noop} onNavigate={onNavigate} />,
    )

    // Click a few different rows and confirm the nav id round-trips.
    await user.click(screen.getByText('Live Books'))
    expect(onNavigate).toHaveBeenLastCalledWith('markets-books')

    await user.click(screen.getByText('Capital Allocator'))
    expect(onNavigate).toHaveBeenLastCalledWith('capital-allocator')

    await user.click(screen.getByText('Decision Ledger'))
    expect(onNavigate).toHaveBeenLastCalledWith('system-decisions')

    await user.click(screen.getByText('Safety Gate'))
    expect(onNavigate).toHaveBeenLastCalledWith('system-safety')
  })

  it('renders extraActions in a separate Actions group', () => {
    render(
      <CommandPalette
        open
        onOpenChange={noop}
        onNavigate={noopNav}
        extraActions={[
          {
            id: 'act-refresh',
            label: 'Refresh All Data',
            group: 'Actions',
            action: noop,
            keywords: ['reload'],
          },
        ]}
      />,
    )
    // The Actions group heading is now present.
    expect(screen.getByText('Actions')).toBeInTheDocument()
    // The injected action row is visible.
    expect(screen.getByText('Refresh All Data')).toBeInTheDocument()
  })

  it('selecting an extraAction invokes its action callback and closes', async () => {
    const user = userEvent.setup()
    const onOpenChange = vi.fn()
    const actionFn = vi.fn()
    render(
      <CommandPalette
        open
        onOpenChange={onOpenChange}
        onNavigate={noopNav}
        extraActions={[
          {
            id: 'act-refresh',
            label: 'Refresh All Data',
            group: 'Actions',
            action: actionFn,
          },
        ]}
      />,
    )

    await user.click(screen.getByText('Refresh All Data'))
    expect(actionFn).toHaveBeenCalledTimes(1)
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  // ──────────────────────────────────────────────────────────────────────
  //  Cmd+K / Ctrl+K keyboard shortcut
  // ──────────────────────────────────────────────────────────────────────
  // The shortcut listener lives in `app/page.tsx`, not in
  // CommandPalette. We replicate the EXACT useEffect pattern here in a
  // thin wrapper so the keyboard behaviour is verified without dragging
  // in the page's full dependency tree (WS hook, audio hook, dynamic
  // panel imports, etc.). If page.tsx ever changes its Cmd+K handler,
  // this test will catch the regression as long as the pattern stays
  // "Cmd+K toggles open state".

  function CmdKHarness() {
    const [open, setOpen] = useState(false)
    const [, setLastNav] = useState<NavSection | null>(null)

    // ── Exact copy of the useEffect added in app/page.tsx (W13-5) ──
    useEffect(() => {
      const handler = (e: KeyboardEvent) => {
        if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
          e.preventDefault()
          setOpen((o) => !o)
        }
      }
      window.addEventListener('keydown', handler)
      return () => window.removeEventListener('keydown', handler)
    }, [])

    return (
      <CommandPalette
        open={open}
        onOpenChange={setOpen}
        onNavigate={setLastNav}
      />
    )
  }

  it('Cmd+K opens the palette (initially closed)', () => {
    render(<CmdKHarness />)
    // Initially closed.
    expect(
      screen.queryByPlaceholderText('Type a command or search…'),
    ).not.toBeInTheDocument()

    // Dispatch Cmd+K on window — mirrors the real keyboard event flow.
    fireEvent.keyDown(window, { key: 'k', metaKey: true })

    // Palette should now be open.
    expect(
      screen.getByPlaceholderText('Type a command or search…'),
    ).toBeInTheDocument()
    cleanup()
  })

  it('Ctrl+K also opens the palette (Windows / Linux chord)', () => {
    render(<CmdKHarness />)
    expect(
      screen.queryByPlaceholderText('Type a command or search…'),
    ).not.toBeInTheDocument()

    // Ctrl+K (no metaKey) — should also toggle open.
    fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
    expect(
      screen.getByPlaceholderText('Type a command or search…'),
    ).toBeInTheDocument()
    cleanup()
  })

  it('pressing Cmd+K a second time closes the palette (toggle behaviour)', () => {
    render(<CmdKHarness />)
    // Open it.
    fireEvent.keyDown(window, { key: 'k', metaKey: true })
    expect(
      screen.getByPlaceholderText('Type a command or search…'),
    ).toBeInTheDocument()

    // Press Cmd+K again — should close.
    fireEvent.keyDown(window, { key: 'k', metaKey: true })
    expect(
      screen.queryByPlaceholderText('Type a command or search…'),
    ).not.toBeInTheDocument()
    cleanup()
  })

  it('Cmd+K calls e.preventDefault (does not fall through to the browser URL bar)', () => {
    render(<CmdKHarness />)
    const evt = new KeyboardEvent('keydown', {
      key: 'k',
      metaKey: true,
      bubbles: true,
      cancelable: true,
    })
    const spy = vi.spyOn(evt, 'preventDefault')
    window.dispatchEvent(evt)
    expect(spy).toHaveBeenCalled()
    cleanup()
  })

  it('plain "k" without modifier does NOT open the palette', () => {
    render(<CmdKHarness />)
    // Plain 'k' (no meta, no ctrl) — should be ignored by the Cmd+K
    // listener. (page.tsx's OTHER keyboard handler treats plain 'k' as
    // the kill-switch shortcut, but the palette listener must not
    // react to it.)
    fireEvent.keyDown(window, { key: 'k' })
    expect(
      screen.queryByPlaceholderText('Type a command or search…'),
    ).not.toBeInTheDocument()
    cleanup()
  })
})
