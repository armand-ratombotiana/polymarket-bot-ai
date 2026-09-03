import type { Metadata, Viewport } from 'next'
import './globals.css'
// W10-3 — Root-level React Error Boundary. Catches render-phase errors
// anywhere in the workstation tree and shows a recoverable fallback instead
// of a blank white screen. Mounted here at the layout root so it sits above
// every page (today only `src/app/page.tsx`) and above every panel rendered
// inside it. See `src/components/ErrorBoundary.tsx` for the full lifecycle
// catch semantics and known limitations (no event-handler / async errors).
import ErrorBoundary from '@/components/ErrorBoundary'
export const metadata: Metadata = {
  title: 'Polymarket Pro — Algorithmic Trading Workstation',
  description: 'Institutional-grade prediction-market trading workstation. Paper trading mode — real Polymarket data.',
  robots: 'noindex, nofollow',
}
export const viewport: Viewport = { width: 'device-width', initialScale: 1 }
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="anonymous" />
      </head>
      <body>
        {/* W9-7 — Skip-to-main-content link: visually hidden, revealed on
            keyboard focus (Tab from URL bar). Lets keyboard & screen-reader
            users jump the long sidebar directly to the workstation content.
            Target is `#main` on the `<main>` element in src/app/page.tsx. */}
        <a href="#main" className="skip-link">
          Skip to main content
        </a>
        {/* W10-3 — Root-level boundary. Sits ABOVE the page so even a
            catastrophic render crash inside `page.tsx` shows the fallback
            card instead of a white screen. The dedicated `app/error.tsx`
            file is the Next.js App-Router-level fallback for route-segment
            errors (it has slightly different semantics from this in-tree
            boundary); both co-exist. */}
        <ErrorBoundary>{children}</ErrorBoundary>
      </body>
    </html>
  )
}
