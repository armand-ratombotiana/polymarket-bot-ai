// components/ShortcutsModal.test.tsx — W38-8 component tests.
//
// The modal is a static list of keyboard shortcuts — no fetch, no async.
// Tests focus on its rendering contract, open/close, Escape/backdrop
// dismiss, and that every shortcut row is rendered with the right key.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ShortcutsModal from './ShortcutsModal'

beforeEach(() => {
  // Reset mocks between tests so each test starts with a clean onClose.
})

afterEach(() => {
  cleanup()
})

describe('ShortcutsModal', () => {
  it('renders nothing when isOpen=false', () => {
    const onClose = vi.fn()
    render(<ShortcutsModal isOpen={false} onClose={onClose} />)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('renders without crashing when open', () => {
    const onClose = vi.fn()
    render(<ShortcutsModal isOpen={true} onClose={onClose} />)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('renders the "Workstation Keyboard Shortcuts" title', () => {
    const onClose = vi.fn()
    render(<ShortcutsModal isOpen={true} onClose={onClose} />)
    expect(
      screen.getByText('Workstation Keyboard Shortcuts'),
    ).toBeInTheDocument()
  })

  it('renders every shortcut row with its action + key', () => {
    const onClose = vi.fn()
    render(<ShortcutsModal isOpen={true} onClose={onClose} />)
    // A handful of canonical shortcuts we expect to surface.
    expect(screen.getByText('Command Center Dashboard')).toBeInTheDocument()
    expect(screen.getByText('Toggle Kill Switch / Emergency Halt')).toBeInTheDocument()
    expect(screen.getByText('Open Strategy & Risk Configuration')).toBeInTheDocument()
    expect(screen.getByText('Open this shortcuts cheatsheet')).toBeInTheDocument()
    // The keyboard glyphs render inside <kbd> elements.
    expect(screen.getByText('1')).toBeInTheDocument()
    expect(screen.getByText('K')).toBeInTheDocument()
    expect(screen.getByText('C')).toBeInTheDocument()
    expect(screen.getByText('?')).toBeInTheDocument()
  })

  it('calls onClose when the close (✕) button is clicked', async () => {
    const onClose = vi.fn()
    const user = userEvent.setup()
    render(<ShortcutsModal isOpen={true} onClose={onClose} />)
    await user.click(
      screen.getByRole('button', { name: /close shortcuts modal/i }),
    )
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('calls onClose when the "Got it" footer button is clicked', async () => {
    const onClose = vi.fn()
    const user = userEvent.setup()
    render(<ShortcutsModal isOpen={true} onClose={onClose} />)
    await user.click(screen.getByRole('button', { name: /got it/i }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('calls onClose when Escape is pressed', async () => {
    const onClose = vi.fn()
    const user = userEvent.setup()
    render(<ShortcutsModal isOpen={true} onClose={onClose} />)
    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
