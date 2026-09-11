// components/AuditLogPanel.test.tsx — Audit log viewer rendering, filters, export & expansion.
//
// Strategy:
//   • Mock window.fetch to return a canned `/api/audit/logs` payload so we
//     can deterministically exercise the table rendering, severity
//     inference, filter bar, row expansion, and export buttons without a
//     live backend.
//   • Use `@testing-library/user-event` (async) for the filter <select> and
//     search <input> interactions so we exercise the same code paths the
//     user does (React state updates → re-filter → re-render).
//   • Verify the inferred severity mapping (INFO/WARNING/ERROR/CRITICAL)
//     by constructing audit rows whose `event_type` keyword uniquely maps
//     to each severity tier.
//   • The download path is exercised via a stubbed URL.createObjectURL +
//     anchor.click so the export functions run end-to-end without
//     actually writing to disk.

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import AuditLogPanel from './AuditLogPanel'

// ── Sample audit payload ──────────────────────────────────────────────────
// Build a set of audit rows that span every severity tier (inferred) and
// a mix of categories. Timestamps are epoch-seconds relative to "now" so
// the fmtAge() freshness formatting doesn't drift.

const NOW = Math.floor(Date.now() / 1000)

const sampleLogs = [
  {
    id: 1,
    timestamp: NOW - 5, // 5s ago
    category: 'system',
    event_type: 'mode_change',
    token_id: null,
    slug: null,
    details: 'mode=paper paper_trade=true live_trading_enabled=false',
    pnl: 0,
    strategy: null,
    idempotency_key: 'system_mode_change_1',
  },
  {
    id: 2,
    timestamp: NOW - 30,
    category: 'security',
    event_type: 'auth_failure',
    token_id: null,
    slug: null,
    details: 'mode=invalid ip=10.0.0.1 path=/api/positions method=GET',
    pnl: 0,
    strategy: null,
    idempotency_key: 'security_auth_failure_2',
  },
  {
    id: 3,
    timestamp: NOW - 60,
    category: 'security',
    event_type: 'weak_token_warning',
    token_id: null,
    slug: null,
    details: 'reason=placeholder mode=startup',
    pnl: 0,
    strategy: null,
    idempotency_key: 'security_weak_token_warning_3',
  },
  {
    id: 4,
    timestamp: NOW - 120,
    category: 'trading',
    event_type: 'position_close',
    token_id: '0xabc123',
    slug: 'will-btc-hit-100k',
    details:
      '{"side":"BUY","size":10,"close_price":0.42,"realized_pnl":2.5,"slug":"will-btc-hit-100k"}',
    pnl: 2.5,
    strategy: 'mm_avellaneda_stoikov',
    idempotency_key: 'trading_position_close_4',
  },
  {
    id: 5,
    timestamp: NOW - 240,
    category: 'trading',
    event_type: 'order_error',
    token_id: '0xdef456',
    slug: 'will-eth-flip',
    details: 'error=insufficient_balance reason=cap_exceeded',
    pnl: 0,
    strategy: 'arb_binary_dutch_book',
    idempotency_key: 'trading_order_error_5',
  },
  {
    id: 6,
    timestamp: NOW - 300,
    category: 'ml',
    event_type: 'critical_model_drift',
    token_id: null,
    slug: null,
    details: 'drift=0.34 threshold=0.20',
    pnl: 0,
    strategy: null,
    idempotency_key: 'ml_critical_model_drift_6',
  },
]

function mockFetchOk(payload: unknown) {
  return vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    text: async () => '',
    json: async () => payload,
  } as unknown as Response)
}

function mockFetchError(status = 500) {
  return vi.fn().mockResolvedValue({
    ok: false,
    status,
    text: async () => 'Internal Server Error',
    json: async () => ({}),
  } as unknown as Response)
}

// Stub the download pathway so export functions run without touching disk.
// We stub `URL.createObjectURL` (jsdom doesn't define it natively) and
// `URL.revokeObjectURL`. We DON'T touch `document.createElement` /
// `document.body.appendChild` because the React Testing Library `render()`
// helper uses `document.createElement('div')` to build its container —
// mocking those breaks the render setup with "Target container is not a
// DOM element". The anchor's `click()` is a no-op in jsdom (no
// navigation), so the export function runs cleanly without further stubs.
function stubDownload() {
  const createObjectURL = vi.fn(() => 'blob:mock-url')
  const revokeObjectURL = vi.fn()
  try {
    Object.defineProperty(URL, 'createObjectURL', {
      value: createObjectURL,
      writable: true,
      configurable: true,
    })
    Object.defineProperty(URL, 'revokeObjectURL', {
      value: revokeObjectURL,
      writable: true,
      configurable: true,
    })
  } catch {
    ;(URL as unknown as Record<string, unknown>).createObjectURL = createObjectURL
    ;(URL as unknown as Record<string, unknown>).revokeObjectURL = revokeObjectURL
  }
  return { createObjectURL, revokeObjectURL }
}

describe('AuditLogPanel', () => {
  beforeEach(() => {
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  // ── Loading / error states ────────────────────────────────────────────

  it('renders the loading skeleton on first mount before data arrives', () => {
    vi.mocked(fetch).mockImplementation(
      () => new Promise<Response>(() => {}),
    )
    render(<AuditLogPanel />)
    expect(screen.getByText('📋 AUDIT LOG')).toBeInTheDocument()
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('renders the error state with a Retry button when fetch returns not-ok', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchError(500))
    render(<AuditLogPanel />)
    await waitFor(() => {
      expect(screen.getByText('Audit trail unavailable')).toBeInTheDocument()
    })
    expect(screen.getByText('Retry')).toBeInTheDocument()
  })

  it('renders the error state when fetch throws', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('Network error'))
    render(<AuditLogPanel />)
    await waitFor(() => {
      expect(screen.getByText('Audit trail unavailable')).toBeInTheDocument()
    })
    expect(screen.getByText(/Network error/)).toBeInTheDocument()
  })

  // ── Step 3 spec: renders the table ───────────────────────────────────

  it('renders the audit log table after data loads', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk({ logs: sampleLogs, count: sampleLogs.length }))
    render(<AuditLogPanel />)
    await waitFor(() => {
      expect(screen.getByText('📋 AUDIT LOG')).toBeInTheDocument()
    })
    // Table header row
    expect(screen.getByText('Timestamp')).toBeInTheDocument()
    expect(screen.getByText('Category')).toBeInTheDocument()
    expect(screen.getByText('Event Type')).toBeInTheDocument()
    expect(screen.getByText('Severity')).toBeInTheDocument()
    expect(screen.getByText('Message')).toBeInTheDocument()
    // First event_type renders in a row
    expect(screen.getByText('mode_change')).toBeInTheDocument()
    // The endpoint was called with the expected URL.
    const firstCallUrl = (vi.mocked(fetch).mock.calls[0] as [string, unknown])[0] as string
    expect(firstCallUrl).toContain('/api/audit/logs')
    expect(firstCallUrl).toContain('limit=100')
    expect(firstCallUrl).toContain('XTransformPort=8080')
  })

  it('renders the empty state when the audit trail has zero events', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk({ logs: [], count: 0 }))
    render(<AuditLogPanel />)
    await waitFor(() => {
      expect(
        screen.getByText('No audit events match your filters'),
      ).toBeInTheDocument()
    })
  })

  // ── Severity inference ────────────────────────────────────────────────

  it('infers severity from event_type + details and renders all four tiers', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchOk({ logs: sampleLogs, count: sampleLogs.length }),
    )
    render(<AuditLogPanel />)
    await waitFor(() => {
      // INFO badge — system mode_change + trading position_close
      expect(screen.getAllByText('INFO').length).toBeGreaterThanOrEqual(1)
    })
    // WARN badge — weak_token_warning (auth_failure matches 'fail' → ERROR, not WARN)
    expect(screen.getAllByText('WARN').length).toBeGreaterThanOrEqual(1)
    // ERROR badge — order_error + auth_failure (both match 'fail' / 'error')
    expect(screen.getAllByText('ERROR').length).toBeGreaterThanOrEqual(2)
    // CRIT badge — critical_model_drift
    expect(screen.getAllByText('CRIT').length).toBeGreaterThanOrEqual(1)
  })

  // ── Step 3 spec: filtering by severity ────────────────────────────────

  it('filters rows by severity when the severity <select> changes', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchOk({ logs: sampleLogs, count: sampleLogs.length }),
    )
    const user = userEvent.setup()
    render(<AuditLogPanel />)
    await waitFor(() => {
      expect(screen.getByText('mode_change')).toBeInTheDocument()
    })

    // Before filtering — all 6 rows are present.
    expect(screen.getByText('mode_change')).toBeInTheDocument()
    expect(screen.getByText('order_error')).toBeInTheDocument()
    expect(screen.getByText('critical_model_drift')).toBeInTheDocument()

    // Select "ERROR" severity.
    const severitySelect = screen.getByLabelText('Filter by severity')
    await user.selectOptions(severitySelect, 'ERROR')

    // Both ERROR-severity rows should remain: auth_failure (matches 'fail')
    // and order_error (matches 'error'). INFO/WARNING/CRITICAL rows hidden.
    expect(screen.getByText('order_error')).toBeInTheDocument()
    expect(screen.getByText('auth_failure')).toBeInTheDocument()
    expect(screen.queryByText('mode_change')).not.toBeInTheDocument()
    expect(screen.queryByText('critical_model_drift')).not.toBeInTheDocument()
    expect(screen.queryByText('weak_token_warning')).not.toBeInTheDocument()
    expect(screen.queryByText('position_close')).not.toBeInTheDocument()
  })

  it('filters rows by category when the category <select> changes', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchOk({ logs: sampleLogs, count: sampleLogs.length }),
    )
    const user = userEvent.setup()
    render(<AuditLogPanel />)
    await waitFor(() => {
      expect(screen.getByText('mode_change')).toBeInTheDocument()
    })

    const categorySelect = screen.getByLabelText('Filter by category')
    await user.selectOptions(categorySelect, 'trading')

    // Only trading rows remain: position_close + order_error
    expect(screen.getByText('position_close')).toBeInTheDocument()
    expect(screen.getByText('order_error')).toBeInTheDocument()
    // Non-trading rows hidden.
    expect(screen.queryByText('mode_change')).not.toBeInTheDocument()
    expect(screen.queryByText('auth_failure')).not.toBeInTheDocument()
    expect(screen.queryByText('critical_model_drift')).not.toBeInTheDocument()
  })

  // ── Step 3 spec: text search ──────────────────────────────────────────

  it('filters rows by text search across event_type + details + slug', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchOk({ logs: sampleLogs, count: sampleLogs.length }),
    )
    const user = userEvent.setup()
    render(<AuditLogPanel />)
    await waitFor(() => {
      expect(screen.getByText('mode_change')).toBeInTheDocument()
    })

    const search = screen.getByLabelText('Search audit events')
    await user.type(search, 'auth_failure')

    // Only auth_failure row remains (matches event_type).
    expect(screen.getByText('auth_failure')).toBeInTheDocument()
    expect(screen.queryByText('mode_change')).not.toBeInTheDocument()
    expect(screen.queryByText('order_error')).not.toBeInTheDocument()

    // Clear search → all rows return.
    await user.clear(search)
    expect(screen.getByText('mode_change')).toBeInTheDocument()
    expect(screen.getByText('auth_failure')).toBeInTheDocument()
  })

  it('text search matches details substrings (e.g. "insufficient_balance")', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchOk({ logs: sampleLogs, count: sampleLogs.length }),
    )
    const user = userEvent.setup()
    render(<AuditLogPanel />)
    await waitFor(() => {
      expect(screen.getByText('order_error')).toBeInTheDocument()
    })

    const search = screen.getByLabelText('Search audit events')
    await user.type(search, 'insufficient_balance')

    expect(screen.getByText('order_error')).toBeInTheDocument()
    expect(screen.queryByText('mode_change')).not.toBeInTheDocument()
    expect(screen.queryByText('position_close')).not.toBeInTheDocument()
  })

  it('renders the "Clear" button only when a filter is active', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchOk({ logs: sampleLogs, count: sampleLogs.length }),
    )
    const user = userEvent.setup()
    render(<AuditLogPanel />)
    await waitFor(() => {
      expect(screen.getByText('mode_change')).toBeInTheDocument()
    })

    expect(screen.queryByText('Clear')).not.toBeInTheDocument()

    const search = screen.getByLabelText('Search audit events')
    await user.type(search, 'mode_change')
    expect(screen.getByText('Clear')).toBeInTheDocument()
  })

  // ── Step 3 spec: export buttons exist ─────────────────────────────────

  it('renders CSV and JSON export buttons', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchOk({ logs: sampleLogs, count: sampleLogs.length }),
    )
    render(<AuditLogPanel />)
    await waitFor(() => {
      expect(screen.getByText('mode_change')).toBeInTheDocument()
    })
    expect(screen.getByLabelText('Export CSV')).toBeInTheDocument()
    expect(screen.getByLabelText('Export JSON')).toBeInTheDocument()
  })

  it('disables export buttons when no rows match the active filter', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchOk({ logs: sampleLogs, count: sampleLogs.length }),
    )
    const user = userEvent.setup()
    render(<AuditLogPanel />)
    await waitFor(() => {
      expect(screen.getByText('mode_change')).toBeInTheDocument()
    })

    const search = screen.getByLabelText('Search audit events')
    // Type a query that matches nothing.
    await user.type(search, 'zzz_no_match_zzz')

    const csvBtn = screen.getByLabelText('Export CSV') as HTMLButtonElement
    const jsonBtn = screen.getByLabelText('Export JSON') as HTMLButtonElement
    expect(csvBtn.disabled).toBe(true)
    expect(jsonBtn.disabled).toBe(true)
  })

  it('CSV export triggers a Blob download with .csv filename', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchOk({ logs: sampleLogs, count: sampleLogs.length }),
    )
    const { createObjectURL } = stubDownload()
    const user = userEvent.setup()
    render(<AuditLogPanel />)
    await waitFor(() => {
      expect(screen.getByText('mode_change')).toBeInTheDocument()
    })

    await user.click(screen.getByLabelText('Export CSV'))

    // URL.createObjectURL was called with a Blob whose MIME is text/csv.
    await waitFor(() => {
      expect(createObjectURL).toHaveBeenCalledTimes(1)
    })
    const blob = (createObjectURL.mock.calls[0] as unknown[])[0] as unknown as Blob
    expect(blob.type).toBe('text/csv')
    const text = await blob.text()
    // CSV header row + at least one event_type from the sample.
    expect(text).toContain('id,timestamp,datetime_utc,category,event_type,severity')
    expect(text).toContain('mode_change')
  })

  it('JSON export triggers a Blob download with .json filename', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchOk({ logs: sampleLogs, count: sampleLogs.length }),
    )
    const { createObjectURL } = stubDownload()
    const user = userEvent.setup()
    render(<AuditLogPanel />)
    await waitFor(() => {
      expect(screen.getByText('mode_change')).toBeInTheDocument()
    })

    await user.click(screen.getByLabelText('Export JSON'))

    // URL.createObjectURL was called with a JSON Blob.
    await waitFor(() => {
      expect(createObjectURL).toHaveBeenCalledTimes(1)
    })
    const blob = (createObjectURL.mock.calls[0] as unknown[])[0] as unknown as Blob
    expect(blob.type).toBe('application/json')
    const text = await blob.text()
    // JSON payload should be parseable + contain a severity field.
    const parsed = JSON.parse(text) as Array<{ event_type: string; severity: string }>
    expect(parsed.length).toBe(sampleLogs.length)
    expect(parsed[0].event_type).toBe('mode_change')
    expect(parsed[0].severity).toBe('INFO')
  })

  // ── Step 3 spec: row expansion ─────────────────────────────────────────

  it('expands a row on click to reveal the metadata JSON payload', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchOk({ logs: sampleLogs, count: sampleLogs.length }),
    )
    const user = userEvent.setup()
    render(<AuditLogPanel />)
    await waitFor(() => {
      expect(screen.getByText('position_close')).toBeInTheDocument()
    })

    // The metadata <pre> shouldn't be in the document before expansion.
    expect(screen.queryByLabelText('Audit event metadata JSON')).not.toBeInTheDocument()

    // Click the row containing position_close.
    // W16-6 — VirtualTable renders rows as <div role="row"> (not <tr>),
    // so the selector was updated to match the new DOM. The behavior
    // under test (click → expand → see metadata JSON) is unchanged.
    const row = screen.getByText('position_close').closest('[role="row"]')
    expect(row).not.toBeNull()
    await user.click(row as HTMLElement)

    // The expanded metadata JSON <pre> should now be in the document.
    const pre = await screen.findByLabelText('Audit event metadata JSON')
    expect(pre).toBeInTheDocument()
    // The parsed JSON details should be present (side, size, close_price, etc.).
    expect(pre.textContent).toContain('side')
    expect(pre.textContent).toContain('BUY')
    expect(pre.textContent).toContain('close_price')
  })

  it('collapses an expanded row on a second click', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchOk({ logs: sampleLogs, count: sampleLogs.length }),
    )
    const user = userEvent.setup()
    render(<AuditLogPanel />)
    await waitFor(() => {
      expect(screen.getByText('position_close')).toBeInTheDocument()
    })

    // W16-6 — selector updated for VirtualTable's <div role="row"> rows.
    const row = screen.getByText('position_close').closest('[role="row"]')
    await user.click(row as HTMLElement)
    expect(screen.getByLabelText('Audit event metadata JSON')).toBeInTheDocument()

    // Click again — collapse. We re-query the row because react-window
    // may replace the underlying DOM node when the AuditLogPanel
    // re-renders (e.g., the chevron rotation in the timestamp cell
    // changes the rendered output of the row).
    const rowAfterExpand = screen
      .getByText('position_close')
      .closest('[role="row"]')
    await user.click(rowAfterExpand as HTMLElement)
    expect(screen.queryByLabelText('Audit event metadata JSON')).not.toBeInTheDocument()
  })

  it('renders the metadata id / strategy / token_id fields in the expanded view', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchOk({ logs: sampleLogs, count: sampleLogs.length }),
    )
    const user = userEvent.setup()
    render(<AuditLogPanel />)
    await waitFor(() => {
      expect(screen.getByText('position_close')).toBeInTheDocument()
    })

    // W16-6 — selector updated for VirtualTable's <div role="row"> rows.
    const row = screen.getByText('position_close').closest('[role="row"]')
    await user.click(row as HTMLElement)

    // Strategy / token_id / id fields rendered in the metadata block.
    expect(screen.getByText('id:')).toBeInTheDocument()
    expect(screen.getByText('strategy:')).toBeInTheDocument()
    expect(screen.getByText('token_id:')).toBeInTheDocument()
    // The strategy value is rendered in a sibling span.
    expect(screen.getByText('mm_avellaneda_stoikov')).toBeInTheDocument()
  })

  // ── Stats header ──────────────────────────────────────────────────────

  it('renders the stats header with total, error, and warning counts', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchOk({ logs: sampleLogs, count: sampleLogs.length }),
    )
    render(<AuditLogPanel />)
    // Wait for a row (only present in loaded state) — the title "📋 AUDIT LOG"
    // also appears in the loading skeleton, so we can't use it as the gate.
    await waitFor(() => {
      expect(screen.getByText('mode_change')).toBeInTheDocument()
    })

    // Total events = 6 (sampleLogs.length).
    expect(screen.getByText('6')).toBeInTheDocument()
    // StatChip labels — uppercase label + ':' rendered in a single span.
    expect(screen.getByText('Events:')).toBeInTheDocument()
    expect(screen.getByText('Errors:')).toBeInTheDocument()
    expect(screen.getByText('Warnings:')).toBeInTheDocument()
    expect(screen.getByText('Latest:')).toBeInTheDocument()
    // Criticals = 1 → the conditional Critical StatChip renders.
    expect(screen.getByText('Critical:')).toBeInTheDocument()
  })

  // ── Polling cleanup ───────────────────────────────────────────────────

  it('stops the polling interval on unmount (no leaked setState warnings)', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchOk({ logs: sampleLogs, count: sampleLogs.length }),
    )
    const { unmount } = render(<AuditLogPanel />)
    await waitFor(() => {
      expect(screen.getByText('📋 AUDIT LOG')).toBeInTheDocument()
    })
    expect(() => act(() => unmount())).not.toThrow()
  })
})
