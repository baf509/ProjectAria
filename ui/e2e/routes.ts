/**
 * Every route the gate walks. A route missing here is a route that can regress
 * unnoticed, so add one with each new screen.
 */
export const ROUTES = [
  '/inbox',
  '/supervise',
  '/supervise/shells',
  '/operate',
  '/converse',
  '/know/memories',
  '/know/tasks',
  '/know/agents',
  '/autonomy',
  '/operate/benchmarks',
]

/** Surfaces that must own their height and never scroll the document. */
export const FLUSH_ROUTES = ['/converse', '/supervise/shells']

/** Touch projects, where the 44px / 12px / 16px floors apply. */
export const TOUCH_PROJECTS = ['phone-se', 'phone', 'phone-landscape', 'slideover', 'tablet', 'phone-dark']
