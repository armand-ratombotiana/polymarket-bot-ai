// components/PortfolioRiskPanel.test.tsx — Basic rendering tests.
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import PortfolioRiskPanel from './PortfolioRiskPanel'

// Minimal positions for rendering
const samplePositions = [
  { token_id: 'tok-a', side: 'LONG', size: 10, avg_price: 0.5, current_price: 0.55, unrealized_pnl: 0.5 } as any,
  { token_id: 'tok-b', side: 'SHORT', size: 20, avg_price: 0.3, current_price: 0.25, unrealized_pnl: 1.0 } as any,
]

describe('PortfolioRiskPanel', () => {
  it('renders the panel title', () => {
    render(<PortfolioRiskPanel positions={samplePositions} />)
    expect(screen.getByText('Portfolio Risk Matrix')).toBeInTheDocument()
  })

  it('renders the panel container', () => {
    const { container } = render(<PortfolioRiskPanel positions={samplePositions} />)
    expect(container.firstChild).toBeTruthy()
  })

  it('renders without crashing with empty positions', () => {
    const { container } = render(<PortfolioRiskPanel positions={[]} />)
    expect(container.firstChild).toBeTruthy()
  })
})
