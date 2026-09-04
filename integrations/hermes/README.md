# Hermes integration overlay

ProjectAria remains the control plane; Hermes is its Signal-facing relay. This
directory versions the small integration pieces that are owned here even though
the deployed Hermes application is a pinned, non-git copy at
`/Users/ben/Services/apps/hermes-agent`.

- `environment-hint.md` is the concise ARIA-first routing policy copied into
  Hermes `agent.environment_hint`.
- `route-coding-to-aria.py` is installed as the Hermes pre-tool hook. It blocks
  unmanaged coding/TMUX launches and repository mutations while allowing
  read-only diagnostics.
- `approval_store.py` is copied to `tools/approval_store.py`. The deployed
  Hermes `tools/approval.py`, `gateway/slash_commands.py`, and `gateway/run.py`
  contain the corresponding narrow adapter changes: redacted durable approval
  rows, operation-ID resolution, explicit restart interruption, and unambiguous
  `/approve`/`/deny` handling.
- `tool-selection-evals.json` is the regression corpus for real Signal routing
  failures. `evaluate-tool-selection.py` scores captured model responses.

The live config uses a 300-second approval TTL, `busy_input_mode: steer`,
interim narration, environment-referenced ARIA inference credentials, and the
Mac-hosted dashboard URL. Dated `*.bak-20260902-aria-reliability` files beside
the deployed files are the rollback source; all live files remain mode `0600`.

Do not restart the gateway while a turn or approval is active. After a drained
restart, compare MCP `tool_contract_status` with the SHA-256 of the deployed
`aria-mcp/server.py` and exercise one harmless approval before declaring the
overlay active.
