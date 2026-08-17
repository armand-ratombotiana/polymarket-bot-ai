// lib/api.ts — Central API/WebSocket URL resolution for the bot backend.

/**
 * Base HTTP URL of the bot API, without a trailing slash.
 * Prefers the build-time NEXT_PUBLIC_API_URL; falls back to the page's
 * own origin so the UI works when served on any host/port.
 */
export function getApiUrl(): string {
  const fromEnv = process.env.NEXT_PUBLIC_API_URL
  if (fromEnv) {
    return fromEnv.replace(/\/+$/, '')
  }
  if (typeof window !== 'undefined') {
    return `${window.location.protocol}//${window.location.host}`
  }
  return 'http://localhost:8087'
}

/**
 * WebSocket URL of the bot's live stream (/ws endpoint).
 * Prefers the build-time NEXT_PUBLIC_WS_URL; otherwise derives
 * the ws:// equivalent of getApiUrl() with a /ws path.
 */
export function getWsUrl(): string {
  const fromEnv = process.env.NEXT_PUBLIC_WS_URL
  if (fromEnv) {
    return fromEnv.endsWith('/ws') ? fromEnv : `${fromEnv.replace(/\/+$/, '')}/ws`
  }
  return `${getApiUrl().replace(/^http/, 'ws')}/ws`
}

/**
 * API bearer token for authenticated endpoints.
 * Preference: localStorage override (settable at runtime) → NEXT_PUBLIC_API_TOKEN.
 * Empty string means the server is running without auth (or will reject 503).
 */
export function getApiToken(): string {
  if (typeof window !== 'undefined') {
    const stored = window.localStorage.getItem('polymarket_api_token')
    if (stored) return stored
  }
  return process.env.NEXT_PUBLIC_API_TOKEN ?? ''
}

/**
 * Headers carrying the bearer token (API_TOKEN must be set server-side).
 */
export function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { ...(extra ?? {}) }
  const token = getApiToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  return headers
}

/**
 * WS URL with the token appended as a query param (WS headers are not usable).
 */
export function getAuthedWsUrl(): string {
  const base = getWsUrl()
  const token = getApiToken()
  if (!token) return base
  return `${base}${base.includes('?') ? '&' : '?'}token=${encodeURIComponent(token)}`
}

/**
 * fetch() wrapper that injects the bearer token on every request.
 * Use this everywhere instead of bare fetch so the UI works when
 * API_TOKEN is enforced server-side.
 */
export async function apiFetch(input: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers)
  const token = getApiToken()
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`)
  }
  return fetch(input, { ...init, headers })
}
