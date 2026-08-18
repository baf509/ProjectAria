const plugin = require('tailwindcss/plugin')

/**
 * The theme REPLACES rather than extends. That is the enforcement mechanism for
 * one visual language: `font-serif`, `rounded-3xl`, `fuchsia-*`, `primary-*`,
 * `text-xs`, `dark:` and friends simply do not compile, so the pre-redesign
 * vocabulary cannot come back by habit.
 *
 * A `theme.legacy.js` shim carried those classes while routes were being
 * refitted. Every route is refitted, no source file references them, so the
 * shim is gone — which is the definition of the rebuild being finished.
 *
 * Colours are `rgb(var(--x-rgb) / <alpha-value>)` so opacity modifiers work —
 * with the old bare `var(--x)` tokens, ~80 `bg-live/40`-style utilities emitted
 * no CSS at all (verified against the deployed stylesheet).
 */
const tokenColor = (name) => `rgb(var(--${name}-rgb) / <alpha-value>)`

const colors = {
  transparent: 'transparent',
  current: 'currentColor',
  inherit: 'inherit',
  ground: tokenColor('ground'),
  panel: tokenColor('panel'),
  'panel-2': tokenColor('panel-2'),
  line: tokenColor('line'),
  ink: tokenColor('ink'),
  'ink-dim': tokenColor('ink-dim'),
  'ink-faint': tokenColor('ink-faint'),
  'ink-mute': tokenColor('ink-mute'),
  accent: tokenColor('accent'),
  'accent-ink': tokenColor('accent-ink'),
  live: tokenColor('live'),
  idle: tokenColor('idle'),
  gone: tokenColor('gone'),
  track: tokenColor('track'),
}

/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './src/pages/**/*.{js,ts,jsx,tsx,mdx}',
    './src/components/**/*.{js,ts,jsx,tsx,mdx}',
    './src/app/**/*.{js,ts,jsx,tsx,mdx}',
    './src/features/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    colors,
    fontFamily: {
      // Mono leads: this is an instrument panel, and every label and number
      // belongs in the vernacular of one. Sans is for prose only.
      mono: ['ui-monospace', 'SF Mono', 'Cascadia Mono', 'Roboto Mono', 'Menlo', 'Consolas', 'monospace'],
      sans: ['ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
    },
    fontSize: {
      micro: ['var(--fs-micro)', { lineHeight: '1.25' }],
      label: ['var(--fs-label)', { lineHeight: '1.35' }],
      body: ['var(--fs-body)', { lineHeight: '1.5' }],
      prose: ['var(--fs-prose)', { lineHeight: '1.6' }],
      title: ['var(--fs-title)', { lineHeight: '1.35' }],
      num: ['var(--fs-num)', { lineHeight: '1.2' }],
      display: ['clamp(1.125rem, 1rem + 0.6vw, 1.5rem)', { lineHeight: '1.2' }],
    },
    borderRadius: {
      none: '0',
      sm: '2px',
      DEFAULT: '4px',
      full: '9999px',
    },
    extend: {
      minHeight: {
        control: 'var(--control-h)',
        row: 'var(--row-h)',
        touch: '2.75rem',
        tabbar: 'var(--tabbar-h)',
      },
      minWidth: { control: 'var(--control-h)', touch: '2.75rem' },
      height: { control: 'var(--control-h)', row: 'var(--row-h)', tabbar: 'var(--tabbar-h)', topbar: 'var(--topbar-h)' },
      spacing: {
        gutter: 'var(--gutter)',
        gap: 'var(--gap)',
        sat: 'var(--sat)',
        sab: 'var(--sab)',
        tabbar: 'var(--tabbar-h)',
      },
      maxWidth: { page: '96rem', prose: '78ch' },
      zIndex: { nav: '40', sheet: '50', toast: '60' },
    },
  },
  plugins: [
    require('@tailwindcss/container-queries'),
    plugin(({ addVariant }) => {
      // Device capability, not viewport width: `coarse:` is the phone/tablet
      // step-up, `fine:` the mouse, `standalone:` the installed PWA.
      addVariant('coarse', '@media (pointer: coarse)')
      addVariant('fine', '@media (pointer: fine)')
      addVariant('hoverable', '@media (hover: hover)')
      addVariant('standalone', '@media (display-mode: standalone)')
    }),
  ],
}
