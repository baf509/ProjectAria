/** Preserve the deployed build-identity endpoint across the Flash Next rollout. */
import buildInfo from '@/build-info.json'

export const dynamic = 'force-static'

export function GET() {
  return Response.json(buildInfo, { headers: { 'Cache-Control': 'no-store' } })
}
