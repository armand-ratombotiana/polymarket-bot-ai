import '@testing-library/jest-dom/vitest'
import { vi, afterEach } from 'vitest'

global.fetch = vi.fn()

afterEach(() => {
  vi.restoreAllMocks()
})

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  configurable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }),
})

class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
;(globalThis as any).ResizeObserver = MockResizeObserver

if (typeof Element !== 'undefined' && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function scrollIntoView() {}
}

const originalError = console.error
console.error = (...args: unknown[]) => {
  const first = args[0]
  if (typeof first === 'string' && first.includes('not wrapped in act')) return
  originalError(...args)
}
