import type { Metadata, Viewport } from 'next'
import { Inter } from 'next/font/google'
import './globals.css'
import ServiceWorkerRegister from './sw-register'

const inter = Inter({ subsets: ['latin'] })

export const metadata: Metadata = {
  title: 'ARIA - Local AI Agent Platform',
  description: 'Personal AI agent with long-term memory, tool use, and computer control',
  applicationName: 'ARIA',
  // manifest is auto-linked from app/manifest.ts, but set explicitly for clarity.
  manifest: '/manifest.webmanifest',
  appleWebApp: {
    capable: true,
    statusBarStyle: 'black-translucent',
    title: 'ARIA',
  },
  icons: {
    icon: '/favicon.ico',
    apple: '/apple-touch-icon.png',
  },
}

export const viewport: Viewport = {
  themeColor: '#0f172a',
  width: 'device-width',
  initialScale: 1,
  viewportFit: 'cover',
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="en">
      {/*
        Global horizontal-overflow guard: `overflow-x-hidden` on <body> clips any
        page that would otherwise scroll sideways on a phone (body overflow does
        not propagate to the viewport, so vertical page scrolling is unaffected).
        `max-w-full` keeps the body itself from being widened by a stray child.
      */}
      <body className={`${inter.className} max-w-full overflow-x-hidden`}>
        {children}
        <ServiceWorkerRegister />
      </body>
    </html>
  )
}
