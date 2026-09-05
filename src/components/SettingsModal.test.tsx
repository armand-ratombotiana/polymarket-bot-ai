// components/SettingsModal.test.tsx — W38-8 component tests.
//
// Strategy: the modal reads from the persisted preferences store
// (localStorage + a CustomEvent subscription). Each test seeds the
// store via `savePreferences` so the modal renders a known state.
// We do NOT mock `usePreferences` — the real hook + lib pair is
// exercised end-to-end (same approach as `usePreferences.test.ts`).
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import SettingsModal from './SettingsModal'
import {
  savePreferences,
  getDefaults,
} from '@/lib/preferences'

beforeEach(() => {
  window.localStorage.clear()
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('SettingsModal', () => {
  it('renders nothing when isOpen=false', () => {
    render(<SettingsModal isOpen={false} onClose={vi.fn()} />)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('renders without crashing when open', () => {
    savePreferences(getDefaults())
    render(<SettingsModal isOpen={true} onClose={vi.fn()} />)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('renders the "User Preferences" title header', () => {
    savePreferences(getDefaults())
    render(<SettingsModal isOpen={true} onClose={vi.fn()} />)
    expect(screen.getByText('User Preferences')).toBeInTheDocument()
  })

  it('renders all six section headers in canonical order', () => {
    savePreferences(getDefaults())
    render(<SettingsModal isOpen={true} onClose={vi.fn()} />)
    // Section headers use <h3> with uppercase text-cyan-400.
    for (const section of [
      'Display',
      'Dashboard',
      'Trading',
      'Notifications',
      'Sound',
      'Privacy',
    ]) {
      expect(screen.getByText(section)).toBeInTheDocument()
    }
  })

  it('renders the Save changes button (disabled when no edits made)', () => {
    savePreferences(getDefaults())
    render(<SettingsModal isOpen={true} onClose={vi.fn()} />)
    const save = screen.getByRole('button', { name: /save preferences and close/i })
    expect(save).toBeDisabled()
  })

  it('renders the Cancel + Reset to defaults buttons', () => {
    savePreferences(getDefaults())
    render(<SettingsModal isOpen={true} onClose={vi.fn()} />)
    expect(
      screen.getByRole('button', { name: /cancel/i }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /reset all preferences to defaults/i }),
    ).toBeInTheDocument()
  })

  it('renders the close (✕) button', () => {
    savePreferences(getDefaults())
    render(<SettingsModal isOpen={true} onClose={vi.fn()} />)
    expect(
      screen.getByRole('button', { name: /close settings modal/i }),
    ).toBeInTheDocument()
  })

  it('calls onClose when the close (✕) button is clicked', async () => {
    const onClose = vi.fn()
    const user = userEvent.setup()
    savePreferences(getDefaults())
    render(<SettingsModal isOpen={true} onClose={onClose} />)
    await user.click(
      screen.getByRole('button', { name: /close settings modal/i }),
    )
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('calls onClose when Escape is pressed', async () => {
    const onClose = vi.fn()
    const user = userEvent.setup()
    savePreferences(getDefaults())
    render(<SettingsModal isOpen={true} onClose={onClose} />)
    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('calls onClose when Cancel is clicked', async () => {
    const onClose = vi.fn()
    const user = userEvent.setup()
    savePreferences(getDefaults())
    render(<SettingsModal isOpen={true} onClose={onClose} />)
    await user.click(
      screen.getByRole('button', { name: /cancel/i }),
    )
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('enables the Save changes button after a preference is edited', async () => {
    const user = userEvent.setup()
    // Start from a known state where autoRefresh=true.
    savePreferences({ ...getDefaults(), autoRefresh: true })
    render(<SettingsModal isOpen={true} onClose={vi.fn()} />)
    // Save should be disabled initially.
    const save = screen.getByRole('button', { name: /save preferences and close/i })
    expect(save).toBeDisabled()
    // Flip autoRefresh off via the Switch control.
    const toggle = screen.getByRole('switch', { name: 'Auto-refresh' })
    await user.click(toggle)
    // Save should now be enabled (draft ≠ preferences).
    await waitFor(() => {
      expect(save).not.toBeDisabled()
    })
  })

  it('persists the edited preference to localStorage on Save', async () => {
    const user = userEvent.setup()
    savePreferences({ ...getDefaults(), autoRefresh: true })
    render(<SettingsModal isOpen={true} onClose={vi.fn()} />)
    const toggle = screen.getByRole('switch', { name: 'Auto-refresh' })
    await user.click(toggle)
    await user.click(
      screen.getByRole('button', { name: /save preferences and close/i }),
    )
    // The persistence call writes to localStorage under the canonical key.
    const persisted = JSON.parse(
      window.localStorage.getItem('polymarket_preferences') ?? '{}',
    )
    expect(persisted.autoRefresh).toBe(false)
  })

  it('does NOT persist when Cancel is clicked (draft is discarded)', async () => {
    const user = userEvent.setup()
    savePreferences({ ...getDefaults(), autoRefresh: true })
    render(<SettingsModal isOpen={true} onClose={vi.fn()} />)
    const toggle = screen.getByRole('switch', { name: 'Auto-refresh' })
    await user.click(toggle)
    await user.click(
      screen.getByRole('button', { name: /cancel/i }),
    )
    const persisted = JSON.parse(
      window.localStorage.getItem('polymarket_preferences') ?? '{}',
    )
    expect(persisted.autoRefresh).toBe(true)
  })

  it('resets the draft to defaults when "Reset to defaults" is clicked', async () => {
    const user = userEvent.setup()
    // Start with non-default values so we can detect the reset.
    savePreferences({
      ...getDefaults(),
      autoRefresh: false,
      soundEnabled: true,
    })
    render(<SettingsModal isOpen={true} onClose={vi.fn()} />)
    await user.click(
      screen.getByRole('button', { name: /reset all preferences to defaults/i }),
    )
    // The Save button should become enabled since the draft now differs
    // from the persisted (non-default) state.
    const save = screen.getByRole('button', { name: /save preferences and close/i })
    await waitFor(() => {
      expect(save).not.toBeDisabled()
    })
  })
})
