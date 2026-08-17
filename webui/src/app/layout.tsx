import type { Metadata } from 'next'
import './globals.css'

export const metadata: Metadata = {
  title: 'Polymarket Bot — Dashboard',
  description: 'Live trading dashboard for the Polymarket automated bot',
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className="min-h-screen bg-[#0a0b0e] text-[#e8eaf0] antialiased">
        {children}
      </body>
    </html>
  )
}
