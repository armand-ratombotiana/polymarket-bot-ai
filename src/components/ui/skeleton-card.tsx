// src/components/ui/skeleton-card.tsx — Improved skeleton primitives.
// W10-8 — Composable skeleton shapes that match the dashboard's
// card / table / KPI visual language. Used by lazy-loaded panels and
// async fetches so the layout doesn't flash blank during data loading.
//
// The styling (shimmer gradient, sizing) lives in globals.css under
// the `.skeleton-*` classnames. Keeping the styling in CSS (instead of
// Tailwind utilities or inline styles) means a single source of truth
// for the shimmer animation, and lets the existing `prefers-reduced-motion`
// global rule automatically disable the animation for users who request
// reduced motion.
//
// These are intentionally framework-agnostic (no 'use client' needed —
// they're plain divs). Server components can use them too.

export function SkeletonCard() {
  return (
    <div className="skeleton-card" role="status" aria-label="Loading content">
      <div className="skeleton-line-lg" />
      <div className="skeleton-line-sm" />
      <div className="skeleton-line-md" />
    </div>
  )
}

export function SkeletonTable({
  rows = 5,
  cols = 4,
}: {
  rows?: number
  cols?: number
}) {
  return (
    <div
      className="skeleton-table"
      role="status"
      aria-label={`Loading ${rows} rows of data`}
    >
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton-row">
          {Array.from({ length: cols }).map((_, j) => (
            <div key={j} className="skeleton-cell" />
          ))}
        </div>
      ))}
    </div>
  )
}

export function SkeletonKPI() {
  return (
    <div
      className="skeleton-kpi"
      role="status"
      aria-label="Loading metric"
    >
      <div className="skeleton-line-sm" />
      <div className="skeleton-line-lg" />
    </div>
  )
}
