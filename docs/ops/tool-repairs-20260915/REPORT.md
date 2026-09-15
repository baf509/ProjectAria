# ARIA and Hermes tool repairs — 2026-09-15

The audit's four repair areas are implemented. Browser installations, internal agent records and benchmark targets are repaired live. The API policy/validation changes are tested and ready for release activation; the persistent steward setting is PARO, but the old API process still reports MXFP4 until restart.

## Live repairs

- Installed the browser required by the pinned ARIA Playwright MCP: Chrome for Testing 149.0.7827.22, Playwright revision 1226. Verified page navigation and screenshot through the actual ARIA tool API. Existing browser caches were retained.
- Replaced Hermes's unusable Linux `uv`/`uvx` with links to native Homebrew binaries. Installed `browser-use==0.13.10` / `browser-harness==0.1.13` in an isolated Hermes managed tool environment, pinned `agent-browser==0.26.0`, and installed its local Chrome. Actual stock `browser_exec` navigation and screenshot pass. No Hermes core patch. Browser recordings remain off.
- Reconciled all five ARIA internal agent records: `web` → `web_fetch`; removed references to absent Claude/deep-think tools. Agent model bindings and enabled/disabled status were preserved. Optional Claude registrations remain supported on installations where the CLI exists; they are not silently enabled here.
- Added `red-paro-int5` and `ninfer-3090` to the live and checked-in benchmark targets. Retained `red-radiance`, `flash-next`, `red-flash-next`, and `halogen`. Every target uses authenticated ARIA and has no model lifecycle commands. PARO notes identify 250 W per GPU; measurements must still record actual runtime and caps.
- Set the persistent service `STEWARD_MODEL` to PARO. This route also serves Qwen coding reviews, which is why it follows Red's new default. Running-process change awaits activation. Hermes main remains PARO; routine cron and vision remain on the 3090.

## API changes awaiting activation

- Correct `web_fetch` allowlisting while retaining the sensitive generic filesystem/shell boundary.
- Correct seeded agent tool lists and add a narrow, idempotent `web` alias migration. Preserve other custom tools and avoid overwriting concurrent agent changes.
- Validate new/updated tool selections and reject unregistered exact names or unmatched prefix patterns with HTTP 422.
- Add `GET /api/v1/tools/health` and startup drift logging. Report registration, agent enablement, optional-tool unavailability, and configuration failures separately. This endpoint performs no tool execution, browser launch or model inference.
- Update steward default and model-related regression assumptions for PARO.

## Verification

180 relevant tests pass (router policy/execution, agent routes, migrations, outcomes and steward behavior). An isolated ASGI app using the new source and a read-only snapshot of live agent/tool inventory passed tool-health validation and real public-page `web_fetch` execution. It did not run application startup or launch services.

`scripts/aria-tools-check` is a repeatable explicit canary. It exercises ARIA Playwright, stock Hermes browsing, Tavily search/extraction, synthetic PDF reading, MCP discovery/contract, benchmark targets and memory retrieval. `--vision` adds one synthetic image inference through ARIA and checks the resolved model identity. Current functional checks pass for these existing/live capabilities, including vision on `NInfer-3090-Qwen3.8-27B`. The report intentionally remains overall failed before activation because the old API lacks `/tools/health`, denies `web_fetch`, and still has the stale in-process steward model.

The canary uses owned browser tabs/sessions, waits for page load, cleans up its browser session, writes only summaries of memory checks, and never starts a model, optional service, cron job or message. Screenshots are synthetic/public artifacts. It is an explicit operator check rather than a periodic job; no inference or downloads are added to ordinary service health requests. `not_tested` is distinct from passed.

## Activation and rollback

Run the adjacent `activate-aria.sh` once it has been staged. It verifies the exact release manifest, obtains sudo in the local Terminal, activates using the existing idle-check/restart/rollback deployment helper, and runs the full functional canary including vision. Mac system launchd restart requires the user's local sudo authentication; the agent session has no passwordless sudo. No privilege bypass is used.

Installation/config backups are under `/Users/ben/Services/backups/aria-tool-repairs-20260915` (private directory). Release activation creates its own standard rollback receipt. The prior `aria.env` contains the old steward setting, agent-tool-settings.json records previous lists, and benchmark-targets.yaml contains all four original targets. These backups are not committed. API release rollback does not undo the independent, working browser fixes or benchmark catalog; restore those backups only if explicitly needed.

## Boundaries

ARIA remains available for research and infrastructure/coding. Personal, scheduling and external productivity integrations remain stock Hermes responsibilities. Optional document/research-start exports and external providers from the audit were additions, not repairs, and were not enabled. Retrieval services deliberately disabled to save resources remain off. Red's model runtime, 250 W GPU caps, Hermes/Pi model bindings and existing deployment options were not changed by this work.
