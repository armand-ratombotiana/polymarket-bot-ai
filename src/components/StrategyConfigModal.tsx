// components/StrategyConfigModal.tsx — Live Strategy Configuration & Risk Tuning
'use client'

import { useEffect, useState, useRef } from 'react'
import { getApiUrl, apiFetch } from '@/lib/api'

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
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  
  const modalRef = useRef<HTMLDivElement>(null)

  // Escape key handler
  useEffect(() => {
    if (!isOpen) return
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [isOpen, onClose])

  useEffect(() => {
    if (!isOpen) return
    const fetchConfig = async () => {
      setLoading(true)
      setError(null)
      try {
        const apiUrl = getApiUrl()
        const res = await apiFetch(`${apiUrl}/api/config`)
        if (res.ok) {
          setConfig(await res.json())
        } else {
          setError(`Failed to load configuration (HTTP ${res.status})`)
        }
      } catch {
        setError('Network error fetching configuration')
      } finally {
        setLoading(false)
      }
    }
    fetchConfig()
  }, [isOpen])

  if (!isOpen) return null

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!config) return
    setSaving(true)
    setMsg(null)
    try {
      const apiUrl = getApiUrl()
      const res = await apiFetch(`${apiUrl}/api/config`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(config),
      })
      if (res.ok) {
        setMsg({ ok: true, text: 'Configuration updated live in memory!' })
        setTimeout(() => onClose(), 1200)
      } else {
        const err = await res.json().catch(() => null)
        setMsg({ ok: false, text: err?.detail || 'Failed to update configuration' })
      }
    } catch {
      setMsg({ ok: false, text: 'Error reaching bot API server' })
    }
    setSaving(false)
  }

  return (
    <div
      className="modal-backdrop"
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
      role="presentation"
    >
      <div
        ref={modalRef}
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="config-modal-title"
      >
        <div className="modal-header">
          <div>
            <h2 id="config-modal-title" className="text-sm font-bold text-[#dde1ed]">
              ⚙️ Strategy &amp; Risk Configuration
            </h2>
            <span className="text-[10px] text-[#7e8aaa]">
              Runtime parameters for $100 operating capital regime
            </span>
          </div>
          <button onClick={onClose} className="modal-close" aria-label="Close configuration modal">
            ✕
          </button>
        </div>

        {loading ? (
          <div className="modal-body flex flex-col items-center justify-center py-12 text-[#7e8aaa] text-xs">
            <span className="spinner mb-2" aria-hidden="true" />
            Loading current parameters…
          </div>
        ) : error ? (
          <div className="modal-body py-8 text-center">
            <div className="banner-danger text-xs py-2 px-3 mb-3">
              ⚠️ {error}
            </div>
            <button onClick={onClose} className="btn btn-ghost btn-sm">Close</button>
          </div>
        ) : config ? (
          <form onSubmit={handleSave} className="modal-body space-y-4 text-xs">
            {/* Market Maker Settings */}
            <div className="space-y-2 bg-[#0e1015] p-3 rounded border border-[#1f2335]">
              <div className="text-[11px] font-bold text-blue-400 uppercase tracking-wider">
                Avellaneda-Stoikov Market Maker
              </div>
              <div className="grid grid-cols-3 gap-2">
                <div>
                  <label className="form-label text-[10px]">Spread (BPS)</label>
                  <input
                    type="number"
                    min="10"
                    max="2000"
                    step="10"
                    value={config.mm_spread_bps}
                    onChange={(e) => setConfig({ ...config, mm_spread_bps: parseInt(e.target.value) || 200 })}
                    className="input input-sm mono"
                  />
                  <span className="form-hint">10–2000 bps</span>
                </div>
                <div>
                  <label className="form-label text-[10px]">Quote Size ($)</label>
                  <input
                    type="number"
                    min="0.5"
                    max="5.0"
                    step="0.5"
                    value={config.mm_quote_size_usdc}
                    onChange={(e) => setConfig({ ...config, mm_quote_size_usdc: parseFloat(e.target.value) || 1.5 })}
                    className="input input-sm mono"
                  />
                  <span className="form-hint">$0.50–$5.00</span>
                </div>
                <div>
                  <label className="form-label text-[10px]">Max Inv ($)</label>
                  <input
                    type="number"
                    min="1.0"
                    max="15.0"
                    step="1.0"
                    value={config.mm_max_inventory_usdc}
                    onChange={(e) => setConfig({ ...config, mm_max_inventory_usdc: parseFloat(e.target.value) || 15 })}
                    className="input input-sm mono"
                  />
                  <span className="form-hint">$1.00–$15.00</span>
                </div>
              </div>
            </div>

            {/* Arbitrage Scanner Settings */}
            <div className="space-y-2 bg-[#0e1015] p-3 rounded border border-[#1f2335]">
              <div className="text-[11px] font-bold text-amber-400 uppercase tracking-wider">
                Dutch-Book Arbitrage Scanner
              </div>
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <label className="form-label text-[10px]">Min Profit (BPS)</label>
                  <input
                    type="number"
                    min="5"
                    max="1000"
                    step="5"
                    value={config.arb_min_profit_bps}
                    onChange={(e) => setConfig({ ...config, arb_min_profit_bps: parseInt(e.target.value) || 50 })}
                    className="input input-sm mono"
                  />
                  <span className="form-hint">5–1000 bps</span>
                </div>
                <div>
                  <label className="form-label text-[10px]">Arb Leg Size ($)</label>
                  <input
                    type="number"
                    min="0.5"
                    max="5.0"
                    step="0.5"
                    value={config.arb_order_size_usdc}
                    onChange={(e) => setConfig({ ...config, arb_order_size_usdc: parseFloat(e.target.value) || 1.5 })}
                    className="input input-sm mono"
                  />
                  <span className="form-hint">$0.50–$5.00</span>
                </div>
              </div>
            </div>

            {/* ML & Risk Settings */}
            <div className="space-y-2 bg-[#0e1015] p-3 rounded border border-[#1f2335]">
              <div className="text-[11px] font-bold text-green-400 uppercase tracking-wider">
                ML Signal &amp; Risk Engine
              </div>
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <label className="form-label text-[10px]">ML Min Confidence</label>
                  <input
                    type="number"
                    min="0.50"
                    max="0.99"
                    step="0.01"
                    value={config.signal_min_confidence}
                    onChange={(e) => setConfig({ ...config, signal_min_confidence: parseFloat(e.target.value) || 0.52 })}
                    className="input input-sm mono"
                  />
                  <span className="form-hint">0.50–0.99</span>
                </div>
                <div>
                  <label className="form-label text-[10px]">Daily Loss Stop ($)</label>
                  <input
                    type="number"
                    min="0.25"
                    max="2.0"
                    step="0.25"
                    value={config.daily_loss_limit_usdc}
                    onChange={(e) => setConfig({ ...config, daily_loss_limit_usdc: parseFloat(e.target.value) || 2.0 })}
                    className="input input-sm mono"
                  />
                  <span className="form-hint">$0.25–$2.00 hard stop</span>
                </div>
              </div>
            </div>

            {msg && (
              <div className={`p-2 rounded text-center text-xs ${
                msg.ok ? 'bg-green-500/10 text-green-400' : 'bg-red-500/10 text-red-400'
              }`} role="status">
                {msg.ok ? '✅ ' : '⚠️ '}{msg.text}
              </div>
            )}

            <div className="modal-footer px-0 pb-0">
              <button
                type="button"
                onClick={onClose}
                className="btn btn-ghost btn-sm"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={saving}
                className="btn btn-primary btn-sm"
              >
                {saving ? 'Applying…' : 'Apply Live'}
              </button>
            </div>
          </form>
        ) : null}
      </div>
    </div>
  )
}
