'use client'

/**
 * Theme (system/light/dark) and density (auto/compact/comfortable).
 *
 * Density is normally derived from `pointer: coarse`, which is right for a
 * phone and a mouse but wrong for hybrids — an iPad with a Magic Keyboard
 * reports `fine` and gets the compact laptop density (the intended default,
 * confirmed with Ben), while a touchscreen laptop gets the big one. This is the
 * escape hatch, persisted so it survives a reload.
 */
import { useEffect, useState } from 'react'

type Theme = 'system' | 'light' | 'dark'
type Density = 'auto' | 'compact' | 'comfortable'

const THEME_KEY = 'aria-theme'
const DENSITY_KEY = 'aria-density'

function apply(theme: Theme, density: Density) {
  const root = document.documentElement
  if (theme === 'system') root.removeAttribute('data-theme')
  else root.setAttribute('data-theme', theme)
  if (density === 'auto') root.removeAttribute('data-density')
  else root.setAttribute('data-density', density)
}

export function ThemeDensityControls() {
  const [theme, setTheme] = useState<Theme>('system')
  const [density, setDensity] = useState<Density>('auto')

  useEffect(() => {
    const t = (localStorage.getItem(THEME_KEY) as Theme) || 'system'
    const d = (localStorage.getItem(DENSITY_KEY) as Density) || 'auto'
    setTheme(t)
    setDensity(d)
  }, [])

  function choose<T extends string>(key: string, value: T, set: (v: T) => void) {
    set(value)
    localStorage.setItem(key, value)
    const t = key === THEME_KEY ? (value as Theme) : theme
    const d = key === DENSITY_KEY ? (value as Density) : density
    apply(t, d)
  }

  const group = (
    label: string,
    value: string,
    options: string[],
    key: string,
    set: (v: never) => void
  ) => (
    <div className="flex min-w-0 flex-wrap items-center gap-2">
      <span className="text-micro uppercase tracking-[0.08em] text-ink-faint">{label}</span>
      <div className="flex gap-1">
        {options.map((o) => (
          <button
            key={o}
            onClick={() => choose(key, o as never, set)}
            aria-pressed={value === o}
            className={`min-h-control rounded-sm border px-2.5 text-micro uppercase tracking-[0.08em] ${
              value === o ? 'border-accent bg-accent/10 text-accent' : 'border-line text-ink-dim hover:text-ink'
            }`}
          >
            {o}
          </button>
        ))}
      </div>
    </div>
  )

  return (
    <div className="flex flex-col gap-3">
      {group('Theme', theme, ['system', 'light', 'dark'], THEME_KEY, setTheme as (v: never) => void)}
      {group('Density', density, ['auto', 'compact', 'comfortable'], DENSITY_KEY, setDensity as (v: never) => void)}
    </div>
  )
}
