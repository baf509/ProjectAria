'use client'

/**
 * ARIA - /operate/services/[slug]: non-LLM service detail.
 */
import { useParams } from 'next/navigation'
import { ServiceDetail } from '@/features/operate/ServiceDetail'

export default function ServiceDetailPage() {
  const params = useParams<{ slug: string }>()
  return <ServiceDetail slug={decodeURIComponent(params.slug)} />
}
