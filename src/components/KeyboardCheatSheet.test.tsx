// components/KeyboardCheatSheet.test.tsx — W40-2 minimal render tests for
// the W17-6 keyboard cheat sheet.
//
// The cheat sheet is a static catalog (no fetch, no clock). Tests cover:
//   1. Renders nothing when `isOpen` is false.
//   2. Renders the dialog + title when `isOpen` is true.
//   3. Calls `onClose` when the Escape key is pressed.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import KeyboardCheatSheet from './KeyboardCheatSheet'

global.fetch = vi.fn()

afterEach(() => {
  cleanup()
})

describe('KeyboardCheatSheet', () => {
  it('renders null when isOpen is false', () => {
    render(<KeyboardCheatSheet isOpen={false} onClose={vi.fn()} />)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('renders without crashing when open', () => {
    render(<KeyboardCheatSheet isOpen={true} onClose={vi.fn()} />)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('renders the "Workstation Keyboard Cheat Sheet" title', () => {
    render(<KeyboardCheatSheet isOpen={true} onClose={vi.fn()} />)
    expect(
      screen.getByText('Workstation Keyboard Cheat Sheet'),
    ).toBeInTheDocument()
  })

  it('calls onClose when Escape is pressed', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    render(<KeyboardCheatSheet isOpen={true} onClose={onClose} />)
    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
