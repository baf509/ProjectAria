/**
 * ARIA - agent icon resolution
 *
 * `mode_metadata.icon` is a LUCIDE IDENTIFIER ('code', 'search'), not a glyph.
 * The old chat page interpolated it into the label, so the agent <select> read
 * "code Pi Coding Agent (Ridge)". Resolve it to a component; unknown or absent
 * identifiers fall back to a generic bot mark rather than leaking the string.
 */
import type { LucideIcon } from 'lucide-react'
import { Bot, Code, Search, Sparkles, Wrench } from 'lucide-react'
import type { Agent } from '@/lib/api/types'

const ICONS: Record<string, LucideIcon> = {
  code: Code,
  search: Search,
  sparkles: Sparkles,
  wrench: Wrench,
}

export function agentIcon(agent?: Agent | null): LucideIcon {
  const key = agent?.mode_metadata?.icon
  return (key && ICONS[key]) || Bot
}

/** Only agents a conversation can actually use; `aria` is disabled by design. */
export function enabledAgents(agents: Agent[] | undefined): Agent[] {
  return (agents ?? []).filter((a) => a.enabled !== false)
}

export function agentById(agents: Agent[] | undefined, id?: string | null): Agent | undefined {
  if (!id) return undefined
  return (agents ?? []).find((a) => a.id === id)
}
