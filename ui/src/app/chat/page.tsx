'use client'

import { useState, useEffect, useRef } from 'react'
import { apiClient } from '@/lib/api-client'
import type { Agent, Conversation, Message as MessageType } from '@/types'
import { Send, Loader2 } from 'lucide-react'

export default function ChatPage() {
  const [agents, setAgents] = useState<Agent[]>([])
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [currentConversation, setCurrentConversation] = useState<Conversation | null>(null)
  const [messages, setMessages] = useState<MessageType[]>([])
  const [input, setInput] = useState('')
  const [isStreaming, setIsStreaming] = useState(false)
  const [streamingContent, setStreamingContent] = useState('')
  const messagesEndRef = useRef<HTMLDivElement>(null)

  // Load conversations on mount
  useEffect(() => {
    loadAgents()
    loadConversations()
  }, [])

  // Auto-scroll to bottom
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, streamingContent])

  const loadAgents = async () => {
    try {
      const availableAgents = await apiClient.listAgents()
      setAgents(availableAgents)
    } catch (error) {
      console.error('Failed to load agents:', error)
    }
  }

  const loadConversations = async () => {
    try {
      const convos = await apiClient.listConversations(20)
      setConversations(convos as any[])

      // Load first conversation if exists
      if (convos.length > 0) {
        await loadConversation(convos[0].id)
      } else {
        // Create new conversation
        await createNewConversation()
      }
    } catch (error) {
      console.error('Failed to load conversations:', error)
    }
  }

  const loadConversation = async (id: string) => {
    try {
      const convo = await apiClient.getConversation(id)
      setCurrentConversation(convo)
      setMessages(convo.messages)
    } catch (error) {
      console.error('Failed to load conversation:', error)
    }
  }

  const createNewConversation = async () => {
    try {
      const convo = await apiClient.createConversation()
      setCurrentConversation(convo)
      setMessages([])
      setConversations(prev => [convo as any, ...prev])
    } catch (error) {
      console.error('Failed to create conversation:', error)
    }
  }

  const handleModeChange = async (agentSlug: string) => {
    if (!currentConversation || isStreaming) return

    try {
      const updated = await apiClient.switchConversationMode(currentConversation.id, agentSlug)
      setCurrentConversation(updated)
      setMessages(updated.messages)
      setConversations(prev =>
        prev.map(convo => (convo.id === updated.id ? { ...convo, ...updated } : convo)),
      )
    } catch (error) {
      console.error('Failed to switch mode:', error)
    }
  }

  const selectedAgentId = currentConversation?.active_agent_id || currentConversation?.agent_id || ''

  const [connectionError, setConnectionError] = useState<string | null>(null)

  const handleSendMessage = async () => {
    if (!input.trim() || !currentConversation || isStreaming) return

    const userMessage = input
    setInput('')
    setIsStreaming(true)
    setStreamingContent('')
    setConnectionError(null)

    const maxRetries = 3
    let attempt = 0
    let lastEventId: string | undefined

    while (attempt < maxRetries) {
      try {
        if (attempt > 0) {
          setConnectionError(`Reconnecting... (attempt ${attempt + 1}/${maxRetries})`)
          // Exponential backoff: 1s, 2s, 4s
          await new Promise(resolve => setTimeout(resolve, 1000 * Math.pow(2, attempt - 1)))
        }

        const chunks: string[] = []
        let streamError = false

        for await (const chunk of apiClient.streamMessage(
          currentConversation.id,
          userMessage,
          lastEventId,
        )) {
          setConnectionError(null)
          if (chunk.type === 'text' && chunk.content) {
            chunks.push(chunk.content)
            setStreamingContent(chunks.join(''))
          } else if (chunk.type === 'error') {
            console.error('Stream error:', chunk.error)
            streamError = true
            break
          }
          if (chunk.event_id) {
            lastEventId = chunk.event_id
          }
        }

        if (streamError) {
          throw new Error('Stream returned an error event')
        }

        // Reload conversation to get updated messages
        await loadConversation(currentConversation.id)
        setStreamingContent('')
        setConnectionError(null)
        break // Success — exit retry loop
      } catch (error) {
        attempt++
        console.error(`Stream attempt ${attempt} failed:`, error)
        if (attempt >= maxRetries) {
          setConnectionError('Connection lost. Please try again.')
        }
      }
    }

    setIsStreaming(false)
  }

  const handleKeyPress = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSendMessage()
    }
  }

  return (
    <div className="flex h-screen bg-gray-50 dark:bg-gray-900">
      {/* Sidebar */}
      <div className="w-64 bg-white dark:bg-gray-800 border-r border-gray-200 dark:border-gray-700 flex flex-col">
        <div className="p-4 border-b border-gray-200 dark:border-gray-700">
          <h1 className="text-xl font-bold">ARIA</h1>
        </div>

        <div className="flex-1 overflow-y-auto p-2">
          <button
            onClick={createNewConversation}
            className="w-full px-4 py-2 mb-2 bg-blue-600 text-white rounded hover:bg-blue-700"
          >
            + New Chat
          </button>

          <div className="space-y-1">
            {conversations.map((convo) => (
              <button
                key={convo.id}
                onClick={() => loadConversation(convo.id)}
                className={`w-full px-4 py-2 text-left rounded hover:bg-gray-100 dark:hover:bg-gray-700 truncate ${
                  currentConversation?.id === convo.id
                    ? 'bg-gray-100 dark:bg-gray-700'
                    : ''
                }`}
              >
                {convo.title}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Main Chat Area */}
      <div className="flex-1 flex flex-col">
        <div className="border-b border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-6 py-3">
          <div className="flex items-center justify-between gap-4">
            <div>
              <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
                {currentConversation?.title || 'Conversation'}
              </h2>
              <p className="text-xs text-gray-500 dark:text-gray-400">
                Active mode: {agents.find(agent => agent.id === selectedAgentId)?.mode_metadata?.icon ? `${agents.find(agent => agent.id === selectedAgentId)?.mode_metadata?.icon} ` : ''}{agents.find(agent => agent.id === selectedAgentId)?.name || 'Default'}
              </p>
            </div>

            <label className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-300">
              <span>Mode</span>
              <select
                value={selectedAgentId}
                onChange={(e) => {
                  const nextAgent = agents.find(agent => agent.id === e.target.value)
                  if (nextAgent) {
                    void handleModeChange(nextAgent.slug)
                  }
                }}
                disabled={!currentConversation || isStreaming || agents.length === 0}
                className="rounded border border-gray-300 bg-white px-3 py-2 text-sm text-gray-900 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100"
              >
                {agents.map(agent => (
                  <option key={agent.id} value={agent.id}>
                    {agent.mode_metadata?.icon ? `${agent.mode_metadata.icon} ` : ''}{agent.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
        </div>

        {/* Connection error banner */}
        {connectionError && (
          <div className="px-6 py-2 bg-yellow-900/50 border-b border-yellow-700 text-yellow-200 text-sm text-center">
            {connectionError}
          </div>
        )}

        {/* Messages */}
        <div className="flex-1 overflow-y-auto p-6 space-y-4">
          {messages.map((msg) => (
            <div
              key={msg.id}
              className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
            >
              <div
                className={`max-w-2xl px-4 py-2 rounded-lg ${
                  msg.role === 'user'
                    ? 'bg-blue-600 text-white'
                    : 'bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700'
                }`}
              >
                <div className="whitespace-pre-wrap">{msg.content}</div>
                {msg.tool_calls && msg.tool_calls.length > 0 && (
                  <div className="mt-2 text-sm opacity-75">
                    🔧 Used {msg.tool_calls.length} tool(s)
                  </div>
                )}
              </div>
            </div>
          ))}

          {/* Streaming message */}
          {isStreaming && streamingContent && (
            <div className="flex justify-start">
              <div className="max-w-2xl px-4 py-2 rounded-lg bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700">
                <div className="whitespace-pre-wrap">{streamingContent}</div>
                <div className="mt-2 flex items-center gap-2 text-sm opacity-75">
                  <Loader2 className="w-4 h-4 animate-spin" />
                  Thinking...
                </div>
              </div>
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        {/* Input */}
        <div className="border-t border-gray-200 dark:border-gray-700 p-4 bg-white dark:bg-gray-800">
          <div className="max-w-4xl mx-auto flex gap-2">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyPress}
              placeholder="Type your message..."
              className="flex-1 px-4 py-2 border border-gray-300 dark:border-gray-600 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-600 dark:bg-gray-700 resize-none"
              rows={1}
              disabled={isStreaming}
            />
            <button
              onClick={handleSendMessage}
              disabled={!input.trim() || isStreaming}
              className="px-6 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
            >
              {isStreaming ? (
                <Loader2 className="w-5 h-5 animate-spin" />
              ) : (
                <Send className="w-5 h-5" />
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
