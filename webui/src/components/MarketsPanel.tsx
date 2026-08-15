// components/MarketsPanel.tsx
'use client'

import { OrderBook } from '@/hooks/useBot'

interface Props {
  books: OrderBook[]
}

function age(ts: number) {
  const s = Math.floor(Date.now() / 1000 - ts)
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s/60)}m`
  return `${Math.floor(s/3600)}h`
}

function MidBar({ mid }: { mid: number | null }) {
  if (mid === null) return <span className="text-[#4a5068]">—</span>
  const pct = Math.round(mid * 100)
  return (
    <div className="flex items-center gap-2">
      <div className="w-20 h-1.5 bg-[#252836] rounded-full overflow-hidden">
        <div
          className="h-full rounded-full transition-all duration-300"
          style={{
            width: `${pct}%`,
            background: mid > 0.5 ? '#22c55e' : mid < 0.5 ? '#ef4444' : '#8b91a8',
          }}
        />
      </div>
      <span className="mono text-[#e8eaf0]">{(mid * 100).toFixed(1)}¢</span>
    </div>
  )
}

export default function MarketsPanel({ books }: Props) {
  return (
    <div className="card flex flex-col min-h-0">
      <div className="card-header">
        <span className="card-title">📈 Markets</span>
        <span className="text-[11px] text-[#4a5068]">{books.length} tracked</span>
      </div>
      <div className="overflow-auto scrollbar-thin flex-1">
        {books.length === 0 ? (
          <div className="flex items-center justify-center h-24 text-[#4a5068] text-xs">
            Waiting for market data…
          </div>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>Market</th>
                <th>Bid</th>
                <th>Ask</th>
                <th>Mid</th>
                <th>Spread</th>
                <th>Age</th>
              </tr>
            </thead>
            <tbody>
              {books.map(b => (
                <tr key={b.token_id}>
                  <td className="max-w-[160px]">
                    <span className="text-[#e8eaf0] truncate block" title={b.slug}>{b.slug}</span>
                  </td>
                  <td className="text-green-400 mono">{b.best_bid != null ? b.best_bid.toFixed(4) : '—'}</td>
                  <td className="text-red-400 mono">{b.best_ask != null ? b.best_ask.toFixed(4) : '—'}</td>
                  <td><MidBar mid={b.mid} /></td>
                  <td className="text-[#8b91a8] mono">{b.spread != null ? b.spread.toFixed(4) : '—'}</td>
                  <td className="text-[#4a5068]">{age(b.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
