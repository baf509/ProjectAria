# Untracked operator sessions

From a normal Mac terminal, outside tmux:

```sh
claude --no-aria
codex --no-aria
pi --no-aria
```

`--local` is an alias for the untracked Mac path. Plain `claude`, `codex` and
`pi` retain the registered, persisted ARIA shell behavior. Refresh an existing
terminal with `source ~/.config/aria/aria-shells-mac.sh` after installation.
The explicit executable `~/.local/bin/aria-untracked-shell TOOL` also works
without refreshing shell functions.

Untracked sessions execute the native CLI in the caller's terminal. They do not
register an ARIA shell, create or attach tmux, start capture, request supervision,
or inherit ARIA session-control environment variables. The opt-out propagates
to nested shell wrappers through `ARIA_UNTRACKED=1`. The launcher refuses inside
tmux or an ARIA-managed shell because that terminal may already be captured.
It does not erase records from earlier tracked sessions.

Pi keeps the normal provider/model and gateway authentication, using the standard
`/llm/v1` endpoint without `X-Aria-*` identity headers. Generated configuration
and native resumable sessions live in `~/.local/share/aria-untracked/pi/`, outside
the watched Pi store. Config files are mode 0600. Extension discovery and project
configuration are disabled for this launch. `pi --no-aria --continue` and
`pi --no-aria --resume` use only this separate store. Custom session paths are
rejected to prevent accidentally writing into the watched store.
The separate profile starts with the managed model/thinking defaults; subsequent
choices made inside Pi persist independently. Use Shift+Tab or `/settings` to
change thinking. Red Qwen3.8-27B supports off, low, medium and high.

Gateway authentication, admission control and ordinary request/usage accounting
still apply. Using the gateway does not register a shell or coding session.
This mode is an observation opt-out, not an OS sandbox: file changes to a watched
project can still be observed by its Git/filesystem sensors.

Claude's independent sensor reads native transcripts even outside tmux. For
untracked Claude launches, `CLAUDE_CODE_SKIP_PROMPT_HISTORY=1` disables transcript
and prompt-history persistence, so these conversations cannot later be resumed.
Hooks and automatic memory writes are disabled; strict empty MCP configuration
prevents the globally configured ARIA bridge from loading. Normal authentication
and model settings remain available. See [Claude's environment reference](https://code.claude.com/docs/en/env-vars).

Codex keeps native local history; ARIA does not scan that history. Existing ARIA
MCP declarations are disabled using per-invocation configuration overrides;
other configured MCP servers remain available. See [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

Source: `scripts/aria-shells-mac.sh`, `scripts/aria-untracked-shell`, and
`scripts/aria-remote-shell`. Mac runtime copies are in `~/.config/aria/` and
`~/.local/bin/`; install the helper before the router. The router and helper
do not require an API/UI release or privileged restart. For a mapped Corsair
directory, `TOOL --corsair --no-aria` uses the same helper on Corsair. Its helper
and remote router must also be installed there. `--local --corsair` is invalid.

The Mac `.zshrc` must load `~/.config/aria/env` only when `ARIA_UNTRACKED != 1`,
so child interactive shells do not reintroduce session-control credentials.

Installed and checked on 2026-09-08: 39 wrapper/launcher tests passed, real Pi
and Codex responses succeeded, and the test directory had zero registered ARIA
shells or coding sessions afterward. Pi's native transcript was present only in
the separate store. Claude generation was rejected by the account's organization
policy disabling subscription access; its launch created no watched transcript.
Mac backups: `/Users/ben/Services/backups/aria-untracked-20260909T025716Z/`.
Corsair backups: `/home/ben/.local/state/aria-untracked-20260909T025902Z/`.
