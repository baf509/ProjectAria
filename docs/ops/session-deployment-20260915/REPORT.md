# Session deployment — September 15, 2026

Ben authorized merging, pushing and deploying the completed model and ARIA/Hermes repairs. All session commits are on `master`; ARIA and CorsairModelHost were pushed to their origins. The dated, previously uncommitted model experiments in CorsairModelHost remain outside this deployment.

## Prepared release

The API/UI and independent MCP releases are staged from the tested session merge `b070d82`, with content hashes recorded in `staging.json`. ARIA master also contains newer weekly-platform work (`0a22320`); that separate feature is not activated by this handoff. No restart helper or scheduled automation is installed here.

- API/UI: `/Users/ben/Services/releases/ProjectAria/20260915T122214Z-b070d82-cd796da4b148`
- MCP: `/Users/ben/Services/releases/aria-mcp/20260915T122207Z-f1885fc5d81e`
- Private evidence, logs and activation receipt: `/Users/ben/Services/backups/aria-session-deploy-20260915`
- Tracking task: `6aa93854ea2ccb4b7f2c8d15`

The source merge passed 448 API/contract tests and 28 UI tests. This staging passed a fresh production build, TypeScript and UI class checks. Staged MCP verification passed through installed Hermes: 127 bridge tools, 131 registered tools, native search/describe, Signal tool selection and read-only live dispatch, including all four Red choices and PARO-int5 as default.

## Activation completed — September 15, 2026

Ben ran the prepared command at 14:26 UTC. The recorded result is `verified`: API/UI and MCP activated, Hermes reloaded with Signal connected, and all nine functional checks passed. The task was completed. Subsequent weekly-platform deployment superseded the API release and MCP bundle; use the live `current` links for today's exact identities. The original command is retained for provenance and must not be rerun over a newer release:

```sh
bash /Users/ben/Development/Infrastructure/ProjectAria/docs/ops/session-deployment-20260915/activate.sh
```

The command verifies the frozen activation script and both releases, rejects intervening release/configuration changes or active work, activates API/UI and the ARIA node, installs the matching MCP/readiness plugin, replaces only the Hermes environment hint in the latest configuration, and requests a native graceful gateway restart under launchd. Configuration and deployment backups are retained. The password remains in the Terminal.

It then verifies the live MCP contract, Signal connection, boot readiness and all nine functional checks, including browser, web, PDF and a synthetic ARIA-routed RTX 3090 vision inference. On success it records `activation.json` with status `verified` and completes the tracking task. On failure it records the phase evidence and leaves the task open; inspect that receipt before claiming completion. Individual API and MCP installers retain their rollback receipts.

No model runtime reinstall or GPU restart is needed. Read-only Red verification confirmed PARO-int5 enabled/running, both R9700 caps at 250 W, and the three production alternatives stopped. The model default, routine 3090 cron/vision routing and existing Pi profiles remain in place.

Interactive Hermes processes already open refresh their cached MCP definitions with native `/reload-mcp` or on their next start. The command refreshes the shared Signal gateway.
