// components/MLPanel.tsx — ML model status card
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl, apiFetch } from '@/lib/api'

interface MLStatus {
  model_type: string
  n_online_updates: number
  last_trained: number
  feature_importances: Record<string, number>
  model_ready: boolean
}

export default function MLPanel() {
  const [ml, setMl] = useState<MLStatus | null>(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    const apiUrl = getApiUrl()
    const fetchML = async () => {
      try {
        const r = await apiFetch(`${apiUrl}/api/ml`)
        if (r.ok) {
          setMl(await r.json())
          setError(false)
        } else {
          setError(true)
        }
      } catch {
        setError(true)
      }
    }
    fetchML()
    const t = setInterval(fetchML, 10000)
    return () => clearInterval(t)
  }, [])

  const sortedFeatures = ml
    ? Object.entries(ml.feature_importances).sort((a, b) => b[1] - a[1]).slice(0, 6)
    : []

  const maxImp = sortedFeatures[0]?.[1] ?? 1

  return (
    <div className="card flex flex-col">
      <div className="card-header">
        <span className="card-title">🤖 ML Model</span>
        <span className={`badge ${ml?.model_ready ? 'badge-green' : 'badge-dim'}`}>
          {ml?.model_ready ? 'Ready' : 'Loading'}
        </span>
      </div>

      {error ? (
        <div className="px-4 py-3 text-[11px] text-[#4a5068]">
          Connecting to bot ML API…
        </div>
      ) : !ml ? (
        <div className="px-4 py-3 text-[11px] text-[#4a5068]">Loading…</div>
      ) : (
        <div className="px-4 py-3 space-y-3">
          {/* Model info */}
          <div className="flex flex-col gap-1 text-[11px]">
            <div className="flex justify-between">
              <span className="text-[#4a5068]">Type</span>
              <span className="text-[#8b91a8]">{ml.model_type}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-[#4a5068]">Online updates</span>
              <span className="mono text-cyan-400 font-semibold">{ml.n_online_updates}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-[#4a5068]">Last trained</span>
              <span className="mono text-[#8b91a8]">
                {ml.last_trained ? new Date(ml.last_trained * 1000).toLocaleTimeString() : '—'}
              </span>
            </div>
          </div>

          {/* Feature importances */}
          {sortedFeatures.length > 0 && (
            <div>
              <div className="text-[10px] uppercase tracking-widest text-[#4a5068] mb-2">
                Feature Importances
              </div>
              <div className="space-y-1.5">
                {sortedFeatures.map(([name, imp]) => (
                  <div key={name} className="flex items-center gap-2">
                    <span className="text-[10px] text-[#8b91a8] w-28 truncate shrink-0">{name}</span>
                    <div className="flex-1 h-1.5 bg-[#252836] rounded-full overflow-hidden">
                      <div
                        className="h-full rounded-full bg-blue-500 transition-all duration-500"
                        style={{ width: `${(imp / maxImp) * 100}%` }}
                      />
                    </div>
                    <span className="mono text-[10px] text-[#4a5068] w-10 text-right shrink-0">
                      {(imp * 100).toFixed(1)}%
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
