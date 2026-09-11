// components/ai-explainability.test.tsx — W40-2 minimal render tests for
// the W39-6 shared AI labeling primitives.
//
// All five primitives are stateless presentational shells — no fetch,
// no clock, no global state. Tests cover the render contract for each:
//   1. AIPredictionLabel renders the "AI Prediction:" label.
//   2. ConfidenceBadge renders the documented testid + percentage.
//   3. NotAGuaranteeInline renders the disclaimer text in both variants.
//   4. ModelStatusStrip renders the version / drift / calibration cells.
//   5. WhyExplanation renders the header + feature rows when expanded.
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import {
  AIPredictionLabel,
  ConfidenceBadge,
  confidenceTone,
  NotAGuaranteeInline,
  ModelStatusStrip,
  driftLevelFromStatus,
  WhyExplanation,
} from './ai-explainability'

global.fetch = vi.fn()

describe('AIPredictionLabel', () => {
  it('renders without crashing', () => {
    const { container } = render(<AIPredictionLabel />)
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the default "AI Prediction:" label text', () => {
    render(<AIPredictionLabel />)
    expect(screen.getByText('AI Prediction:')).toBeInTheDocument()
  })

  it('honors the documented data-testid="ai-prediction-label"', () => {
    render(<AIPredictionLabel />)
    expect(screen.getByTestId('ai-prediction-label')).toBeInTheDocument()
  })
})

describe('ConfidenceBadge', () => {
  it('renders without crashing', () => {
    const { container } = render(<ConfidenceBadge value={0.65} />)
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the percentage text and data-testid', () => {
    render(<ConfidenceBadge value={0.65} />)
    expect(screen.getByTestId('confidence-badge')).toBeInTheDocument()
    expect(screen.getByText('65%')).toBeInTheDocument()
  })

  it('exposes the derived tone via data-confidence-tone', () => {
    render(<ConfidenceBadge value={0.65} />)
    // 0.65 falls in the [0.50, 0.70) → 'medium' bucket.
    expect(screen.getByTestId('confidence-badge')).toHaveAttribute(
      'data-confidence-tone',
      'medium',
    )
  })

  it('confidenceTone() helper maps the documented buckets', () => {
    expect(confidenceTone(0.95)).toBe('high')
    expect(confidenceTone(0.55)).toBe('medium')
    expect(confidenceTone(0.30)).toBe('low')
    expect(confidenceTone(null)).toBe('unknown')
    expect(confidenceTone(undefined)).toBe('unknown')
    expect(confidenceTone(Number.NaN)).toBe('unknown')
  })
})

describe('NotAGuaranteeInline', () => {
  it('renders without crashing in the bordered (default) variant', () => {
    const { container } = render(<NotAGuaranteeInline />)
    expect(container.firstChild).toBeTruthy()
  })

  it('renders without crashing in the compact variant', () => {
    const { container } = render(<NotAGuaranteeInline compact />)
    expect(container.firstChild).toBeTruthy()
  })

  it('exposes data-testid="not-a-guarantee-inline" in both variants', () => {
    const { unmount } = render(<NotAGuaranteeInline />)
    expect(screen.getByTestId('not-a-guarantee-inline')).toBeInTheDocument()
    unmount()
    render(<NotAGuaranteeInline compact />)
    expect(screen.getByTestId('not-a-guarantee-inline')).toBeInTheDocument()
  })

  it('renders the "NOT A GUARANTEE" headline in the bordered variant', () => {
    render(<NotAGuaranteeInline />)
    expect(screen.getByText('NOT A GUARANTEE.')).toBeInTheDocument()
  })
})

describe('ModelStatusStrip', () => {
  it('renders without crashing', () => {
    const { container } = render(
      <ModelStatusStrip
        version="v1.155.0"
        trainedAt={Math.floor(Date.now() / 1000) - 7200}
        drift="ok"
        calibrated
        featureAgeSeconds={3}
      />,
    )
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the documented testid + version cell', () => {
    render(
      <ModelStatusStrip
        version="v1.155.0"
        trainedAt={null}
        drift="unknown"
        calibrated={false}
        featureAgeSeconds={null}
      />,
    )
    expect(screen.getByTestId('model-status-strip')).toBeInTheDocument()
    expect(screen.getByTestId('status-version')).toHaveTextContent('v1.155.0')
    expect(screen.getByTestId('status-calibration')).toHaveTextContent(
      'Needs recalibration',
    )
  })

  it('driftLevelFromStatus() maps documented status strings', () => {
    expect(driftLevelFromStatus('HEALTHY')).toBe('ok')
    expect(driftLevelFromStatus('OK')).toBe('ok')
    expect(driftLevelFromStatus('MODERATE_DRIFT')).toBe('warning')
    expect(driftLevelFromStatus('CRITICAL_DRIFT')).toBe('critical')
    expect(driftLevelFromStatus(null)).toBe('unknown')
    expect(driftLevelFromStatus('')).toBe('unknown')
  })
})

describe('WhyExplanation', () => {
  it('renders without crashing', () => {
    const { container } = render(
      <WhyExplanation features={[]} agreement={null} />,
    )
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the "Why?" header label', () => {
    render(<WhyExplanation features={[]} agreement={null} />)
    expect(screen.getByText('Why?')).toBeInTheDocument()
  })

  it('renders the top contributing features when expanded', () => {
    render(
      <WhyExplanation
        features={[
          { name: 'microstructure.spread_pct', value: 0.012, contribution: 0.21 },
          { name: 'regime.volatility_30s', value: 0.34, contribution: -0.14 },
        ]}
        agreement={0.92}
        defaultExpanded
      />,
    )
    expect(screen.getByText('microstructure.spread_pct')).toBeInTheDocument()
    expect(screen.getByText('regime.volatility_30s')).toBeInTheDocument()
  })
})
