// Responsive audit harness for the ARIA web UI.
// Usage: node measure.mjs [baseUrl]
// Writes shots/<route>__<viewport>.png and audit.json.
import { chromium } from 'playwright'
import fs from 'node:fs'
import path from 'node:path'

const BASE = process.argv[2] || 'http://localhost:3000'
const OUT = path.resolve('shots')
fs.mkdirSync(OUT, { recursive: true })

const ROUTES = [
  '/', '/inbox', '/chat', '/cockpit', '/cockpit/aria', '/operate',
  '/dashboard', '/dashboard/shells', '/dashboard/benchmarks', '/autonomy',
]
const VIEWPORTS = [
  { name: 'iphone-se-375', viewport: { width: 375, height: 667 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 },
  { name: 'iphone-390', viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 3 },
  { name: 'ipad-768', viewport: { width: 768, height: 1024 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 },
  { name: 'laptop-1280', viewport: { width: 1280, height: 800 }, isMobile: false, hasTouch: false, deviceScaleFactor: 1 },
]
const SCHEMES = ['light', 'dark']

const b = await chromium.launch()
const results = []

for (const vp of VIEWPORTS) {
  for (const scheme of SCHEMES) {
    // Only run light for the phones + one desktop to keep the run bounded; dark on the phone too.
    if (scheme === 'dark' && vp.name !== 'iphone-390') continue
    const ctx = await b.newContext({
      viewport: vp.viewport, isMobile: vp.isMobile, hasTouch: vp.hasTouch,
      deviceScaleFactor: vp.deviceScaleFactor, colorScheme: scheme,
    })
    for (const route of ROUTES) {
      const page = await ctx.newPage()
      const consoleErrors = []
      const failedRequests = []
      const requests = []
      const t0 = Date.now()
      page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 200)) })
      page.on('requestfailed', (r) => failedRequests.push(`${r.method()} ${r.url()} ${r.failure()?.errorText}`))
      page.on('request', (r) => requests.push({ url: r.url(), method: r.method(), t: Date.now() - t0 }))
      let navErr = null
      try {
        await page.goto(BASE + route, { waitUntil: 'networkidle', timeout: 45000 })
      } catch (e) { navErr = String(e).slice(0, 200) }
      // Let polling/data land.
      await page.waitForTimeout(2500)
      const metrics = await page.evaluate(() => {
        const iw = window.innerWidth, ih = window.innerHeight
        const se = document.scrollingElement
        const docW = Math.max(se.scrollWidth, document.body.scrollWidth)
        const bodyBg = getComputedStyle(document.body).backgroundColor
        const htmlBg = getComputedStyle(document.documentElement).backgroundColor
        const bodyOverflowX = getComputedStyle(document.body).overflowX
        const htmlOverflowX = getComputedStyle(document.documentElement).overflowX
        const meta = document.querySelector('meta[name=viewport]')?.getAttribute('content')
        // Elements that extend beyond the viewport width (right edge past innerWidth or left < 0)
        const overflowers = []
        const all = document.querySelectorAll('body *')
        for (const el of all) {
          const r = el.getBoundingClientRect()
          if (r.width === 0 && r.height === 0) continue
          if (r.right > iw + 1 || r.left < -1) {
            const cs = getComputedStyle(el)
            overflowers.push({
              tag: el.tagName.toLowerCase(),
              cls: (el.className && typeof el.className === 'string') ? el.className.slice(0, 120) : '',
              w: Math.round(r.width), right: Math.round(r.right), left: Math.round(r.left),
              ovx: cs.overflowX, ws: cs.whiteSpace,
              text: (el.textContent || '').trim().slice(0, 50),
            })
          }
        }
        // Keep the top-most (outermost) offenders: sort by width desc, cap 12
        overflowers.sort((a, b) => b.w - a.w)
        // Tap targets: interactive elements with a small box
        const smallTargets = []
        const interactive = document.querySelectorAll('a,button,input,select,textarea,[role=button],[role=tab],summary')
        let interactiveCount = 0
        for (const el of interactive) {
          const r = el.getBoundingClientRect()
          if (r.width === 0 || r.height === 0) continue
          interactiveCount++
          if (r.height < 44 || r.width < 44) {
            smallTargets.push({ tag: el.tagName.toLowerCase(), h: Math.round(r.height), w: Math.round(r.width), text: (el.textContent || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim().slice(0, 30) })
          }
        }
        // font sizes
        const sizes = new Map()
        let minFont = 999
        const textEls = document.querySelectorAll('body *')
        for (const el of textEls) {
          if (!el.childNodes.length) continue
          let hasText = false
          for (const n of el.childNodes) if (n.nodeType === 3 && n.textContent.trim()) { hasText = true; break }
          if (!hasText) continue
          const fs = parseFloat(getComputedStyle(el).fontSize)
          if (!isFinite(fs)) continue
          minFont = Math.min(minFont, fs)
          const k = fs.toFixed(1)
          sizes.set(k, (sizes.get(k) || 0) + 1)
        }
        const under12 = [...sizes.entries()].filter(([k]) => parseFloat(k) < 12).reduce((a, [, v]) => a + v, 0)
        const total = [...sizes.values()].reduce((a, v) => a + v, 0)
        // Fixed-height / vh usage that might cause layout issues
        const vhEls = []
        for (const el of all) {
          const cs = getComputedStyle(el)
          if (cs.position === 'fixed' || cs.position === 'sticky') {
            const r = el.getBoundingClientRect()
            vhEls.push({ tag: el.tagName.toLowerCase(), pos: cs.position, h: Math.round(r.height), w: Math.round(r.width), cls: (typeof el.className === 'string' ? el.className : '').slice(0, 80) })
          }
        }
        // Text nodes that are visually clipped (truncate/hidden)  — approximate: elements with text-overflow ellipsis and scrollWidth > clientWidth
        let truncated = 0
        for (const el of all) {
          const cs = getComputedStyle(el)
          if (cs.textOverflow === 'ellipsis' && el.scrollWidth > el.clientWidth + 1) truncated++
        }
        // Horizontal scroll containers (overflow-x auto/scroll with actual overflow)
        let hscroll = 0
        const hscrollEls = []
        for (const el of all) {
          const cs = getComputedStyle(el)
          if ((cs.overflowX === 'auto' || cs.overflowX === 'scroll') && el.scrollWidth > el.clientWidth + 1) { hscroll++; hscrollEls.push({ tag: el.tagName.toLowerCase(), cls: (typeof el.className === 'string' ? el.className : '').slice(0, 80), sw: el.scrollWidth, cw: el.clientWidth }) }
        }
        // Count of DOM nodes and of <select>
        return {
          innerWidth: iw, innerHeight: ih, docScrollWidth: docW, docScrollHeight: se.scrollHeight,
          horizontalOverflowPx: Math.max(0, docW - iw),
          bodyBg, htmlBg, bodyOverflowX, htmlOverflowX, viewportMeta: meta,
          overflowers: overflowers.slice(0, 15), overflowerCount: overflowers.length,
          smallTargets: smallTargets.slice(0, 40), smallTargetCount: smallTargets.length, interactiveCount,
          minFont, under12, textElsTotal: total, fontHistogram: Object.fromEntries([...sizes.entries()].sort((a, b) => parseFloat(a[0]) - parseFloat(b[0]))),
          fixedEls: vhEls.slice(0, 10), truncated, hscroll, hscrollEls: hscrollEls.slice(0, 10),
          domNodes: all.length, selects: document.querySelectorAll('select').length,
          h1: document.querySelector('h1')?.textContent?.trim().slice(0, 60) || null,
          title: document.title,
        }
      }).catch((e) => ({ evalError: String(e).slice(0, 200) }))
      const shot = path.join(OUT, `${route.replace(/\//g, '_') || '_root'}__${vp.name}__${scheme}.png`)
      try { await page.screenshot({ path: shot, fullPage: true }) } catch (e) { /* ignore */ }
      const apiReqs = requests.filter((r) => /:8200|\/api\//.test(r.url))
      results.push({
        route, viewport: vp.name, scheme, navErr,
        loadMs: requests.length ? Math.max(...requests.map((r) => r.t)) : null,
        apiRequestCount: apiReqs.length,
        apiUrls: [...new Set(apiReqs.map((r) => r.url.replace(/^https?:\/\/[^/]+/, '')))].slice(0, 30),
        consoleErrors: consoleErrors.slice(0, 8), failedRequests: failedRequests.slice(0, 8),
        ...metrics,
        shot: path.basename(shot),
      })
      process.stderr.write(`${vp.name} ${scheme} ${route} ovf=${metrics.horizontalOverflowPx}px small=${metrics.smallTargetCount}/${metrics.interactiveCount} minFont=${metrics.minFont} nodes=${metrics.domNodes} api=${apiReqs.length}\n`)
      await page.close()
    }
    await ctx.close()
  }
}
await b.close()
fs.writeFileSync('audit.json', JSON.stringify(results, null, 2))
console.log('wrote audit.json with', results.length, 'rows')
