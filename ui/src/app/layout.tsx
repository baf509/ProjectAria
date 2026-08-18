/**
 * ARIA - root layout
 *
 * Two deletions matter more than anything added here:
 *  - `overflow-x-hidden` on <body>: it MASKED overflow rather than preventing
 *    it (the layout was still 482px wide in a 390px viewport, the chrome
 *    stopped at 390, and the difference read as a blank strip beside the
 *    header). Containment is now structural — see globals.css's base layer and
 *    the Grid/Row primitives — and any regression fails the Playwright gate.
 *  - `minimumScale: 1`: it removed the user's only recovery (pinch out) from a
 *    page that was too wide. The real cause of the "page is zoomed" symptom was
 *    iOS auto-zooming into sub-16px form controls, which is fixed in the base
 *    layer instead, so user zoom stays unrestricted (WCAG 1.4.4).
 */
import type { Metadata, Viewport } from 'next'
import './globals.css'
import ServiceWorkerRegister from './sw-register'
import { themeColor } from '@/design/tokens'

export const metadata: Metadata = {
  title: { default: 'ARIA', template: '%s · ARIA' },
  description: 'Local-first agent substrate: memory, fleet, models and coding sessions',
  applicationName: 'ARIA',
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
  width: 'device-width',
  initialScale: 1,
  viewportFit: 'cover',
  colorScheme: 'dark light',
  // Chromium honours this for the on-screen keyboard; iOS ignores it, which is
  // why the shell also tracks visualViewport into --vvh.
  interactiveWidget: 'resizes-content',
  themeColor: [
    { media: '(prefers-color-scheme: light)', color: themeColor.light },
    { media: '(prefers-color-scheme: dark)', color: themeColor.dark },
  ],
}

/** Applies the persisted theme/density before first paint (no flash). */
const THEME_SCRIPT = `(function(){try{var t=localStorage.getItem('aria-theme');var d=localStorage.getItem('aria-density');var r=document.documentElement;if(t&&t!=='system')r.setAttribute('data-theme',t);if(d&&d!=='auto')r.setAttribute('data-density',d);}catch(e){}})()`

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body>
        {children}
        <ServiceWorkerRegister />
      </body>
    </html>
  )
}
