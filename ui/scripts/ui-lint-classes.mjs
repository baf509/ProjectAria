/**
 * ARIA - class/pattern ban list
 *
 * The Tailwind theme replacement stops most of the old vocabulary from
 * COMPILING, but a few things still would (arbitrary sizes, viewport
 * arithmetic, raw fetch/setInterval) — and `theme.legacy.js` deliberately keeps
 * the pre-redesign palette alive for routes that have not been refitted yet.
 * This lint is what stops those from spreading: it applies only to directories
 * listed in REFITTED_DIRS, and that list only ever grows.
 *
 * Exit code 1 on any violation; run from `npm run lint` and `make ui-check`.
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

/** Directories held to the rebuilt contract. Add each area as it is refitted. */
const REFITTED_DIRS = [
  'src/components',
  'src/lib',
  'src/design',
  'src/features',
  'src/app/inbox',
  'src/app/supervise',
  'src/app/operate',
  'src/app/converse',
  'src/app/know',
  'src/app/autonomy',
  'src/app/layout.tsx',
  'src/app/manifest.ts',
  'src/app/error.tsx',
  'src/app/not-found.tsx',
]

/**
 * Files still awaiting their refit. This list only ever SHRINKS; when it is
 * empty the rebuild is done and `theme.legacy.js` can be deleted.
 */
const PENDING_REFIT = [
  // Empty: every pre-rebuild file has been refitted or deleted. Adding to this
  // list again means something regressed.
]

// Rules about CSS classes only make sense where classes can appear. Applying
// them to plain .ts modules produces false positives — `dark:` is also how you
// write an object key (src/design/tokens.ts holds the dark palette).
const CLASS_FILE = /\.(tsx|css)$/

const RULES = [
  { re: /text-\[\d+(\.\d+)?px\]/, classOnly: true, msg: 'arbitrary font size — use the scale (text-micro|label|body|prose|title|num|display)' },
  { re: /\b(min-h|max-h|h|w)-\[calc\(100vh/, classOnly: true, msg: 'viewport arithmetic — use AppShell flush + flex-1 min-h-0' },
  { re: /\b(min-h|max-h|h)-\[\d+vh\]/, classOnly: true, msg: 'vh height — use dvh via the shell, or flex-1 min-h-0' },
  { re: /\bfuchsia-\d|\bprimary-\d|\bslate-\d|\bemerald-\d|\bindigo-\d/, classOnly: true, msg: 'raw palette colour — use design tokens' },
  { re: /\bfont-serif\b/, classOnly: true, msg: 'serif type — the instrument panel is mono + sans' },
  { re: /\brounded-(lg|xl|2xl|3xl)\b/, classOnly: true, msg: 'legacy radius — use rounded / rounded-sm / rounded-full' },
  { re: /\bdark:/, classOnly: true, msg: 'dark: variant — both themes are defined by tokens, components never branch' },
  { re: /\boverflow-x-hidden\b/, classOnly: true, msg: 'overflow mask — contain width structurally instead' },
  // 2.75:1 — it exists for hairlines and dots. Used as a text colour it fails
  // AA every time, and axe only catches it on a route the gate happens to
  // render with that data. Naming it here moves the failure to lint time.
  { re: /\btext-ink-mute\b/, classOnly: true, msg: 'text-ink-mute is a decoration token, not a text colour — use text-ink-faint' },
  { re: /NEXT_PUBLIC_API/, msg: 'build-time API config — read it server-side at request time' },
]

/** Rules that apply only outside the data layer. */
const APP_ONLY_RULES = [
  { re: /\bsetInterval\s*\(/, msg: 'hand-rolled polling — use useResource(key, {tier})' },
  { re: /(?<!\/\/.*)\bfetch\s*\(/, msg: 'direct fetch — use api()/useResource from src/lib' },
  { re: /process\.env\./, msg: 'env access outside src/lib/server — configuration is server-side' },
]

const DATA_LAYER = ['src/lib/http.ts', 'src/lib/stream.ts', 'src/lib/swr.ts', 'src/lib/server', 'src/lib/api', 'src/app/api']

function* walk(target) {
  const abs = path.join(root, target)
  if (!fs.existsSync(abs)) return
  if (fs.statSync(abs).isFile()) {
    yield target
    return
  }
  for (const entry of fs.readdirSync(abs, { withFileTypes: true })) {
    const rel = path.join(target, entry.name)
    if (entry.isDirectory()) yield* walk(rel)
    else if (/\.(tsx?|css)$/.test(entry.name)) yield rel
  }
}

let violations = 0
for (const dir of REFITTED_DIRS) {
  for (const rel of walk(dir)) {
    if (PENDING_REFIT.includes(rel)) continue
    const text = fs.readFileSync(path.join(root, rel), 'utf8')
    const inDataLayer = DATA_LAYER.some((d) => rel.startsWith(d))
    const classRules = CLASS_FILE.test(rel) ? RULES : RULES.filter((r) => r.classOnly !== true)
    const rules = inDataLayer ? classRules : [...classRules, ...APP_ONLY_RULES]
    text.split('\n').forEach((line, i) => {
      // Comments explain the bans; they are not violations of them.
      const code = line.replace(/^\s*(\/\/|\*).*$/, '')
      for (const rule of rules) {
        if (rule.re.test(code)) {
          console.error(`${rel}:${i + 1}: ${rule.msg}\n    ${line.trim().slice(0, 120)}`)
          violations++
        }
      }
    })
  }
}

if (violations) {
  console.error(`\n${violations} contract violation(s)`)
  process.exit(1)
}
console.log(
  `ui-lint-classes: clean across ${REFITTED_DIRS.length} refitted paths` +
    (PENDING_REFIT.length ? ` (${PENDING_REFIT.length} files still pending refit)` : '')
)
