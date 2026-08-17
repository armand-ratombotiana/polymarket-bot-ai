// components/DatabaseExplorerView.tsx — Time-Series Data Explorer
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl, apiFetch } from '@/lib/api'

type TableName = 'market_snapshots' | 'orderbook_ticks' | 'fundamental_news' | 'ml_feature_store'

const TABLE_DESCRIPTIONS: Record<TableName, string> = {
  market_snapshots: 'Periodic snapshots of top-of-book prices, spreads, and implied probabilities.',
  orderbook_ticks: 'L2 book depth updates and order flow imbalance (OFI) calculations.',
  fundamental_news: 'Fundamental news headlines, sentiment scores, and event tags.',
  ml_feature_store: '32-dimensional feature vectors computed from live microstructure data.',
}

export default function DatabaseExplorerView() {
  const [selectedTable, setSelectedTable] = useState<TableName>('market_snapshots')
  const [records, setRecords] = useState<any[]>([])
  const [loading, setLoading] = useState(true)

  const fetchRecords = async (table: TableName) => {
    setLoading(true)
    try {
      const apiUrl = getApiUrl()
      const res = await apiFetch(`${apiUrl}/api/database/records?table=${table}&limit=30`)
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
    { id: 'fundamental_news', label: 'Fundamental News', icon: '📰' },
    { id: 'ml_feature_store', label: 'ML Feature Store (32D)', icon: '🧠' },
  ]

  return (
    <div className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden p-4 space-y-3 overflow-y-auto scrollbar-thin">
      {/* Header */}
      <div className="flex justify-between items-center pb-2 border-b border-[#1f2335]">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-lg" aria-hidden="true">🗄️</span>
            <span className="text-sm font-bold text-[#dde1ed]">
              Database &amp; Time-Series Explorer
            </span>
          </div>
          <p className="text-xs text-[#7e8aaa]">
            Inspect persisted historical tables, tick depth records, and ML feature stores
          </p>
        </div>
      </div>

      {/* Table Selector Tabs */}
      <div className="flex gap-2 bg-[#0e1015] p-1.5 rounded-lg border border-[#1f2335] overflow-x-auto scrollbar-thin">
        {tables.map((t) => (
          <button
            key={t.id}
            onClick={() => setSelectedTable(t.id)}
            className={`btn btn-sm ${
              selectedTable === t.id
                ? 'btn-primary'
                : 'btn-ghost'
            }`}
          >
            <span aria-hidden="true">{t.icon}</span>
            <span>{t.label}</span>
          </button>
        ))}
      </div>

      {/* Data Table */}
      <div className="card p-3 bg-[#0e1015] border border-[#1f2335] flex-1 flex flex-col">
        <div className="card-header pb-2 mb-2 border-b border-[#1f2335] flex flex-wrap justify-between items-center gap-2">
          <div>
            <span className="card-title text-xs font-bold text-[#dde1ed]">
              📋 Table: <span className="mono text-cyan-400">{selectedTable}</span> ({records.length} records)
            </span>
            <span className="text-[11px] text-[#7e8aaa] block mt-0.5">
              {TABLE_DESCRIPTIONS[selectedTable]}
            </span>
          </div>
          <span className="text-[10px] text-[#7e8aaa] mono">Polled every 5s</span>
        </div>

        {loading && records.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-48 text-xs text-[#7e8aaa]">
            <span className="spinner mb-2" aria-hidden="true" />
            Querying table records…
          </div>
        ) : records.length === 0 ? (
          <div className="empty-state py-12">
            <span className="empty-state-icon" aria-hidden="true">🗄️</span>
            <span className="empty-state-title">No records in {selectedTable}</span>
            <span className="empty-state-desc">
              Data is currently buffered in memory or writing to storage. Persisted records will appear here as ticks occur.
            </span>
          </div>
        ) : (
          <div className="overflow-x-auto scrollbar-thin flex-1 table-container">
            <table className="data-table text-xs" role="table" aria-label={`Database table ${selectedTable}`}>
              <thead>
                <tr>
                  {Object.keys(records[0] || {}).map((col) => (
                    <th key={col} scope="col" className="mono capitalize">{col.replace(/_/g, ' ')}</th>
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
