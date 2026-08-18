import type { Page } from '@playwright/test'

/** Give SWR a moment to paint real data over the skeletons. */
export async function settle(page: Page, ms = 2500) {
  await page.waitForTimeout(ms)
}

export async function goto(page: Page, route: string) {
  // networkidle never arrives on a page that polls; domcontentloaded + settle
  // is the honest wait.
  await page.goto(route, { waitUntil: 'domcontentloaded' })
  await settle(page)
}

/** Elements whose right edge escapes the viewport, ignoring sanctioned scrollers. */
export async function overflowOffenders(page: Page) {
  return page.evaluate(() => {
    const iw = window.innerWidth
    const out: Array<{ tag: string; cls: string; right: number; text: string }> = []
    for (const el of Array.from(document.querySelectorAll('body *'))) {
      if ((el as HTMLElement).closest('[data-scroll-x]')) continue
      const r = el.getBoundingClientRect()
      if (r.width === 0 && r.height === 0) continue
      if (r.right > iw + 1) {
        let leaf = true
        for (const c of Array.from(el.children)) {
          if (c.getBoundingClientRect().right >= r.right - 1) leaf = false
        }
        if (leaf) {
          out.push({
            tag: el.tagName.toLowerCase(),
            cls: (typeof el.className === 'string' ? el.className : '').slice(0, 100),
            right: Math.round(r.right),
            text: (el.textContent || '').trim().slice(0, 50),
          })
        }
      }
    }
    return out
  })
}

export async function documentOverflow(page: Page) {
  return page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
}

export async function smallTargets(page: Page) {
  return page.evaluate(() => {
    const out: Array<{ tag: string; w: number; h: number; text: string }> = []
    const sel = 'a,button,input,select,textarea,[role=button],summary'
    for (const el of Array.from(document.querySelectorAll(sel))) {
      const r = el.getBoundingClientRect()
      if (r.width === 0 || r.height === 0) continue
      // WCAG 2.5.8 exempts links inside a run of text.
      if (el.hasAttribute('data-inline')) continue
      if (r.height < 44 || r.width < 44) {
        out.push({
          tag: el.tagName.toLowerCase(),
          w: Math.round(r.width),
          h: Math.round(r.height),
          text: (el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 30),
        })
      }
    }
    return out
  })
}

export async function tinyText(page: Page, floor: number) {
  return page.evaluate((min) => {
    const out: Array<{ px: number; text: string }> = []
    for (const el of Array.from(document.querySelectorAll('body *'))) {
      let hasText = false
      for (const n of Array.from(el.childNodes)) {
        if (n.nodeType === 3 && (n.textContent || '').trim()) hasText = true
      }
      if (!hasText) continue
      const px = parseFloat(getComputedStyle(el).fontSize)
      if (px < min) out.push({ px, text: (el.textContent || '').trim().slice(0, 30) })
    }
    return out
  }, floor)
}

/** iOS zooms into any focused control under 16px and does not zoom back out. */
export async function smallFormControls(page: Page) {
  return page.evaluate(() =>
    Array.from(document.querySelectorAll('input,select,textarea'))
      .map((el) => ({ tag: el.tagName.toLowerCase(), px: parseFloat(getComputedStyle(el).fontSize) }))
      .filter((c) => c.px < 16)
  )
}

export async function domNodes(page: Page) {
  return page.evaluate(() => document.querySelectorAll('*').length)
}
