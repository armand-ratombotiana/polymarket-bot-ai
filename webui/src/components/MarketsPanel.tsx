// components/MarketsPanel.tsx
'use client'

import { OrderBook } from '@/hooks/useBot'

interface Props {
  books: OrderBook[]
  onSelectMarket?: (tokenId: string, slug: string) => void
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
      <div className="w-16 h-1.5 bg-[#252836] rounded-full overflow-hidden">
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

export default function MarketsPanel({ books, onSelectMarket }: Props) {
  return (
    <div className="card flex flex-col min-h-0">
      <div className="card-header flex justify-between items-center">
        <span className="card-title">📈 Markets &amp; Live Order Books</span>
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-blue-400 bg-blue-500/10 px-2 py-0.5 rounded border border-blue-500/20">
            Click row for Depth &amp; Trade
          </span>
          <span className="text-[11px] text-[#4a5068]">{books.length} tracked</span>
        </div>
      </div>
      <div className="overflow-auto scrollbar-thin flex-1">
        {books.length === 0 ? (
          <div className="flex items-center justify-center h-24 text-[#4a5068] text-xs">
            Loading market data…
          </div>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>Market</th>
                <th>Bid</th>
                <th>Ask</th>
                <th>Mid Price</th>
                <th>Spread</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {books.map((b) => (
                <tr
                  key={b.token_id}
                  onClick={() => onSelectMarket && onSelectMarket(b.token_id, b.slug)}
                  className="cursor-pointer hover:bg-blue-500/10 transition-colors"
                  title="Click to view live depth chart & execute trade"
                >
                  <td className="max-w-[160px]">
                    <span className="text-[#e8eaf0] truncate block font-medium" title={b.slug}>
                      {b.slug}
                    </span>
                  </td>
                  <td className="text-green-400 mono font-medium">
                    {b.best_bid != null ? b.best_bid.toFixed(4) : '—'}
                  </td>
                  <td className="text-red-400 mono font-medium">
                    {b.best_ask != null ? b.best_ask.toFixed(4) : '—'}
                  </td>
                  <td>
                    <MidBar mid={b.mid} />
                  </td>
                  <td className="text-[#8b91a8] mono">
                    {b.spread != null ? `${(b.spread * 100).toFixed(1)}¢` : '—'}
                  </td>
                  <td className="text-[#4a5068] mono">{age(b.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
