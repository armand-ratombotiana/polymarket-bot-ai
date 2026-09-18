// components/Sidebar.tsx — Primary navigation sidebar
//
// W49-2 — Redesigned for a cleaner, more professional navigation
// experience. Slimmer (200px / 48px collapsed), grouped by function,
// with a live status footer (bot indicator, backend connection, paper/
// live mode badge, UTC clock) and a polished active state (3px accent
// left border + slightly elevated background).
//
// Group structure (functional, not feature-tile):
//   Overview     — Command Center
//   Markets      — Live Books, Screener, Order Flow
//   Portfolio    — Positions, Orders, Trades
//   Capital      — Capital Allocator
//   Strategies   — Strategy Registry, Arbitrage, Performance
//   Intelligence — Deep Analysis, AI / ML Engine, Copilot, Shadow, ML Validation
//   Analytics    — Performance Report, Backtest, Attribution, Execution, Closed
//   System       — Health, Data Explorer, Database, Ingestion, Observability,
//                  Retention, Decisions, Safety, Rate Limits, Audit
//
// Notes:
//  - The first group keeps `id: 'main'` and `group: 'main'` for back-
//    compat with any consumer reading `NavItem.group` (e.g. CommandPalette
//    filters). Its visible label is now "Overview" via `groups.overview`.
//  - `intelligence-explainer` and `analytics-performance` are intentionally
//    absent from the sidebar (per the task spec) but remain in the
//    `NavSection` type so page.tsx + CommandPalette keep type-checking
//    against them (they're still reachable via the keyboard shortcut 8
//    and the command palette).

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
  | 'markets-order-flow'
  | 'portfolio-positions'
  | 'portfolio-orders'
  | 'portfolio-trades'
  | 'strategies-registry'
  | 'strategies-arbitrage'
  | 'strategies-performance'
  | 'intelligence-analysis'
  | 'intelligence-aiml'
  | 'intelligence-explainer'
  | 'intelligence-copilot'
  | 'intelligence-shadow'
  | 'intelligence-validation'
  | 'analytics-performance'
  | 'analytics-performance-report'
  | 'analytics-backtest'
  | 'analytics-attribution'
  | 'analytics-execution'
  | 'analytics-closed'
  | 'capital-allocator'
  | 'system-health'
  | 'system-database'
  | 'system-database-status'
  | 'system-observability'
  | 'system-retention'
  | 'system-decisions'
  | 'system-safety'
  | 'system-rate-limit'
  | 'system-audit'
  | 'system-ingestion'

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
  /** i18n key — e.g. `groups.markets`. The first group uses
   *  `groups.overview` (the bare `groups.main` key is still defined for
   *  back-compat with consumers that read it). */
  labelKey: string
  label: string
  items: NavItem[]
}

const NAV_GROUPS: NavGroup[] = [
  {
    id: 'main',
    labelKey: 'groups.overview',
    label: 'Overview',
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
      { id: 'markets-order-flow', labelKey: 'nav.order_flow', label: 'Order Flow', shortLabel: 'Flow', icon: '∿', group: 'markets' },
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
      // W23-5 — Strategy Performance dashboard: per-strategy P&L, win rate,
      // Sharpe / Sortino / Calmar, equity overlay, and risk-adjusted ranking.
      { id: 'strategies-performance', labelKey: 'nav.strategies_performance', label: 'Performance', shortLabel: 'Perf', icon: '◷', group: 'strategies' },
    ],
  },
  {
    id: 'intelligence',
    labelKey: 'groups.intelligence',
    label: 'Intelligence',
    items: [
      { id: 'intelligence-analysis', labelKey: 'nav.analysis', label: 'Deep Analysis', shortLabel: 'Analysis', icon: '⊘', kbd: '7', group: 'intelligence' },
      { id: 'intelligence-aiml', labelKey: 'nav.aiml', label: 'AI / ML Engine', shortLabel: 'AI/ML', icon: '⊛', group: 'intelligence' },
      // W49-2 — AI Prediction Explainer removed from the visible sidebar
      // per the redesign's tighter Intelligence grouping. Still reachable
      // via the command palette + the W38-5 panel in page.tsx.
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
      // W49-2 — Performance (analytics-performance) removed from the
      // visible sidebar in favour of the more comprehensive Performance
      // Report (analytics-performance-report) — the latter already
      // covers per-category breakdown with confidence intervals + p-values
      // + slippage + fees. The keyboard shortcut 8 still routes to
      // analytics-performance via page.tsx, so the panel remains
      // reachable.
      // W26-2 — Honest Performance Report: dedicated per-category
      // breakdown (backtest / walk-forward / paper / live) with
      // confidence intervals, p-values, slippage + fees, and an
      // always-on disclaimer banner.
      { id: 'analytics-performance-report', labelKey: 'nav.performance_report', label: 'Performance Report', shortLabel: 'Honest', icon: '∉', group: 'analytics' },
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
      { id: 'system-database-status', labelKey: 'nav.database_status', label: 'Database', shortLabel: 'DB', icon: '🗄', group: 'system' },
      // W31-5 — Data Ingestion: promoted up to sit next to the other
      // data-source panels (Database / Data Explorer) so all ingestion-
      // related surfaces cluster together instead of trailing the group.
      { id: 'system-ingestion', labelKey: 'nav.ingestion', label: 'Data Ingestion', shortLabel: 'Ingest', icon: '⇶', group: 'system' },
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

/** Format a Date as `HH:MM:SS` UTC. Done manually so the output is
 *  locale-independent and stable under jsdom (no Intl dependency). */
function formatUTC(d: Date): string {
  const hh = String(d.getUTCHours()).padStart(2, '0')
  const mm = String(d.getUTCMinutes()).padStart(2, '0')
  const ss = String(d.getUTCSeconds()).padStart(2, '0')
  return `${hh}:${mm}:${ss}`
}

export default function Sidebar({ active, onChange, mobileOpen, onMobileClose }: SidebarProps) {
  const [collapsed, setCollapsed] = useState(false)
  // W49-2 — Live UTC clock for the footer status section. Updates every
  // second so the trader can eyeball "is the bot's clock the same as
  // mine" without leaving the sidebar. Cleared on unmount.
  const [now, setNow] = useState<Date>(() => new Date())
  // W14-2 — i18n: `t()` resolves label keys at render. Initial render
  // uses 'en' (the SSR-payload match) so first paint matches the
  // server; the mount effect inside the hook reconciles to the
  // persisted locale afterwards.
  const { t } = useTranslation()

  // Detect viewport for auto-collapse on tablet/narrow desktop widths.
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 1024px)')
    const handler = (e: MediaQueryListEvent) => setCollapsed(e.matches)
    setCollapsed(mq.matches)
    mq.addEventListener('change', handler)
    return () => mq.removeEventListener('change', handler)
  }, [])

  // Tick the footer clock once per second. The interval is created in
  // an effect so SSR doesn't try to start a timer (window check via
  // the effect body itself — effects don't run on the server).
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(id)
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
                <div className="sidebar-group-label">
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
                  <span className="sidebar-icon" aria-hidden="true">
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
                    <span className="sidebar-kbd-badge" aria-hidden="true">
                      {item.kbd}
                    </span>
                  )}
                </button>
                )
              })}
            </div>
          ))}
        </div>

        {/* W49-2 — Footer status section.
            Four rows:
              1. Bot status (green dot + "Bot Engine Active" label)
              2. Backend connection status (green dot + "Backend: Connected")
              3. Mode badge (PAPER / LIVE)
              4. Current UTC time (live clock)

            The text content is resolved through the i18n `t()` lookup so
            both en + fr locales work without code changes. The clock is
            formatted via `formatUTC` to stay locale-independent. When the
            sidebar is collapsed, only the status dot remains (per the
            `.sidebar.collapsed .sidebar-status-text { display: none }`
            rule) so the rail stays scannable at 48px wide. */}
        <div className="sidebar-status-section" role="status" aria-live="polite">
          <div className="sidebar-status-row">
            <span className="sidebar-status-dot" aria-hidden="true" />
            <span className="sidebar-status-text">{t('status.bot_active')}</span>
          </div>
          <div className="sidebar-status-row">
            <span className="sidebar-status-dot" aria-hidden="true" />
            <span className="sidebar-status-text">
              {t('status.connected')}
            </span>
          </div>
          <div className="sidebar-status-row">
            <span className="sidebar-mode-badge" aria-label={t('status.paper_mode')}>
              {t('status.paper_mode').split(' ')[0]}
            </span>
            <span className="sidebar-status-text" style={{ fontSize: '9.5px', color: 'var(--text-dim)' }}>
              {t('status.paper_mode')}
            </span>
          </div>
          <div className="sidebar-status-row">
            <span className="sidebar-time" aria-label="Current UTC time">
              {formatUTC(now)}
            </span>
            <span className="sidebar-status-text" style={{ fontSize: '9.5px', color: 'var(--text-dim)' }}>
              UTC
            </span>
          </div>
        </div>
      </nav>
    </>
  )
}
