// components/Sidebar.test.tsx — Navigation, accessibility & mobile behaviour.
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import Sidebar from './Sidebar'

const EXPECTED_GROUPS = [
  'Main',
  'Markets',
  'Portfolio',
  'Capital',
  'Strategies',
  'Intelligence',
  'Analytics',
  'System',
] as const

/** All clickable nav-item buttons inside the sidebar (excludes the collapse
 *  toggle and any other buttons that may exist outside the nav). */
function getNavItemButtons(container: HTMLElement): HTMLButtonElement[] {
  return Array.from(
    container.querySelectorAll('button.sidebar-item'),
  ) as HTMLButtonElement[]
}

describe('Sidebar', () => {
  it('renders all eight nav group labels', () => {
    render(<Sidebar active="command" onChange={() => {}} />)
    EXPECTED_GROUPS.forEach((label) => {
      expect(screen.getByText(label)).toBeInTheDocument()
    })
  })

  it('renders nav items as <button> elements', () => {
    const { container } = render(<Sidebar active="command" onChange={() => {}} />)
    const items = getNavItemButtons(container)
    // 8 groups × at least 1 item each = at least 8 items.
    expect(items.length).toBeGreaterThanOrEqual(8)
    items.forEach((item) => {
      expect(item.tagName).toBe('BUTTON')
    })
  })

  it('renders a navigation landmark with aria-label', () => {
    render(<Sidebar active="command" onChange={() => {}} />)
    const nav = screen.getByRole('navigation')
    expect(nav).toHaveAttribute('aria-label', 'Primary navigation')
  })

  it('calls onChange with the correct section id when a nav item is clicked', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    render(<Sidebar active="command" onChange={onChange} />)

    await user.click(screen.getByText('Positions'))
    expect(onChange).toHaveBeenCalledWith('portfolio-positions')

    await user.click(screen.getByText('Live Books'))
    expect(onChange).toHaveBeenCalledWith('markets-books')

    await user.click(screen.getByText('Capital Allocator'))
    expect(onChange).toHaveBeenCalledWith('capital-allocator')
  })

  it('marks the active item with the "active" class', () => {
    render(<Sidebar active="portfolio-positions" onChange={() => {}} />)
    const activeItem = screen.getByText('Positions').closest('button')
    expect(activeItem?.className).toContain('active')
  })

  it('does NOT mark inactive items with the "active" class', () => {
    render(<Sidebar active="command" onChange={() => {}} />)
    const inactiveItem = screen.getByText('Positions').closest('button')
    expect(inactiveItem?.className).not.toContain('active')
  })

  it('sets aria-current="page" on the active item', () => {
    render(<Sidebar active="command" onChange={() => {}} />)
    // The only button with aria-current="page" is the active nav item.
    const activeItem = screen.getByRole('button', { current: 'page' })
    expect(activeItem).toBeInTheDocument()
    expect(activeItem.textContent).toContain('Command Center')
  })

  it('does not set aria-current on inactive items', () => {
    const { container } = render(<Sidebar active="command" onChange={() => {}} />)
    const navButtons = getNavItemButtons(container)
    const withCurrent = navButtons.filter(
      (b) => b.getAttribute('aria-current') === 'page',
    )
    expect(withCurrent).toHaveLength(1)
    expect(withCurrent[0].textContent).toContain('Command Center')
  })

  it('calls onMobileClose when an item is clicked on mobile (mobileOpen=true)', async () => {
    const user = userEvent.setup()
    const onChange = vi.fn()
    const onMobileClose = vi.fn()
    render(
      <Sidebar
        active="command"
        onChange={onChange}
        mobileOpen
        onMobileClose={onMobileClose}
      />,
    )
    await user.click(screen.getByText('Positions'))
    expect(onChange).toHaveBeenCalledWith('portfolio-positions')
    expect(onMobileClose).toHaveBeenCalledTimes(1)
  })

  it('calls onMobileClose on every nav click when the callback is provided', async () => {
    // The component invokes `onMobileClose?.()` unconditionally after each
    // selection — the parent decides whether to actually close the drawer
    // (typically by checking mobileOpen state itself).
    const user = userEvent.setup()
    const onMobileClose = vi.fn()
    render(
      <Sidebar
        active="command"
        onChange={() => {}}
        onMobileClose={onMobileClose}
      />,
    )
    await user.click(screen.getByText('Positions'))
    expect(onMobileClose).toHaveBeenCalledTimes(1)
  })

  it('renders the mobile backdrop when mobileOpen=true', () => {
    render(
      <Sidebar
        active="command"
        onChange={() => {}}
        mobileOpen
        onMobileClose={() => {}}
      />,
    )
    const backdrop = document.querySelector('[aria-hidden="true"].fixed.inset-0')
    expect(backdrop).toBeInTheDocument()
  })

  it('does NOT render the mobile backdrop when mobileOpen is not set', () => {
    render(<Sidebar active="command" onChange={() => {}} />)
    const backdrop = document.querySelector('[aria-hidden="true"].fixed.inset-0')
    expect(backdrop).not.toBeInTheDocument()
  })

  it('clicking the mobile backdrop calls onMobileClose', async () => {
    const user = userEvent.setup()
    const onMobileClose = vi.fn()
    render(
      <Sidebar
        active="command"
        onChange={() => {}}
        mobileOpen
        onMobileClose={onMobileClose}
      />,
    )
    const backdrop = document.querySelector('[aria-hidden="true"].fixed.inset-0') as HTMLElement
    expect(backdrop).not.toBeNull()
    await user.click(backdrop)
    expect(onMobileClose).toHaveBeenCalledTimes(1)
  })

  it('renders the footer "Bot Engine Active" status', () => {
    render(<Sidebar active="command" onChange={() => {}} />)
    expect(screen.getByText('Bot Engine Active')).toBeInTheDocument()
  })

  it('renders the collapse/expand toggle button', () => {
    render(<Sidebar active="command" onChange={() => {}} />)
    const toggle = screen.getByRole('button', { name: /collapse sidebar/i })
    expect(toggle).toBeInTheDocument()
  })

  it('keyboard focusable: nav item buttons are not disabled', () => {
    const { container } = render(<Sidebar active="command" onChange={() => {}} />)
    const items = getNavItemButtons(container)
    expect(items.length).toBeGreaterThan(0)
    items.forEach((item) => {
      expect(item).not.toHaveAttribute('disabled')
    })
  })

  it('renders all expected primary nav items', () => {
    render(<Sidebar active="command" onChange={() => {}} />)
    const expectedItems = [
      'Command Center',
      'Live Books',
      'Screener',
      'Positions',
      'Orders',
      'Trades & Fills',
      'Capital Allocator',
      'Strategy Registry',
      'Arbitrage',
      'Deep Analysis',
      'System Health',
      'Data Explorer',
    ]
    expectedItems.forEach((label) => {
      expect(screen.getByText(label)).toBeInTheDocument()
    })
  })

  it('renders sr-only keyboard-shortcut hints for items that have one', () => {
    render(<Sidebar active="command" onChange={() => {}} />)
    // Command Center has kbd="1" → sr-only "(Keyboard shortcut: press 1)"
    expect(screen.getByText(/Keyboard shortcut: press 1/)).toBeInTheDocument()
    // Markets-Books has kbd="2"
    expect(screen.getByText(/Keyboard shortcut: press 2/)).toBeInTheDocument()
  })
})
