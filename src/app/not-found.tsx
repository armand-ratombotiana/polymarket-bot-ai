// src/app/not-found.tsx — W10-3
// Next.js App Router 404 page. Rendered automatically when the router
// can't match a path to a route segment. Matches the dark workstation
// design system so the 404 page doesn't visually break out of the
// dashboard aesthetic if a user lands on a non-existent URL.
//
// This is a Server Component (no `'use client'`) — Next.js allows that
// for `not-found.tsx` because no event handlers or hooks are needed.

import Link from 'next/link'

export default function NotFound() {
  return (
    <div
      className="not-found-page"
      role="alertdialog"
      aria-labelledby="nf-title"
      aria-describedby="nf-desc"
    >
      <div className="not-found-page-code mono" aria-hidden="true">
        404
      </div>
      <h2 id="nf-title" className="not-found-page-title">
        Page not found
      </h2>
      <p id="nf-desc" className="not-found-page-message">
        The page you&apos;re looking for doesn&apos;t exist or may have been moved.
      </p>
      <div className="not-found-page-actions">
        <Link href="/" className="btn btn-primary">
          ← Back to Workstation
        </Link>
      </div>
    </div>
  )
}
