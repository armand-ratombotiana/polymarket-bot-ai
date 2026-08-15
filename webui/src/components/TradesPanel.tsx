// components/TradesPanel.tsx
'use client'

import { Trade } from '@/hooks/useBot'

interface Props { trades: Trade[] }

function fmtTime(ts: number) {
  return new Date(ts * 1000).toLocaleTimeString('en', { hour12: false })
}

export default function TradesPanel({ trades }: Props) {
  return (
    <div className="card flex flex-col min-h-0">
      <div className="card-header">
        <span className="card-title">⚡ Recent Trades</span>
        <span className="text-[11px] text-[#4a5068]">{trades.length}</span>
      </div>
      <div className="overflow-auto scrollbar-thin flex-1">
        {trades.length === 0 ? (
          <div className="flex items-center justify-center h-20 text-[#4a5068] text-xs">No trades yet</div>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Market</th>
                <th>Side</th>
                <th>Price</th>
                <th>Size</th>
                <th>P&L</th>
                <th>Strategy</th>
              </tr>
            </thead>
            <tbody>
              {trades.map(t => (
                <tr key={t.trade_id}>
                  <td className="mono text-[#4a5068] text-[11px]">{fmtTime(t.timestamp)}</td>
                  <td><span className="text-[#e8eaf0] truncate block max-w-[120px]" title={t.slug}>{t.slug}</span></td>
                  <td>
                    <span className={`badge ${t.side === 'BUY' ? 'badge-green' : 'badge-red'}`}>{t.side}</span>
                  </td>
                  <td className="mono text-[#e8eaf0]">{t.price.toFixed(4)}</td>
                  <td className="mono text-[#8b91a8]">{t.size.toFixed(2)}</td>
                  <td className={`mono font-semibold ${t.pnl > 0 ? 'text-green-400' : t.pnl < 0 ? 'text-red-400' : 'text-[#8b91a8]'}`}>
                    {t.pnl >= 0 ? '+' : ''}${t.pnl.toFixed(3)}
                  </td>
                  <td><span className="badge badge-dim">{t.strategy}{t.paper ? ' (P)' : ''}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
