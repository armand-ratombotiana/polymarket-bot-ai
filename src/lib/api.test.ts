// lib/api.test.ts — Unit tests for the gateway/auth utilities.
import { describe, it, expect, beforeEach, vi } from 'vitest'
import {
  apiFetch,
  authHeaders,
  getApiToken,
  getWsUrl,
  getAuthedWsUrl,
  getApiUrl,
} from '@/lib/api'

const DEFAULT_TOKEN = 'I76FCamSbBw0e1r_V0RRX81uG-3DUCS_pofbYNC-RgHO3x9b3DIovCPe01iDREBT'

describe('api utilities', () => {
  beforeEach(() => {
    // Re-install a fresh vi.fn for fetch on every test so call history + return
    // values are isolated. (api.ts already installed its wrapper once at module
    // load; replacing window.fetch here bypasses the wrapper, but apiFetch calls
    // withGatewayPort itself, so behaviour is unchanged.)
    global.fetch = vi.fn() as unknown as typeof fetch
    localStorage.clear()
  })

  describe('getApiToken', () => {
    it('returns the default token when localStorage is empty', () => {
      expect(getApiToken()).toBe(DEFAULT_TOKEN)
    })

    it('returns the stored token when localStorage has one', () => {
      localStorage.setItem('polymarket_api_token', 'my-custom-token')
      expect(getApiToken()).toBe('my-custom-token')
    })

    it('returns the default again after localStorage is cleared', () => {
      localStorage.setItem('polymarket_api_token', 'temp')
      expect(getApiToken()).toBe('temp')
      localStorage.clear()
      expect(getApiToken()).toBe(DEFAULT_TOKEN)
    })
  })

  describe('authHeaders', () => {
    it('includes a Bearer token derived from getApiToken', () => {
      localStorage.setItem('polymarket_api_token', 'abc123')
      const h = authHeaders()
      expect(h['Authorization']).toBe('Bearer abc123')
    })

    it('uses the default token when no override is provided', () => {
      const h = authHeaders()
      expect(h['Authorization']).toBe(`Bearer ${DEFAULT_TOKEN}`)
    })

    it('merges extra headers passed in', () => {
      localStorage.setItem('polymarket_api_token', 'abc123')
      const h = authHeaders({ 'Content-Type': 'application/json' })
      expect(h['Content-Type']).toBe('application/json')
      expect(h['Authorization']).toBe('Bearer abc123')
    })

    it('returns a fresh object each call (no shared mutable state)', () => {
      const a = authHeaders({ 'X-Custom': '1' })
      const b = authHeaders()
      expect(a).not.toBe(b)
      expect(b['X-Custom']).toBeUndefined()
    })
  })

  describe('apiFetch — gateway port injection', () => {
    it('appends XTransformPort=8080 to /api/* URLs', async () => {
      vi.mocked(fetch).mockResolvedValue(new Response('{}', { status: 200 }))
      await apiFetch('/api/foo')
      const [url] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/foo')
      expect(url).toContain('XTransformPort=8080')
    })

    it('appends with & when the URL already has a query string', async () => {
      vi.mocked(fetch).mockResolvedValue(new Response('{}', { status: 200 }))
      await apiFetch('/api/foo?bar=1')
      const [url] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('?bar=1&XTransformPort=8080')
    })

    it('does NOT modify absolute http:// URLs', async () => {
      vi.mocked(fetch).mockResolvedValue(new Response('{}', { status: 200 }))
      await apiFetch('http://example.com/api/foo')
      const [url] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toBe('http://example.com/api/foo')
      expect(url).not.toContain('XTransformPort')
    })

    it('does NOT modify absolute https:// URLs', async () => {
      vi.mocked(fetch).mockResolvedValue(new Response('{}', { status: 200 }))
      await apiFetch('https://example.com/api/foo')
      const [url] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toBe('https://example.com/api/foo')
      expect(url).not.toContain('XTransformPort')
    })

    it('does NOT modify /api/bot/* URLs (handled by Next.js)', async () => {
      vi.mocked(fetch).mockResolvedValue(new Response('{}', { status: 200 }))
      await apiFetch('/api/bot/something')
      const [url] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toBe('/api/bot/something')
      expect(url).not.toContain('XTransformPort')
    })

    it('does NOT modify non-API URLs', async () => {
      vi.mocked(fetch).mockResolvedValue(new Response('{}', { status: 200 }))
      await apiFetch('/static/asset.png')
      const [url] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toBe('/static/asset.png')
    })
  })

  describe('apiFetch — auth header injection', () => {
    it('injects an Authorization header derived from the stored token', async () => {
      vi.mocked(fetch).mockResolvedValue(new Response('{}', { status: 200 }))
      localStorage.setItem('polymarket_api_token', 'tok-xyz')
      await apiFetch('/api/foo')
      const [, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      const headers = new Headers(init.headers)
      expect(headers.get('Authorization')).toBe('Bearer tok-xyz')
    })

    it('uses the default token when no override is set', async () => {
      vi.mocked(fetch).mockResolvedValue(new Response('{}', { status: 200 }))
      await apiFetch('/api/foo')
      const [, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      const headers = new Headers(init.headers)
      expect(headers.get('Authorization')).toBe(`Bearer ${DEFAULT_TOKEN}`)
    })

    it('preserves a caller-provided Authorization header', async () => {
      vi.mocked(fetch).mockResolvedValue(new Response('{}', { status: 200 }))
      localStorage.setItem('polymarket_api_token', 'tok-xyz')
      await apiFetch('/api/foo', {
        headers: { Authorization: 'Bearer custom' },
      })
      const [, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      const headers = new Headers(init.headers)
      expect(headers.get('Authorization')).toBe('Bearer custom')
    })

    it('preserves other caller-provided headers', async () => {
      vi.mocked(fetch).mockResolvedValue(new Response('{}', { status: 200 }))
      await apiFetch('/api/foo', {
        headers: { 'Content-Type': 'application/json' },
      })
      const [, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      const headers = new Headers(init.headers)
      expect(headers.get('Content-Type')).toBe('application/json')
      // And the auth header is also injected
      expect(headers.get('Authorization')).toBe(`Bearer ${DEFAULT_TOKEN}`)
    })
  })

  describe('getWsUrl / getAuthedWsUrl', () => {
    it('getApiUrl returns an empty string (relative-path mode)', () => {
      expect(getApiUrl()).toBe('')
    })

    it('getWsUrl includes /ws and XTransformPort=8080', () => {
      const url = getWsUrl()
      expect(url).toContain('/ws')
      expect(url).toContain('XTransformPort=8080')
    })

    it('getAuthedWsUrl includes the token from localStorage', () => {
      localStorage.setItem('polymarket_api_token', 'ws-token-123')
      const url = getAuthedWsUrl()
      expect(url).toContain('token=ws-token-123')
      expect(url).toContain('XTransformPort=8080')
    })

    it('getAuthedWsUrl URL-encodes the token', () => {
      localStorage.setItem('polymarket_api_token', 'tok with spaces')
      const url = getAuthedWsUrl()
      expect(url).toContain('token=tok%20with%20spaces')
    })
  })
})
