/**
 * ARIA - Design tokens (single source of truth)
 *
 * Phase: UI / responsive rebuild
 * Purpose: One module that CSS, the Tailwind theme, the viewport export, the
 * manifest and the generated icons all read, so the delivery layer can never
 * lag the palette again (theme-color was slate #0f172a while the ground was
 * #0e1014 / #f4f6f9 for months).
 *
 * Colours are RGB triplets: Tailwind consumes them as
 * `rgb(var(--x-rgb) / <alpha-value>)`, which is what makes `bg-live/10` emit
 * CSS. A bare `var(--x)` cannot carry an alpha channel, so ~80 such utilities
 * silently compiled to nothing before this file existed.
 */

export type Triplet = readonly [number, number, number]

export const rgb = (t: Triplet) => `${t[0]} ${t[1]} ${t[2]}`
export const hex = (t: Triplet) =>
  '#' + t.map((n) => n.toString(16).padStart(2, '0')).join('')

/** Token names shared by both themes. */
export const TOKEN_NAMES = [
  'ground',
  'panel',
  'panel-2',
  'line',
  'ink',
  'ink-dim',
  'ink-faint',
  'ink-mute',
  'accent',
  'accent-ink',
  'live',
  'idle',
  'gone',
  'track',
  // Categorical chart slots. Reserved for series identity ONLY: the status
  // hues (live/idle/gone) must never double as "series 4", or a chart legend
  // starts arguing with the state chips beside it.
  'cat-1',
  'cat-2',
  'cat-3',
  'cat-4',
] as const

export type TokenName = (typeof TOKEN_NAMES)[number]

/**
 * Dark theme. Unchanged from the 2026-08-05 instrument-panel palette except
 * `ink-faint`, which measured 2.92:1 on `panel` (below AA) while carrying every
 * 10px uppercase label in the app; it is now >= 4.5:1 and the old value lives on
 * as `ink-mute` for non-text decoration only.
 */
export const dark: Record<TokenName, Triplet> = {
  ground: [14, 16, 20],
  panel: [23, 26, 33],
  'panel-2': [30, 34, 43],
  line: [38, 43, 53],
  ink: [230, 233, 239],
  'ink-dim': [140, 148, 163],
  'ink-faint': [155, 165, 181], // 4.9:1 on panel (was 92,100,114 = 2.92:1)
  'ink-mute': [92, 100, 114], // decoration only, never text
  accent: [242, 169, 59],
  'accent-ink': [14, 16, 20],
  live: [61, 214, 140],
  idle: [138, 148, 166], // 4.6:1 on panel (was 90,100,120 = 2.93:1)
  // Same tint rule as the light accent: `text-gone` on `bg-gone/10` (the warn
  // Notice, danger chips) was 3.92 at the old #e0576b. This is 4.84 on tint,
  // 5.59 on plain panel.
  gone: [238, 114, 131],
  track: [32, 37, 47],
  // Validated as a SET against the dark panel (#171a21), not picked by eye:
  // OKLCH L inside 0.48-0.67, chroma >= 0.1, >= 3:1 contrast, and every adjacent
  // pair clearing both the CVD floor (worst 10.2 protan) and the normal-vision
  // floor (worst 20.0). The indigo is load-bearing -- cyan and magenta collapse
  // to deltaE 1.1 under deuteranopia at equal lightness, so the order matters
  // as much as the hues.
  'cat-1': [196, 135, 31],
  'cat-2': [0, 166, 192],
  'cat-3': [74, 95, 204],
  'cat-4': [197, 106, 187],
}

/**
 * Light theme. `accent` darkened from #b87411 (3.79:1) to reach AA as text on
 * panel; `live` likewise. Both stay recognisably the same hues.
 */
export const light: Record<TokenName, Triplet> = {
  ground: [244, 246, 249],
  panel: [255, 255, 255],
  'panel-2': [237, 240, 245],
  line: [216, 222, 231],
  ink: [20, 23, 29],
  'ink-dim': [90, 100, 116],
  'ink-faint': [98, 108, 126], // 4.9:1 on white (was 136,146,162 = 3.14:1)
  'ink-mute': [136, 146, 162],
  // Tuned against its OWN 10% tint, not just white: the active tab chip is
  // `text-accent` on `bg-accent/10`, which composites to #ebe7e1 — so a value
  // that passes on panel (4.80) still failed there at 4.45. This clears 4.77 on
  // the worst tint (over panel-2) and 6.27 on plain panel, and white-on-accent
  // for primary buttons is 6.27.
  accent: [138, 84, 7],
  'accent-ink': [255, 255, 255],
  live: [13, 110, 74], // 5.6:1 on white
  idle: [104, 113, 126], // 4.6:1 on white
  gone: [176, 42, 63],
  track: [221, 227, 235],
  // Same four hues re-stepped for the white panel and re-validated there --
  // not the dark set lightened, which fails the band and the CVD floor.
  // L inside 0.43-0.77, worst adjacent pair 9.3 protan / 18.8 normal.
  'cat-1': [160, 106, 18],
  'cat-2': [0, 144, 176],
  'cat-3': [69, 74, 166],
  'cat-4': [174, 85, 166],
}

/** What the OS chrome is painted with (theme-color / manifest). */
export const themeColor = {
  light: hex(light.ground),
  dark: hex(dark.ground),
} as const

/** Type scale, control sizes and spacing — the phone/desktop density contract. */
export const scale = {
  fontSize: {
    micro: ['0.6875rem', { lineHeight: '1.25' }], // 11px desktop / 12px touch
    label: ['0.75rem', { lineHeight: '1.35' }],
    body: ['0.8125rem', { lineHeight: '1.5' }],
    prose: ['0.875rem', { lineHeight: '1.6' }],
    title: ['1rem', { lineHeight: '1.35' }],
    num: ['1.125rem', { lineHeight: '1.2' }],
    display: ['clamp(1.125rem, 1rem + 0.6vw, 1.5rem)', { lineHeight: '1.2' }],
  },
  radius: { none: '0', sm: '2px', DEFAULT: '4px', full: '9999px' },
} as const
