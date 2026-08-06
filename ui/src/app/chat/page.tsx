'use client'

import { useState, useEffect, useRef } from 'react'
import { apiClient } from '@/lib/api-client'
import type { Agent, Conversation, Message as MessageType } from '@/types'
import { Send, Loader2 } from 'lucide-react'
import { AppShell } from '@/components/AppShell'

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

  // h-[100dvh] rather than h-screen: on mobile, 100vh exceeds the visible area
  // (browser chrome), which would push the composer off-screen.
  return (
    <AppShell area="Converse" flush>
      {/* Fills whatever the shell leaves; the shell owns the viewport height,
          so this stays correct on mobile where the nav stacks above. */}
      <div className="flex h-full min-h-0 bg-ground">
      {/* Conversation list — desktop only; below md the header carries a picker */}
      <div className="hidden w-64 bg-panel border-r border-line md:flex md:flex-col">

        <div className="flex-1 overflow-y-auto p-2">
          <button
            onClick={createNewConversation}
            className="w-full px-4 py-2 mb-2 bg-accent text-ink rounded hover:bg-accent"
          >
            + New Chat
          </button>

          <div className="space-y-1">
            {conversations.map((convo) => (
              <button
                key={convo.id}
                onClick={() => loadConversation(convo.id)}
                className={`w-full px-4 py-2 text-left rounded hover:bg-panel-2 dark:hover:bg-panel-2 truncate ${
                  currentConversation?.id === convo.id
                    ? 'bg-panel-2 dark:bg-panel-2'
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
      <div className="flex-1 flex flex-col min-w-0">
        <div className="border-b border-line bg-panel px-4 py-3 sm:px-6">
          <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center sm:justify-between sm:gap-4">
            <div className="min-w-0 flex-1">
              <h2 className="truncate text-sm font-semibold text-ink">
                {currentConversation?.title || 'Conversation'}
              </h2>
              <p className="truncate text-xs text-ink-dim">
                Active mode: {agents.find(agent => agent.id === selectedAgentId)?.mode_metadata?.icon ? `${agents.find(agent => agent.id === selectedAgentId)?.mode_metadata?.icon} ` : ''}{agents.find(agent => agent.id === selectedAgentId)?.name || 'Default'}
              </p>
            </div>

            <label className="flex min-w-0 items-center gap-2 text-sm text-ink-dim sm:w-auto">
              <span className="shrink-0">Mode</span>
              <select
                value={selectedAgentId}
                onChange={(e) => {
                  const nextAgent = agents.find(agent => agent.id === e.target.value)
                  if (nextAgent) {
                    void handleModeChange(nextAgent.slug)
                  }
                }}
                disabled={!currentConversation || isStreaming || agents.length === 0}
                className="min-w-0 flex-1 truncate rounded border border-line bg-panel px-3 py-2 text-sm text-ink dark:bg-panel-2 sm:max-w-[16rem]"
              >
                {agents.map(agent => (
                  <option key={agent.id} value={agent.id}>
                    {agent.mode_metadata?.icon ? `${agent.mode_metadata.icon} ` : ''}{agent.name}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {/* Mobile conversation switcher — stands in for the hidden sidebar */}
          <div className="mt-3 flex items-center gap-2 md:hidden">
            <select
              value={currentConversation?.id || ''}
              onChange={(e) => {
                if (e.target.value) {
                  void loadConversation(e.target.value)
                }
              }}
              disabled={isStreaming}
              className="min-w-0 flex-1 rounded border border-line bg-panel px-3 py-2 text-sm text-ink dark:bg-panel-2"
            >
              {/* A freshly created conversation is prepended to `conversations`, but
                  guard anyway so the picker never shows an unrelated title */}
              {currentConversation && !conversations.some(convo => convo.id === currentConversation.id) && (
                <option value={currentConversation.id}>{currentConversation.title}</option>
              )}
              {conversations.map((convo) => (
                <option key={convo.id} value={convo.id}>
                  {convo.title}
                </option>
              ))}
            </select>
            <button
              onClick={createNewConversation}
              className="shrink-0 rounded bg-accent px-4 py-2 text-sm text-ink hover:bg-accent"
            >
              + New
            </button>
          </div>
        </div>

        {/* Connection error banner */}
        {connectionError && (
          <div className="px-4 py-2 sm:px-6 bg-accent/10 border-b border-accent text-accent text-sm text-center">
            {connectionError}
          </div>
        )}

        {/* Messages */}
        <div className="flex-1 overflow-y-auto p-4 sm:p-6 space-y-4">
          {messages.map((msg) => (
            <div
              key={msg.id}
              className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
            >
              <div
                className={`max-w-[85%] sm:max-w-2xl px-4 py-2 rounded-lg ${
                  msg.role === 'user'
                    ? 'bg-accent text-ink'
                    : 'bg-panel dark:bg-panel border border-line dark:border-line'
                }`}
              >
                <div className="whitespace-pre-wrap break-words">{msg.content}</div>
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
              <div className="max-w-[85%] sm:max-w-2xl px-4 py-2 rounded-lg bg-panel border border-line">
                <div className="whitespace-pre-wrap break-words">{streamingContent}</div>
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
        <div className="border-t border-line p-4 bg-panel">
          <div className="max-w-4xl mx-auto flex gap-2">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyPress}
              placeholder="Type your message..."
              className="flex-1 min-w-0 px-4 py-3 sm:py-2 border border-line rounded-lg focus:outline-none focus:ring-2 focus:ring-accent dark:bg-panel-2 resize-none"
              rows={1}
              disabled={isStreaming}
            />
            <button
              onClick={handleSendMessage}
              disabled={!input.trim() || isStreaming}
              className="shrink-0 px-5 py-3 sm:px-6 sm:py-2 bg-accent text-ink rounded-lg hover:bg-accent disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
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
    </AppShell>
  )
}
