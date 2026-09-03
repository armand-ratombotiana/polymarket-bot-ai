// src/components/OfflineIndicator.stories.tsx — W12-7 Storybook
// documentation for the PWA offline banner.
//
// Two stories:
//   - Default (online): navigator.onLine === true → the component
//     renders nothing. Storybook will display an empty canvas; the
//     notes panel explains this is intentional.
//   - Offline: we force navigator.onLine to false and dispatch the
//     `offline` window event after a microtask so the component's
//     useEffect re-syncs `isOffline=true` and renders the sticky
//     banner exactly as it appears in production when the OS
//     reports the network went down.
//
// Implementation notes:
//   - The OfflineIndicator reads `navigator.onLine` once on mount
//     and on every `online`/`offline` event. We override
//     `navigator.onLine` via Object.defineProperty (configurable so
//     the next story can reset it) and dispatch the synthetic event
//     in a useEffect, mirroring how a real browser would notify the
//     page when the OS network stack flips.
//   - We restore `navigator.onLine = true` on story unmount so the
//     Default story (which renders nothing) doesn't accidentally
//     pick up the offline state from a prior story.

import type { Meta, StoryObj } from '@storybook/react'
import { useEffect } from 'react'
import OfflineIndicator from './OfflineIndicator'

const meta: Meta<typeof OfflineIndicator> = {
  title: 'Status/OfflineIndicator',
  component: OfflineIndicator,
  parameters: { layout: 'fullscreen' },
  tags: ['autodocs'],
}
export default meta

type Story = StoryObj<typeof OfflineIndicator>

/**
 * OnlineStory — defensive: ensure any prior story that flipped
 * navigator.onLine back to true so subsequent stories (and the
 * canvas) see the online state. Renders the OfflineIndicator, which
 * returns null when online — Storybook shows an empty canvas, which
 * is the expected production behavior when connectivity is healthy.
 */
function OnlineStory() {
  useEffect(() => {
    Object.defineProperty(navigator, 'onLine', {
      configurable: true,
      value: true,
    })
    window.dispatchEvent(new Event('online'))
  }, [])
  return <OfflineIndicator />
}

/**
 * OfflineStory — force the browser's `navigator.onLine` to false and
 * notify any listeners (the OfflineIndicator subscribes to the
 * `offline` event in its own useEffect). Renders the sticky amber
 * banner at the top of the viewport, telling the trader that data
 * is stale and new trades will queue locally until connectivity
 * returns.
 *
 * The banner uses `position: sticky; top: 0` so it overlays the
 * dashboard chrome without pushing layout (avoids a jarring reflow
 * when connectivity flaps). We restore the online state when the
 * story unmounts so the Default story is accurate.
 */
function OfflineStory() {
  useEffect(() => {
    Object.defineProperty(navigator, 'onLine', {
      configurable: true,
      value: false,
    })
    window.dispatchEvent(new Event('offline'))
    return () => {
      Object.defineProperty(navigator, 'onLine', {
        configurable: true,
        value: true,
      })
      window.dispatchEvent(new Event('online'))
    }
  }, [])
  return (
    <div style={{ minHeight: '100vh', background: '#0b0e14' }}>
      <OfflineIndicator />
      <div
        style={{
          padding: '2rem',
          color: '#7e8aaa',
          fontFamily: 'system-ui, sans-serif',
          fontSize: '0.875rem',
        }}
      >
        Dashboard content sits below the sticky offline banner. The
        banner uses <code>position: sticky; top: 0</code> so it
        overlays the dashboard chrome without pushing layout.
      </div>
    </div>
  )
}

/**
 * Default (online) — the component renders nothing (returns null).
 * Storybook will show an empty canvas; this is the expected
 * production behavior when connectivity is healthy.
 */
export const Default: Story = {
  render: () => <OnlineStory />,
}

/**
 * Offline — the sticky amber banner renders at the top of the
 * viewport, telling the trader that data is stale and new trades
 * will queue locally until connectivity returns.
 */
export const Offline: Story = {
  render: () => <OfflineStory />,
}
