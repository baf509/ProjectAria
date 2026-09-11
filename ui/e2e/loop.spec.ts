import { expect, test } from '@playwright/test'
import { documentOverflow, overflowOffenders, smallTargets } from './lib'
import { TOUCH_PROJECTS } from './routes'

function runFixture() {
  return {
    _id: 'abcdef123456', version: 7, project: 'calculator-example', state: 'draft', stop_reason: null,
    specification: 'Add positive, negative, and zero integers correctly.',
    plan: { version: 1, tasks: [{ id: 'addition', description: 'Repair addition', acceptance_criteria: ['Returns arithmetic sums'] }] },
    tasks: [], attempts: [], accepted_revision: 'a'.repeat(40), final_revision: null,
    human_review: [], limits: { attempts: 10 }, usage: { attempts: 0 }, metrics: {}, events: [], final_evidence: [],
  }
}

test('draft approval uses the displayed version and reveals execution controls', async ({ page }, info) => {
  const run = runFixture()
  let approval: unknown
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname
    let body: unknown = {}
    if (path.endsWith('/loop/policy')) body = { enabled: true, projects: { 'calculator-example': { check_ids: ['addition'] } } }
    else if (path.endsWith('/loop/runs')) body = [run]
    else if (path.endsWith('/approve')) {
      approval = route.request().postDataJSON()
      run.state = 'approved'
      run.version++
      body = run
    } else if (path.endsWith(run._id)) body = run
    await route.fulfill({ json: body })
  })
  await page.goto('/supervise/loop')
  await page.getByRole('button', { name: 'calculator-example · abcdef12' }).click()
  await expect(page.getByText('Add positive, negative, and zero integers correctly.')).toBeVisible()
  await page.getByRole('button', { name: 'Approve displayed plan' }).click()
  expect(approval).toEqual({ expected_version: 7 })
  await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeVisible()
  expect(await documentOverflow(page)).toBeLessThanOrEqual(0)
  expect(await overflowOffenders(page)).toEqual([])
  if (TOUCH_PROJECTS.includes(info.project.name)) expect(await smallTargets(page)).toEqual([])
})

test('failed verification exposes retained handoff, exact revision and logs', async ({ page }) => {
  const run = {
    ...runFixture(), state: 'blocked', stop_reason: 'final_integration_failed',
    tasks: [{ id: 'addition', description: 'Repair addition', state: 'verified', attempt_count: 2,
      accepted_commit: 'b'.repeat(40), handoff: 'Retained useful work', acceptance_criteria: ['Returns sums'], dependencies: [], check_ids: ['addition'], allowed_paths: ['calculator.py'], human_review: [] }],
    attempts: [{ id: 'attempt123', task_id: 'addition', session_id: 'fresh-session123', outcome: 'verification_failed',
      execution_outcome: 'valid', verification_outcome: 'failed', candidate_revision: 'c'.repeat(40),
      changes: ['calculator.py'], checks: [{ check_id: 'addition', outcome: 'failed', output_excerpt: 'wrong answer' }] }],
  }
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname
    let body: unknown = {}
    if (path.endsWith('/loop/policy')) body = { enabled: true, projects: {} }
    else if (path.endsWith('/loop/runs')) body = [run]
    else if (path.endsWith('/logs')) body = [{ _id: 'log1', kind: 'verification', content: 'Trusted check: wrong answer' }]
    else if (path.endsWith(run._id)) body = run
    await route.fulfill({ json: body })
  })
  await page.goto('/supervise/loop')
  await page.getByRole('button', { name: 'calculator-example · abcdef12' }).click()
  await expect(page.getByText('Retained useful work')).toBeVisible()
  await page.getByText('addition · verification_failed', { exact: true }).click()
  await expect(page.getByText('"fresh-session123"', { exact: false })).toBeVisible()
  await page.getByRole('button', { name: 'View logs and diff' }).click()
  await page.getByText('verification', { exact: true }).click()
  await expect(page.getByText('Trusted check: wrong answer')).toBeVisible()
  expect(await documentOverflow(page)).toBeLessThanOrEqual(0)
  expect(await overflowOffenders(page)).toEqual([])
})
