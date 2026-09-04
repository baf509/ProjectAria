# Web UI operations

The ARIA UI is a Next.js 14 application in `ui/`. Production runs natively on
the MacBook Pro as `ben`; the old Corsair Docker and Tailscale Serve deployment
is retired.

Last verified: **2026-09-03**.

## Current topology

```text
tailnet browser
  -> https://bens-macbook-pro.tailb286a5.ts.net/
     (or direct private TCP at http://bens-macbook-pro.tailb286a5.ts.net:3000)
  -> Mac Tailscale publication
  -> 127.0.0.1:3000 (Next.js, com.ben.devbox.aria-ui)
  -> same-origin /api/v1/* proxy
  -> 127.0.0.1:8200 (ARIA API)
```

The browser uses one origin. `src/app/api/v1/[...path]/route.ts` proxies API
requests and injects the API credential on the server. Do not put an ARIA key in
browser-visible `NEXT_PUBLIC_*` variables or query strings.

Current URLs:

- Mac local: `http://127.0.0.1:3000`
- Tailnet HTTPS: `https://bens-macbook-pro.tailb286a5.ts.net/`
- Tailnet direct TCP: `http://bens-macbook-pro.tailb286a5.ts.net:3000`

Both tailnet URLs were verified from Corsair on 2026-08-30. Corsair has no
Tailscale Serve configuration and does not publish an alternate dashboard.

## Production layout

- Source: `/Users/ben/Development/Infrastructure/ProjectAria/ui`
- Deployed tree: `/Users/ben/Services/apps/ProjectAria/ui`
- Launcher: `/Users/ben/Services/apps/bin/run-aria-ui`
- LaunchDaemon: `/Library/LaunchDaemons/com.ben.devbox.aria-ui.plist`
- Logs: `/Users/ben/Services/logs/aria-ui.log` and `aria-ui.error.log`
- Bind: `127.0.0.1:3000`
- API target: `http://127.0.0.1:8200`

Source and deployment are separate. A source build does not update production.

## Current routes

- `/inbox`
- `/supervise`, `/supervise/projects/[slug]`, `/supervise/shells`
- `/operate`, `/operate/servers/[slug]`, `/operate/services/[slug]`, `/operate/benchmarks`
- `/converse`
- `/know/agents`, `/know/memories`, `/know/research`, `/know/tasks`, `/know/usage`, `/know/workflows`
- `/autonomy`

ARIA's default chat agent is disabled; `/converse` is retained for explicit
non-default agents and existing conversation records, not as the primary human
front door.

`/operate` has two whole-machine loadout controls. “Load Qwen dual resident”
starts Radiance on the R9700, waits for readiness, then starts Halo-only Flash
Next. “Load Flash Next hybrid” unloads both and starts the registered R9700 +
Halo split on `:8121`. The controls clear a stale operator route pin after a
successful switch so normal model-aware routing can select the new residents.

## Build and verification

```bash
cd /Users/ben/Development/Infrastructure/ProjectAria
make ui-check

cd ui
npm run typecheck
npm run build
npm run gate
```

After an intentional deployment:

```bash
curl -fsS http://127.0.0.1:3000/ >/dev/null
curl -fsS http://127.0.0.1:8200/api/v1/health
sudo launchctl print system/com.ben.devbox.aria-ui
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve status
```

The Makefile now fails closed for `ui-deploy` instead of running the retired
Docker deployment. `ui-https` prints the exact human-only Mac command because
agents may not change Tailscale settings. The production service tree and
launchd job are the authority.

## Safety rules

- Keep Next.js loopback-bound; publish it through an explicitly registered
  private-tailnet front door.
- Keep API credentials server-side and verify they do not appear in
  `ui/.next/static`.
- Use a spare loopback port for test servers and stop them after the gate.
- Treat a running build SHA and route smoke test as deployment acceptance, not
  merely a successful source build.
- Do not add another UI on Corsair.

The responsive-design decision record remains in the vault at
`ProjectAria/Planning/WEB_UI_RESPONSIVE_REBUILD_20260817.md`; it is historical
rationale, while this file is the operations guide.
