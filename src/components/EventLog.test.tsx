// components/EventLog.test.tsx — Event Log panel tests (W22-2)
//
// Covers the contract surfaces required by the W22-2 spec for the
// previously-untested EventLog panel:
//   1. Renders without crashing.
//   2. Renders the "Live System Events" header.
//   3. Renders the events count badge.
//   4. Renders all passed events as rows.
//   5. Filters events by the search box.
//   6. Filters events by the fill filter (matches "fill" or "trade").
//   7. Filters events by the order filter (matches "order", "cancel", "quoted").
//   8. Filters events by the risk filter (matches "kill", "risk", "limit").
//   9. Filters events by the ml filter (matches "ml", "learned", "model").
//  10. Parses leading timestamps from event strings ([HH:MM:SS] and ISO formats).
//  11. Renders the severity icon based on the event content.
//  12. Fires the Copy button — copies events to clipboard.
//  13. Fires the CSV export button — generates a CSV download.
//  14. Renders the "No events match current filter" empty-state.
//  15. Renders gracefully with an empty events array.
//
// Note: EventLog does NOT fetch — events are passed as props. So no fetch
// mocks are required here. The Copy button uses navigator.clipboard.writeText,
// which we mock.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import EventLog from './EventLog'

// ── Sample payloads ─────────────────────────────────────────────────────────

const sampleEvents = [
  '[12:34:56] Order filled: BUY 100 tok_btc_100k_yes @ 0.42',
  '[12:35:10] ML model retrained: brier 0.1842 (was 0.2105)',
  '[12:35:25] Risk gate tripped: max exposure limit reached',
  '[12:35:42] Order cancelled: timeout 5s on tok_trump_2028_yes',
  '[12:36:00] Kill switch engaged: drawdown 8.5% breached',
  '[12:36:15] Trade closed: +1.42 USDC realized PnL',
  '[12:36:30] Quoted market: bid 0.41 ask 0.43 spread 0.02',
  '2024-01-15T12:36:45Z Random event with no recognized keyword',
]

// ── Tests ────────────────────────────────────────────────────────────────────

describe('EventLog', () => {
  beforeEach(() => {
    // Mock navigator.clipboard.writeText.
    Object.assign(navigator, {
      clipboard: {
        writeText: vi.fn().mockResolvedValue(undefined),
      },
    })
    // Mock URL.createObjectURL + anchor click for CSV export.
    global.URL.createObjectURL = vi.fn().mockReturnValue('blob:mock')
    global.URL.revokeObjectURL = vi.fn()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('renders without crashing', () => {
    const { container } = render(<EventLog events={[]} />)
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the "Live System Events" header', () => {
    render(<EventLog events={[]} />)
    expect(screen.getByText(/Live System Events/i)).toBeInTheDocument()
  })

  it('renders the events count badge (0 events)', () => {
    render(<EventLog events={[]} />)
    // The count "(0)" appears in the header.
    expect(screen.getByText('(0)')).toBeInTheDocument()
  })

  it('renders the events count badge with multiple events', () => {
    render(<EventLog events={sampleEvents} />)
    expect(screen.getByText('(8)')).toBeInTheDocument()
  })

  it('renders all passed events as rows', () => {
    render(<EventLog events={sampleEvents} />)
    expect(
      screen.getByText(/Order filled: BUY 100 tok_btc_100k_yes @ 0\.42/i),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/ML model retrained: brier 0\.1842/i),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/Risk gate tripped: max exposure limit reached/i),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/Kill switch engaged: drawdown 8\.5% breached/i),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/Trade closed: \+1\.42 USDC realized PnL/i),
    ).toBeInTheDocument()
  })

  it('filters events by the search box', () => {
    render(<EventLog events={sampleEvents} />)
    const input = screen.getByLabelText(/Filter events/i)
    fireEvent.change(input, { target: { value: 'kill' } })
    // Only the kill switch event should remain.
    expect(
      screen.getByText(/Kill switch engaged/i),
    ).toBeInTheDocument()
    expect(
      screen.queryByText(/Order filled/i),
    ).not.toBeInTheDocument()
    // Match count badge "1 match".
    expect(screen.getByText(/1 match/i)).toBeInTheDocument()
  })

  it('filters events by the "fill" filter (matches "fill" or "trade")', () => {
    render(<EventLog events={sampleEvents} />)
    fireEvent.click(screen.getByRole('button', { name: /^fill$/i }))
    // "Order filled" + "Trade closed" should remain.
    expect(
      screen.getByText(/Order filled/i),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/Trade closed/i),
    ).toBeInTheDocument()
    // ML event should be filtered out.
    expect(
      screen.queryByText(/ML model retrained/i),
    ).not.toBeInTheDocument()
  })

  it('filters events by the "order" filter (matches "order", "cancel", "quoted")', () => {
    render(<EventLog events={sampleEvents} />)
    fireEvent.click(screen.getByRole('button', { name: /^order$/i }))
    // "Order filled" (contains "order") + "Order cancelled" + "Quoted market" should remain.
    const filled = screen.getAllByText(/Order filled/i)
    expect(filled.length).toBeGreaterThanOrEqual(1)
    expect(
      screen.getByText(/Order cancelled/i),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/Quoted market/i),
    ).toBeInTheDocument()
    // Trade closed should NOT match the order filter (it contains "trade", not "order").
    expect(
      screen.queryByText(/Trade closed/i),
    ).not.toBeInTheDocument()
  })

  it('filters events by the "risk" filter (matches "kill", "risk", "limit")', () => {
    render(<EventLog events={sampleEvents} />)
    fireEvent.click(screen.getByRole('button', { name: /^risk$/i }))
    // "Risk gate tripped" + "Kill switch" should remain.
    expect(
      screen.getByText(/Risk gate tripped/i),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/Kill switch engaged/i),
    ).toBeInTheDocument()
    // ML event should be filtered out.
    expect(
      screen.queryByText(/ML model retrained/i),
    ).not.toBeInTheDocument()
  })

  it('filters events by the "ml" filter (matches "ml", "learned", "model")', () => {
    render(<EventLog events={sampleEvents} />)
    fireEvent.click(screen.getByRole('button', { name: /^ml$/i }))
    // Only "ML model retrained" should remain.
    expect(
      screen.getByText(/ML model retrained/i),
    ).toBeInTheDocument()
    // Risk event should be filtered out.
    expect(
      screen.queryByText(/Risk gate tripped/i),
    ).not.toBeInTheDocument()
  })

  it('parses the [HH:MM:SS] leading timestamp and renders it in the timestamp column', () => {
    render(<EventLog events={['[12:34:56] Order filled: BUY 100 tok_btc_100k_yes']} />)
    // The parsed timestamp "12:34:56" is rendered in the timestamp column
    // (mono text class).
    expect(screen.getByText('12:34:56')).toBeInTheDocument()
  })

  it('parses the ISO 8601 leading timestamp and renders the time portion', () => {
    render(<EventLog events={['2024-01-15T12:36:45Z Random event with no recognized keyword']} />)
    // The parsed timestamp is "12:36:45" (the time portion of the ISO 8601).
    expect(screen.getByText('12:36:45')).toBeInTheDocument()
  })

  it('renders the severity icon based on event content (✅ for fill, 🛑 for kill, 🤖 for ml)', () => {
    render(<EventLog events={sampleEvents} />)
    // ✅ appears in "Order filled" + "Trade closed" events.
    const fillIcons = screen.getAllByText('✅')
    expect(fillIcons.length).toBeGreaterThanOrEqual(2)
    // 🛑 appears in "Risk gate tripped" + "Kill switch engaged" events.
    const killIcons = screen.getAllByText('🛑')
    expect(killIcons.length).toBeGreaterThanOrEqual(2)
    // 🤖 appears in "ML model retrained" event.
    const mlIcons = screen.getAllByText('🤖')
    expect(mlIcons.length).toBeGreaterThanOrEqual(1)
    // ⚡ appears in "Order cancelled" + "Quoted market" events.
    const orderIcons = screen.getAllByText('⚡')
    expect(orderIcons.length).toBeGreaterThanOrEqual(2)
  })

  it('fires the Copy button — calls navigator.clipboard.writeText with all events joined', async () => {
    render(<EventLog events={sampleEvents} />)
    const copyBtn = screen.getByRole('button', { name: /^Copy$/i })
    fireEvent.click(copyBtn)
    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledTimes(1)
    })
    const writtenText = (navigator.clipboard.writeText as ReturnType<typeof vi.fn>).mock.calls[0][0]
    // The joined text should contain at least one of the events.
    expect(writtenText).toContain('Order filled: BUY 100 tok_btc_100k_yes')
    expect(writtenText).toContain('Kill switch engaged')
  })

  it('shows the ✓ confirmation label briefly after Copy is clicked', async () => {
    render(<EventLog events={sampleEvents} />)
    fireEvent.click(screen.getByRole('button', { name: /^Copy$/i }))
    await waitFor(() => {
      expect(screen.getByText('✓')).toBeInTheDocument()
    })
  })

  it('fires the CSV export button — does not crash and triggers a download anchor', () => {
    // Use a real <a> element so document.body.appendChild() accepts it
    // (jsdom's appendChild enforces Node type). Then spy on its methods
    // to verify the component wired up the download attributes correctly.
    const origCreateElement = document.createElement.bind(document)
    const realAnchor = origCreateElement('a')
    const clickSpy = vi.spyOn(realAnchor, 'click').mockImplementation(() => {})
    const setAttributeSpy = vi.spyOn(realAnchor, 'setAttribute')
    const createElementSpy = vi
      .spyOn(document, 'createElement')
      .mockImplementation((tagName: string) => {
        if (tagName === 'a') {
          return realAnchor
        }
        return origCreateElement(tagName)
      })

    render(<EventLog events={sampleEvents} />)
    fireEvent.click(screen.getByRole('button', { name: /CSV/i }))
    expect(clickSpy).toHaveBeenCalledTimes(1)
    // The anchor setAttribute should have been called with a download name.
    expect(setAttributeSpy).toHaveBeenCalledWith(
      'download',
      expect.stringMatching(/^event_log_\d+\.csv$/),
    )
    // The href should be a data: URL (the component builds a CSV data URI).
    expect(setAttributeSpy).toHaveBeenCalledWith(
      'href',
      expect.stringMatching(/^data:text\/csv;charset=utf-8,/),
    )
    createElementSpy.mockRestore()
  })

  it('renders the "No events match current filter" empty-state when filter excludes all', () => {
    render(<EventLog events={sampleEvents} />)
    const input = screen.getByLabelText(/Filter events/i)
    fireEvent.change(input, { target: { value: 'zzzzzzz-no-match' } })
    expect(
      screen.getByText(/No events match current filter/i),
    ).toBeInTheDocument()
  })

  it('renders gracefully with an empty events array (no rows, no error)', () => {
    render(<EventLog events={[]} />)
    expect(screen.getByText(/Live System Events/i)).toBeInTheDocument()
    expect(screen.getByText('(0)')).toBeInTheDocument()
  })

  it('renders all 5 filter buttons (all, fill, order, risk, ml)', () => {
    render(<EventLog events={[]} />)
    for (const filter of ['all', 'fill', 'order', 'risk', 'ml']) {
      expect(
        screen.getByRole('button', { name: new RegExp(`^${filter}$`, 'i') }),
      ).toBeInTheDocument()
    }
  })

  it('highlights the active filter button with a different class', () => {
    render(<EventLog events={sampleEvents} />)
    // Initially "all" is active.
    const allBtn = screen.getByRole('button', { name: /^all$/i })
    expect(allBtn.className).toContain('bg-blue-500/20')
    expect(allBtn.className).toContain('text-cyan-300')
    // Click "ml" — it should become active.
    const mlBtn = screen.getByRole('button', { name: /^ml$/i })
    fireEvent.click(mlBtn)
    expect(mlBtn.className).toContain('bg-blue-500/20')
    expect(mlBtn.className).toContain('text-cyan-300')
    // "all" should no longer be active.
    expect(allBtn.className).not.toContain('bg-blue-500/20')
  })

  it('renders the "Clear search" × button when the search box has a value', () => {
    render(<EventLog events={sampleEvents} />)
    const input = screen.getByLabelText(/Filter events/i)
    fireEvent.change(input, { target: { value: 'kill' } })
    const clearBtn = screen.getByLabelText(/Clear search/i)
    expect(clearBtn).toBeInTheDocument()
    // Clicking clear should reset the search.
    fireEvent.click(clearBtn)
    expect(
      (screen.getByLabelText(/Filter events/i) as HTMLInputElement).value,
    ).toBe('')
  })

  it('renders the match count badge when a filter or search is active', () => {
    render(<EventLog events={sampleEvents} />)
    fireEvent.click(screen.getByRole('button', { name: /^fill$/i }))
    // 2 events match the fill filter (Order filled + Trade closed).
    expect(screen.getByText(/2 matches/i)).toBeInTheDocument()
  })

  it('renders the singular "match" (no s) when exactly one event matches', () => {
    render(<EventLog events={sampleEvents} />)
    fireEvent.click(screen.getByRole('button', { name: /^ml$/i }))
    // 1 event matches the ml filter (ML model retrained).
    expect(screen.getByText(/1 match/i)).toBeInTheDocument()
  })

  it('does NOT render the match count badge when filter is "all" and search is empty', () => {
    render(<EventLog events={sampleEvents} />)
    // Initial state — filter is "all", search is empty.
    expect(
      screen.queryByText(/match(es)?/i),
    ).not.toBeInTheDocument()
  })
})
