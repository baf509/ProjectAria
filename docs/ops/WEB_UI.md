# Web UI — operations

The UI is a Next.js app in `ui/`, built into a container (`aria-ui`) and served
to Ben's phone and laptop over the tailnet. This is the runbook; the design and
the rebuild plan live in
`vault/ProjectAria/Planning/WEB_UI_RESPONSIVE_REBUILD_20260817.md`.

## Topology

```
iPhone / laptop
      │  https
      ▼
tailscale serve :443  ──▶ 127.0.0.1:3000  (aria-ui container, Next.js)
                                  │  server-side, injects X-API-Key
                                  ▼
                          127.0.0.1:8200  (aria-api, host systemd service)
```

**The dashboard has exactly one URL: `https://corsair-ai.tailb286a5.ts.net`** (no port).
It redirects to `/inbox`. On the box itself, `http://127.0.0.1:3000` is the same app —
that is the container's bind, not a second UI.

⚠️ **Reconciled 2026-08-19.** There were three tailscale-serve front doors: `443` and
`8443` both proxied `127.0.0.1:8787` (**Hermes WebUI**, `disabled` and `inactive` since
2026-08-13, so both were dead), and `8444` proxied the dashboard. Finding the dashboard
therefore meant knowing a port that looked like a test artifact. Now: one handler on 443 →
3000; 8443 and 8444 are removed. **If Hermes WebUI is ever re-enabled it needs its own
`tailscale serve` entry on a different port — it no longer has one.**

⚠️ **`:3100` is a test harness, not a deployment.** `make ui-serve` runs `next start` from
the WORKING TREE on `:3100`, bound to **all interfaces** (the webkit gate reaches it as
`corsair:3100` over the tailnet). `make ui-check` stops it; running `ui-serve` by hand does
not. One was left running from 2026-08-17 to 2026-08-19, serving a **two-day-old build over
plain HTTP on the tailnet** while the real UI sat behind HTTPS on loopback — the same app,
a different build, and a weaker auth path. If you start it by hand, stop it when you are done.

The browser only ever talks to **one origin**. `/api/v1/*` is a Next route
handler (`src/app/api/v1/[...path]/route.ts`) that forwards to the API and adds
the key server-side. That is what removes the baked `NEXT_PUBLIC_API_KEY`, the
CORS preflight per URL, the `?api_key=` in the shells stream URL, and the
mixed-content wall that made HTTPS — and therefore the service worker —
impossible.

## Configuration (runtime, not build time)

| Variable | Where | Default |
|---|---|---|
| `ARIA_API_URL` | compose `environment` | `http://host.docker.internal:8200` |
| `ARIA_API_KEY` | compose `environment`, from `.env`'s `API_KEY` | — (the container refuses to proxy without it) |
| `BUILD_SHA` / `BUILD_BRANCH` | compose `build.args` | set by `make ui-deploy` |

Changing the URL or rotating the key is `docker compose up -d ui` — **no
rebuild**. Nothing about the API is compiled into the browser bundle;
`grep -r "$API_KEY" ui/.next/static` must return nothing.

## Commands

```bash
make ui-check     # typecheck + class lint + build + responsive gate  (the merge bar)
make ui-build     # production build only
make ui-deploy    # build image, restart container, VERIFY the running sha == HEAD
make ui-serve     # serve the production build on :3100 (for the gate)
make ui-gate      # run the responsive gate against a running :3100
make ui-https     # tailscale serve --https=443 -> 127.0.0.1:3000
cd ui && npx playwright test           # the full gate (all viewports, contrast, polling)
cd ui && node e2e/quickcheck.mjs /inbox /operate   # fast single-route check
```

`make ui-deploy` fails if the container's `/api/build` sha does not match HEAD.
That check exists because the deployed image was six days behind source when the
2026-08-17 audit ran and nothing on the page said so — the build sha is now
rendered in the rail footer and the More sheet.

## The gate

`ui/e2e/*.spec.ts`, run against the production build on :3100:

| Spec | Fails when |
|---|---|
| `overflow.spec.ts` | any element's right edge passes the viewport (outside a `[data-scroll-x]` scroller), or the document scrolls sideways |
| `touch.spec.ts` | a tap target is under 44px, text is under 12px, or a form control is under 16px (touch projects only) |
| `flush.spec.ts` | `/converse` or `/supervise/shells` lets the document scroll |
| `polling.spec.ts` | a hidden tab issues requests, or a back-navigation refetches the 73KB fleet payload |
| `contrast.spec.ts` | any AA colour-contrast violation, in either theme |

Viewports: 375, 390, 844×390 (landscape), 320 (iPad Slide Over), 768, 1280, plus
a dark-theme phone. Chromium only — **WebKit cannot launch on this box** (needs
root-installed system libs), so iOS Safari specifics are covered by construction
and by the manual checklist below. The MacBook is an `aria-node` and can run the
same config with the `webkit` project against `corsair:3100`.

## Manual phone checklist (per change to a flush surface)

- Keyboard open: the composer stays visible (that is `--vvh` from
  `visualViewport`, since iOS ignores `interactive-widget`).
- Installed app: no content under the status bar or the home indicator.
- Background the app for a minute, return: no replayed terminal scrollback, no
  duplicated chat turn.
- Focusing any input does not zoom the page.
- After a rebuild: the "new build is ready" prompt appears (needs HTTPS).

## Gotchas

- **`output: 'standalone'` is container-only** (`NEXT_OUTPUT=standalone`, set in
  the Dockerfile). Locally it would break `next start`, which the gate uses —
  and a gate that cannot serve real CSS silently passes an unstyled document.
- **The container reaches the API through `host.docker.internal`** because
  `aria-api` is a host systemd service, not a container. If the API is ever
  re-bound off `0.0.0.0`, use `network_mode: host` instead.
- **`:3000` is bound to loopback.** The proxy is an authenticated path to the
  whole API, so it must not listen on `0.0.0.0`; the tailnet reaches it through
  `tailscale serve`.
- **`theme.legacy.js` is temporary.** It keeps the pre-redesign palette
  compiling for routes that have not been refitted. When
  `scripts/ui-lint-classes.mjs` reports no pending files, delete it.
