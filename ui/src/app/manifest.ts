/**
 * ARIA - Web App Manifest
 *
 * Phase: UI / PWA
 * Purpose: Make the ARIA web UI installable as a PWA via the Next.js Metadata API.
 *
 * Auto-served at /manifest.webmanifest and auto-linked into <head> by Next.js.
 */
import type { MetadataRoute } from 'next'

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: 'ARIA',
    short_name: 'ARIA',
    description:
      'Personal AI agent with long-term memory, tool use, and computer control',
    start_url: '/',
    display: 'standalone',
    background_color: '#0f172a',
    theme_color: '#0f172a',
    icons: [
      {
        src: '/icon-192.png',
        sizes: '192x192',
        type: 'image/png',
        purpose: 'any',
      },
      {
        src: '/icon-512.png',
        sizes: '512x512',
        type: 'image/png',
        purpose: 'any',
      },
      {
        src: '/icon-512.png',
        sizes: '512x512',
        type: 'image/png',
        purpose: 'maskable',
      },
    ],
  }
}
