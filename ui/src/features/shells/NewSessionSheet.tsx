'use client'

/**
 * ARIA - New coding session form
 *
 * Starts a real `start_coding_session` on the watched-shell substrate (the
 * session lands in the shell list once its shell registers), not a chat
 * conversation. Rebuilt from the old dashboard modal as a `Sheet`, with two
 * behaviour fixes:
 *  - worktree choice is a Select rather than a 16px checkbox (a sub-44px
 *    checkbox was one of the audit's flagged targets, and a two-option choice
 *    reads better than checkbox + conditional reveal);
 *  - refusals surface: the API returns 400/409 with a real `detail`
 *    ("Unknown coding backend", killswitch, worktree provisioning failure) and
 *    `ApiError` carries it to the toast, where the old page swallowed it.
 */
import { FormEvent, useState } from 'react'
import { useAction } from '@/lib/swr'
import { createCodingSession } from '@/lib/api/endpoints'
import { Button, Field, Input, Select, Sheet, Textarea } from '@/components/ui/controls'

// `backend` maps straight to start_coding_session's substrate;
// `subagent_profile` resolves a db.agents specialist (backend/model + role
// prompt) — see agents/session.py's subagent_profile resolution.
const CODING_AGENTS: Array<{ key: string; label: string; backend?: string; subagent_profile?: string }> = [
  { key: 'claude_code', label: 'Claude Code', backend: 'claude_code' },
  { key: 'pi-coding', label: 'Pi Coding Agent', subagent_profile: 'pi-coding' },
  { key: 'pi-coding-ridge', label: 'Pi Coding Agent (Ridge)', subagent_profile: 'pi-coding-ridge' },
  { key: 'codex', label: 'Codex', backend: 'codex' },
]

export function NewSessionSheet({
  open,
  onClose,
  onDone,
  onError,
}: {
  open: boolean
  onClose: () => void
  onDone: (text: string) => void
  onError: (text: string) => void
}) {
  const run = useAction()
  const [repo, setRepo] = useState('')
  const [agentKey, setAgentKey] = useState(CODING_AGENTS[0].key)
  const [prompt, setPrompt] = useState('')
  const [worktree, setWorktree] = useState<'isolated' | 'direct'>('isolated')
  const [worktreeName, setWorktreeName] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!repo.trim() || !prompt.trim() || busy) return
    const agent = CODING_AGENTS.find((a) => a.key === agentKey) ?? CODING_AGENTS[0]
    setBusy(true)
    const ok = await run(
      () =>
        createCodingSession({
          workspace: repo.trim(),
          prompt: prompt.trim(),
          backend: agent.backend,
          subagent_profile: agent.subagent_profile,
          create_worktree: worktree === 'isolated',
          worktree_name: worktreeName.trim() || undefined,
        }),
      { invalidate: ['/shells'], onError: (err) => onError(err.message) }
    )
    setBusy(false)
    if (ok !== undefined) {
      // The shell doc only exists once the launch completes, so there is
      // nothing to navigate to yet — the list's normal poll picks it up.
      onDone('Session starting — it appears in the list once its shell registers')
      setRepo('')
      setPrompt('')
      setWorktreeName('')
      onClose()
    }
  }

  return (
    <Sheet open={open} onClose={onClose} title="New coding session">
      <form onSubmit={submit} className="flex flex-col gap-3">
        <Field label="Repo path">
          <Input
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
            placeholder="/home/ben/Development/ProjectAria"
            className="coarse:[font-size:1rem]"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
          />
        </Field>
        <Field label="Agent">
          <Select value={agentKey} onChange={(e) => setAgentKey(e.target.value)} className="coarse:[font-size:1rem]">
            {CODING_AGENTS.map((a) => (
              <option key={a.key} value={a.key}>
                {a.label}
              </option>
            ))}
          </Select>
        </Field>
        <Field label="Task">
          <Textarea
            value={prompt}
            className="coarse:[font-size:1rem]"
            onChange={(e) => setPrompt(e.target.value)}
            rows={3}
            placeholder="What should the agent do?"
          />
        </Field>
        <Field
          label="Workspace"
          hint="A worktree lands on a new branch under <repo>/.worktrees/; the repo is initialised first if needed."
        >
          <Select
            value={worktree}
            onChange={(e) => setWorktree(e.target.value as 'isolated' | 'direct')}
            className="coarse:[font-size:1rem]"
          >
            <option value="isolated">Isolated git worktree (recommended)</option>
            <option value="direct">Run directly in the repo</option>
          </Select>
        </Field>
        {worktree === 'isolated' && (
          <Field label="Worktree name (optional)">
            <Input
              value={worktreeName}
              onChange={(e) => setWorktreeName(e.target.value)}
              placeholder="e.g. fix-login-bug"
              className="coarse:[font-size:1rem]"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck={false}
            />
          </Field>
        )}
        <div className="flex justify-end gap-2">
          <Button type="button" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" busy={busy} disabled={!repo.trim() || !prompt.trim()}>
            Start session
          </Button>
        </div>
      </form>
    </Sheet>
  )
}
