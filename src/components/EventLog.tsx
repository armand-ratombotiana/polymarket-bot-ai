// components/EventLog.tsx — Pro Terminal Audit & Event Stream
'use client'

import { useState } from 'react'

interface Props {
  events: string[]
}

type EventFilter = 'all' | 'fill' | 'order' | 'risk' | 'ml'

const SEVERITY_ICON: Record<string, string> = {
  fill:  '✅',
  trade: '✅',
  win:   '✅',
  kill:  '🛑',
  risk:  '🛑',
  error: '🛑',
  reject:'🛑',
  ml:    '🤖',
  ai:    '🤖',
  prob:  '🤖',
  order: '⚡',
  cancel:'⚡',
  quoted:'⚡',
}

function getEventSeverityIcon(text: string): string {
  const lower = text.toLowerCase()
  for (const [keyword, icon] of Object.entries(SEVERITY_ICON)) {
    if (lower.includes(keyword)) return icon
  }
  return '◦'
}

function getEventStyle(text: string): string {
  const lower = text.toLowerCase()
  if (lower.includes('fill') || lower.includes('trade') || lower.includes('win'))
    return 'text-green-400'
  if (lower.includes('kill') || lower.includes('risk') || lower.includes('reject') || lower.includes('error'))
    return 'text-red-400'
  if (lower.includes('ml') || lower.includes('ai') || lower.includes('prob'))
    return 'text-cyan-400'
  if (lower.includes('order') || lower.includes('cancel'))
    return 'text-[#8b91a8]'
  return 'text-[#e8eaf0]'
}

// Parse leading timestamp from event strings like "[12:34:56]" or "12:34:56 - "
function parseEventParts(text: string): { timestamp: string; message: string } {
  // Try [HH:MM:SS] prefix
  const bracketMatch = text.match(/^\[(\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]\s*/)
  if (bracketMatch) {
    return { timestamp: bracketMatch[1], message: text.slice(bracketMatch[0].length) }
  }
  // Try HH:MM:SS - prefix
  const dashMatch = text.match(/^(\d{2}:\d{2}:\d{2})\s*[-–]\s*/)
  if (dashMatch) {
    return { timestamp: dashMatch[1], message: text.slice(dashMatch[0].length) }
  }
  // Try ISO 8601 prefix
  const isoMatch = text.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})/)
  if (isoMatch) {
    return { timestamp: isoMatch[1].slice(11), message: text.slice(isoMatch[0].length).replace(/^[Z\s,:-]+/, '') }
  }
  return { timestamp: '', message: text }
}

export default function EventLog({ events }: Props) {
  const [filter, setFilter] = useState<EventFilter>('all')
  const [search, setSearch] = useState('')
  const [copied, setCopied] = useState(false)

  const filtered = events.filter((e) => {
    const lower = e.toLowerCase()
    const matchSearch = lower.includes(search.toLowerCase())
    if (!matchSearch) return false

    if (filter === 'fill') return lower.includes('fill') || lower.includes('trade')
    if (filter === 'order') return lower.includes('order') || lower.includes('cancel') || lower.includes('quoted')
    if (filter === 'risk') return lower.includes('kill') || lower.includes('risk') || lower.includes('limit')
    if (filter === 'ml') return lower.includes('ml') || lower.includes('learned') || lower.includes('model')
    return true
  })

  const handleCopy = () => {
    navigator.clipboard.writeText(events.join('\n'))
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  const handleExportCsv = () => {
    const header = 'Timestamp,Severity,Message'
    const rows = events.map((e) => {
      const { timestamp, message } = parseEventParts(e)
      const icon = getEventSeverityIcon(e)
      return `"${timestamp}","${icon}","${message.replace(/"/g, '""')}"`
    })
    const csvContent = 'data:text/csv;charset=utf-8,' + [header, ...rows].join('\n')
    const encodedUri = encodeURI(csvContent)
    const link = document.createElement('a')
    link.setAttribute('href', encodedUri)
    link.setAttribute('download', `event_log_${Date.now()}.csv`)
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
  }

  const matchCount = filter !== 'all' || search ? filtered.length : null

  return (
    <div className="card flex flex-col h-full min-h-0 bg-[#13161e] border border-[#1f2335] shadow-md">
      {/* Header */}
      <div className="card-header flex flex-wrap justify-between items-center px-3 py-2 border-b border-[#1f2335] gap-2">
        <div className="flex items-center gap-2">
          <span className="card-title text-xs font-bold text-[#dde1ed]">📜 Live System Events</span>
          <span className="text-[10px] text-[#7e8aaa] mono">
            ({matchCount !== null ? `${matchCount}/${events.length}` : events.length})
          </span>
          {matchCount !== null && (
            <span className="badge badge-blue text-[9px]">{matchCount} match{matchCount !== 1 ? 'es' : ''}</span>
          )}
        </div>

        {/* Search & Filter Controls */}
        <div className="flex items-center gap-1.5">
          <div className="relative">
            <input
              type="text"
              placeholder="Search events…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="input input-sm w-28 text-[10px] py-0.5 bg-[#0e1015] border border-[#1f2335] pr-5"
              aria-label="Filter events"
            />
            {search && (
              <button
                onClick={() => setSearch('')}
                className="absolute right-1.5 top-1/2 -translate-y-1/2 text-[#7e8aaa] hover:text-white text-[11px] leading-none"
                aria-label="Clear search"
              >
                ×
              </button>
            )}
          </div>
          <div className="flex items-center gap-1 bg-[#0e1015] p-0.5 rounded border border-[#1f2335]">
            {(['all', 'fill', 'order', 'risk', 'ml'] as EventFilter[]).map((f) => (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className={`px-2 py-0.5 rounded text-[10px] uppercase font-semibold transition-all ${
                  filter === f ? 'bg-blue-500/20 text-cyan-300 border border-blue-500/40' : 'text-[#7e8aaa] hover:text-white'
                }`}
              >
                {f}
              </button>
            ))}
          </div>
          <button
            onClick={handleCopy}
            className="text-[10px] text-[#7e8aaa] hover:text-white mono bg-[#0e1015] px-2 py-0.5 rounded border border-[#1f2335] transition-colors"
            title="Copy all events to clipboard"
          >
            {copied ? '✓' : 'Copy'}
          </button>
          <button
            onClick={handleExportCsv}
            disabled={events.length === 0}
            className="text-[10px] text-[#7e8aaa] hover:text-white mono bg-[#0e1015] px-2 py-0.5 rounded border border-[#1f2335] transition-colors disabled:opacity-40"
            title="Export event log as CSV"
          >
            📥 CSV
          </button>
        </div>
      </div>

      {/* Event Stream — structured columns */}
      <div className="flex-1 overflow-y-auto p-1.5 space-y-0.5 scrollbar-thin bg-[#0e1015]">
        {filtered.length === 0 ? (
          <div className="flex items-center justify-center h-28 text-[#7e8aaa] text-xs">
            No events match current filter.
          </div>
        ) : (
          filtered.map((e, i) => {
            const { timestamp, message } = parseEventParts(e)
            const icon = getEventSeverityIcon(e)
            return (
              <div
                key={i}
                className="flex items-start gap-2 px-1.5 py-0.5 rounded hover:bg-[#13161e] transition-colors group"
              >
                {/* Severity icon */}
                <span className="shrink-0 w-4 text-center text-[11px] mt-[1px]" aria-hidden="true">
                  {icon}
                </span>
                {/* Timestamp column */}
                {timestamp && (
                  <span className="mono text-[9.5px] text-[#3e4560] shrink-0 mt-[2px] group-hover:text-[#7e8aaa] transition-colors w-16">
                    {timestamp}
                  </span>
                )}
                {/* Message */}
                <span className={`font-mono text-[11px] leading-relaxed ${getEventStyle(e)}`}>
                  {message}
                </span>
              </div>
            )
          })
        )}
      </div>
    </div>
  )
}
