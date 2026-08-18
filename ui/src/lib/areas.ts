/**
 * ARIA - Information architecture (single source)
 *
 * The areas were declared in three places that had already drifted apart
 * (AppShell's rail listed six, the old landing page listed four with different
 * blurbs). Rail, bottom tab bar, More sheet, breadcrumb and metadata all read
 * this file now.
 *
 * `phoneTab` picks the five thumb tabs; everything else lives behind More.
 * Promoting an area back to a tab is a one-flag change.
 */
import type { LucideIcon } from 'lucide-react'
import {
  Inbox,
  MessagesSquare,
  Activity,
  Cpu,
  Library,
  Sparkles,
  Gauge,
  MoreHorizontal,
} from 'lucide-react'

export type Area = {
  href: string
  label: string
  blurb: string
  icon: LucideIcon
  /** Shown as one of the five bottom tabs on a phone. */
  phoneTab: boolean
  /** Extra path prefixes that should light this area up. */
  alsoMatches?: string[]
}

export const AREAS: Area[] = [
  {
    href: '/inbox',
    label: 'Inbox',
    blurb: 'waiting on you',
    icon: Inbox,
    phoneTab: true,
  },
  {
    href: '/supervise',
    label: 'Supervise',
    blurb: 'projects, shells, sessions',
    icon: Activity,
    phoneTab: true,
    alsoMatches: ['/cockpit', '/dashboard/shells'],
  },
  {
    href: '/operate',
    label: 'Operate',
    blurb: 'models, services, benchmarks',
    icon: Cpu,
    phoneTab: true,
    alsoMatches: ['/dashboard/benchmarks'],
  },
  {
    href: '/know',
    label: 'Know',
    blurb: 'memory, research, projects',
    icon: Library,
    phoneTab: true,
    alsoMatches: ['/dashboard'],
  },
  {
    href: '/converse',
    label: 'Converse',
    blurb: 'chat, voice, history',
    icon: MessagesSquare,
    phoneTab: false,
    alsoMatches: ['/chat'],
  },
  {
    href: '/autonomy',
    label: 'Autonomy',
    blurb: 'awareness, dreams',
    icon: Sparkles,
    phoneTab: false,
  },
]

/** Extra destinations that live only in the More sheet. */
export const MORE_LINKS: Area[] = [
  {
    href: '/operate/benchmarks',
    label: 'Benchmarks',
    blurb: 'suites, targets, runs',
    icon: Gauge,
    phoneTab: false,
  },
]

export const MORE_ICON = MoreHorizontal

/** Most-specific-match wins, so /supervise/shells does not also light /supervise's parent. */
export function matchArea(pathname: string): Area | undefined {
  const candidates = AREAS.flatMap((a) =>
    [a.href, ...(a.alsoMatches ?? [])].map((prefix) => ({ area: a, prefix }))
  ).filter(({ prefix }) => pathname === prefix || pathname.startsWith(prefix + '/'))
  if (candidates.length === 0) return undefined
  candidates.sort((a, b) => b.prefix.length - a.prefix.length)
  return candidates[0].area
}

export const PHONE_TABS = AREAS.filter((a) => a.phoneTab)
export const MORE_AREAS = [...AREAS.filter((a) => !a.phoneTab), ...MORE_LINKS]
