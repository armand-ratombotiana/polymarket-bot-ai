// components/StrategyConfigModal.tsx — Live Strategy Configuration & Risk Tuning
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl } from '@/lib/api'

interface Props {
  isOpen: boolean
  onClose: () => void
}

interface ConfigState {
  mm_spread_bps: number
  mm_quote_size_usdc: number
  mm_max_inventory_usdc: number
  arb_min_profit_bps: number
  arb_order_size_usdc: number
  signal_min_confidence: number
  daily_loss_limit_usdc: number
  max_total_exposure_usdc: number
  max_open_orders: number
}

export default function StrategyConfigModal({ isOpen, onClose }: Props) {
  const [config, setConfig] = useState<ConfigState | null>(null)
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)

  useEffect(() => {
    if (!isOpen) return
    const fetchConfig = async () => {
      try {
        const apiUrl = getApiUrl()
        const res = await fetch(`${apiUrl}/api/config`)
        if (res.ok) {
          setConfig(await res.json())
        }
      } catch {}
    }
    fetchConfig()
  }, [isOpen])

  if (!isOpen || !config) return null

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault()
    setSaving(true)
    setMsg(null)
    try {
      const apiUrl = getApiUrl()
      const res = await fetch(`${apiUrl}/api/config`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(config),
      })
      if (res.ok) {
        setMsg('✅ Configuration updated live!')
        setTimeout(() => onClose(), 1200)
      } else {
        setMsg('❌ Failed to update configuration')
      }
    } catch {
      setMsg('❌ Error reaching API server')
    }
    setSaving(false)
  }

  return (
    <div className="fixed inset-0 bg-black/80 flex items-center justify-center z-50 backdrop-blur-sm p-4">
      <div className="card w-full max-w-lg bg-[#111318] border border-[#252836] shadow-2xl rounded-lg overflow-hidden flex flex-col">
        <div className="card-header flex justify-between items-center px-4 py-3 border-b border-[#252836]">
          <span className="card-title text-sm font-semibold text-[#e8eaf0]">
            ⚙️ Strategy &amp; Risk Configuration
          </span>
          <button onClick={onClose} className="text-[#8b91a8] hover:text-white text-lg px-2">
            ✕
          </button>
        </div>

        <form onSubmit={handleSave} className="p-4 space-y-4 text-xs">
          {/* Market Maker Settings */}
          <div className="space-y-2">
            <div className="text-[11px] font-semibold text-blue-400 uppercase tracking-wider">
              Market Maker Strategy
            </div>
            <div className="grid grid-cols-3 gap-2">
              <div>
                <label className="text-[10px] text-[#4a5068] block mb-1">Spread (BPS)</label>
                <input
                  type="number"
                  min="20"
                  max="1000"
                  step="10"
                  value={config.mm_spread_bps}
                  onChange={(e) => setConfig({ ...config, mm_spread_bps: parseInt(e.target.value) || 200 })}
                  className="w-full bg-[#161822] border border-[#252836] rounded px-2 py-1 mono text-[#e8eaf0]"
                />
              </div>
              <div>
                <label className="text-[10px] text-[#4a5068] block mb-1">Quote Size ($)</label>
                <input
                  type="number"
                  min="1"
                  max="500"
                  step="5"
                  value={config.mm_quote_size_usdc}
                  onChange={(e) => setConfig({ ...config, mm_quote_size_usdc: parseFloat(e.target.value) || 10 })}
                  className="w-full bg-[#161822] border border-[#252836] rounded px-2 py-1 mono text-[#e8eaf0]"
                />
              </div>
              <div>
                <label className="text-[10px] text-[#4a5068] block mb-1">Max Inv ($)</label>
                <input
                  type="number"
                  min="10"
                  max="2000"
                  step="20"
                  value={config.mm_max_inventory_usdc}
                  onChange={(e) => setConfig({ ...config, mm_max_inventory_usdc: parseFloat(e.target.value) || 100 })}
                  className="w-full bg-[#161822] border border-[#252836] rounded px-2 py-1 mono text-[#e8eaf0]"
                />
              </div>
            </div>
          </div>

          {/* Arbitrage Scanner Settings */}
          <div className="space-y-2">
            <div className="text-[11px] font-semibold text-amber-400 uppercase tracking-wider">
              Arb Scanner Strategy
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="text-[10px] text-[#4a5068] block mb-1">Min Profit (BPS)</label>
                <input
                  type="number"
                  min="10"
                  max="500"
                  step="5"
                  value={config.arb_min_profit_bps}
                  onChange={(e) => setConfig({ ...config, arb_min_profit_bps: parseInt(e.target.value) || 50 })}
                  className="w-full bg-[#161822] border border-[#252836] rounded px-2 py-1 mono text-[#e8eaf0]"
                />
              </div>
              <div>
                <label className="text-[10px] text-[#4a5068] block mb-1">Arb Leg Size ($)</label>
                <input
                  type="number"
                  min="1"
                  max="500"
                  step="5"
                  value={config.arb_order_size_usdc}
                  onChange={(e) => setConfig({ ...config, arb_order_size_usdc: parseFloat(e.target.value) || 20 })}
                  className="w-full bg-[#161822] border border-[#252836] rounded px-2 py-1 mono text-[#e8eaf0]"
                />
              </div>
            </div>
          </div>

          {/* ML & Risk Settings */}
          <div className="space-y-2">
            <div className="text-[11px] font-semibold text-green-400 uppercase tracking-wider">
              ML &amp; Risk Parameters
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div>
                <label className="text-[10px] text-[#4a5068] block mb-1">ML Min Confidence</label>
                <input
                  type="number"
                  min="0.50"
                  max="0.95"
                  step="0.05"
                  value={config.signal_min_confidence}
                  onChange={(e) => setConfig({ ...config, signal_min_confidence: parseFloat(e.target.value) || 0.65 })}
                  className="w-full bg-[#161822] border border-[#252836] rounded px-2 py-1 mono text-[#e8eaf0]"
                />
              </div>
              <div>
                <label className="text-[10px] text-[#4a5068] block mb-1">Daily Loss Cap ($)</label>
                <input
                  type="number"
                  min="10"
                  max="1000"
                  step="10"
                  value={config.daily_loss_limit_usdc}
                  onChange={(e) => setConfig({ ...config, daily_loss_limit_usdc: parseFloat(e.target.value) || 50 })}
                  className="w-full bg-[#161822] border border-[#252836] rounded px-2 py-1 mono text-[#e8eaf0]"
                />
              </div>
            </div>
          </div>

          {msg && <div className="text-center font-medium py-1">{msg}</div>}

          <div className="flex justify-end gap-2 pt-2 border-t border-[#252836]">
            <button
              type="button"
              onClick={onClose}
              className="btn btn-ghost px-4 py-1.5 text-xs text-[#8b91a8]"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={saving}
              className="btn btn-primary px-5 py-1.5 text-xs font-semibold"
            >
              {saving ? 'Applying…' : 'Apply Live'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
