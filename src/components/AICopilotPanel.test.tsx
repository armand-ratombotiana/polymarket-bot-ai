// components/AICopilotPanel.test.tsx — W38-8 component tests.
//
// Strategy: mock `apiFetch` so we can drive the chat session through
// success / HTTP-error / network-error paths. The panel renders an
// initial assistant greeting on mount so basic rendering is verifiable
// without any fetches.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import AICopilotPanel from './AICopilotPanel'

// ── Mocks ─────────────────────────────────────────────────────────────────
const apiFetchMock = vi.fn()
vi.mock('@/lib/api', () => ({
  getApiUrl: () => '',
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}))

function mockOk(payload: unknown) {
  return {
    ok: true,
    status: 200,
    json: async () => payload,
  } as Response
}

function mockNotOk(status = 500) {
  return {
    ok: false,
    status,
    json: async () => ({}),
  } as Response
}

beforeEach(() => {
  apiFetchMock.mockReset()
})

afterEach(() => {
  cleanup()
})

describe('AICopilotPanel', () => {
  it('renders without crashing', () => {
    render(<AICopilotPanel />)
    expect(screen.getByRole('textbox')).toBeInTheDocument()
  })

  it('renders the "Market Intelligence & Quant Copilot" header', () => {
    render(<AICopilotPanel />)
    expect(
      screen.getByText(/Market Intelligence & Quant Copilot/),
    ).toBeInTheDocument()
  })

  it('renders the initial assistant greeting on mount', () => {
    render(<AICopilotPanel />)
    expect(
      screen.getByText(/Welcome to the \*\*Polymarket Pro Copilot\*\*/),
    ).toBeInTheDocument()
  })

  it('renders the quick-prompt buttons', () => {
    render(<AICopilotPanel />)
    expect(
      screen.getByText(/Top high-conviction ML opportunities/),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/Current 4-member ensemble weights/),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/Scan Dutch-Book arbitrage pairs/),
    ).toBeInTheDocument()
  })

  it('renders the Send button (disabled when the input is empty)', () => {
    render(<AICopilotPanel />)
    const send = screen.getByRole('button', { name: 'Send' })
    expect(send).toBeDisabled()
  })

  it('posts the query to /api/ai/copilot and renders the assistant reply', async () => {
    const user = userEvent.setup()
    apiFetchMock.mockResolvedValue(
      mockOk({ reply: 'Top opportunity: token-XYZ at 0.62' }),
    )
    render(<AICopilotPanel />)
    const input = screen.getByRole('textbox')
    await user.type(input, 'show me the top high-conviction markets')
    await user.click(screen.getByRole('button', { name: 'Send' }))
    // The user message should be echoed in the feed.
    await waitFor(() => {
      expect(
        screen.getByText('show me the top high-conviction markets'),
      ).toBeInTheDocument()
    })
    // The assistant reply should be rendered too.
    await waitFor(() => {
      expect(
        screen.getByText('Top opportunity: token-XYZ at 0.62'),
      ).toBeInTheDocument()
    })
    expect(apiFetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = apiFetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/api/ai/copilot')
    expect(init.method).toBe('POST')
  })

  it('shows a Copilot engine error when the API returns not-ok', async () => {
    const user = userEvent.setup()
    apiFetchMock.mockResolvedValue(mockNotOk(500))
    render(<AICopilotPanel />)
    await user.type(screen.getByRole('textbox'), 'hello')
    await user.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => {
      expect(
        screen.getByText('❌ Copilot engine error. Please try again.'),
      ).toBeInTheDocument()
    })
  })

  it('shows a network error when the API fetch throws', async () => {
    const user = userEvent.setup()
    apiFetchMock.mockRejectedValue(new Error('Network error'))
    render(<AICopilotPanel />)
    await user.type(screen.getByRole('textbox'), 'hello')
    await user.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => {
      expect(
        screen.getByText('❌ Could not reach bot API server.'),
      ).toBeInTheDocument()
    })
  })

  it('renders matched-market pills when the assistant reply includes them', async () => {
    const user = userEvent.setup()
    apiFetchMock.mockResolvedValue(
      mockOk({
        reply: 'I found these matching contracts:',
        matched_markets: [
          {
            token_id: 'tok-1',
            title: 'Will it rain in Paris?',
            slug: 'paris-rain',
            similarity: 0.93,
            mid_price: 0.62,
          },
        ],
      }),
    )
    render(<AICopilotPanel />)
    await user.type(screen.getByRole('textbox'), 'rain')
    await user.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => {
      expect(screen.getByText('Will it rain in Paris?')).toBeInTheDocument()
    })
  })

  it('invokes onSelectMarket when a matched-market pill is clicked', async () => {
    const user = userEvent.setup()
    const onSelectMarket = vi.fn()
    apiFetchMock.mockResolvedValue(
      mockOk({
        reply: 'Found:',
        matched_markets: [
          {
            token_id: 'tok-1',
            title: 'Will it rain in Paris?',
            slug: 'paris-rain',
            similarity: 0.93,
            mid_price: 0.62,
          },
        ],
      }),
    )
    render(<AICopilotPanel onSelectMarket={onSelectMarket} />)
    await user.type(screen.getByRole('textbox'), 'rain')
    await user.click(screen.getByRole('button', { name: 'Send' }))
    const pill = await screen.findByText('Will it rain in Paris?')
    await user.click(pill)
    expect(onSelectMarket).toHaveBeenCalledWith({
      tokenId: 'tok-1',
      slug: 'paris-rain',
    })
  })
})
