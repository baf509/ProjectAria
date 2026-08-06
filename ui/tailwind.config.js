/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './src/pages/**/*.{js,ts,jsx,tsx,mdx}',
    './src/components/**/*.{js,ts,jsx,tsx,mdx}',
    './src/app/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  theme: {
    extend: {
      // Design tokens from globals.css. Both themes are defined there, so a
      // component written against `bg-panel` is correct in light and dark
      // without any theme branching in the component itself.
      colors: {
        ground: 'var(--ground)',
        panel: 'var(--panel)',
        'panel-2': 'var(--panel-2)',
        line: 'var(--line)',
        ink: 'var(--ink)',
        'ink-dim': 'var(--ink-dim)',
        'ink-faint': 'var(--ink-faint)',
        accent: 'var(--accent)',
        'accent-ink': 'var(--accent-ink)',
        live: 'var(--live)',
        idle: 'var(--idle)',
        gone: 'var(--gone)',
        track: 'var(--track)',

        // Retained: the pre-redesign pages still reference these.
        primary: {
          50: '#f0f9ff',
          100: '#e0f2fe',
          200: '#bae6fd',
          300: '#7dd3fc',
          400: '#38bdf8',
          500: '#0ea5e9',
          600: '#0284c7',
          700: '#0369a1',
          800: '#075985',
          900: '#0c4a6e',
        },
      },
      fontFamily: {
        // Mono leads: this is an instrument panel, and every label and number
        // belongs in the vernacular of one. Sans is for prose only.
        mono: ['ui-monospace', 'SF Mono', 'Cascadia Mono', 'Roboto Mono', 'Menlo', 'Consolas', 'monospace'],
        sans: ['ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
      },
    },
  },
  plugins: [],
}
