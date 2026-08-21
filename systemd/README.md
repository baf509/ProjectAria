# systemd units for ARIA

Vendored copies of the units that run ARIA on `corsair-ai`. **These are copies, not
the live files** — systemd reads `~/.config/systemd/user/`. Edit there, then copy back.

## Why

`ds4-halo-xxs.service` and its drop-ins vanished in the 2026-08-21 reboot on this
host. It had never been in git, so there is no copy and no record of what it held.
The registry entry that names it is still `startable=True`, so ARIA offers a start
that cannot succeed. A unit carries a deployment's operating knowledge — guards,
timeouts, ordering — none of which is reconstructable from the launch script.

## Units

| unit | role |
|---|---|
| `aria-api.service` | the API server on `:8200`; `EnvironmentFile=…/ProjectAria/.env` |
| `aria-tmux.service` | owns the watched-shell tmux server; ordered **before** `aria-api` |
| `aria-backup.service` + `.timer` | daily 03:30 MongoDB + identity-state backup |

Drop-ins (flattened as `aria-api.service.d--<name>.conf`):

| drop-in | effect |
|---|---|
| `deepseek-research-safety.conf` | `SHELLS_EXTRACTION_ENABLED=false`, `LLM_PROXY_AUTOSTART=false` |
| `override.conf` | resets and re-declares `ExecStart` |
| `tmux-ordering.conf` | `Wants=`/`After=aria-tmux.service` |
| `toolpath.conf` | explicit `PATH` for spawned tools |

## Restore

```bash
cp systemd/aria-*.service systemd/aria-*.timer ~/.config/systemd/user/
mkdir -p ~/.config/systemd/user/aria-api.service.d
for f in systemd/dropins/aria-api.service.d--*.conf; do
  cp "$f" ~/.config/systemd/user/aria-api.service.d/"${f##*--}"
done
systemctl --user daemon-reload
systemctl --user enable --now aria-tmux aria-api aria-backup.timer
```

⚠️ **This repo is public.** These units contain paths and flags only — no
credentials; secrets live in `.env`, which is not tracked. Re-scan before adding
any new unit here. The model-server units on this host stay in the *private*
`infrastructure` repo, and one of them (`signal-cli.service`) is deliberately not
tracked anywhere because it embeds a phone number.

⚠️ ARIA-generated `aria-model-*.service` units are **not** vendored — they are
rewritten from the registry on every start. Only hand-written drop-ins on them are
worth keeping, and those live in `infrastructure/systemd/dropins/`.
