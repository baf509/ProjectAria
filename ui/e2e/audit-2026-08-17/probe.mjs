import { chromium } from '/home/ben/.npm/_npx/e41f203b7505f1fb/node_modules/playwright/index.mjs'
const b = await chromium.launch()
const ctx = await b.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 2 })
const p = await ctx.newPage()
for (const route of ['/cockpit', '/operate', '/dashboard']) {
  await p.goto('http://localhost:3000' + route, { waitUntil: 'networkidle' })
  await p.waitForTimeout(2000)
  const res = await p.evaluate(() => {
    const iw = window.innerWidth
    // Find the deepest elements whose right edge > iw, excluding the nav ul subtree
    const nav = document.querySelector('nav')
    const out = []
    for (const el of document.querySelectorAll('main *')) {
      if (nav && nav.contains(el)) continue
      const r = el.getBoundingClientRect()
      if (r.right > iw + 1 && r.width > 0) {
        // keep only if no child also overflows by the same amount (i.e. leaf-ish)
        let leaf = true
        for (const c of el.children) { const cr = c.getBoundingClientRect(); if (cr.right >= r.right - 1) { leaf = false; break } }
        if (leaf) {
          const cs = getComputedStyle(el)
          out.push({ tag: el.tagName.toLowerCase(), cls: (typeof el.className === 'string' ? el.className : '').slice(0, 140), w: Math.round(r.width), right: Math.round(r.right), ws: cs.whiteSpace, minW: cs.minWidth, flexShrink: cs.flexShrink, text: (el.textContent || '').trim().slice(0, 80), parentCls: (typeof el.parentElement?.className === 'string' ? el.parentElement.className : '').slice(0, 120) })
        }
      }
    }
    return { iw, out: out.slice(0, 12) }
  })
  console.log('\n###', route, JSON.stringify(res, null, 1))
}
await b.close()
