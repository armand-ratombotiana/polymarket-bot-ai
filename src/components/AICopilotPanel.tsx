// components/AICopilotPanel.tsx — Market Intelligence & GenAI Copilot Workspace
'use client'

import { useState, useRef, useEffect } from 'react'
import { getApiUrl, apiFetch } from '@/lib/api'

interface Message {
  role: 'user' | 'assistant'
  content: string
  matched_markets?: Array<{ token_id: string; title: string; slug: string; similarity: number; mid_price?: number }>
  timestamp?: number
}

const QUICK_PROMPTS = [
  '🎯 Top high-conviction ML opportunities',
  '⚖️ Current 4-member ensemble weights',
  '⚡ Scan Dutch-Book arbitrage pairs',
  '🛡 Concept drift & Brier loss health',
  '📊 Explain Avellaneda-Stoikov quoting logic',
]

export default function AICopilotPanel({ onSelectMarket }: { onSelectMarket?: (m: { tokenId: string; slug: string }) => void }) {
  const [messages, setMessages] = useState<Message[]>([
    {
      role: 'assistant',
      content:
        '👋 Welcome to the **Polymarket Pro Copilot**. I analyze active order books, 38-feature quant vectors, ensemble probability edges, and macroeconomic news sentiment.\n\nAsk about any live contract, strategy rules, or click a quick prompt below to start.',
      timestamp: Date.now(),
    },
  ])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const messagesEndRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, loading])

  const handleSendQuery = async (queryText: string) => {
    if (!queryText.trim() || loading) return

    setInput('')
    setMessages((prev) => [...prev, { role: 'user', content: queryText, timestamp: Date.now() }])
    setLoading(true)

    try {
      const apiUrl = getApiUrl()
      const res = await apiFetch(`${apiUrl}/api/ai/copilot`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: queryText }),
      })
      if (res.ok) {
        const data = await res.json()
        setMessages((prev) => [
          ...prev,
          {
            role: 'assistant',
            content: data.reply,
            matched_markets: data.matched_markets,
            timestamp: Date.now(),
          },
        ])
      } else {
        setMessages((prev) => [
          ...prev,
          { role: 'assistant', content: '❌ Copilot engine error. Please try again.', timestamp: Date.now() },
        ])
      }
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: '❌ Could not reach bot API server.', timestamp: Date.now() },
      ])
    }
    setLoading(false)
  }

  const handleFormSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    handleSendQuery(input)
  }

  return (
    <div className="card flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden shadow-2xl">
      {/* Header */}
      <div className="card-header flex flex-wrap justify-between items-center px-4 py-3 border-b border-[#1f2335] bg-[#0e1015]">
        <div className="flex items-center gap-2">
          <span className="text-xl" aria-hidden="true">💡</span>
          <div>
            <span className="card-title text-sm font-bold text-[#dde1ed] tracking-wide block">
              Market Intelligence &amp; Quant Copilot
            </span>
            <span className="text-[10.5px] text-[#7e8aaa]">TF/IDF Semantic Search + 4-Member Calibrated ML Insights</span>
          </div>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="badge badge-purple text-[10px] font-bold">GenAI Hybrid Engine</span>
          <span className="badge badge-green text-[10px] font-bold">Online</span>
        </div>
      </div>

      {/* Quick Prompts Bar */}
      <div className="px-4 py-2 bg-[#0e1015] border-b border-[#1f2335] flex flex-wrap gap-1.5 overflow-x-auto scrollbar-thin">
        {QUICK_PROMPTS.map((prompt, i) => (
          <button
            key={i}
            onClick={() => handleSendQuery(prompt)}
            disabled={loading}
            className="text-[10.5px] bg-[#13161e] text-[#dde1ed] hover:text-cyan-300 border border-[#1f2335] hover:border-cyan-500/40 px-2.5 py-1 rounded-full transition-all whitespace-nowrap"
          >
            {prompt}
          </button>
        ))}
      </div>

      {/* Messages Feed */}
      <div className="flex-1 overflow-y-auto p-4 space-y-3.5 scrollbar-thin text-xs leading-relaxed">
        {messages.map((m, i) => {
          const isUser = m.role === 'user'
          return (
            <div key={i} className={`flex flex-col ${isUser ? 'items-end' : 'items-start'}`}>
              <div className="flex items-center gap-1.5 mb-1 px-1 text-[10px] text-[#7e8aaa]">
                <span>{isUser ? '👤 You' : '🤖 Copilot'}</span>
                {m.timestamp && (
                  <span>• {new Date(m.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</span>
                )}
              </div>

              <div
                className={`max-w-[88%] rounded-xl p-3.5 shadow-md ${
                  isUser
                    ? 'bg-gradient-to-r from-blue-600 to-cyan-600 text-white rounded-br-none'
                    : 'bg-[#0e1015] text-[#dde1ed] border border-[#1f2335] rounded-bl-none'
                }`}
              >
                <div className="whitespace-pre-line text-xs font-normal leading-relaxed">{m.content}</div>

                {/* Semantic Matched Market Pills */}
                {m.matched_markets && m.matched_markets.length > 0 && (
                  <div className="mt-3 pt-2.5 border-t border-[#1f2335] flex flex-col gap-1.5">
                    <span className="text-[10px] text-[#7e8aaa] font-semibold uppercase tracking-wider">
                      Matched Contracts (Click to Inspect):
                    </span>
                    <div className="flex flex-wrap gap-1.5">
                      {m.matched_markets.map((mkt) => (
                        <button
                          key={mkt.token_id}
                          onClick={() => onSelectMarket?.({ tokenId: mkt.token_id, slug: mkt.slug })}
                          className="text-[10px] bg-[#13161e] text-cyan-300 hover:text-white border border-[#1f2335] hover:border-cyan-500 px-2.5 py-1 rounded-md mono transition-all flex items-center gap-1.5"
                        >
                          <span className="truncate max-w-[180px]">{mkt.title || mkt.slug}</span>
                          {mkt.mid_price !== undefined && (
                            <span className="text-amber-400 font-bold">{(mkt.mid_price * 100).toFixed(0)}¢</span>
                          )}
                          <span className="text-[9px] text-[#7e8aaa]">({(mkt.similarity * 100).toFixed(0)}%)</span>
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </div>
          )
        })}

        {loading && (
          <div className="flex items-center gap-2 text-xs text-cyan-400 bg-[#0e1015] p-3 rounded-lg border border-[#1f2335] w-fit shadow-md animate-pulse">
            <span className="spinner mr-1" aria-hidden="true" />
            Analyzing 38-feature vectors &amp; semantic index…
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Input Box */}
      <form onSubmit={handleFormSubmit} className="p-3 border-t border-[#1f2335] bg-[#0e1015] flex gap-2">
        <input
          type="text"
          placeholder="Ask Copilot about any market contract, probability edge, or strategy rule..."
          value={input}
          onChange={(e) => setInput(e.target.value)}
          className="w-full bg-[#13161e] border border-[#1f2335] focus:border-cyan-500/50 rounded-lg text-xs px-3 py-2 text-[#dde1ed] placeholder-[#3e4560] outline-none transition-all"
          aria-label="Ask copilot message"
        />
        <button
          type="submit"
          disabled={loading || !input.trim()}
          className="btn btn-primary btn-sm px-4 font-bold shadow-md hover:shadow-cyan-500/20"
        >
          Send
        </button>
      </form>
    </div>
  )
}
