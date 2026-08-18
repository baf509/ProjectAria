/**
 * Fast per-route check used during the rebuild: horizontal overflow, tap
 * targets, type floor. The full gate (fixtures, contrast, visual regression,
 * request budgets) lives in the *.spec.ts files.
 *
 * Usage: node e2e/quickcheck.mjs /inbox /operate  [--base http://localhost:3100]
 */
import { chromium } from '/home/ben/.npm/_npx/e41f203b7505f1fb/node_modules/playwright/index.mjs'

const args = process.argv.slice(2)
const baseIdx = args.indexOf('--base')
const BASE = baseIdx === -1 ? 'http://localhost:3100' : args[baseIdx + 1]
const routes = args.filter((a, i) => !a.startsWith('--') && (baseIdx === -1 || i !== baseIdx + 1))
if (!routes.length) {
  console.error('usage: node e2e/quickcheck.mjs <route...> [--base url]')
  process.exit(2)
}

const VIEWPORTS = [
  { name: '375', viewport: { width: 375, height: 667 } },
  { name: '390', viewport: { width: 390, height: 844 } },
  { name: '1280', viewport: { width: 1280, height: 800 }, touch: false },
]

const browser = await chromium.launch()
let failures = 0

for (const vp of VIEWPORTS) {
  const touch = vp.touch !== false
  const ctx = await browser.newContext({
    viewport: vp.viewport,
    isMobile: touch,
    hasTouch: touch,
    deviceScaleFactor: 2,
  })
  for (const route of routes) {
    const page = await ctx.newPage()
    const errors = []
    page.on('console', (m) => m.type() === 'error' && errors.push(m.text().slice(0, 120)))
    try {
      await page.goto(BASE + route, { waitUntil: 'domcontentloaded', timeout: 30000 })
    } catch (e) {
      console.log(`FAIL ${route} @${vp.name}: navigation ${String(e).slice(0, 120)}`)
      failures++
      await page.close()
      continue
    }
    await page.waitForTimeout(2500)
    const r = await page.evaluate(() => {
      const iw = window.innerWidth
      const overflow = Math.max(0, document.documentElement.scrollWidth - iw)
      const offenders = []
      for (const el of document.querySelectorAll('body *')) {
        if (el.closest('[data-scroll-x]')) continue
        const rect = el.getBoundingClientRect()
        if (rect.width === 0 && rect.height === 0) continue
        if (rect.right > iw + 1) {
          let leaf = true
          for (const c of el.children) if (c.getBoundingClientRect().right >= rect.right - 1) leaf = false
          if (leaf)
            offenders.push({
              tag: el.tagName.toLowerCase(),
              cls: (typeof el.className === 'string' ? el.className : '').slice(0, 90),
              right: Math.round(rect.right),
              text: (el.textContent || '').trim().slice(0, 40),
            })
        }
      }
      const small = []
      for (const el of document.querySelectorAll('a,button,input,select,textarea,[role=button],summary')) {
        const rect = el.getBoundingClientRect()
        if (rect.width === 0 || rect.height === 0) continue
        if (el.hasAttribute('data-inline')) continue
        if (rect.height < 44 || rect.width < 44)
          small.push({ tag: el.tagName.toLowerCase(), w: Math.round(rect.width), h: Math.round(rect.height), text: (el.textContent || '').trim().slice(0, 24) })
      }
      const tiny = []
      for (const el of document.querySelectorAll('body *')) {
        let hasText = false
        for (const n of el.childNodes) if (n.nodeType === 3 && n.textContent.trim()) hasText = true
        if (!hasText) continue
        const fs = parseFloat(getComputedStyle(el).fontSize)
        if (fs < 12) tiny.push({ fs, text: (el.textContent || '').trim().slice(0, 24) })
      }
      const controls = []
      for (const el of document.querySelectorAll('input,select,textarea')) {
        const fs = parseFloat(getComputedStyle(el).fontSize)
        if (fs < 16) controls.push({ tag: el.tagName.toLowerCase(), fs })
      }
      return { overflow, offenders: offenders.slice(0, 6), small: small.slice(0, 8), smallCount: small.length, tiny: tiny.slice(0, 6), tinyCount: tiny.length, controls, nodes: document.querySelectorAll('*').length }
    })
    const touchIssues = touch ? r.smallCount + r.tinyCount + r.controls.length : 0
    const bad = r.overflow > 0 || touchIssues > 0
    if (bad) failures++
    console.log(
      `${bad ? 'FAIL' : ' ok '} ${route} @${vp.name}  ovf=${r.overflow}  small=${r.smallCount}  tiny=${r.tinyCount}  ctrl<16=${r.controls.length}  nodes=${r.nodes}`
    )
    if (r.overflow > 0) console.log('      overflow:', JSON.stringify(r.offenders))
    if (touch && r.smallCount) console.log('      small:', JSON.stringify(r.small))
    if (touch && r.tinyCount) console.log('      tiny:', JSON.stringify(r.tiny))
    if (touch && r.controls.length) console.log('      controls:', JSON.stringify(r.controls))
    if (errors.length) console.log('      console:', errors.slice(0, 3))
    await page.close()
  }
  await ctx.close()
}

await browser.close()
console.log(failures ? `\n${failures} failing route/viewport combinations` : '\nall clean')
process.exit(failures ? 1 : 0)
