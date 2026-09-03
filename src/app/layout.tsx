import type { Metadata, Viewport } from 'next'
import './globals.css'
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
        {children}
      </body>
    </html>
  )
}
