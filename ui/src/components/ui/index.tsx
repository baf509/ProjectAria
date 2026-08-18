/**
 * ARIA - UI primitive barrel.
 *
 * Split into primitives.tsx (server-safe) and controls.tsx (client) — importing
 * from here is fine in a client component; a Server Component should import
 * from './primitives' directly so it does not pull the client bundle in.
 */
export * from './primitives'
export * from './controls'

// Layout primitives are re-exported so existing pages that imported ScrollX
// from '@/components/ui' keep working during the refit.
export { ScrollX, Stack, Cluster, Grid, Columns, Row } from '../layout'
