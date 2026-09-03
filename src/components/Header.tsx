// components/Header.tsx — Simplified header (tab nav moved to Sidebar)
// Retains: logo, kill switch button, global utility actions.
// REMOVED: "50+ Strategies", "Calibrated", "1-Click", "WAL", "Monte Carlo" badges.
'use client'

import { useState } from 'react'
import { getApiToken } from '@/lib/api'

// Keep ActiveTab exported for backward compat with any remaining callers.
// New code should use NavSection from Sidebar instead.
export type ActiveTab = 'terminal' | 'strategies' | 'aiml' | 'arbitrage' | 'analysis' | 'backtest' | 'database' | 'copilot' | 'screener' | 'health'

export default function Header() {
  const [tokenDraft, setTokenDraft] = useState('')
  const [tokenSet, setTokenSet] = useState(Boolean(getApiToken()))

  const saveToken = () => {
    const value = tokenDraft.trim()
    if (typeof window !== 'undefined') {
      if (value) window.localStorage.setItem('polymarket_api_token', value)
      else window.localStorage.removeItem('polymarket_api_token')
    }
    setTokenSet(Boolean(value))
    setTokenDraft('')
    if (value) window.location.reload()
  }

  return (
    <div style={{ display: 'none' }}>
      {/* Header tab navigation has been replaced by Sidebar + TopStatusBar.
          This component is retained only for the API token widget which is
          now accessible via TopStatusBar config controls. */}
      {!tokenSet && (
        <form onSubmit={(e) => { e.preventDefault(); saveToken() }}>
          <input
            type="password"
            value={tokenDraft}
            onChange={(e) => setTokenDraft(e.target.value)}
            placeholder="API token"
            aria-label="API authentication token"
          />
          <button type="submit">Set Token</button>
        </form>
      )}
    </div>
  )
}
