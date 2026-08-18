/**
 * ARIA - Web App Manifest
 *
 * Colours come from the token module, so the OS chrome can no longer drift from
 * the palette (it was slate #0f172a from the pre-redesign theme while the app's
 * ground was #0e1014 / #f4f6f9).
 */
import type { MetadataRoute } from 'next'
import { themeColor } from '@/design/tokens'

export default function manifest(): MetadataRoute.Manifest {
  return {
    id: '/',
    scope: '/',
    name: 'ARIA',
    short_name: 'ARIA',
    description: 'Local-first agent substrate: memory, fleet, models and coding sessions',
    // The installed app opens on what is waiting, not on a launcher.
    start_url: '/inbox?src=pwa',
    display: 'standalone',
    background_color: themeColor.dark,
    theme_color: themeColor.dark,
    shortcuts: [
      { name: 'Inbox', url: '/inbox' },
      { name: 'Shells', url: '/supervise/shells' },
      { name: 'Operate', url: '/operate' },
    ],
    icons: [
      { src: '/icon-192.png', sizes: '192x192', type: 'image/png', purpose: 'any' },
      { src: '/icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'any' },
      { src: '/icon-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
    ],
  }
}
