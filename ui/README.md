# ARIA Web UI

Next.js 14/React 18 operator interface for ARIA. It is an operations cockpit,
not ARIA's conversational identity.

## Surfaces

- Inbox and approval decisions
- Supervision of projects and watched shells
- Fleet services, model servers, and benchmarks
- Memories, agents, research, tasks, usage, and workflows
- Autonomy controls
- Explicit conversations with enabled non-default agents

The root redirects to `/inbox`. ARIA's default `aria` chat agent is disabled by
design; Hermes over Signal is the human conversational front door.

## Development

```bash
npm install
npm run dev
npm run typecheck
npm run build
npm run gate
npm run test:e2e
```

The development server defaults to `http://127.0.0.1:3000`. Use a spare port if
the production Mac service is already listening there.

The browser calls the same-origin `/api/v1/*` route handler. Set `ARIA_API_URL`
for the server-side upstream when needed; never expose ARIA credentials through
`NEXT_PUBLIC_API_KEY`, a browser bundle, or a URL query parameter.

Production deployment details are in `docs/ops/WEB_UI.md`. Production runs from
`/Users/ben/Services/apps/ProjectAria/ui` under the Mac
`com.ben.devbox.aria-ui` LaunchDaemon, loopback-bound on `:3000` and privately
published over the tailnet.
