// components/DecisionLedgerPanel.tsx — Unified Decision Ledger Inspector
//
// Surfaces the SQLite-backed decision chain (PREDICTION → SIGNAL → RISK →
// ORDER → FILL) maintained by `core/decision_ledger.py`. Each decision row
// is expandable to reveal its full correlation chain fetched from the
// token-scoped inspection endpoint.
//
// Backend endpoints (verified in `decision_ledger.register_routes`):
//   GET /api/decisions/rejected?limit=50   — recent rejection list (drives the
//                                              primary list view; rejections
//                                              are the most-recent-first
//                                              surface of the ledger).
//   GET /api/decision/{token_id}?limit=50  — full stage-chain for a token;
//                                              filtered client-side to the
//                                              expanded rejection's decision_id
//                                              to render the correlation chain.
//
// Auto-refreshes every 10s when the tab is visible; pauses on hide.
'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  ChevronRight,
  Clock,
  Filter,
  Loader2,
  RefreshCw,
  Search,
} from 'lucide-react'
import { apiFetch, getApiUrl } from '@/lib/api'
import { fmtAge, fmtPct, fmtPrice } from '@/lib/design-tokens'

// ── Types ──────────────────────────────────────────────────────────────────

type StageName =
  | 'PREDICTION'
  | 'SIGNAL'
  | 'RISK_APPROVED'
  | 'RISK_REJECTED'
  | 'ORDER'
  | 'FILL'
  | (string & {}) // allow forward-compat with future stages without breaking exhaustive checks

interface DecisionEvent {
  timestamp: number
  decision_id: string
  stage: StageName
  token_id: string | null
  strategy: string | null
  pnl: number
  data_json: string | null
  /** Decoded `data_json` payload — surfaced by the backend for caller convenience. */
  data: Record<string, unknown> | null
}

interface RejectionRow {
  timestamp: number
  decision_id: string
  token_id: string
  strategy: string
  predicted_edge: number
  confidence: number
  reason: string
  market_mid: number | null
}

interface DecisionsResponse {
  count: number
  rejections: RejectionRow[]
}

interface ChainResponse {
  token_id: string
  count: number
  events: DecisionEvent[]
}

type OutcomeFilter = 'ALL' | 'REJECTED' | 'FILLED' | 'PENDING' | 'EXPIRED'
type ActionFilter = 'ALL' | 'TRADE_LONG_YES' | 'TRADE_SHORT_NO' | 'REJECT_RISK' | 'MONITOR'

// ── Constants ──────────────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 10_000
const LIST_LIMIT = 50
const CHAIN_LIMIT = 50

/**
 * Per-stage visual identity (color-coded per task spec):
 *   PREDICTION = blue, SIGNAL = cyan, RISK = amber,
 *   ORDER = violet, FILL = green
 *
 * `RISK_REJECTED` uses a slightly stronger amber to distinguish from
 * `RISK_APPROVED` while staying in the amber family (both are RISK stage).
 */
const STAGE_STYLE: Record<
  string,
  { dot: string; text: string; bg: string; border: string; label: string }
> = {
  PREDICTION: {
    dot: 'bg-blue-400',
    text: 'text-blue-300',
    bg: 'bg-blue-500/10',
    border: 'border-blue-500/30',
    label: 'PREDICTION',
  },
  SIGNAL: {
    dot: 'bg-cyan-400',
    text: 'text-cyan-300',
    bg: 'bg-cyan-500/10',
    border: 'border-cyan-500/30',
    label: 'SIGNAL',
  },
  RISK_APPROVED: {
    dot: 'bg-amber-400',
    text: 'text-amber-300',
    bg: 'bg-amber-500/10',
    border: 'border-amber-500/30',
    label: 'RISK · APPROVED',
  },
  RISK_REJECTED: {
    dot: 'bg-amber-500',
    text: 'text-amber-400',
    bg: 'bg-amber-500/15',
    border: 'border-amber-500/40',
    label: 'RISK · REJECTED',
  },
  ORDER: {
    dot: 'bg-violet-400',
    text: 'text-violet-300',
    bg: 'bg-violet-500/10',
    border: 'border-violet-500/30',
    label: 'ORDER',
  },
  FILL: {
    dot: 'bg-green-400',
    text: 'text-green-300',
    bg: 'bg-green-500/10',
    border: 'border-green-500/30',
    label: 'FILL',
  },
}

const FALLBACK_STAGE_STYLE = {
  dot: 'bg-slate-500',
  text: 'text-slate-300',
  bg: 'bg-slate-500/10',
  border: 'border-slate-500/30',
}

const REASON_LABELS: Record<string, string> = {
  low_confidence: 'Low Confidence',
  wide_spread: 'Wide Spread',
  neutral_zone: 'Neutral Zone',
  insufficient_kelly_edge: 'Insufficient Kelly Edge',
}

const OUTCOME_LABELS: Record<OutcomeFilter, string> = {
  ALL: 'All Outcomes',
  REJECTED: 'Rejected',
  FILLED: 'Filled',
  PENDING: 'Pending',
  EXPIRED: 'Expired',
}

const ACTION_LABELS: Record<ActionFilter, string> = {
  ALL: 'All Actions',
  TRADE_LONG_YES: 'Trade Long YES',
  TRADE_SHORT_NO: 'Trade Short NO',
  REJECT_RISK: 'Reject (Risk)',
  MONITOR: 'Monitor',
}

// ── Helpers ────────────────────────────────────────────────────────────────

function getStageStyle(stage: string) {
  return STAGE_STYLE[stage] ?? { ...FALLBACK_STAGE_STYLE, label: stage }
}

function shortId(id: string | null | undefined, n = 8): string {
  if (!id) return '—'
  return id.length <= n ? id : `${id.slice(0, n)}…`
}

function fmtEpochMs(epochSec: number): string {
  if (!epochSec || !Number.isFinite(epochSec)) return '—'
  const d = new Date(epochSec * 1000)
  const time = d.toLocaleTimeString('en-US', { hour12: false })
  const date = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
  return `${time} · ${date}`
}

/** Map a rejection reason to the strategy's intended action type. */
function reasonToAction(reason: string): ActionFilter {
  switch (reason) {
    case 'wide_spread':
    case 'insufficient_kelly_edge':
      return 'REJECT_RISK'
    case 'low_confidence':
    case 'neutral_zone':
    default:
      return 'MONITOR'
  }
}

/** Safely coerce a possibly-numeric `unknown` from the decoded data payload. */
function asNumber(v: unknown): number | null {
  if (typeof v === 'number' && Number.isFinite(v)) return v
  if (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v))) return Number(v)
  return null
}

// ── Sub-components ─────────────────────────────────────────────────────────

function StatChip({
  label,
  value,
  sub,
  color,
  title,
}: {
  label: string
  value: string
  sub?: string
  color?: string
  title?: string
}) {
  return (
    <div
      className="bg-[#0e1015] border border-[#1f2335] px-2.5 py-1 rounded-md flex items-center gap-1.5"
      title={title}
    >
      <span className="text-[10px] text-[#7e8aaa] uppercase font-semibold whitespace-nowrap">
        {label}:
      </span>
      <span className="mono font-bold text-xs" style={color ? { color } : undefined}>
        {value}
      </span>
      {sub && <span className="text-[9.5px] text-[#5a637a]">{sub}</span>}
    </div>
  )
}

function StageNode({ event, isLast }: { event: DecisionEvent; isLast: boolean }) {
  const style = getStageStyle(event.stage)
  const data = event.data ?? {}

  // Build a per-stage detail string from the decoded `data` payload.
  let detail = ''
  if (event.stage === 'PREDICTION') {
    const pYes = asNumber(data['p_yes'] ?? data['ml_forecast_prob'] ?? data['p_yes_ml'])
    const edge = asNumber(data['edge'] ?? data['raw_edge'] ?? data['predicted_edge'])
    const conf = asNumber(data['confidence'] ?? data['confidence_score'])
    const mv = data['model_version']
    detail = [
      pYes != null ? `P(YES)=${fmtPct(pYes)}` : null,
      edge != null ? `edge=${edge >= 0 ? '+' : ''}${edge.toFixed(3)}` : null,
      conf != null ? `conf=${fmtPct(conf)}` : null,
      mv ? `model=${mv}` : null,
    ]
      .filter(Boolean)
      .join(' · ')
  } else if (event.stage === 'SIGNAL') {
    detail = [
      data['action'] ? `action=${data['action']}` : null,
      data['reason'] ? `reason=${data['reason']}` : null,
      data['suggested_action'] ? `suggested=${data['suggested_action']}` : null,
    ]
      .filter(Boolean)
      .join(' · ')
  } else if (event.stage === 'RISK_APPROVED') {
    const size = asNumber(data['size'] ?? data['position_size'])
    detail = [
      size != null ? `size=${size.toFixed(2)}` : null,
      data['reason'] ? String(data['reason']) : 'approved',
    ]
      .filter(Boolean)
      .join(' · ')
  } else if (event.stage === 'RISK_REJECTED') {
    const reasonRaw = typeof data['reason'] === 'string' ? data['reason'] : ''
    const reasonLabel = REASON_LABELS[reasonRaw] ?? reasonRaw
    const mid = asNumber(data['market_mid'])
    const conf = asNumber(data['confidence'])
    detail = [
      reasonLabel ? `reason=${reasonLabel}` : null,
      mid != null ? `mid=${fmtPrice(mid)}` : null,
      conf != null ? `conf=${fmtPct(conf)}` : null,
    ]
      .filter(Boolean)
      .join(' · ')
  } else if (event.stage === 'ORDER') {
    const price = asNumber(data['price'])
    const size = asNumber(data['size'])
    detail = [
      data['side'] ? `side=${data['side']}` : null,
      price != null ? `price=${fmtPrice(price)}` : null,
      size != null ? `size=${size.toFixed(2)}` : null,
      data['status'] ? `status=${data['status']}` : null,
      data['order_id'] ? `oid=${shortId(String(data['order_id']), 10)}` : null,
    ]
      .filter(Boolean)
      .join(' · ')
  } else if (event.stage === 'FILL') {
    const fillPrice = asNumber(data['fill_price'] ?? data['price'])
    const slip = asNumber(data['slippage'])
    detail = [
      fillPrice != null ? `fill=${fmtPrice(fillPrice)}` : null,
      slip != null ? `slip=${slip >= 0 ? '+' : ''}${slip.toFixed(4)}` : null,
      data['size'] != null ? `size=${asNumber(data['size'])?.toFixed(2)}` : null,
      event.pnl ? `pnl=${event.pnl >= 0 ? '+' : ''}$${event.pnl.toFixed(2)}` : null,
    ]
      .filter(Boolean)
      .join(' · ')
  }

  return (
    <div className="flex items-start gap-2 min-w-0">
      {/* Timeline rail */}
      <div className="flex flex-col items-center pt-0.5 shrink-0">
        <span className={`w-2.5 h-2.5 rounded-full ${style.dot} ring-2 ring-[#13161e]`} />
        {!isLast && <span className="w-px flex-1 bg-[#1f2335] min-h-[24px]" />}
      </div>
      {/* Stage card */}
      <div
        className={`flex-1 min-w-0 mb-2 px-2.5 py-1.5 rounded-md border ${style.bg} ${style.border}`}
      >
        <div className="flex items-center justify-between gap-2">
          <span
            className={`text-[10px] font-bold uppercase tracking-wider ${style.text}`}
          >
            {style.label}
          </span>
          <span
            className="text-[9.5px] mono text-[#7e8aaa]"
            title={fmtEpochMs(event.timestamp)}
          >
            {fmtAge(event.timestamp)}
          </span>
        </div>
        {detail && (
          <div className="mt-0.5 text-[11px] mono text-[#c8cfe0] break-words">{detail}</div>
        )}
        <div className="mt-0.5 text-[9.5px] text-[#5a637a] mono">
          {fmtEpochMs(event.timestamp)}
        </div>
      </div>
    </div>
  )
}

function DecisionChainView({
  events,
  decisionId,
}: {
  events: DecisionEvent[]
  decisionId: string
}) {
  // Filter to the chain for the rejection's decision_id (the most relevant
  // chain — other decisions for the same token are surfaced separately below).
  const primaryChain = useMemo(
    () => events.filter((e) => e.decision_id === decisionId),
    [events, decisionId]
  )

  // Other recent decisions for the same token (different decision_ids).
  const siblingDecisions = useMemo(() => {
    const groups = new Map<string, DecisionEvent[]>()
    for (const e of events) {
      if (e.decision_id && e.decision_id !== decisionId) {
        const arr = groups.get(e.decision_id) ?? []
        arr.push(e)
        groups.set(e.decision_id, arr)
      }
    }
    // Sort groups by their max timestamp desc — most recent sibling first.
    return Array.from(groups.entries())
      .map(([id, evs]) => ({
        id,
        events: evs.sort((a, b) => a.timestamp - b.timestamp),
        lastTs: evs.reduce((m, e) => Math.max(m, e.timestamp), 0),
      }))
      .sort((a, b) => b.lastTs - a.lastTs)
      .slice(0, 5)
  }, [events, decisionId])

  if (primaryChain.length === 0) {
    return (
      <div className="px-3 py-2 text-[11px] text-[#7e8aaa]">
        No chain events recorded for decision{' '}
        <span className="mono text-[#dde1ed]">{shortId(decisionId, 16)}</span>.
      </div>
    )
  }

  return (
    <div className="px-3 py-2.5">
      <div className="text-[10px] font-bold uppercase tracking-wider text-[#7e8aaa] mb-2 flex items-center gap-1.5">
        <Activity size={11} />
        <span>Decision Chain · {shortId(decisionId, 16)}</span>
        <span className="text-[9.5px] text-[#5a637a] font-normal normal-case tracking-normal">
          ({primaryChain.length} stage{primaryChain.length === 1 ? '' : 's'})
        </span>
      </div>
      <div className="flex flex-col">
        {primaryChain.map((e, i) => (
          <StageNode
            key={`${e.stage}-${i}-${e.timestamp}`}
            event={e}
            isLast={i === primaryChain.length - 1}
          />
        ))}
      </div>

      {siblingDecisions.length > 0 && (
        <div className="mt-3 pt-2 border-t border-[#1f2335]/60">
          <div className="text-[10px] font-bold uppercase tracking-wider text-[#7e8aaa] mb-1.5">
            Other Recent Decisions for this Token ({siblingDecisions.length})
          </div>
          <div className="space-y-1">
            {siblingDecisions.map((d) => {
              const stages = d.events.map((e) => e.stage)
              const hasFill = stages.includes('FILL')
              const hasRejected = stages.includes('RISK_REJECTED')
              const outcomeBadge = hasFill
                ? 'bg-green-500/15 text-green-400 border border-green-500/30'
                : hasRejected
                ? 'bg-red-500/15 text-red-400 border border-red-500/30'
                : 'bg-amber-500/15 text-amber-400 border border-amber-500/30'
              const outcomeLabel = hasFill ? 'FILLED' : hasRejected ? 'REJECTED' : 'PENDING'
              return (
                <div
                  key={d.id}
                  className="flex items-center justify-between gap-2 text-[11px] bg-[#0e1015] px-2 py-1 rounded border border-[#1f2335]"
                  title={`Decision ${d.id}`}
                >
                  <span className="mono text-[#c8cfe0] truncate">{shortId(d.id, 18)}</span>
                  <div className="flex items-center gap-1.5 shrink-0">
                    <span className="text-[9.5px] mono text-[#5a637a]">
                      {d.events.length} stage{d.events.length === 1 ? '' : 's'}
                    </span>
                    <span
                      className={`text-[9px] font-bold px-1.5 py-0.5 rounded ${outcomeBadge}`}
                    >
                      {outcomeLabel}
                    </span>
                    <span className="text-[9.5px] mono text-[#7e8aaa]">{fmtAge(d.lastTs)}</span>
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

interface DecisionCardProps {
  row: RejectionRow
  expanded: boolean
  onToggle: () => void
  chainData: DecisionEvent[] | null
  chainLoading: boolean
  chainError: string | null
}

function DecisionCard({
  row,
  expanded,
  onToggle,
  chainData,
  chainLoading,
  chainError,
}: DecisionCardProps) {
  const action = reasonToAction(row.reason)
  const isRejectRisk = action === 'REJECT_RISK'
  const edgePos = row.predicted_edge >= 0
  const reasonLabel = REASON_LABELS[row.reason] ?? row.reason

  return (
    <div
      className={`border-b border-[#1f2335]/60 transition-colors ${
        expanded ? 'bg-[#0e1015]' : 'hover:bg-blue-500/5'
      }`}
    >
      {/* Summary row (clickable) */}
      <button
        type="button"
        onClick={onToggle}
        className="w-full text-left px-3 py-2.5 flex items-center gap-2 cursor-pointer"
        aria-expanded={expanded}
        aria-label={`Expand decision ${row.decision_id}`}
      >
        <ChevronRight
          size={14}
          className={`text-[#7e8aaa] shrink-0 transition-transform ${
            expanded ? 'rotate-90' : ''
          }`}
        />

        {/* Outcome badge */}
        <span
          className="badge badge-red text-[9px] shrink-0 w-[80px] justify-center"
          title="Risk-rejected decision"
        >
          REJECTED
        </span>

        {/* Action badge */}
        <span
          className={`badge text-[9px] shrink-0 ${
            isRejectRisk ? 'badge-amber' : 'badge-cyan'
          }`}
          title={`Reason: ${row.reason}`}
        >
          {ACTION_LABELS[action]}
        </span>

        {/* Token + age + reason */}
        <div className="flex-1 min-w-0">
          <div
            className="text-[11px] mono text-[#dde1ed] truncate"
            title={row.token_id}
          >
            {shortId(row.token_id, 22)}
          </div>
          <div className="text-[9.5px] text-[#5a637a] flex items-center gap-1.5 flex-wrap">
            <span className="mono">{fmtAge(row.timestamp)}</span>
            <span className="text-[#3e4560]">·</span>
            <span>{reasonLabel}</span>
          </div>
        </div>

        {/* Metrics */}
        <div className="hidden sm:flex items-center gap-3 shrink-0">
          <div className="text-right">
            <div className="text-[9px] text-[#7e8aaa] uppercase font-semibold">Edge</div>
            <div
              className={`mono text-[11px] font-bold ${
                edgePos ? 'text-green-400' : 'text-red-400'
              }`}
            >
              {row.predicted_edge >= 0 ? '+' : ''}
              {row.predicted_edge.toFixed(3)}
            </div>
          </div>
          <div className="text-right">
            <div className="text-[9px] text-[#7e8aaa] uppercase font-semibold">Conf</div>
            <div className="mono text-[11px] font-bold text-cyan-300">
              {fmtPct(row.confidence)}
            </div>
          </div>
          <div className="text-right">
            <div className="text-[9px] text-[#7e8aaa] uppercase font-semibold">Mid</div>
            <div className="mono text-[11px] text-[#c8cfe0]">
              {row.market_mid != null ? fmtPrice(row.market_mid) : '—'}
            </div>
          </div>
        </div>

        {/* Strategy pill */}
        <span
          className="text-[9.5px] mono px-1.5 py-0.5 rounded bg-[#0e1015] border border-[#1f2335] text-[#7e8aaa] shrink-0 hidden md:inline-block"
          title="Strategy"
        >
          {row.strategy || '—'}
        </span>
      </button>

      {/* Expanded chain view (detail drawer) */}
      {expanded && (
        <div className="px-1 pb-2">
          {chainLoading && (
            <div className="px-3 py-3 flex items-center gap-2 text-[11px] text-[#7e8aaa]">
              <Loader2 size={12} className="animate-spin" />
              Loading decision chain…
            </div>
          )}
          {chainError && !chainLoading && (
            <div className="px-3 py-2 text-[11px] text-red-400 flex items-center gap-1.5">
              <AlertTriangle size={12} />
              {chainError}
            </div>
          )}
          {chainData && !chainLoading && !chainError && (
            <DecisionChainView events={chainData} decisionId={row.decision_id} />
          )}
        </div>
      )}
    </div>
  )
}

// ── Main Component ─────────────────────────────────────────────────────────

export default function DecisionLedgerPanel() {
  const [rows, setRows] = useState<RejectionRow[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)

  const [actionFilter, setActionFilter] = useState<ActionFilter>('ALL')
  const [outcomeFilter, setOutcomeFilter] = useState<OutcomeFilter>('ALL')
  const [tokenQuery, setTokenQuery] = useState('')

  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [chainCache, setChainCache] = useState<Record<string, DecisionEvent[]>>({})
  const [chainLoading, setChainLoading] = useState<Record<string, boolean>>({})
  const [chainErrors, setChainErrors] = useState<Record<string, string | null>>({})

  // ── Primary list fetch ────────────────────────────────────────────────
  const fetchList = useCallback(async () => {
    try {
      const apiUrl = getApiUrl()
      const res = await apiFetch(`${apiUrl}/api/decisions/rejected?limit=${LIST_LIMIT}`)
      if (!res.ok) {
        const text = await res.text().catch(() => '')
        throw new Error(
          `HTTP ${res.status}${text ? `: ${text.slice(0, 120)}` : ''}`
        )
      }
      const json: DecisionsResponse = await res.json()
      setRows(json.rejections ?? [])
      setError(null)
      setLastUpdated(Date.now())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load decision ledger')
    } finally {
      setLoading(false)
    }
  }, [])

  // ── Polling with visibility-aware auto-pause ───────────────────────────
  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null

    const startPolling = () => {
      if (timer) return
      timer = setInterval(() => {
        if (document.visibilityState === 'visible') fetchList()
      }, POLL_INTERVAL_MS)
    }
    const stopPolling = () => {
      if (timer) {
        clearInterval(timer)
        timer = null
      }
    }
    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        fetchList()
        startPolling()
      } else {
        stopPolling()
      }
    }

    fetchList()
    if (document.visibilityState === 'visible') startPolling()
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      stopPolling()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [fetchList])

  // ── Chain expansion (detail drawer fetch) ─────────────────────────────
  const toggleExpand = useCallback(
    (row: RejectionRow) => {
      const id = row.decision_id
      if (!id) return
      if (expandedId === id) {
        setExpandedId(null)
        return
      }
      setExpandedId(id)
      // Already loaded or in-flight — short-circuit.
      if (chainCache[id] || chainLoading[id]) return
      if (!row.token_id) {
        setChainErrors((p) => ({ ...p, [id]: 'No token_id associated with this decision' }))
        return
      }
      setChainLoading((p) => ({ ...p, [id]: true }))
      setChainErrors((p) => ({ ...p, [id]: null }))
      apiFetch(
        `${getApiUrl()}/api/decision/${encodeURIComponent(row.token_id)}?limit=${CHAIN_LIMIT}`
      )
        .then(async (res) => {
          if (res.status === 404) {
            // No chain events recorded for this token — surface an empty
            // chain so the "no events" UI renders (the rejection row itself
            // is still visible above).
            setChainCache((p) => ({ ...p, [id]: [] }))
            return
          }
          if (!res.ok) {
            const text = await res.text().catch(() => '')
            throw new Error(
              `HTTP ${res.status}${text ? `: ${text.slice(0, 120)}` : ''}`
            )
          }
          const json: ChainResponse = await res.json()
          setChainCache((p) => ({ ...p, [id]: json.events ?? [] }))
        })
        .catch((e: unknown) => {
          setChainErrors((p) => ({
            ...p,
            [id]: e instanceof Error ? e.message : 'Failed to load decision chain',
          }))
        })
        .finally(() => {
          setChainLoading((p) => ({ ...p, [id]: false }))
        })
    },
    [expandedId, chainCache, chainLoading]
  )

  // ── Derived stats ──────────────────────────────────────────────────────
  const stats = useMemo(() => {
    const total = rows.length
    const avgEdge = total > 0 ? rows.reduce((s, r) => s + (r.predicted_edge || 0), 0) / total : 0
    const avgConf = total > 0 ? rows.reduce((s, r) => s + (r.confidence || 0), 0) / total : 0
    // Reason distribution → top reason
    const reasonCounts: Record<string, number> = {}
    for (const r of rows) {
      reasonCounts[r.reason] = (reasonCounts[r.reason] ?? 0) + 1
    }
    const topReason =
      Object.entries(reasonCounts).sort((a, b) => b[1] - a[1])[0]?.[0] ?? null
    // Approval rate (inferred): the ledger exposes only rejections, so the
    // observed approval rate against this surface is 0% by definition.
    // Fill rate (inferred from expanded chains): ratio of decision_ids with
    // a FILL stage present in the chain cache, over total expanded.
    const expandedIds = Object.keys(chainCache)
    const filledExpanded = expandedIds.filter((id) => {
      const evs = chainCache[id] ?? []
      // A FILL on the same token (any decision_id) counts as evidence of fills.
      return evs.some((e) => e.stage === 'FILL')
    }).length
    const fillRate = expandedIds.length > 0 ? filledExpanded / expandedIds.length : null
    return { total, avgEdge, avgConf, topReason, fillRate, expandedCount: expandedIds.length }
  }, [rows, chainCache])

  // ── Filtering ─────────────────────────────────────────────────────────
  const filteredRows = useMemo(() => {
    return rows.filter((r) => {
      if (actionFilter !== 'ALL' && reasonToAction(r.reason) !== actionFilter) return false
      // The exposed list surface only contains rejections, so non-REJECTED
      // outcome filters yield an empty set (with a friendly note rendered in
      // the empty state).
      if (outcomeFilter !== 'ALL' && outcomeFilter !== 'REJECTED') return false
      if (tokenQuery.trim()) {
        const q = tokenQuery.trim().toLowerCase()
        const matchesToken = r.token_id.toLowerCase().includes(q)
        const matchesStrat = (r.strategy ?? '').toLowerCase().includes(q)
        const matchesDec = r.decision_id.toLowerCase().includes(q)
        const matchesReason = (r.reason ?? '').toLowerCase().includes(q)
        if (!matchesToken && !matchesStrat && !matchesDec && !matchesReason) return false
      }
      return true
    })
  }, [rows, actionFilter, outcomeFilter, tokenQuery])

  // ── Loading state (skeleton) ──────────────────────────────────────────
  if (loading) {
    return (
      <div className="card h-full flex flex-col p-3 bg-[#13161e] border border-[#1f2335] shadow-xl">
        <div className="card-header pb-2 mb-3 border-b border-[#1f2335] flex items-center justify-between">
          <span className="card-title text-xs font-bold text-[#dde1ed]">
            🧠 DECISION LEDGER
          </span>
          <span className="badge badge-cyan text-[9.5px] animate-pulse">Loading…</span>
        </div>
        <div className="flex-1 space-y-2 p-2">
          {Array.from({ length: 7 }).map((_, i) => (
            <div
              key={i}
              className="skeleton-line-lg"
              style={{ width: `${65 + ((i * 7) % 30)}%` }}
            />
          ))}
        </div>
      </div>
    )
  }

  // ── Error state ────────────────────────────────────────────────────────
  if (error) {
    return (
      <div className="card h-full flex flex-col p-3 bg-[#13161e] border border-[#1f2335] shadow-xl">
        <div className="card-header pb-2 mb-2 border-b border-[#1f2335] flex items-center justify-between">
          <span className="card-title text-xs font-bold text-[#dde1ed]">
            🧠 DECISION LEDGER
          </span>
          <span className="badge badge-red text-[9.5px]">Offline</span>
        </div>
        <div className="flex-1 flex flex-col items-center justify-center gap-2 p-6 text-center">
          <AlertTriangle size={20} className="text-red-400" />
          <span className="text-xs text-[#dde1ed] font-medium">
            Decision ledger unavailable
          </span>
          <span className="text-[11px] text-[#7e8aaa] max-w-md break-words">{error}</span>
          <button onClick={fetchList} className="btn btn-ghost btn-sm mt-2">
            <RefreshCw size={12} /> Retry
          </button>
        </div>
      </div>
    )
  }

  // ── Main render ───────────────────────────────────────────────────────
  return (
    <div className="card h-full flex flex-col p-3 bg-[#13161e] border border-[#1f2335] shadow-xl">
      {/* Header with Stats Strip */}
      <div className="card-header pb-2 mb-2 border-b border-[#1f2335] flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <div className="w-2 h-2 rounded-full bg-cyan-400 animate-pulse" />
          <span className="card-title text-xs font-bold text-[#dde1ed] tracking-wide">
            🧠 DECISION LEDGER
          </span>
          <span
            className="badge badge-cyan text-[9.5px]"
            title="Audit trail: PREDICTION → SIGNAL → RISK → ORDER → FILL"
          >
            <Activity size={10} /> Correlation Audit
          </span>
        </div>
        {/* KPI strip */}
        <div className="flex items-center gap-2 flex-wrap">
          <StatChip
            label="Decisions"
            value={stats.total.toString()}
            sub="rejections"
            title="Total rejection-stage decisions in the recent window (limit 50)"
          />
          <StatChip
            label="Avg Edge"
            value={`${stats.avgEdge >= 0 ? '+' : ''}${stats.avgEdge.toFixed(3)}`}
            color={stats.avgEdge >= 0 ? 'var(--color-green-fg)' : 'var(--color-red-fg)'}
            title="Mean predicted edge across recent rejections"
          />
          <StatChip
            label="Avg Conf"
            value={fmtPct(stats.avgConf)}
            color="var(--color-cyan-fg)"
            title="Mean model confidence across recent rejections"
          />
          <StatChip
            label="Top Reason"
            value={stats.topReason ? REASON_LABELS[stats.topReason] ?? stats.topReason : '—'}
            title="Most frequent rejection reason in the recent window"
          />
          {stats.fillRate != null && (
            <StatChip
              label="Fill Rate"
              value={`${(stats.fillRate * 100).toFixed(0)}%`}
              sub={`of ${stats.expandedCount} expanded`}
              color="var(--color-green-fg)"
              title="Ratio of expanded tokens that have at least one FILL event in their chain (token-level, not decision-level)"
            />
          )}
          {lastUpdated && (
            <span
              className="text-[9.5px] text-[#5a637a] mono ml-1 flex items-center gap-1"
              title={`Last refresh: ${new Date(lastUpdated).toLocaleString()}`}
            >
              <Clock size={10} />
              {fmtAge(lastUpdated / 1000)}
            </span>
          )}
        </div>
      </div>

      {/* Filter Bar */}
      <div className="flex flex-wrap items-center gap-2 mb-2">
        {/* Token search */}
        <div className="relative flex-1 min-w-[180px] max-w-xs">
          <Search
            size={12}
            className="absolute left-2 top-1/2 -translate-y-1/2 text-[#5a637a] pointer-events-none"
          />
          <input
            type="text"
            value={tokenQuery}
            onChange={(e) => setTokenQuery(e.target.value)}
            placeholder="Search token, strategy, decision_id…"
            className="w-full text-xs bg-[#0e1015] border border-[#1f2335] focus:border-cyan-500/50 rounded pl-7 pr-2.5 py-1.5 text-[#dde1ed] placeholder-[#3e4560] outline-none transition-all"
            aria-label="Search decisions"
          />
          {tokenQuery && (
            <button
              onClick={() => setTokenQuery('')}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-[10px] text-[#7e8aaa] hover:text-white"
              aria-label="Clear search"
            >
              ✕
            </button>
          )}
        </div>
        {/* Action filter */}
        <select
          value={actionFilter}
          onChange={(e) => setActionFilter(e.target.value as ActionFilter)}
          className="bg-[#0e1015] border border-[#1f2335] text-[#7e8aaa] rounded text-[10px] font-semibold px-2 py-1 outline-none cursor-pointer hover:border-[#2d3450]"
          aria-label="Filter by action type"
        >
          {(Object.keys(ACTION_LABELS) as ActionFilter[]).map((k) => (
            <option key={k} value={k}>
              {ACTION_LABELS[k]}
            </option>
          ))}
        </select>
        {/* Outcome filter */}
        <select
          value={outcomeFilter}
          onChange={(e) => setOutcomeFilter(e.target.value as OutcomeFilter)}
          className="bg-[#0e1015] border border-[#1f2335] text-[#7e8aaa] rounded text-[10px] font-semibold px-2 py-1 outline-none cursor-pointer hover:border-[#2d3450]"
          aria-label="Filter by outcome"
        >
          {(Object.keys(OUTCOME_LABELS) as OutcomeFilter[]).map((k) => (
            <option key={k} value={k}>
              {OUTCOME_LABELS[k]}
            </option>
          ))}
        </select>
        <button
          onClick={fetchList}
          className="btn btn-ghost btn-sm text-[10px] px-2 py-1 border border-[#1f2335] text-[#7e8aaa] hover:text-white hover:border-[#2d3450] flex items-center gap-1"
          title="Refresh now"
        >
          <RefreshCw size={11} /> Refresh
        </button>
      </div>

      {/* Decision List (expandable cards) */}
      <div className="overflow-auto scrollbar-thin flex-1 min-h-0">
        {filteredRows.length === 0 ? (
          <div className="empty-state py-8">
            <span className="empty-state-icon text-2xl" aria-hidden="true">
              🧠
            </span>
            <span className="empty-state-title text-sm font-semibold">
              No decisions recorded
            </span>
            <span className="empty-state-desc text-xs max-w-sm text-center">
              {rows.length === 0
                ? 'Decision events will appear here as the signal trader evaluates markets. Each rejection is recorded with its full PREDICTION → SIGNAL → RISK chain; expand any row to inspect the audit trail.'
                : outcomeFilter !== 'ALL' && outcomeFilter !== 'REJECTED'
                ? `No ${outcomeFilter.toLowerCase()} decisions in the recent window — the exposed ledger surface currently lists rejections only. Switch to "All Outcomes" or "Rejected" to see rows.`
                : 'No decisions match your active filters.'}
            </span>
          </div>
        ) : (
          <div className="divide-y divide-[#1f2335]/40">
            {filteredRows.map((r) => (
              <DecisionCard
                key={r.decision_id || `${r.token_id}-${r.timestamp}`}
                row={r}
                expanded={expandedId === r.decision_id}
                onToggle={() => toggleExpand(r)}
                chainData={
                  r.decision_id ? chainCache[r.decision_id] ?? null : null
                }
                chainLoading={
                  r.decision_id ? chainLoading[r.decision_id] ?? false : false
                }
                chainError={
                  r.decision_id ? chainErrors[r.decision_id] ?? null : null
                }
              />
            ))}
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="table-footer">
        <span className="flex items-center gap-1.5">
          <Filter size={10} />
          <span>
            {filteredRows.length} of {rows.length} decisions
          </span>
        </span>
        <span className="mono text-[9.5px] flex items-center gap-1">
          <Clock size={10} /> Polling every 10s · auto-pause when tab hidden
        </span>
      </div>
    </div>
  )
}
