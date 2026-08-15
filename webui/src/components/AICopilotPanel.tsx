// components/AICopilotPanel.tsx — GenAI Market Intelligence & Copilot Workspace
'use client'

import { useState } from 'react'
import { getApiUrl } from '@/lib/api'

interface Message {
  role: 'user' | 'assistant'
  content: string
  matched_markets?: Array<{ token_id: string; title: string; slug: string; similarity: number }>
}

export default function AICopilotPanel() {
  const [messages, setMessages] = useState<Message[]>([
    {
      role: 'assistant',
      content:
        '👋 Welcome to the **Polymarket AI Copilot**! I analyze order books, calculate win probabilities with our calibrated ML ensemble, and identify multi-market arbitrage dislocations. What market would you like to analyze?',
    },
  ])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)

  const handleSend = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!input.trim() || loading) return

    const userText = input.trim()
    setInput('')
    setMessages((prev) => [...prev, { role: 'user', content: userText }])
    setLoading(true)

    try {
      const apiUrl = getApiUrl()
      const res = await fetch(`${apiUrl}/api/ai/copilot`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: userText }),
      })
      if (res.ok) {
        const data = await res.json()
        setMessages((prev) => [
          ...prev,
          {
            role: 'assistant',
            content: data.reply,
            matched_markets: data.matched_markets,
          },
        ])
      } else {
        setMessages((prev) => [
          ...prev,
          { role: 'assistant', content: '❌ Copilot engine error. Please try again.' },
        ])
      }
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', content: '❌ Could not reach bot API server.' },
      ])
    }
    setLoading(false)
  }

  return (
    <div className="card flex flex-col h-full bg-[#111318] border border-[#252836] overflow-hidden">
      {/* Header */}
      <div className="card-header flex justify-between items-center px-4 py-3 border-b border-[#252836]">
        <div className="flex items-center gap-2">
          <span className="text-base">🤖</span>
          <span className="card-title text-sm font-bold text-[#e8eaf0]">
            AI Trading Copilot &amp; Market Intelligence
          </span>
        </div>
        <span className="badge badge-blue text-[10px]">RAG Vector Search Active</span>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4 space-y-3.5 scrollbar-thin text-xs leading-relaxed">
        {messages.map((m, i) => (
          <div
            key={i}
            className={`flex flex-col ${
              m.role === 'user' ? 'items-end' : 'items-start'
            }`}
          >
            <div
              className={`max-w-[85%] rounded-lg p-3 ${
                m.role === 'user'
                  ? 'bg-blue-600 text-white rounded-br-none'
                  : 'bg-[#161822] text-[#e8eaf0] border border-[#252836] rounded-bl-none'
              }`}
            >
              <div className="whitespace-pre-line">{m.content}</div>

              {/* Matched semantic markets pills */}
              {m.matched_markets && m.matched_markets.length > 0 && (
                <div className="mt-2.5 pt-2 border-t border-[#252836] flex flex-wrap gap-1.5">
                  <span className="text-[10px] text-[#8b91a8] block w-full">
                    Semantic Market Matches:
                  </span>
                  {m.matched_markets.map((mkt) => (
                    <span
                      key={mkt.token_id}
                      className="text-[10px] bg-[#111318] text-cyan-400 border border-[#252836] px-2 py-0.5 rounded mono"
                    >
                      {mkt.slug} ({(mkt.similarity * 100).toFixed(0)}%)
                    </span>
                  ))}
                </div>
              )}
            </div>
          </div>
        ))}
        {loading && (
          <div className="flex items-center gap-2 text-xs text-blue-400 bg-[#161822] p-2.5 rounded-lg border border-[#252836] w-fit">
            <span className="status-dot bg-blue-400 animate-pulse" />
            Analyzing order books &amp; semantic index…
          </div>
        )}
      </div>

      {/* Input Form */}
      <form onSubmit={handleSend} className="p-3 border-t border-[#252836] bg-[#0e1015] flex gap-2">
        <input
          type="text"
          placeholder="Ask Copilot about any market, probability shift, or strategy idea…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          className="flex-1 bg-[#161822] border border-[#252836] rounded-md px-3.5 py-2 text-xs text-[#e8eaf0] placeholder-[#4a5068] focus:outline-none focus:border-blue-500"
        />
        <button
          type="submit"
          disabled={loading || !input.trim()}
          className="btn btn-primary px-4 py-2 text-xs font-semibold"
        >
          Send
        </button>
      </form>
    </div>
  )
}
