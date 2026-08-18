'use client'

/**
 * ARIA - Know status bridge
 *
 * The /know/* segments share one AppShell, owned by know/layout.tsx, but the
 * counters in the TopBar status strip belong to whichever segment is mounted
 * (contract rule 10: the page header lives in the shell, not in a page-level
 * hero). A segment cannot pass props upward through the App Router, so this
 * tiny context carries its stats to the layout instead.
 *
 * Stats are plain serialisable values (not ReactNodes) and the effect keys on
 * their JSON: setting a ReactNode in state on every render is a re-render loop
 * waiting to happen.
 */
import { createContext, useContext, useEffect, ReactNode } from 'react'

export type KnowStat = { label: string; value: string | number; tone?: 'default' | 'warn' | 'ok' }

const KnowStatusContext = createContext<(stats: KnowStat[]) => void>(() => {})

export function KnowStatusProvider({
  onStats,
  children,
}: {
  onStats: (stats: KnowStat[]) => void
  children: ReactNode
}) {
  return <KnowStatusContext.Provider value={onStats}>{children}</KnowStatusContext.Provider>
}

/** Publish this segment's counters to the shared TopBar; clears on unmount. */
export function useKnowStats(stats: KnowStat[]) {
  const set = useContext(KnowStatusContext)
  const key = JSON.stringify(stats)
  useEffect(() => {
    set(JSON.parse(key) as KnowStat[])
    return () => set([])
  }, [key, set])
}
