// components/Sidebar.tsx — Primary navigation sidebar
'use client'

import { useState, useEffect } from 'react'

export type NavSection =
  | 'command'
  | 'markets-books'
  | 'markets-screener'
  | 'portfolio-positions'
  | 'portfolio-orders'
  | 'portfolio-trades'
  | 'strategies-registry'
  | 'strategies-arbitrage'
  | 'intelligence-analysis'
  | 'intelligence-aiml'
  | 'intelligence-copilot'
  | 'analytics-performance'
  | 'analytics-backtest'
  | 'system-health'
  | 'system-database'

interface NavItem {
  id: NavSection
  label: string
  shortLabel: string
  icon: string
  kbd?: string
  group: string
}

interface NavGroup {
  id: string
  label: string
  items: NavItem[]
}

const NAV_GROUPS: NavGroup[] = [
  {
    id: 'main',
    label: 'Main',
    items: [
      { id: 'command', label: 'Command Center', shortLabel: 'Command', icon: '⊞', kbd: '1', group: 'main' },
    ],
  },
  {
    id: 'markets',
    label: 'Markets',
    items: [
      { id: 'markets-books', label: 'Live Books', shortLabel: 'Books', icon: '◈', kbd: '2', group: 'markets' },
      { id: 'markets-screener', label: 'Screener', shortLabel: 'Screen', icon: '⊡', kbd: '3', group: 'markets' },
    ],
  },
  {
    id: 'portfolio',
    label: 'Portfolio',
    items: [
      { id: 'portfolio-positions', label: 'Positions', shortLabel: 'Positions', icon: '◉', kbd: '4', group: 'portfolio' },
      { id: 'portfolio-orders', label: 'Orders', shortLabel: 'Orders', icon: '⊕', group: 'portfolio' },
      { id: 'portfolio-trades', label: 'Trades & Fills', shortLabel: 'Trades', icon: '◎', group: 'portfolio' },
    ],
  },
  {
    id: 'strategies',
    label: 'Strategies',
    items: [
      { id: 'strategies-registry', label: 'Strategy Registry', shortLabel: 'Strategies', icon: '⊗', kbd: '5', group: 'strategies' },
      { id: 'strategies-arbitrage', label: 'Arbitrage', shortLabel: 'Arbitrage', icon: '⇌', kbd: '6', group: 'strategies' },
    ],
  },
  {
    id: 'intelligence',
    label: 'Intelligence',
    items: [
      { id: 'intelligence-analysis', label: 'Deep Analysis', shortLabel: 'Analysis', icon: '⊘', kbd: '7', group: 'intelligence' },
      { id: 'intelligence-aiml', label: 'AI / ML Engine', shortLabel: 'AI/ML', icon: '⊛', group: 'intelligence' },
      { id: 'intelligence-copilot', label: 'Copilot', shortLabel: 'Copilot', icon: '◈', group: 'intelligence' },
    ],
  },
  {
    id: 'analytics',
    label: 'Analytics',
    items: [
      { id: 'analytics-performance', label: 'Performance', shortLabel: 'Perf', icon: '◷', kbd: '8', group: 'analytics' },
      { id: 'analytics-backtest', label: 'Backtest Lab', shortLabel: 'Backtest', icon: '⊙', group: 'analytics' },
    ],
  },
  {
    id: 'system',
    label: 'System',
    items: [
      { id: 'system-health', label: 'System Health', shortLabel: 'Health', icon: '⊜', group: 'system' },
      { id: 'system-database', label: 'Data Explorer', shortLabel: 'Data', icon: '⊞', group: 'system' },
    ],
  },
]

interface SidebarProps {
  active: NavSection
  onChange: (section: NavSection) => void
  mobileOpen?: boolean
  onMobileClose?: () => void
}

export default function Sidebar({ active, onChange, mobileOpen, onMobileClose }: SidebarProps) {
  const [collapsed, setCollapsed] = useState(false)

  // Detect viewport for auto-collapse
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 1024px)')
    const handler = (e: MediaQueryListEvent) => setCollapsed(e.matches)
    setCollapsed(mq.matches)
    mq.addEventListener('change', handler)
    return () => mq.removeEventListener('change', handler)
  }, [])

  const handleSelect = (id: NavSection) => {
    onChange(id)
    onMobileClose?.()
  }

  return (
    <>
      {/* Mobile backdrop */}
      {mobileOpen && (
        <div
          className="fixed inset-0 bg-black/60 z-[35] md:hidden"
          onClick={onMobileClose}
          aria-hidden="true"
        />
      )}

      <nav
        className={`sidebar${collapsed ? ' collapsed' : ''}${mobileOpen ? ' mobile-open' : ''}`}
        aria-label="Primary navigation"
        role="navigation"
        style={mobileOpen ? { width: 'var(--sidebar-width)' } : undefined}
      >
        {/* Logo header */}
        <div className="sidebar-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0 }}>
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" aria-hidden="true" style={{ flexShrink: 0 }}>
              <circle cx="12" cy="12" r="10.5" stroke="#3b82f6" strokeWidth="1.5" />
              <circle cx="12" cy="12" r="5.5" stroke="#3b82f6" strokeWidth="1" strokeOpacity="0.5" />
              <line x1="12" y1="2" x2="12" y2="22" stroke="#3b82f6" strokeWidth="1" strokeOpacity="0.35" />
              <line x1="2" y1="12" x2="22" y2="12" stroke="#3b82f6" strokeWidth="1" strokeOpacity="0.35" />
            </svg>
            <span className="app-name" style={{
              fontSize: '13px',
              fontWeight: 700,
              color: 'var(--text-primary)',
              letterSpacing: '-0.01em',
              whiteSpace: 'nowrap',
            }}>
              Polymarket<span style={{ color: '#60a5fa' }}>Pro</span>
            </span>
          </div>
          <button
            onClick={() => setCollapsed(c => !c)}
            style={{
              background: 'transparent',
              border: 'none',
              color: 'var(--text-dim)',
              cursor: 'pointer',
              padding: '4px',
              borderRadius: '4px',
              display: 'flex',
              alignItems: 'center',
              lineHeight: 1,
              flexShrink: 0,
            }}
            aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          >
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
              <rect x="1" y="2" width="12" height="1.5" rx="0.75" fill="currentColor" />
              <rect x="1" y="6.25" width="12" height="1.5" rx="0.75" fill="currentColor" />
              <rect x="1" y="10.5" width="12" height="1.5" rx="0.75" fill="currentColor" />
            </svg>
          </button>
        </div>

        {/* Nav groups */}
        <div className="sidebar-nav" role="list">
          {NAV_GROUPS.map((group) => (
            <div key={group.id} role="listitem">
              {!collapsed && (
                <div style={{
                  fontSize: '9.5px',
                  fontWeight: 700,
                  textTransform: 'uppercase',
                  letterSpacing: '0.1em',
                  color: 'var(--text-dim)',
                  padding: '10px 12px 4px',
                  userSelect: 'none',
                }}>
                  {group.label}
                </div>
              )}
              {group.items.map((item) => (
                <button
                  key={item.id}
                  onClick={() => handleSelect(item.id)}
                  className={`sidebar-item${active === item.id ? ' active' : ''}`}
                  role="menuitem"
                  aria-current={active === item.id ? 'page' : undefined}
                  title={collapsed ? `${item.label}${item.kbd ? ` (${item.kbd})` : ''}` : undefined}
                >
                  <span className="sidebar-icon" aria-hidden="true"
                    style={{ fontSize: '15px', fontFamily: 'system-ui, sans-serif' }}>
                    {item.icon}
                  </span>
                  <span className="sidebar-label">{item.label}</span>
                  {!collapsed && item.kbd && (
                    <span style={{
                      fontSize: '9px',
                      color: 'var(--text-dim)',
                      background: 'var(--bg-surface)',
                      border: '1px solid var(--border)',
                      borderRadius: '3px',
                      padding: '1px 4px',
                      fontFamily: 'JetBrains Mono, monospace',
                      flexShrink: 0,
                    }}>
                      {item.kbd}
                    </span>
                  )}
                </button>
              ))}
            </div>
          ))}
        </div>

        {/* Footer */}
        <div className="sidebar-footer">
          <div style={{ padding: '4px 8px' }}>
            <div className="sidebar-item" style={{ opacity: 0.75, cursor: 'default', fontSize: '10px' }}>
              <span className="sidebar-icon" aria-hidden="true" style={{ fontSize: '12px' }}>🟢</span>
              <span className="sidebar-label" style={{ fontSize: '10.5px' }}>Bot Engine Active</span>
            </div>
          </div>
        </div>
      </nav>
    </>
  )
}
