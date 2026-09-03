'use client'
const API_PORT = '8080'
export function getApiUrl(): string { return '' }
function withGatewayPort(input: string): string {
  if (!input || typeof input !== 'string') return input
  if (/^https?:\/\//i.test(input) || /^wss?:\/\//i.test(input)) return input
  if (!input.startsWith('/api') && !input.startsWith('api/')) return input
  if (input.startsWith('/api/bot') || input.startsWith('api/bot')) return input
  const sep = input.includes('?') ? '&' : '?'
  if (input.includes('XTransformPort=')) return input
  return `${input}${sep}XTransformPort=${API_PORT}`
}
let _fetchInstalled = false
function installFetchWrapper() {
  if (_fetchInstalled || typeof window === 'undefined') return
  const nativeFetch = window.fetch.bind(window)
  ;(nativeFetch as any).__nativeFetch = nativeFetch
  const wrapped: typeof fetch = async (input, init) => {
    let urlStr = ''
    if (typeof input === 'string') { urlStr = input; input = withGatewayPort(input) }
    else if (input instanceof Request) { urlStr = input.url; const r = withGatewayPort(input.url); if (r !== input.url) input = new Request(r, input) }
    try { return await nativeFetch(input as any, init) } catch (err) { throw err }
  }
  window.fetch = wrapped as typeof fetch
  _fetchInstalled = true
}
if (typeof window !== 'undefined') installFetchWrapper()
export function getWsUrl(): string {
  if (typeof window !== 'undefined') { const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'; return `${proto}://${window.location.host}/ws?XTransformPort=${API_PORT}` }
  return `ws://localhost:${API_PORT}/ws`
}
export function getApiToken(): string {
  if (typeof window !== 'undefined') { const s = window.localStorage.getItem('polymarket_api_token'); if (s) return s }
  return process.env.NEXT_PUBLIC_API_TOKEN ?? 'change_me_generate_a_strong_token'
}
export function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const h: Record<string, string> = { ...(extra ?? {}) }; const t = getApiToken(); if (t) h['Authorization'] = `Bearer ${t}`; return h
}
export function getAuthedWsUrl(): string {
  const base = getWsUrl(); const t = getApiToken(); if (!t) return base
  return `${base}${base.includes('?') ? '&' : '?'}token=${encodeURIComponent(t)}`
}
export async function apiFetch(input: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers); const t = getApiToken()
  if (t && !headers.has('Authorization')) headers.set('Authorization', `Bearer ${t}`)
  return fetch(withGatewayPort(input), { ...init, headers })
}
