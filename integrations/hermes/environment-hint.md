## ARIA is the shell and coding control plane

This Mac runs Hermes and Project ARIA. Fleet machines connect through Tailscale.
Use ARIA's MCP tools as the authoritative path for shell discovery, creation,
input, removal, and delegated coding work.

- Start fleet questions with `fleet_status`; use `list_nodes` when placement or
  connectivity matters. Treat semantic state (`working`, `blocked`, `done`,
  `idle`) separately from connectivity (`local`, `online`, `unreachable`).
- Use `create_shell` for a new interactive watched shell. Prefer its typed
  `profile` (`claude`, `codex`, `pi`, or `shell`) and pass `host` only when the
  user names a machine or the work requires one. Never create/register tmux by
  hand when `create_shell` can express the request.
- Use `create_coding_session` for a self-contained coding task. Pass the full
  task and absolute workspace. Honor an explicit backend/model/host request;
  otherwise let ARIA apply deployment policy. The call returns promptly, so
  report its session id. Wait only when the user asked you to monitor it.
- Use the shell identifier returned by ARIA. Displayed short aliases are
  accepted only when unique; if ARIA reports ambiguity, present the canonical
  matches rather than guessing.
- An offline remote node is a placement failure, not permission to create an
  untracked local shell. Report the structured retryable error or choose
  another host only when the user's request permits it.
- If a user redirects an active run, preserve completed work and steer the
  current run unless they clearly asked to cancel it. Approval replies are
  control messages and must be handled independently of ordinary steering.

Local inspection and explanations are fine. Repository mutations must go
through ARIA. If ARIA is unavailable, say so and stop instead of silently
falling back to an invisible local coding loop.
