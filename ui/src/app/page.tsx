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
        if (health.status === 'healthy') {
          setApiStatus('healthy')
          // Redirect to chat after brief delay
          setTimeout(() => {
            router.push('/chat')
          }, 1000)
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
    <main className="flex min-h-screen flex-col items-center justify-center p-24">
      <div className="text-center">
        <h1 className="text-6xl font-bold mb-4 bg-gradient-to-r from-blue-600 to-cyan-500 bg-clip-text text-transparent">
          ARIA
        </h1>
        <p className="text-xl text-gray-600 dark:text-gray-400 mb-8">
          Local AI Agent Platform
        </p>

        {apiStatus === 'checking' && (
          <div className="flex flex-col items-center gap-4">
            <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600"></div>
            <p className="text-sm text-gray-500">Connecting to API...</p>
          </div>
        )}

        {apiStatus === 'healthy' && (
          <div className="flex flex-col items-center gap-4">
            <div className="text-green-500 text-5xl">✓</div>
            <p className="text-sm text-gray-500">Connected! Redirecting...</p>
          </div>
        )}

        {apiStatus === 'error' && (
          <div className="flex flex-col items-center gap-4">
            <div className="text-red-500 text-5xl">✗</div>
            <p className="text-sm text-red-500">Cannot connect to ARIA API</p>
            <p className="text-xs text-gray-500">
              Make sure the API is running at {process.env.NEXT_PUBLIC_API_URL}
            </p>
            <button
              onClick={() => window.location.reload()}
              className="mt-4 px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700"
            >
              Retry
            </button>
          </div>
        )}
      </div>
    </main>
  )
}
