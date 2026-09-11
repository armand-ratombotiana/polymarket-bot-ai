// src/components/ui/motion.stories.tsx — W12-7 Storybook documentation
// for the reusable Framer Motion wrappers (W10-8 UI polish layer).
//
// Each story showcases one motion primitive with a representative
// child so the designer can see how the transition reads on a
// real-time trading dashboard:
//   - FadeIn: subtle opacity + 8px rise (panel transition).
//   - SlideIn: enters from a screen edge (drawer/modal).
//   - AnimatedListItem: staggered list rows.
//   - NumberTicker: KPI value swap with re-key fade-up.
//   - Pulse: gentle opacity oscillation (live indicator).
//
// All primitives are 'use client' (Framer Motion touches
// window/requestAnimationFrame on mount). Storybook's React renderer
// runs entirely client-side so the directive is a no-op here but kept
// for source parity.

import type { Meta, StoryObj } from '@storybook/react'
import { useState } from 'react'
import {
  FadeIn,
  SlideIn,
  AnimatedListItem,
  NumberTicker,
  Pulse,
  AnimatePresence,
} from './motion'

const meta: Meta = {
  title: 'UI/Motion',
  parameters: { layout: 'fullscreen' },
  tags: ['autodocs'],
}
export default meta

type Story = StoryObj

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div
      style={{
        background: '#13161e',
        border: '1px solid #1f2335',
        borderRadius: 8,
        padding: 16,
        color: '#dde1ed',
        fontFamily: 'system-ui, sans-serif',
        minHeight: 120,
      }}
    >
      <div
        style={{
          fontSize: 11,
          color: '#7e8aaa',
          marginBottom: 8,
          textTransform: 'uppercase',
          letterSpacing: '0.1em',
        }}
      >
        {title}
      </div>
      {children}
    </div>
  )
}

const toggleBtn: React.CSSProperties = {
  background: '#1f2335',
  color: '#dde1ed',
  border: '1px solid #2d3450',
  borderRadius: 6,
  padding: '6px 12px',
  cursor: 'pointer',
  marginBottom: 16,
  fontSize: 12,
}

/**
 * FadeInStory — subtle 200ms opacity + 8px rise. The default panel-
 * transition wrapper. Toggle the panel via the button to see the
 * exit animation (AnimatePresence mode="wait" pattern).
 */
function FadeInStory() {
  const [panel, setPanel] = useState<'a' | 'b'>('a')
  return (
    <div style={{ padding: 24, background: '#0b0e14', minHeight: '100vh' }}>
      <button
        onClick={() => setPanel(p => (p === 'a' ? 'b' : 'a'))}
        style={toggleBtn}
      >
        Toggle panel (currently: {panel})
      </button>
      <AnimatePresence mode="wait">
        <FadeIn key={panel}>
          <Panel title={`FadeIn panel ${panel.toUpperCase()}`}>
            This panel mounts with opacity 0 → 1 and y: 8px → 0 over
            200ms. Swapping the key forces AnimatePresence to
            animate-out the previous panel, then animate-in the new
            one.
          </Panel>
        </FadeIn>
      </AnimatePresence>
    </div>
  )
}

/**
 * SlideInStory — enters from a screen edge. Default direction is
 * 'right' (drawer pattern). The demo cycles through all four
 * directions.
 */
function SlideInStory() {
  const [dir, setDir] = useState<'left' | 'right' | 'up' | 'down'>('right')
  const dirs: Array<'left' | 'right' | 'up' | 'down'> = ['left', 'right', 'up', 'down']
  return (
    <div style={{ padding: 24, background: '#0b0e14', minHeight: '100vh' }}>
      <div style={{ display: 'flex', gap: 8, marginBottom: 16 }}>
        {dirs.map(d => (
          <button
            key={d}
            onClick={() => setDir(d)}
            style={{
              ...toggleBtn,
              background: dir === d ? '#1e3a8a' : '#1f2335',
              marginBottom: 0,
            }}
          >
            {d}
          </button>
        ))}
      </div>
      <AnimatePresence mode="wait">
        <SlideIn key={dir} direction={dir}>
          <Panel title={`SlideIn from ${dir}`}>
            The panel slides in from the {dir} edge over 250ms with
            easeInOut. Used by modals, drawers, and side panels that
            should "leave the way they came in".
          </Panel>
        </SlideIn>
      </AnimatePresence>
    </div>
  )
}

/**
 * AnimatedListItemStory — staggers each row by `index * 20ms` (capped
 * at 300ms). Animates x (not y) so items read as "slotting into
 * place" rather than "dropping from above".
 */
function AnimatedListItemStory() {
  const items = [
    'AAPL — 100 shares @ $182.40',
    'TSLA — 50 shares @ $245.10',
    'NVDA — 25 shares @ $890.55',
    'MSFT — 75 shares @ $412.30',
    'GOOG — 60 shares @ $158.20',
    'META — 40 shares @ $485.75',
  ]
  return (
    <div style={{ padding: 24, background: '#0b0e14', minHeight: '100vh' }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxWidth: 480 }}>
        {items.map((label, i) => (
          <AnimatedListItem key={label} index={i}>
            <div
              style={{
                background: '#13161e',
                border: '1px solid #1f2335',
                borderRadius: 6,
                padding: '8px 12px',
                color: '#dde1ed',
                fontFamily: 'JetBrains Mono, monospace',
                fontSize: 12,
              }}
            >
              {label}
            </div>
          </AnimatedListItem>
        ))}
      </div>
    </div>
  )
}

/**
 * NumberTickerStory — for KPI / stat values that change in place.
 * Re-keys on `value` change so Framer treats each new value as a
 * fresh mount → the fade-up runs again, drawing the eye to the
 * updated figure without a heavy counting animation. Click "Next
 * tick" to advance the value.
 */
function NumberTickerStory() {
  const [value, setValue] = useState(1234.56)
  return (
    <div style={{ padding: 24, background: '#0b0e14', minHeight: '100vh' }}>
      <button
        onClick={() => setValue(v => +(v + (Math.random() - 0.4) * 100).toFixed(2))}
        style={toggleBtn}
      >
        Next tick
      </button>
      <Panel title="NumberTicker (KPI value)">
        <div
          style={{
            fontFamily: 'JetBrains Mono, monospace',
            fontSize: 32,
            fontWeight: 700,
            color: '#22d3ee',
          }}
        >
          <NumberTicker value={value} format={n => `$${n.toFixed(2)}`} />
        </div>
        <div style={{ marginTop: 8, fontSize: 11, color: '#7e8aaa' }}>
          Current value: ${value.toFixed(2)}
        </div>
      </Panel>
    </div>
  )
}

/**
 * PulseStory — gentle 1.5s opacity oscillation (0.5 → 1 → 0.5).
 * Used for "live"/"connecting"/"loading" states. Cadence mirrors the
 * `.skeleton-shimmer` keyframe so motion feels cohesive.
 */
function PulseStory() {
  return (
    <div style={{ padding: 24, background: '#0b0e14', minHeight: '100vh' }}>
      <Panel title="Pulse (live indicator)">
        <Pulse>
          <div
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 8,
              fontFamily: 'system-ui, sans-serif',
              fontSize: 13,
              color: '#4ade80',
            }}
          >
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: '50%',
                background: '#4ade80',
                display: 'inline-block',
              }}
              aria-hidden="true"
            />
            Bot engine live — 4 strategies active
          </div>
        </Pulse>
      </Panel>
    </div>
  )
}

/**
 * FadeIn — subtle 200ms opacity + 8px rise. The default panel-
 * transition wrapper. Toggle the panel via the button to see the
 * exit animation (AnimatePresence mode="wait" pattern).
 */
export const FadeInDemo: Story = {
  render: () => <FadeInStory />,
}

/**
 * SlideIn — enters from a screen edge. Default direction is 'right'
 * (drawer pattern). The demo cycles through all four directions.
 */
export const SlideInDemo: Story = {
  render: () => <SlideInStory />,
}

/**
 * AnimatedListItem — staggers each row by `index * 20ms` (capped at
 * 300ms). Animates x (not y) so items read as "slotting into place"
 * rather than "dropping from above".
 */
export const AnimatedListItemDemo: Story = {
  render: () => <AnimatedListItemStory />,
}

/**
 * NumberTicker — for KPI / stat values that change in place. Re-keys
 * on `value` change so Framer treats each new value as a fresh mount
 * → the fade-up runs again, drawing the eye to the updated figure
 * without a heavy counting animation. Click "Next tick" to advance
 * the value.
 */
export const NumberTickerDemo: Story = {
  render: () => <NumberTickerStory />,
}

/**
 * Pulse — gentle 1.5s opacity oscillation (0.5 → 1 → 0.5). Used for
 * "live"/"connecting"/"loading" states. Cadence mirrors the
 * `.skeleton-shimmer` keyframe so motion feels cohesive.
 */
export const PulseDemo: Story = {
  render: () => <PulseStory />,
}
