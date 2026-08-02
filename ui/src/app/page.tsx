'use client'

import { useRouter } from 'next/navigation'
import { useEffect, useState } from 'react'
import { apiClient } from '@/lib/api-client'

export default function Home() {
  const router = useRouter()
  const [isLoading, setIsLoading] = useState(true)
  const [apiStatus, setApiStatus] = useState<'checking' | 'healthy' | 'error'>('checking')

  useEffect(() => {
    // Check API health and redirect to chat
    const checkAPI = async () => {
      try {
        const health = await apiClient.checkHealth()
        if (health.status === 'healthy' || health.status === 'degraded') {
          setApiStatus('healthy')
          setIsLoading(false)
        } else {
          setApiStatus('error')
          setIsLoading(false)
        }
      } catch (error) {
        setApiStatus('error')
        setIsLoading(false)
      }
    }

    checkAPI()
  }, [router])

  return (
    <main className="relative flex min-h-screen flex-col justify-center overflow-hidden bg-stone-950 px-4 py-12 text-stone-100 sm:px-6 sm:py-20">
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_top_left,_rgba(245,158,11,0.18),_transparent_35%),radial-gradient(circle_at_bottom_right,_rgba(14,165,233,0.16),_transparent_30%)]" />
      <div className="relative mx-auto max-w-5xl">
        <div className="mb-8 max-w-3xl sm:mb-12">
          <p className="mb-4 text-[10px] uppercase tracking-[0.25em] text-amber-400 sm:text-xs sm:tracking-[0.35em]">
            Local Agent Platform
          </p>
          <h1 className="mb-4 font-serif text-4xl tracking-tight text-stone-50 sm:text-6xl">
          ARIA
          </h1>
          <p className="text-base text-stone-400 sm:text-lg">
            Chat, memory, research, coding sessions, and infrastructure controls in one local-first surface.
          </p>
        </div>

        {apiStatus === 'checking' && (
          <div className="flex flex-col items-start gap-4">
            <div className="h-12 w-12 animate-spin rounded-full border-b-2 border-amber-400"></div>
            <p className="text-sm text-stone-400">Connecting to API...</p>
          </div>
        )}

        {apiStatus === 'healthy' && (
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
            <button
              onClick={() => router.push('/chat')}
              className="rounded-3xl border border-stone-800 bg-stone-900 p-5 text-left transition hover:border-amber-400 sm:p-6"
            >
              <div className="mb-3 text-sm uppercase tracking-wide text-amber-400">Chat</div>
              <div className="text-xl font-semibold text-stone-50 sm:text-2xl">Open Conversation UI</div>
            </button>
            <button
              onClick={() => router.push('/cockpit')}
              className="rounded-3xl border border-stone-800 bg-stone-900 p-5 text-left transition hover:border-amber-400 sm:p-6"
            >
              <div className="mb-3 text-sm uppercase tracking-wide text-amber-400">Cockpit</div>
              <div className="text-xl font-semibold text-stone-50 sm:text-2xl">Coherence Project Cockpit</div>
            </button>
            <button
              onClick={() => router.push('/dashboard')}
              className="rounded-3xl border border-stone-800 bg-stone-900 p-5 text-left transition hover:border-sky-400 sm:p-6"
            >
              <div className="mb-3 text-sm uppercase tracking-wide text-sky-400">Dashboard</div>
              <div className="text-xl font-semibold text-stone-50 sm:text-2xl">Manage Memory, Research, Usage</div>
            </button>
            <button
              onClick={() => router.push('/dashboard/shells')}
              className="rounded-3xl border border-stone-800 bg-stone-900 p-5 text-left transition hover:border-fuchsia-400 sm:p-6"
            >
              <div className="mb-3 text-sm uppercase tracking-wide text-fuchsia-400">Shells</div>
              <div className="text-xl font-semibold text-stone-50 sm:text-2xl">Browse ARIA Shells</div>
            </button>
            <div className="rounded-3xl border border-stone-800 bg-stone-900 p-5 sm:p-6">
              <div className="mb-3 text-sm uppercase tracking-wide text-emerald-400">Status</div>
              <div className="text-xl font-semibold text-stone-50 sm:text-2xl">API Connected</div>
              <p className="mt-2 text-sm text-stone-400">The backend is healthy and ready.</p>
            </div>
          </div>
        )}

        {apiStatus === 'error' && (
          <div className="flex flex-col items-start gap-4">
            <div className="text-5xl text-red-500">✗</div>
            <p className="text-sm text-red-400">Cannot connect to ARIA API</p>
            {/* The API URL is arbitrary-length config — wrap it so it can't widen the page. */}
            <p className="max-w-full break-words text-xs text-stone-500">
              Make sure the API is running at {process.env.NEXT_PUBLIC_API_URL}
            </p>
            <button
              onClick={() => window.location.reload()}
              className="mt-4 rounded-full bg-amber-400 px-4 py-2 text-sm font-medium text-stone-950 hover:bg-amber-300"
            >
              Retry
            </button>
          </div>
        )}
      </div>
    </main>
  )
}
