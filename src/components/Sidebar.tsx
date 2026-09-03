// components/Sidebar.tsx — Primary navigation sidebar
'use client'

import { useState, useEffect } from 'react'
// W14-2 — i18n: pulls the active locale + `t()` lookup so the sidebar
// labels re-render in the trader's chosen language without a server
// roundtrip. The hook initialises to 'en' (matching SSR payload) then
// reconciles to the persisted locale on mount — see
// `src/hooks/useTranslation.ts` for the hydration-safe pattern.
import { useTranslation } from '@/hooks/useTranslation'

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
  | 'intelligence-shadow'
  | 'intelligence-validation'
  | 'analytics-performance'
  | 'analytics-backtest'
  | 'analytics-attribution'
  | 'analytics-execution'
  | 'analytics-closed'
  | 'capital-allocator'
  | 'system-health'
  | 'system-database'
  | 'system-observability'
  | 'system-retention'
  | 'system-decisions'
  | 'system-safety'
  | 'system-rate-limit'
  | 'system-audit'

interface NavItem {
  id: NavSection
  /** i18n key — e.g. `nav.command`. Resolved via `t()` at render time. */
  labelKey: string
  /** English fallback label (kept for back-compat with any consumer that
   *  still reads `item.label` directly + for grep-ability). */
  label: string
  shortLabel: string
  icon: string
  kbd?: string
  group: string
}

interface NavGroup {
  id: string
  /** i18n key — e.g. `groups.main`. Capital group is `groups.capital_group`
   *  (the bare `capital` key is reserved for the nav item label). */
  labelKey: string
  label: string
  items: NavItem[]
}

const NAV_GROUPS: NavGroup[] = [
  {
    id: 'main',
    labelKey: 'groups.main',
    label: 'Main',
    items: [
      { id: 'command', labelKey: 'nav.command', label: 'Command Center', shortLabel: 'Command', icon: '⊞', kbd: '1', group: 'main' },
    ],
  },
  {
    id: 'markets',
    labelKey: 'groups.markets',
    label: 'Markets',
    items: [
      { id: 'markets-books', labelKey: 'nav.books', label: 'Live Books', shortLabel: 'Books', icon: '◈', kbd: '2', group: 'markets' },
      { id: 'markets-screener', labelKey: 'nav.screener', label: 'Screener', shortLabel: 'Screen', icon: '⊡', kbd: '3', group: 'markets' },
    ],
  },
  {
    id: 'portfolio',
    labelKey: 'groups.portfolio',
    label: 'Portfolio',
    items: [
      { id: 'portfolio-positions', labelKey: 'nav.positions', label: 'Positions', shortLabel: 'Positions', icon: '◉', kbd: '4', group: 'portfolio' },
      { id: 'portfolio-orders', labelKey: 'nav.orders', label: 'Orders', shortLabel: 'Orders', icon: '⊕', group: 'portfolio' },
      { id: 'portfolio-trades', labelKey: 'nav.trades', label: 'Trades & Fills', shortLabel: 'Trades', icon: '◎', group: 'portfolio' },
    ],
  },
  {
    id: 'capital',
    labelKey: 'groups.capital_group',
    label: 'Capital',
    items: [
      { id: 'capital-allocator', labelKey: 'nav.capital', label: 'Capital Allocator', shortLabel: 'Allocator', icon: '$', group: 'capital' },
    ],
  },
  {
    id: 'strategies',
    labelKey: 'groups.strategies',
    label: 'Strategies',
    items: [
      { id: 'strategies-registry', labelKey: 'nav.strategies', label: 'Strategy Registry', shortLabel: 'Strategies', icon: '⊗', kbd: '5', group: 'strategies' },
      { id: 'strategies-arbitrage', labelKey: 'nav.arbitrage', label: 'Arbitrage', shortLabel: 'Arbitrage', icon: '⇌', kbd: '6', group: 'strategies' },
    ],
  },
  {
    id: 'intelligence',
    labelKey: 'groups.intelligence',
    label: 'Intelligence',
    items: [
      { id: 'intelligence-analysis', labelKey: 'nav.analysis', label: 'Deep Analysis', shortLabel: 'Analysis', icon: '⊘', kbd: '7', group: 'intelligence' },
      { id: 'intelligence-aiml', labelKey: 'nav.aiml', label: 'AI / ML Engine', shortLabel: 'AI/ML', icon: '⊛', group: 'intelligence' },
      { id: 'intelligence-copilot', labelKey: 'nav.copilot', label: 'Copilot', shortLabel: 'Copilot', icon: '◈', group: 'intelligence' },
      { id: 'intelligence-shadow', labelKey: 'nav.shadow', label: 'Shadow Inference', shortLabel: 'Shadow', icon: '⬡', group: 'intelligence' },
      { id: 'intelligence-validation', labelKey: 'nav.validation', label: 'ML Validation', shortLabel: 'ML Valid', icon: '⊕', group: 'intelligence' },
    ],
  },
  {
    id: 'analytics',
    labelKey: 'groups.analytics',
    label: 'Analytics',
    items: [
      { id: 'analytics-performance', labelKey: 'nav.performance', label: 'Performance', shortLabel: 'Perf', icon: '◷', kbd: '8', group: 'analytics' },
      { id: 'analytics-backtest', labelKey: 'nav.backtest', label: 'Backtest Lab', shortLabel: 'Backtest', icon: '⊙', group: 'analytics' },
      { id: 'analytics-attribution', labelKey: 'nav.attribution', label: 'Attribution', shortLabel: 'Attrib', icon: '◫', group: 'analytics' },
      { id: 'analytics-execution', labelKey: 'nav.execution', label: 'Execution Quality', shortLabel: 'Exec Q', icon: '⌖', group: 'analytics' },
      { id: 'analytics-closed', labelKey: 'nav.closed', label: 'Closed Positions', shortLabel: 'Closed', icon: '⊟', group: 'analytics' },
    ],
  },
  {
    id: 'system',
    labelKey: 'groups.system',
    label: 'System',
    items: [
      { id: 'system-health', labelKey: 'nav.health', label: 'System Health', shortLabel: 'Health', icon: '⊜', group: 'system' },
      { id: 'system-database', labelKey: 'nav.database', label: 'Data Explorer', shortLabel: 'Data', icon: '⊞', group: 'system' },
      { id: 'system-observability', labelKey: 'nav.observability', label: 'Observability', shortLabel: 'Observ', icon: '◉', group: 'system' },
      { id: 'system-retention', labelKey: 'nav.retention', label: 'Retention', shortLabel: 'Retain', icon: '⌫', group: 'system' },
      { id: 'system-decisions', labelKey: 'nav.decisions', label: 'Decision Ledger', shortLabel: 'Ledger', icon: '↹', group: 'system' },
      { id: 'system-safety', labelKey: 'nav.safety', label: 'Safety Gate', shortLabel: 'Safety', icon: '🛡', group: 'system' },
      { id: 'system-rate-limit', labelKey: 'nav.rate_limits', label: 'Rate Limits', shortLabel: 'Limits', icon: '⏱', group: 'system' },
      { id: 'system-audit', labelKey: 'nav.audit', label: 'Audit Log', shortLabel: 'Audit', icon: '📋', group: 'system' },
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
  // W14-2 — i18n: `t()` resolves label keys at render. Initial render
  // uses 'en' (the SSR-payload match) so first paint matches the
  // server; the mount effect inside the hook reconciles to the
  // persisted locale afterwards. We only need `t` here — `locale`
  // and `setLocale` aren't used directly in this component.
  const { t } = useTranslation()

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
                  {t(group.labelKey)}
                </div>
              )}
              {group.items.map((item) => {
                // Resolve once per item — used in the visible label,
                // the collapsed-mode tooltip, and nowhere else.
                const itemLabel = t(item.labelKey)
                return (
                <button
                  key={item.id}
                  onClick={() => handleSelect(item.id)}
                  className={`sidebar-item${active === item.id ? ' active' : ''}`}
                  aria-current={active === item.id ? 'page' : undefined}
                  title={collapsed ? `${itemLabel}${item.kbd ? ` (${item.kbd})` : ''}` : undefined}
                >
                  <span className="sidebar-icon" aria-hidden="true"
                    style={{ fontSize: '15px', fontFamily: 'system-ui, sans-serif' }}>
                    {item.icon}
                  </span>
                  <span className="sidebar-label">{itemLabel}</span>
                  {/* W9-7 — Announce the keyboard shortcut to screen readers.
                      When the sidebar is collapsed the visible kbd badge is
                      hidden, so the sr-only text is the only way AT users
                      learn the shortcut. */}
                  {item.kbd && (
                    <span className="sr-only">
                      (Keyboard shortcut: press {item.kbd})
                    </span>
                  )}
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
                    }} aria-hidden="true">
                      {item.kbd}
                    </span>
                  )}
                </button>
                )
              })}
            </div>
          ))}
        </div>

        {/* Footer */}
        <div className="sidebar-footer">
          <div style={{ padding: '4px 8px' }} role="status" aria-live="polite">
            <div className="sidebar-item" style={{ opacity: 0.75, cursor: 'default', fontSize: '10px' }}>
              <span className="sidebar-icon" aria-hidden="true" style={{ fontSize: '12px' }}>🟢</span>
              {/* W14-2 — i18n: footer status label resolved via t(). */}
              <span className="sidebar-label" style={{ fontSize: '10.5px' }}>{t('status.bot_active')}</span>
            </div>
          </div>
        </div>
      </nav>
    </>
  )
}
