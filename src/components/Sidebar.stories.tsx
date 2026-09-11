// src/components/Sidebar.stories.tsx — W12-7 Storybook documentation
// for the primary navigation Sidebar. Showcases three states:
//   - Default (Command Center active, desktop layout)
//   - OnPositions (a different section highlighted to verify the
//     active-state styling)
//   - MobileOpen (drawer mode with backdrop, mobile viewport)
//
// The sidebar uses CSS variables (--sidebar-width, --text-primary,
// --text-dim, --bg-surface, --border) plus the .sidebar / .sidebar-item
// / .sidebar-nav / .sidebar-footer classnames defined in
// src/app/globals.css, which preview.ts imports globally so all
// stories inherit the real dashboard look.

import type { Meta, StoryObj } from '@storybook/react'
import Sidebar, { NavSection } from './Sidebar'

const meta: Meta<typeof Sidebar> = {
  title: 'Navigation/Sidebar',
  component: Sidebar,
  parameters: { layout: 'fullscreen' },
  tags: ['autodocs'],
}
export default meta

type Story = StoryObj<typeof Sidebar>

export const Default: Story = {
  args: {
    active: 'command',
    onChange: (section: NavSection) => console.log('Selected:', section),
  },
}

export const OnPositions: Story = {
  args: {
    active: 'portfolio-positions',
    onChange: (section: NavSection) => console.log('Selected:', section),
  },
}

export const MobileOpen: Story = {
  args: {
    active: 'command',
    onChange: () => {},
    mobileOpen: true,
    onMobileClose: () => console.log('Close mobile'),
  },
  parameters: {
    viewport: {
      defaultViewport: 'mobile1',
    },
  },
}
