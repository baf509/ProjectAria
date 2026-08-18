'use client'

/**
 * ARIA - /operate/servers/[slug]: model-server detail.
 * Selection lives in the URL, so it survives reload, Back works on the phone,
 * and a link from a Signal alert lands here directly.
 */
import { useParams } from 'next/navigation'
import { ServerDetail } from '@/features/operate/ServerDetail'

export default function ServerDetailPage() {
  const params = useParams<{ slug: string }>()
  return <ServerDetail slug={decodeURIComponent(params.slug)} />
}
