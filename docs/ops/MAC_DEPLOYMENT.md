# Mac release deployment

API, UI and the `mac-agents` node run from a common release selected by
`/Users/ben/Services/apps/ProjectAria/current`. The symlink points into
`/Users/ben/Services/releases/ProjectAria/`. The existing service-tree `.env`,
Python environments and UI dependencies stay outside releases. A source edit
does not deploy; a release gets both a Git baseline and a content hash so dirty
working-tree changes cannot masquerade as the baseline commit.

Run these commands as `ben` on the Mac, from the canonical checkout:

```sh
scripts/aria-deploy-mac stage
# Use the exact release path printed by stage:
scripts/aria-deploy-mac verify /Users/ben/Services/releases/ProjectAria/RELEASE
scripts/aria-deploy-mac activate /Users/ben/Services/releases/ProjectAria/RELEASE
scripts/aria-boot-check --wait 120
```

Stage copies source, runs UI type checking, class lint and production build, and
writes `release.json` with source and artifact hashes. Run relevant API tests
in a separate development environment, and the staged UI responsive gate on a
spare loopback port before activation. Do not install test packages into the
production virtualenv. Dependency upgrades are a separate reviewed operation;
this release mechanism deliberately reuses the existing installed dependencies.

One-time setup: run `scripts/install-aria-restart-helper` in an administrator
terminal. It installs a root-owned helper and exact-command sudo rules for the
registered Aria/Hermes services. Routine activation and rollback run as `ben`
without `sudo -v` or a password prompt, including after reboot. Activation checks
the helper entitlement before changing files; missing setup fails before mutation.
It verifies all manifest hashes and checks both model admission and backend
utilization for active/queued work. If busy, it refuses: retry after work drains.
It saves launchers and the previous pointer, changes the current symlink atomically,
and gracefully restarts API, UI and node through their existing launchd jobs.
The three processes transition sequentially; this is not a zero-downtime promise.
No model start/stop, routing, credential, retrieval capability or power policy is
changed. The live API must become ready and `/api/build` must identify this release.
An activation failure restores the old launchers/pointer and restarts the old code.
It never force-kills a process that fails graceful shutdown.

Acceptance also requires:

- Retired Corsair models have `startable=false`, `allow_force_start=false`,
  `auto_route=false`, and the live Operate bundle uses the CUDA/Halo candidate.
- Both gateway `/backend?model=<slug>` mounts agree on model and admission.
- `/api/v1/health` honors disabled embeddings, and service health honors the TTS
  disable marker. Mongo reports Mac tunnel and guest ports separately.
- Boot check discovers the installed MCP tools over stdio, compares both bridge
  source hashes with the release, calls read-only API readiness, and validates
  fresh heartbeats for the required always-on nodes. Red/Ridge may sleep and are
  not required nodes. Any required failure produces nonzero exit status.
- Mac tailnet API/UI access works; raw Red/Ridge model proxies are not published.

Activation prints its private backup/receipt directory. To roll back explicitly:

```sh
scripts/aria-deploy-mac rollback /Users/ben/Services/backups/aria-release-TIMESTAMP
```

Rollback refuses if another release has since become active or inference is busy.
It restores the recorded launcher bytes and previous release (including the
pre-release legacy tree for the initial cutover), then verifies the old UI build
and API readiness. Keep releases and backups until their replacements are verified.

The launchers are `/Users/ben/Services/apps/bin/run-aria-api`, `run-aria-ui`,
`run-aria-ui-server` and `run-aria-node`. Original root files under the service
tree remain recovery material after the first cutover; `current/` is authoritative.
Hermes and its separately installed `aria-mcp` bridge have their own deployment
paths. `scripts/aria-deploy-mcp` selects a separately pinned MCP bundle and its
boot check; full API activation preserves that entry point when present. Before
the first independent MCP deployment, the API release supplies the MCP contract.

Tailscale TCP `8099` is a separate registered War Audio artifact server. It is
not a model proxy. ARIA UI TCP `3000`/HTTPS `443` and API `8200` remain published.
Network changes require explicit operator authorization; routine release activation
does not alter these settings.
