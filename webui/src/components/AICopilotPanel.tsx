// components/AICopilotPanel.tsx — Market Intelligence & Copilot Workspace
'use client'

import { useState } from 'react'
import { getApiUrl, apiFetch } from '@/lib/api'

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
        '👋 Welcome to the **Polymarket Copilot**. I use rule-based heuristics and TF-IDF semantic matching to explore order books, strategy rules, and market probabilities. Ask about any active market slug or quant topic.',
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
      const res = await apiFetch(`${apiUrl}/api/ai/copilot`, {
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
    <div className="card flex flex-col h-full bg-[#13161e] border border-[#1f2335] overflow-hidden">
      {/* Header */}
      <div className="card-header flex justify-between items-center px-4 py-3 border-b border-[#1f2335]">
        <div className="flex items-center gap-2">
          <span className="text-base" aria-hidden="true">💡</span>
          <span className="card-title text-sm font-bold text-[#dde1ed]">
            Market Intelligence Copilot
          </span>
        </div>
        <span className="badge badge-purple text-[9.5px]">Heuristic &amp; Template Assistant</span>
      </div>

      {/* Experimental Notice */}
      <div className="banner-experimental text-[11px] mx-4 mt-2 py-1.5 px-3" role="note">
        <span aria-hidden="true">ℹ️</span>
        <span>
          <strong>TEMPLATE-BASED:</strong> Copilot answers are generated from structured heuristics and in-memory TF-IDF index. Not validated financial advice.
        </span>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4 space-y-3 scrollbar-thin text-xs leading-relaxed">
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
                  : 'bg-[#0e1015] text-[#dde1ed] border border-[#1f2335] rounded-bl-none'
              }`}
            >
              <div className="whitespace-pre-line">{m.content}</div>

              {/* Matched semantic markets pills */}
              {m.matched_markets && m.matched_markets.length > 0 && (
                <div className="mt-2.5 pt-2 border-t border-[#1f2335] flex flex-wrap gap-1.5">
                  <span className="text-[10px] text-[#7e8aaa] block w-full">
                    Matched Markets (Lexical TF-IDF):
                  </span>
                  {m.matched_markets.map((mkt) => (
                    <span
                      key={mkt.token_id}
                      className="text-[10px] bg-[#13161e] text-cyan-400 border border-[#1f2335] px-2 py-0.5 rounded mono"
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
          <div className="flex items-center gap-2 text-xs text-blue-400 bg-[#0e1015] p-2.5 rounded-lg border border-[#1f2335] w-fit">
            <span className="spinner mr-1" aria-hidden="true" />
            Scanning market index…
          </div>
        )}
      </div>

      {/* Input Form */}
      <form onSubmit={handleSend} className="p-3 border-t border-[#1f2335] bg-[#0e1015] flex gap-2">
        <input
          type="text"
          placeholder="Ask about an active market, strategy logic, or probability..."
          value={input}
          onChange={(e) => setInput(e.target.value)}
          className="input flex-1 text-xs"
          aria-label="Ask copilot message"
        />
        <button
          type="submit"
          disabled={loading || !input.trim()}
          className="btn btn-primary btn-sm"
        >
          Send
        </button>
      </form>
    </div>
  )
}
