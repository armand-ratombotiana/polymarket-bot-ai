// components/DatabaseExplorerView.tsx — TimescaleDB & PostgreSQL Time-Series Data Explorer
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl } from '@/lib/api'

type TableName = 'market_snapshots' | 'orderbook_ticks' | 'fundamental_news' | 'ml_feature_store'

export default function DatabaseExplorerView() {
  const [selectedTable, setSelectedTable] = useState<TableName>('market_snapshots')
  const [records, setRecords] = useState<any[]>([])
  const [loading, setLoading] = useState(true)

  const fetchRecords = async (table: TableName) => {
    setLoading(true)
    try {
      const apiUrl = getApiUrl()
      const res = await fetch(`${apiUrl}/api/database/records?table=${table}&limit=30`)
      if (res.ok) {
        const json = await res.json()
        setRecords(json.records || [])
      }
    } catch {}
    setLoading(false)
  }

  useEffect(() => {
    fetchRecords(selectedTable)
    const timer = setInterval(() => fetchRecords(selectedTable), 5000)
    return () => clearInterval(timer)
  }, [selectedTable])

  const tables: Array<{ id: TableName; label: string; icon: string }> = [
    { id: 'market_snapshots', label: 'Market Snapshots', icon: '📊' },
    { id: 'orderbook_ticks', label: 'Orderbook Ticks (OFI)', icon: '⚡' },
    { id: 'fundamental_news', label: 'Fundamental News & Sentiment', icon: '📰' },
    { id: 'ml_feature_store', label: 'ML Feature Store (32D)', icon: '🧠' },
  ]

  return (
    <div className="flex flex-col h-full bg-[#111318] border border-[#252836] rounded-lg overflow-hidden p-4 space-y-4 overflow-y-auto scrollbar-thin">
      {/* Header */}
      <div className="flex justify-between items-center pb-3 border-b border-[#252836]">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-xl">🗄️</span>
            <span className="text-base font-bold text-[#e8eaf0]">
              TimescaleDB &amp; PostgreSQL Time-Series Explorer
            </span>
          </div>
          <p className="text-xs text-[#8b91a8]">
            Real-time hypertable queries for micro-depth ticks, order flow imbalance, and ML feature stores
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className="badge badge-green text-xs font-semibold">WAL Mode / Auto-Sync Active</span>
        </div>
      </div>

      {/* Table Selector Tabs */}
      <div className="flex gap-2 bg-[#161822] p-1.5 rounded-lg border border-[#252836]">
        {tables.map((t) => (
          <button
            key={t.id}
            onClick={() => setSelectedTable(t.id)}
            className={`px-3 py-1.5 rounded text-xs font-bold transition-all flex items-center gap-1.5 ${
              selectedTable === t.id
                ? 'bg-blue-500 text-black shadow-md'
                : 'text-[#8b91a8] hover:text-white hover:bg-[#252836]/60'
            }`}
          >
            <span>{t.icon}</span>
            <span>{t.label}</span>
          </button>
        ))}
      </div>

      {/* Data Table */}
      <div className="card p-3.5 bg-[#161822] border border-[#252836] flex-1">
        <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex justify-between items-center">
          <span className="card-title text-xs font-bold text-[#e8eaf0]">
            📋 Hypertable: <span className="mono text-cyan-400">{selectedTable}</span> ({records.length} records)
          </span>
          <span className="text-[10px] text-[#8b91a8] mono">Auto-refreshes every 5s</span>
        </div>

        {loading ? (
          <div className="flex items-center justify-center h-48 text-xs text-[#8b91a8]">
            Querying TimescaleDB hypertable records…
          </div>
        ) : records.length === 0 ? (
          <div className="flex items-center justify-center h-48 text-xs text-[#4a5068]">
            No records in this hypertable yet.
          </div>
        ) : (
          <div className="overflow-x-auto scrollbar-thin">
            <table className="data-table text-xs">
              <thead>
                <tr>
                  {Object.keys(records[0] || {}).map((col) => (
                    <th key={col} className="mono capitalize">{col.replace(/_/g, ' ')}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {records.map((r, i) => (
                  <tr key={i} className="hover:bg-blue-500/10 transition-colors">
                    {Object.entries(r).map(([k, val]: [string, any], j) => (
                      <td key={j} className="mono text-xs max-w-[200px] truncate">
                        {typeof val === 'number'
                          ? val.toFixed(k.includes('time') ? 0 : 4)
                          : String(val)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
