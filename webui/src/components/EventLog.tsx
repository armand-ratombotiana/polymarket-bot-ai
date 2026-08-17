// components/EventLog.tsx — Pro Terminal Audit & Event Stream
'use client'

import { useState } from 'react'

interface Props {
  events: string[]
}

type EventFilter = 'all' | 'fill' | 'order' | 'risk' | 'ml'

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

  const getEventStyle = (text: string) => {
    const lower = text.toLowerCase()
    if (lower.includes('fill') || lower.includes('trade') || lower.includes('win') || lower.includes('+'))
      return 'text-green-400'
    if (lower.includes('kill') || lower.includes('risk') || lower.includes('reject') || lower.includes('error'))
      return 'text-red-400'
    if (lower.includes('ml') || lower.includes('ai') || lower.includes('prob'))
      return 'text-cyan-400'
    if (lower.includes('order') || lower.includes('cancel'))
      return 'text-[#8b91a8]'
    return 'text-[#e8eaf0]'
  }

  return (
    <div className="card flex flex-col h-full min-h-0 bg-[#111318] border border-[#252836]">
      {/* Header */}
      <div className="card-header flex flex-wrap justify-between items-center px-3 py-2 border-b border-[#252836] gap-2">
        <div className="flex items-center gap-2">
          <span className="card-title text-xs font-bold text-[#e8eaf0]">📜 Live System Events</span>
          <span className="text-[10px] text-[#8b91a8] mono">({events.length})</span>
        </div>

        {/* Search & Filter Controls */}
        <div className="flex items-center gap-1.5">
          <input
            type="text"
            placeholder="Search events…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="input input-sm w-28 text-[10px] py-0.5"
            aria-label="Filter events"
          />
          <div className="flex items-center gap-1 bg-[#161822] p-0.5 rounded border border-[#252836]">
            {(['all', 'fill', 'order', 'risk', 'ml'] as EventFilter[]).map((f) => (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className={`px-2 py-0.5 rounded text-[10px] uppercase font-semibold transition-all ${
                  filter === f ? 'bg-blue-500 text-black' : 'text-[#8b91a8] hover:text-white'
                }`}
              >
                {f}
              </button>
            ))}
          </div>
          <button
            onClick={handleCopy}
            className="text-[10px] text-[#8b91a8] hover:text-white mono bg-[#161822] px-2 py-0.5 rounded border border-[#252836]"
          >
            {copied ? '✓' : 'Copy'}
          </button>
        </div>
      </div>

      {/* Event Stream */}
      <div className="flex-1 overflow-y-auto p-2.5 font-mono text-[11px] space-y-1 scrollbar-thin bg-[#0e1015]">
        {filtered.length === 0 ? (
          <div className="flex items-center justify-center h-28 text-[#4a5068] text-xs">
            No events match current filter.
          </div>
        ) : (
          filtered.map((e, i) => (
            <div key={i} className="leading-relaxed hover:bg-[#161822] px-1.5 py-0.5 rounded transition-colors flex items-start gap-1.5">
              <span className={getEventStyle(e)}>{e}</span>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
