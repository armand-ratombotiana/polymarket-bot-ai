// src/components/ui/skeleton-card.stories.tsx — W12-7 Storybook
// documentation for the W10-8 skeleton primitives.
//
// Three stories showcase the composable loading shapes that match
// the dashboard's card / table / KPI visual language:
//   - SkeletonCard: card-shaped loading placeholder (header + lines).
//   - SkeletonTable: configurable rows × cols of shimmering cells,
//     with controls for rows/cols in the Args panel.
//   - SkeletonKPI: compact KPI metric placeholder.
//
// The shimmer animation + sizing lives in globals.css under the
// .skeleton-* classnames (single source of truth). preview.ts
// imports globals.css so the shimmer keyframe runs in Storybook
// exactly as it does in production.

import type { Meta, StoryObj } from '@storybook/react'
import { SkeletonCard, SkeletonTable, SkeletonKPI } from './skeleton-card'

const meta: Meta = {
  title: 'UI/Skeleton',
  parameters: { layout: 'fullscreen' },
  tags: ['autodocs'],
}
export default meta

type Story = StoryObj

const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <div
    style={{
      padding: 24,
      background: '#0b0e14',
      minHeight: '100vh',
      maxWidth: 720,
    }}
  >
    {children}
  </div>
)

/**
 * SkeletonCard — the standard card-loading placeholder. Renders a
 * card-shaped container with three shimmering lines (lg / sm / md)
 * that mirror the typical card header + content layout. Used by
 * lazy-loaded panels and async fetches so the layout doesn't flash
 * blank during data loading.
 */
export const Card: Story = {
  render: () => (
    <Wrapper>
      <SkeletonCard />
    </Wrapper>
  ),
}

/**
 * SkeletonTable — configurable rows × cols of shimmering cells.
 * Use the Controls panel below to vary rows/cols and see how the
 * shimmer scales (e.g. simulate a 50-row positions table loading).
 */
export const Table: Story = {
  args: {
    rows: 5,
    cols: 4,
  },
  render: (args: { rows?: number; cols?: number }) => (
    <Wrapper>
      <SkeletonTable rows={args.rows ?? 5} cols={args.cols ?? 4} />
    </Wrapper>
  ),
}

/**
 * SkeletonKPI — compact KPI metric placeholder (label + value
 * lines). Used inside the analytics grid and command-center KPI
 * strips while the backend `/api/analytics` call is in-flight.
 */
export const KPI: Story = {
  render: () => (
    <Wrapper>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fill, minmax(180px, 1fr))',
          gap: 12,
        }}
      >
        {Array.from({ length: 6 }).map((_, i) => (
          <SkeletonKPI key={i} />
        ))}
      </div>
    </Wrapper>
  ),
}
