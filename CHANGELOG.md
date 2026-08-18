# ARIA Changelog

All notable changes to ARIA will be documented in this file.

## [2026-08-17] - Project retirement; the "focused project" pointer removed

### Added
- **Retire a project** (`POST /api/v1/projects/{ident}/retire`,
  `planning/retirement.py`, MCP `retire_project`, and a panel on the project
  cockpit). Ending a project is normally avoided because deleting the row feels
  like discarding the record of it — so this is a **transfer, not a delete**: a
  bounded slice of the project's shell scrollback and coding sessions is
  distilled into `aria.memories` (`source.type = "project_retirement"`), and
  only then is the project row removed.
  - **Order is the feature.** Memories are written AND verified before anything
    is deleted; if nothing could be stored the project is kept and the caller is
    told why. A deterministic record (what it was, its path, unfinished work,
    open tasks, what was kept) is written LLM-free first, so a dead extraction
    model degrades the result rather than silently producing nothing.
  - **Refuses while work is live** — a running session or an active shell aborts
    with 409 rather than stranding an agent whose project vanished.
  - **Bounded**: `shell_events` holds 17.4M rows and one shell can carry 7M
    lines, so retirement reads the most recent 1200 events per
    shell up to a 60k-char budget, and reports what it scanned.
  - **Kept, not deleted**: scrollback, coding sessions and previously-extracted
    memories. Only the project row and its tasks go — those other lifecycles
    have their own retention.
  - Attribution uses most-specific-root-wins, the same rule `PathIndex`
    enforces, so retiring `~/Development` cannot swallow a child project's
    transcripts. Covered by 7 tests including that case and the ordering
    guarantee.
  - `dry_run` (the UI's "Preview retirement") runs the identical server path
    without writing or deleting.

### Removed
- **The "focused project" pointer.** The Focus control wrote
  `app_state.active_project_slug`, which the web card ring, the TUI star and MCP
  read back — and which **nothing acted on**: no worker, router, session spawn
  or alert scoping keyed off it. (Not to be confused with
  `PlanningService.active_projects()`, the steward's active *set*, which is
  load-bearing and untouched — the name collision is most of why the pointer
  looked meaningful.) Removed from the web UI, `GET`/`PUT /projects/active`, the
  `active_project` field on `/projects/overview`, MCP `set_active_project`, the
  TUI (`f` key, star marker, title suffix) and the stored value, which had been
  pointing at a project last touched 2026-07-24. A test pins the removal so the
  routes cannot return without a consumer.

### Fixed
- `/projects/overview` now returns `kind` and `charter_purpose`. They were set in
  the database but never projected, so the switcher rendered all 64 rows as equal
  cards — 32 real projects, 18 ignored and 14 scratch dirs, indistinguishable.
- `text-ink-mute` used as a text colour on the fleet list (2.75:1). It is a
  decoration token; `scripts/ui-lint-classes.mjs` now rejects it as text so the
  failure arrives at lint time rather than from an axe run on whichever route
  happened to render it.

## [2026-08-17] - Web UI responsive rebuild: measured audit + plan (docs only, no UI code)

### Added
- **`vault/ProjectAria/Planning/WEB_UI_RESPONSIVE_REBUILD_20260817.md`** — the plan for
  rebuilding how the web UI is built so it fits a phone: measured baseline (Playwright/Chromium,
  10 routes × 4 viewports × light/dark), root causes, target architecture, page-by-page refit,
  11 phases with gate-enforced exits, risks, open questions. Produced from a live audit + an
  8-scope code audit (175 findings) + a four-lens architecture panel with two judges and a critic.
- **`ui/e2e/audit-2026-08-17/`** (untracked) — the measurement harness (`measure.mjs`, `probe.mjs`)
  and the baseline (`audit.json`, `audit-summary.md`); above-the-fold screenshots in
  `vault/ProjectAria/Planning/attachments/webui-audit-20260817/`.
- `BACKLOG.md` §6 — pointer + the API-side companion items (serial `/model-servers` inspect =
  8.84 s; untyped 200 schemas; `APPLY` has no executor; CORS list to shrink after the proxy).

### Found (not yet fixed)
- `/cockpit` overflows a 390 px viewport by 92–107 px (`grid gap-4 md:grid-cols-2 lg:grid-cols-3`
  with no base column → implicit `auto` track sized to the widest card's min-content);
  `/operate` by 40–55 px (unbreakable path in `server.description`, `operate/page.tsx:470`);
  `body{overflow-x:hidden}` + `minimumScale:1` mask both — the "blank strip beside the header".
- ~80 alpha-modified token utilities (`bg-live/40`, `bg-gone/10`, `bg-accent/60`, `ring-live/25`)
  compile to **nothing** — `tailwind.config.js` tokens are bare `var()` with no `<alpha-value>`;
  verified against the deployed CSS. Shells page light-mode buttons (`text-fuchsia-200` on
  `bg-fuchsia-500/20`) have invisible labels.
- Every tap target on `/inbox`, `/cockpit`, `/operate` is < 44 px; most text is 10–11 px in
  `ink-faint` (2.7–3.2:1). `/dashboard/shells`: 6,046 DOM nodes, SSE opened with no `since_line`.
- `NEXT_PUBLIC_API_KEY` is in 8 JS chunks of the running `aria-ui` image, which was built
  2026-08-09 against source last changed 2026-08-15.

## [2026-08-17] - `pi` desk-path parity, and the green bar says `aria`

### Added

- **`pi()` wrapper in `~/.bashrc`** — a bare `pi` (or `pi -c`) now gets the same ARIA
  Shells experience as `claude` and `codex`: one persisted, watched, per-directory tmux
  session (`claude-pi-<dir>` — the `claude-` prefix keeps it in the fleet's single
  adoption namespace), spawned via `POST /api/v1/shells` with `launch_command`, the same
  never-freeze / never-bail-early attach contract, and `--no-aria` / `command pi` escape
  hatches. Anything with other args (subcommands, `-p`, an initial prompt, `--model …`)
  cannot be applied to an already-running session, so it goes straight to the real
  binary.
- **`scripts/aria-pi-launch`** (symlinked into `~/.local/bin`) — the launch shim: `exec pi
  --continue`, which is pi's own resume (most recent session for the cwd under
  `~/.pi/agent/sessions/`, or a fresh one when there is none — no probe/fallback dance
  needed, unlike the claude and codex shims). Provider/model are deliberately not pinned
  here; pi's `~/.pi/agent/settings.json` is where the desk model is chosen.
- **`~/.local/bin/pi` symlink** — the reason the first spawn died: `~/.bashrc` returns
  early for non-interactive shells, so the `bash -lc` that tmux runs never sees
  `~/.npm-global/bin`. `codex` only worked because it already had a `~/.local/bin` link;
  pi now has the same.

### Changed

- **The tmux status bar identifies the shell as ARIA's, and names the tool.** tmux's
  default `status-left "[#{session_name}]"` at `status-left-length 10` rendered
  `claude-ProjectAria`, `claude-codex-ProjectAria` and `claude-pi-ProjectAria` all as
  `[claude-Pr` — so every ARIA shell looked like a Claude one. `scripts/aria-tmux-hook.conf`
  now renders `claude-*` sessions as **`aria ▸ <name without the prefix>`** (non-ARIA
  sessions keep the plain `[name]`), and each `aria-*-launch` shim renames its tmux window
  after the tool (`0:claude` / `0:codex` / `0:pi`) instead of automatic-rename's `0:bash`
  (the shim is the pane's foreground process-group leader on the resume path). Session
  *names* are unchanged — the `claude-` prefix is what the adoption hooks match on.
- **`extended-keys on`** in the same conf — pi warned on every launch that modified Enter
  keys may not work; only apps that request extended-key reporting are affected.

## [2026-08-17] - DwarfStar selected as the DS4 stack; utilization telemetry is backend-aware

### Added

- **`DS4-0731-Q8Protected-Halo-DwarfStar`** (`:8112`, `infrastructure/dwarfstar-ds4/serve.sh`,
  ARIA-generated unit) — DeepSeek V4 Flash on the Strix Halo via antirez's
  [DwarfStar](https://github.com/antirez/ds4), a native-ROCm engine written for DS4.
  **Selected as the APU resident after a six-way bakeoff**
  (`vault/infrastructure/Analysis/DS4_STACK_BAKEOFF_20260817.md`): tied top quality (13/15
  LCB-medium at 16k) with the fewest genuine wrong answers, and the only top scorer whose
  weights are neither abliterated (Ember) nor expert-pruned on stale saliency (REAP);
  loads in ~40 s vs ~15 min. ⚠️ **NOT CUT OVER** — every consumer still points at `:8108`;
  flipping is a separate, deliberate step. ⚠️ **~100 GiB measured GTT** — DwarfStar
  self-reports 82.46 and understates by ~17.5 GiB, so this is a *wash* with IQ3_XXS, not a
  saving; co-resident with radiance + gemma on ~8 GiB of headroom. `batched_sessions ×
  ctx` multiplies context buffers per resident session — 6 × 262144 hard-crashed the box
  once today; raise one at a time and measure.
- **`DS4-0731-REAP150B-MXFP4`** (`:8109`) — the REAP-pruned challenger, now
  `startable=False`: weights deleted 2026-08-17 after DwarfStar was selected. Kept as the
  record that the stale-saliency worry was tested (REAP 10/15 vs unpruned 9/15, one
  discordant pair — no evidence of prune damage) and that it lost on provenance, not
  measured quality.
- **`ModelServerSpec.runtime_family`** (`llamacpp` | `vllm` | `dwarfstar`) and
  per-family runtime probes. `GET /infrastructure/model-servers/utilization` used to speak
  only llama.cpp's `/slots` + `/metrics`, so it returned `null` for two of the three live
  models with no way to tell "unknown" from "idle". Now vLLM is read from Prometheus
  `/metrics` (`kv_cache_usage_pct`, `prefix_cache_hit_rate`, `prompt_tokens_cached_total`)
  and DwarfStar from `/v1/models` (its only endpoint); every row carries `runtime_family`
  and a `telemetry_hint` saying *why* a field is missing. ⚠️ `null` still means UNKNOWN,
  never "not busy".
- **`check_runtime_updates` tool** (`tools/builtin/runtime_updates.py`) — a **read-only**
  check of whether the pinned inference runtimes (DwarfStar, Nathan's Vulkan fork,
  vllm-radiance 0.5.8, mainline llama.cpp on the unmerged bailingmoe3 PR branch, Ember)
  have moved upstream. Reports only; never pulls, builds or restarts. Allowlisted so
  ARIA's scheduler can run it unattended — a loop that runs while Ben is not talking
  belongs in ARIA, not a Hermes cron prompt.

### Changed

- **Triage classification moved to gemma** (`triage_classify_backend/model/endpoint`,
  `:8104`) instead of riding `steward_model` on `:8080`. Only the informational-vs-failure
  call moved; the DIAGNOSE session is unchanged. `steward_model` also drives the Steward
  service and `agents/review.py`, so repointing it would have moved three consumers onto a
  4B model. A failed classification returns `None` and leaves the alert alone — a dead
  classifier costs a downgrade, never a delivery. (gemma's unit had been exiting on every
  boot because it gated on the retired `:18211` endpoint — repaired 2026-08-17.)
- **`Qwen3.8-27B-R9700-Radiance`** now declares `runtime_family="vllm"`.

## [2026-08-16] - The R9700 moved off llama.cpp: Qwen3.8 now serves through vllm-radiance

### Changed

- **`:8080` is vLLM now, not llama.cpp.** New registry entry
  `Qwen3.8-27B-R9700-Radiance` (`qwen3.8-radiance.service`,
  `infrastructure/qwen3.8-radiance/serve.sh`) serving **int4 W4A16 (AutoRound)** weights
  through **vllm-radiance 0.5.8**, multimodal, **196608 ctx × 1 slot**, 29 GiB VRAM.
  `Qwen3.8-27B-R9700-HIP` is `startable=False` with the reason (kept, not deleted).
- **Why: ~2× on both axes for no measurable quality cost.** Measured same card, wikitext-2
  test, 100 chunks @ c=512 — perplexity **6.6094** vs the retired GGUF's **6.6029** (inside
  ±0.10), prefill **~890 → ~1850 tok/s**, decode **27.2 → 54.4 tok/s**.
- **Hermes: declared context 250000 → 196608**, so its compaction trigger moved 187,500 →
  **135,168**. This is a *server ceiling*, not a preference: vLLM preallocates its KV pool
  (measured ~236,790 tokens) where llama.cpp allocated lazily at q4_0, and the old
  327680 × 2 does not fit alongside 17.9 GiB of weights on a 32 GiB card. Raising
  `compression.threshold` was rejected — it is global and would have moved DS4 from 60K
  to 96K of its 120K declaration.
- **Two aliases on one endpoint**, deliberately: `qwen3.8-27b-r9700` (Hermes main provider)
  and `qwen3.8-27b-rocmfp4-r9700` (Hermes auxiliary roles + ARIA `config.steward_model`).
  The launcher gained a `SERVED_NAMES` env knob for this; its default is unchanged.

### Removed

- **~135 GB of retired Qwen3.8 artifacts**: the ROCmFP4 GGUF (17G), the Q6_K GGUF (23G),
  the bf16 safetensors quantization source (52G), the three `rocmfpx-src` HIP builds
  (~2.6G) and benchmark scratch. **Radiance is now the only way Qwen3.8 is deployed**, so
  there is no rollback to the GGUF path without re-downloading.

### Fixed / learned

- ⚠️ **llama.cpp's MTP speculative decoding is NOT distribution-preserving here.** Greedy
  output (temp 0 / top_k 1) diverged mid-content on **6 of 8** prompts versus unspeculated
  decoding, while a baseline-vs-baseline control was 8/8 identical. **radiance's MTP passed
  the same test 8/8.** Never enable `--spec-type draft-mtp` on a llama.cpp path on this box.
  Acceptance rate is a throughput number and is not evidence either way.
- ⚠️ **An OpenAI-style `echo` + `logprobs` request crashes the llama.cpp server**
  (`ggml_abort` in `common_context_seq_rm` ← `update_slots`, exit 6/ABRT). Hit accidentally
  against the live `:8080` before the cutover.
- ⚠️ **The AutoRound checkpoint ships three fields that make vLLM load it wrong, silently**
  (`quant_method: auto-round`, missing `desc_act`, missing `modules_in_block_to_quantize`).
  `CONFIG_FIX=1` patches them. The proof it worked is the startup line
  `Using RDNAHybridW4A16LinearKernel`.
- **`shared-embeddings` is exited**, so memories written during this work are stored
  `embedding_pending` and are not semantically recallable until it is started and the
  backfill drains.

## [2026-08-15] - ARIA↔infrastructure is now stated as one system, not two unrelated repos

### Added

- **`~/Development/CLAUDE.md`** (new, above both repos — Claude Code loads project memory
  from the cwd upward, so *any* session in *either* repo inherits it). It states the fact
  that was previously implicit: **ARIA is the control plane, `infrastructure/` is the data
  plane it drives, and they are one system in two git repos** — so a deployment change
  usually touches both and must be committed in both.
- **`infrastructure/CLAUDE.md`** (new — that repo had **no** `CLAUDE.md` at all, only a
  README, so an agent landing there had zero project instructions). Carries the
  never-hand-run-docker/systemctl rule, the inverted-DRM and tailnet-only-`:8092` hazards,
  the two-repo commit rule, and doc routing. Previously the hand-run prohibition existed
  *only* in ProjectAria's CLAUDE.md — i.e. not where the temptation is.
- **`CLAUDE.md` → *Working set: the `infrastructure` repo*** (this repo): reframes
  `infrastructure/` from a dependency ("must be started first") to part of the working set,
  with the explicit 4-step recipe for adding/changing a deployment — artifacts under
  `infrastructure/<slug>/`, register in `model_servers.REGISTRY` (or the sibling
  `services.py`), tests, docs — plus the retire-with-`startable=False` mirror image. Notes
  that **`start_coding_session` defaults to `coding_default_workspace`
  (`aria-projects`)**, so an ARIA-spawned agent lands in *neither* repo unless `workspace`
  is passed explicitly.

### Changed

- **`HOUSE_AGENT_ARCHITECTURE_20260815` moved `vault/infrastructure/Planning/` →
  `vault/ProjectAria/Planning/`** (Ben's call). It is an agent-architecture doc — Hermes as
  the always-on house agent, coding agents on projects, ARIA as the management console —
  and the model topology is an *input* to it, not its subject. The move is recorded in its
  frontmatter; `aria` is now its lead tag. Obsidian wikilinks resolve by name so
  `[[HOUSE_AGENT_ARCHITECTURE_20260815]]` backlinks are unaffected; the one path-based
  reference (`docs/ops/LOCAL_INFERENCE_TOPOLOGY.md` §Sources) was updated.
- Both CLAUDE.md doc-routing notes now state the vault-folder filing test explicitly, since
  the vault has **both** an `infrastructure/` and a `ProjectAria/` folder: how a model is
  built/quantized/tuned/measured → `infrastructure/`; how the agents are
  architected/routed/operated → `ProjectAria/`.

## [2026-08-15] - mongot and embeddings are switchable at runtime, and re-enabling self-heals

### Added

- **Retrieval capability switches** (`api/aria/memory/capabilities.py`) — mongot
  (`search`) and the embeddings model (`embeddings`) can each be turned off
  **without stopping ARIA**, and turned back on without a repair step. State is
  a persisted fixed-`_id` doc (`db.capabilities`/`_id=retrieval`), same pattern
  as the killswitch, so a capability an operator switched off **stays off across
  an `aria-api` restart** rather than silently resuming the alerts they
  silenced. `EMBEDDINGS_ENABLED` / `SEARCH_ENABLED` are boot defaults only.
- **`EmbeddingBackfillWorker`** (`api/aria/memory/backfill.py`) — the missing
  half of the existing `embedding_pending` graceful-degradation flag. Nothing
  ever came back for a memory written during an embeddings outage; now the flag
  **is** a queue, drained on a timer *and* immediately when embeddings are
  switched back on (`set_backfill_trigger` → `kick()`). Also picks up
  `embedding: null` docs the flag predates, and ontology entities whose
  `_embed_entity` degraded to `None`. Bounded per pass (batch + concurrency
  caps) so it can't starve live writes of the same CPU-only service.
- **API** `GET/PUT /api/v1/capabilities/retrieval` (+ `POST .../backfill` for a
  synchronous catch-up pass). `with_service=true` on the PUT also stops/starts
  the backing container via the non-LLM service registry, in the order that
  matters: **switch off → stop**, **start → switch on**. A failed container
  transition never rolls back the switch.
- **MCP** `retrieval_capabilities` / `set_retrieval_capabilities`, so Hermes can
  flip either switch and read `retrieval_mode` when recall looks off.
- Partial index `memory_embedding_pending` (only the docs actually waiting).

### Deployed + operator state (2026-08-15T17:19-04:00)

- Deployed (`aria-api` restarted) and **both capabilities switched OFF at Ben's
  request**: `search=false` (mongot container left running — it is shared with
  AgentBenchPlatform, so stopping it is a cross-project call) and
  `embeddings=false` with `with_service=true`, which stopped `shared-embeddings`
  (exit 137). `retrieval_mode` is now **`fallback`** — recall is served by the
  mongod-native scan. **This is the expected state; check
  `GET /api/v1/capabilities/retrieval` before debugging recall.**
- **826 memories + 21 ontology entities are queued** for re-embedding. 825 of
  those predate this change — they had been stranded by past embeddings
  outages with nothing to pick them up, which is the gap the backfill worker
  closes. They drain automatically when `embeddings` is switched back on.
- Verified live: switches survived an `aria-api` restart with `.env` still
  saying `true`; `/health/services` reports both as `capability disabled`
  (13/15 healthy, the two reds pre-existing and unrelated); recall in fallback
  mode returns relevant results over the live 19,889-memory collection; a store
  issued while degraded returned 201 and landed `embedding_pending: true`.
- `_drive_service` now re-reads the real container state before reporting a
  failure: the registry's docker call times out at 10s while `docker stop`
  itself waits 10s for SIGTERM before SIGKILL, so a container that ignores
  SIGTERM reliably reports a timeout **and stops anyway** (observed on
  `shared-embeddings`). Reporting that as "failed to stop" sends the operator
  to fix something already done. ⚠️ `services._run`'s own 10s timeout is
  unchanged and still affects every slow-stopping container in that registry.
- Docs: new runbook `docs/ops/RETRIEVAL_CAPABILITIES.md`; design writeup
  `vault/ProjectAria/Design/RETRIEVAL_CAPABILITIES.md`; current state recorded
  in CLAUDE.md, README, PROJECT_STATUS, and both service-registry entries
  (`shared-embeddings`'s "Memory store/recall both block on this" note was
  simply no longer true).

### Changed

- **`LongTermMemory.search()` degrades by capability instead of failing**:
  `embeddings off` → BM25 `$search` only, with **no query embedding computed**;
  `search off` → a mongod-native fallback scan (`_fallback_search`, token
  overlap + importance, stopword-filtered, bounded candidate window). The
  branches now raise `SearchBranchUnavailable` rather than returning `[]`, so
  "mongot answered and nothing matched" is distinguishable from "mongot could
  not answer" — the second now degrades to the scan, closing the silent-empty-
  recall failure that a dead mongot used to produce.
- `create_memory` keeps a good embedding even when mongot is off (only *vector
  dedup* needs mongot), and falls back to **exact-content dedup** while
  degraded, so a degraded window doesn't fill `memories` with the literal
  duplicates the machine emitters re-emit routinely.
- `EmbeddingService.embed()` raises `EmbeddingsDisabled` (a `RuntimeError`, so
  the existing 503 mapping in `/memories/search` applies) **before** the
  circuit breaker — a disabled capability costs no HTTP call and cannot trip
  the breaker.
- **A disabled capability no longer pages.** `/health/services` reports mongot
  and embeddings as `capability disabled` instead of probing them, and
  `shells/selfcheck.py` skips both checks — same rule as a deliberately-stopped
  model server. Startup's embeddings probe is skipped too.
- `OntologyStore.search()` skips `$vectorSearch` when mongot is off and goes
  straight to its existing lexical fallback.

## [2026-08-15] - DS4 halo profile: q8_0 @131K adopted; drafter device is a declared knob

### Changed

- **Hermes → Qwen3.8 on the R9700; DS4 → the coding agent.** `qwen-r9700.service`
  now serves `-c 327680 -np 2 --kv-unified` (one 320K KV pool: Hermes's main
  conversation up to the native 262144, a second slot for crons; measured
  23.7 GiB VRAM, 26.3 t/s short / 22.2 t/s @13K, 852 t/s prefill; a 13K cold
  prefill on one slot drops the other's decode to ~6 t/s while it runs).
  `serve-rocmfp4.sh` gained `NP`/`KVU` knobs; `~/.hermes/config.yaml` default
  is `custom:qwen38-r9700` (declared 250000; backup `config.yaml.bak-pre-qwen-default-*`).
- **Registry: `Qwen3.8-27B-Q6_K-R9700-HIP` → `Qwen3.8-27B-R9700-HIP`.** The unit
  had been swapped to the ROCmFP4 model + ROCmFPX HIP build by drop-in while the
  registry still described Q6_K/mainline and pointed `launch_script` at the
  wrong script. Entry rewritten to the real script with `model`/`ctx`/`slots`/
  `kv_unified`/`kv`/`cache_ram` parameters and `slots_param`, so utilization
  reports `declared 2 × 327680` against live `2 × 262144`.

### Fixed

- **Gap sweep after the topology change (2026-08-15 evening).** `endpoints.env`
  (`DS4_URL`→`:8108`, `QWEN38_27B_URL`→`:8080`, `LING3_FLASH_URL` commented — `:8108`
  is DS4 now); `docs/ops/LOCAL_INFERENCE_TOPOLOGY.md` gained §13 (today's layout,
  the four measured facts behind it) and a correction on the affine-only 6,880 B/token
  KV figure; CLAUDE.md's "current Hermes/pi default" and "six slots" lines; the DS4
  registry `consumers_note`; the DS4 unit `Description`; Hermes's `ds4-halo` provider
  text; pi's `qwen38-r9700` contextWindow (64000 → 250000); `POST-REBOOT-CHECKLIST.md`
  now says start through ARIA — a bare `ds4-halo-xxs/serve.sh` uses script defaults
  (bf16 drafter on the Halo) and trips the OOM guard.
- **Hermes had had no ARIA tools for a day: `mcp/server.py` pinned `mcp>=1.2`,
  which now resolves to mcp 2.0.0 — `mcp.server.fastmcp` is gone there — so
  `~/.local/share/aria-mcp/run.sh` crashed on import and the gateway parked the
  `aria` MCP connection 251×/24h.** Pinned `mcp>=1.2,<2` in the repo file and the
  installed copy; verified `initialize` → `serverInfo aria 1.29.0`, and zero
  parked-connection warnings after the gateway restart.

- **`DS4-0731-IQ3_XXS-Halo-Vulkan` now serves the Flow Z13 reference profile** —
  Nathan v0.6.1, **q8_0 KV, one 131,072-token slot**, ub2048 — as the unit's own
  default (`ds4-halo-xxs.service.d/profile-flowz13.conf`; the registry reports it
  as `unit_dropin`, so a start with no overrides lands there). `context.conf`
  (f16 @65536) and `no-draft.conf` are retired in place (`*.retired-20260815`)
  as the fallback. Measured after the swap: 19.2–19.4 t/s short, 16.1 t/s at
  22.8K depth, 250 t/s prefill at 22.8K, MemAvailable 18 → 14.9 GiB after that
  fill. Decision + evidence: vault `HOUSE_AGENT_ARCHITECTURE_20260815` Prime
  question; residency: `MEMORY_BUDGET_POLICY_20260814` §7.
- New launch parameter **`draft_device`** (`DRAFT_DEV`, `serve.sh` `-devd`) on
  that entry, with the measured caveat baked into its choices: on Nathan v0.6.1
  the DSpark head shares the target's `output.weight`, so a drafter on the
  R9700 (`Vulkan0`) with the target on the Halo **aborts at draft-context init**
  (`pre-allocated tensor (output.weight) in a buffer (Vulkan1) that cannot run
  the operation`). Draft and target must share a device; on the Halo the
  drafter trips the co-resident floor, hence the profile runs `DRAFT=none`.
  The `kv`/`ctx`/`draft` descriptions were refreshed to match (KV is allocated
  lazily — ~45 KiB/token q8_0, ~90 KiB/token f16 — a *filled* slot is what
  costs memory, not `-c`).

## [2026-08-14] - Choose which model loads, and how — plus per-device memory accounting

### Added

- **Launch parameters on the model-server registry.** A registry entry can now
  declare the knobs its deployment already exposes — device placement, KV cache
  type, context, drafter, slot count, prompt cache — and
  `POST /infrastructure/model-servers/{slug}/start` accepts `overrides` for
  them. Values are validated against the declared type/choices before anything
  is written (an allowlist, not an escape: these end up in a systemd
  `Environment=` line read by a shell script).
- ARIA applies a choice as a systemd drop-in (`<unit>.d/zz-aria-overrides.conf`,
  which sorts last and therefore wins) rather than by building its own command
  line. That keeps every ExecStartPre guard, the `OOMScoreAdjust=900` backstop
  and the launcher's MemAvailable floors — a hand-rolled argv would silently
  drop all of them — and leaves the override a file that can be read or deleted
  from outside ARIA. A start with **no** overrides removes ARIA's drop-in, so a
  context size chosen for one experiment cannot silently outlive it.
- Deployments that ship a `serve.sh` but no unit of their own (`ds4-affine`,
  `ds4-hybrid`) get an ARIA-generated `aria-model-<slug>.service`, with the
  guard environment and ExecStartPre checks declared in the registry entry.
- **Six live deployments registered**, one per model+runtime+placement pair
  actually present on the box: `DS4-0731-IQ3_XXS-Halo-Vulkan` (APU-only, Nathan
  Vulkan fork), `DS4-0731-IQ3_S-Hybrid-ROCm-Dual` (split 80/20 across both
  GPUs), `DS4-0731-ROCmFPX-Affine-Quality` (sealed O5 runtime, the quality
  reference), `Qwen3.8-27B-Q6_K-R9700-HIP`, `Qwen3.8-27B-Q6_K-R9700-Vulkan-MTP`
  and `Qwen3.8-27B-ROCmFP4-R9700-Vulkan`.
- `infrastructure/gpu_devices.py`: DRM device discovery and per-pool memory
  accounting, plus `GET /infrastructure/model-servers/devices` and the MCP tool
  `list_gpu_devices`.
- A port-conflict check on `start()` — several entries deliberately share a
  port (the three `:8110` Qwen variants, the DS4s on `:8107`), which is a hard
  conflict independent of memory.
- Web `/operate` gained a Launch configuration panel; the TUI gained a model
  screen under `g`; `start_model_server` gained an `overrides` argument.
- `qwen3.8-27b/docker-compose.yml` gained a profile-gated
  `qwen3.8-27b-rocmfp4` service for the ROCmFPX-native weights (17.7 GB, 4.6
  GiB smaller than Q6_K). Not yet brought up — verify decode speed on first run.

### Fixed

- **The memory safety gate was reading the wrong GPU.** `model_servers.py` and
  `shells/selfcheck.py` both hardcoded `/sys/class/drm/card0/device/
  mem_info_gtt_*`, which was correct while the box had one GPU. Adding the
  OCuLink R9700 made `card0` the *discrete* card, so the gate read the dGPU's
  near-empty pool: measured live 2026-08-14, card0 reported 0.22 GiB used while
  the Halo held 97.8 GiB. It would have approved starting a second ~100 GiB
  model onto a full box. Cards are now classified by VRAM size and each server
  is gated against the pool it actually draws from.
- **Vulkan servers measured as ~0 GiB.** `measure_resident_gib()` read the KFD
  tree, which only covers HIP/ROCm; a RADV process has no KFD entry, so every
  Vulkan deployment looked idle while holding ~98 GiB — and `_server_pid()`
  fell through to the launcher wrapper. Both now use amdgpu's per-device
  `drm-resident-{gtt,vram}` fdinfo, which covers both runtimes and reports per
  card.
- Models on different cards are no longer treated as mutually exclusive. The
  verified dual-serving deployment (DS4 on the Halo + Qwen3.8 on the R9700,
  both resident) was previously unrepresentable.
- `_script_defaults()` reads every `VAR="${VAR:-default}"` on a line, not just
  the first. The serve.sh scripts pack several onto one line
  (`PORT=…; HOST=…; CTX=…`), so a line-anchored pattern silently missed all but
  the first of each group.

### Changed

- Registry entries whose runtime or weights the 2026-08-11..14 infrastructure
  consolidation removed are marked `startable=False` with a stated reason
  rather than deleted: the Ling and Step entries kept their weights but lost
  their runtimes, the IQ2_M profile lost both, and the Laguna/chadrockv2 GGUFs
  are gone. `ROCmFP4-qwen3.6-35b-a3b` and the chadrock entries are also flagged
  pending a device audit — their compose files pin `Vulkan0`, which now means
  the 32 GiB R9700 rather than the iGPU they were written for.
- The exclusivity graph is generated from two named groups (one Halo-resident
  model at a time; one R9700-resident model at a time) instead of being
  hand-enumerated pair by pair.

## [2026-08-11] - aria-shell-register fails quietly instead of popping up in every shell

### Fixed

- `scripts/aria-shell-register` no longer lets failures reach the tmux client.
  It runs from the `session-created`/`client-attached` hooks, so an uncaught
  exception surfaced as a `'... --ensure-capture ...' returned 1` message that
  intercepted keystrokes in every watched shell until dismissed with Escape.
  All paths now exit 0 and append the traceback to `/tmp/aria-shell-register.log`
  (`ARIA_SHELL_REGISTER_LOG`); `tmux pipe-pane`'s own stderr is captured for the
  same reason. This was the visible symptom of mongod being down for 9 hours —
  the popup said nothing about the actual outage.
- Mongo `serverSelectionTimeoutMS` for that script is now 3s
  (`ARIA_MONGO_TIMEOUT_MS`) rather than the 30s default, so a hook can't stall
  an attach for half a minute when the database is unreachable.

## [2026-08-10] - DS4 quality-first affine profile after the broad gate

### Changed

- The resident DS4 default is again ROCmFPX affine. A frozen 256-case suite tied
  affine and UD-IQ2_M at 238/256, but affine recovered all three deepest early-
  recall failures. Exact DSpark width 4 fell to 235/256 with three regressions
  and no recoveries; it remains an explicit optional throughput profile.
- Compatibility unit `deepseek-v4-quality-256k.service` now authoritatively
  serves `-c 65536 -np 6 --kv-unified -ub 256`, with prompt caching, Decode
  Fusion, batched APE, and 108 GiB start / 12 GiB live memory guards. The
  filename and registry slug retain `256k`; parsed unit geometry reports the
  actual six 64K slots.
- Six-by-128K and six-by-64K/ub512 both failed closed during a 33K prefill.
  Six-by-64K/ub256 completed it with about 17.8 GiB available and reused 32,768
  tokens in 4.33-4.40 seconds. Hermes and pi now advertise 65,536 tokens.
- Batched APE is enabled after 24/24 long-context responses were correct and
  byte-identical to the off baseline, closing its prior path-coverage gap.

Evidence: `benchmarks/ds4-selection/run-20260810T062000-0400/`.

## [2026-08-09] - DS4 to 200K/slot; the box has been OOMing

### Changed
- **DS4 `-c 230400` → `-c 204800`** (225K → **200K per agent**, `-np 6`
  unchanged), per Ben. Live and verified: healthy in ~130 s, `n_slots = 6`,
  `new slot, n_ctx = 204800` ×6. Hermes's `ds4`/`ds4-fast` `context_length`,
  CLAUDE.md and the topology runbook updated to match.
  - Worth knowing before reaching for this lever again: it frees only
    **~0.99 GiB**. Weights (85.26) and compute buffers (~15.6) are ~92% of the
    footprint and neither scales with `-c`. Stopping a co-resident server is
    worth ~10× more than a context cut.
- **`DS4.overhead_gib` 10.7 → 15.6.** Compute buffers scale with in-flight
  work, so the constant is now sized at the PEAK rather than at rest. Three
  readings, same weights: 94.56 GiB loaded-idle, 104.82 with one slot on a small
  request, **108.73 during a 56K-token prefill**. Projection is now 108.70
  against 108.73 observed. The 10.7 set earlier the same day came from the
  middle reading — on a day the box had already OOM-killed llama-server 8×.

### Measured — DS4 at 200K (2026-08-09, unique prompts, no cache hits)
| | measured | prior baseline |
|---|---|---|
| decode @ depth 0 | 19.49 tok/s | 19.69 |
| prefill, 883 tok | 139.64 tok/s | — |
| prefill, 3.5K tok | 162.80 tok/s | — |
| prefill, 14.2K tok | 137.17 tok/s | 160.66 (pp8192) |

Prefill peaks near 2–3K tokens and falls off with depth. Cutting context 225K →
200K cost nothing measurable, as expected — `-c` sizes the cache, not the math.

### Found — this box has been OOMing, repeatedly
Eight `oom-kill` events on 2026-08-09 alone, and the pattern starts the day
before. Not a rogue service: every non-LLM process is small (mongod 750 MB,
mongot 632 MB, embeddings 408 MB, every Hermes/proxy unit < 80 MB, ~4.4 GiB
total). The cause is that DS4 alone is **108.7 GiB of 124** under load.
- `2026-08-08 13:07, 13:27` — `deepseek-v4-quality-128k.service` OOM-killed ×2.
- `2026-08-08 13:27:56` — **`aria-tmux.service` OOM-killed**, taking a `claude`
  process with it. That unit exists specifically to own the tmux server hosting
  every watched session (see the Critical Gotchas in CLAUDE.md); an OOM kill
  reaches it the same way a careless `systemctl restart` would.
- `2026-08-08 13:30, 13:38` — a docker llama-server killed mid-load, allocating
  86.8 GiB — i.e. something tried to bring up DS4-sized weights inside Docker.
- `2026-08-09 01:04, 01:07` — the `-c 1382400` KV failure → SEGV (known).
- `2026-08-09 09:08–09:23` — llama-server OOM-killed 4× in 15 minutes.
Leading indicators were in the log the whole time: `swap 100% used AND only
3365 MB RAM free - OOM risk`, `disk: root 96% full`.

### Open — the GTT gate's blind spot
`_read_gtt_gib()` reads GPU-visible memory only, but on this unified-memory box
the CPU side draws from the same 124 GiB. gemma-aux (~2.6), the mongo/embeddings
containers (~1.9) and the desk's claude sessions (~1.5) are all invisible to it,
so the gate reads ~15 GiB free when ~8 is the truth. `overhead_gib` cannot fix
this — the gate needs a second term for non-GPU RSS. Noted, not yet implemented.

## [2026-08-09] - Slot occupancy is now observable (not just declared)

Follow-on to the six-slot cutover below. That work made the *declared* geometry
trustworthy; this makes the *live* occupancy visible, because over-subscription
produces no error — just a cold prefill every turn, which reads to a human as
"the model got slow" with nothing in any log.

### Added
- **`GET /api/v1/infrastructure/model-servers/utilization`** — busy/total slots,
  queue depth and throughput per RUNNING on-box server, read live from
  llama.cpp `/slots` + `/metrics`. Probes concurrently, 4 s timeout, degrades to
  `reachable: false` per server rather than failing the request.
  - **`saturated` is the field to watch, not `slot_utilisation`.** Every slot
    busy is the *design* (one slot per consumer, each holding its prefix). Slots
    *queuing* (`requests_deferred > 0`) is the failure: a queued request lands
    in whichever slot frees first, not the one holding its prefix.
  - **`saturated: null` means unknown, never false.** Without `--metrics` the
    server exposes `/slots` but not `/metrics`, so occupancy is readable while
    queue depth and throughput are not; `metrics_available` + `metrics_hint` say
    so instead of reporting zeroes.
  - `declared_*` (from the unit file) vs live (from the server) is a drift
    check — they disagree exactly when a unit was edited without a restart.
    This is the runtime counterpart to `check_pi_slot_budget()`, which can only
    catch the static misconfiguration.
- `probe_runtime()` / `RuntimeStats` and `base_url_for_spec()` in
  `infrastructure/model_servers.py` — the latter is the spec-side twin of
  `llm_route.base_url_for`, honouring `endpoint_override` so DS4's tailnet-only
  bind is not probed at a refused `localhost` URL.
- **MCP `model_server_utilization`** — Hermes can now see local load itself
  (before starting another server, or when the model "feels slow"). Verified by
  a direct `tools/list` + `tools/call` against the stdio server: 68 tools
  registered, live payload returned.
- **`/operate` "Slots" panel** — per-server occupancy meter beside the memory
  budget, plus a `SLOTS` status stat for the serving model. Colour follows the
  alarm, not the fill level (full is normal; queuing turns it red), and missing
  metrics render as blank rather than as zero.
- 5 tests, including that `saturated` is `None` — not a confident `False` —
  when metrics are unavailable.

### Fixed
- **`DS4.overhead_gib` 2.1 → 10.7.** Measured 2026-08-09: GTT read 94.56 GiB
  immediately after load but **104.82 with a single slot active** — compute
  buffers for an in-flight batch are ~10 GiB at `-b 2048` across 6 slots. The
  gate exists to refuse overcommit, so it must be sized for a *loaded* server;
  the idle figure under-counted by ~9 GiB in exactly the case that matters (a
  cold `aria-api` with no live measurement to prefer). Projection now 104.8
  against 104.82 observed.
- **DS4 was launched without `--metrics`** (gemma had it), so throughput and
  queue depth were unreadable on the one server they matter most on. Added to
  `ExecStart`; takes effect on its next start.

## [2026-08-09] - One big server, six slots; served context set in one place

### Changed
- **DS4 re-provisioned for multi-agent use** (`deepseek-v4-quality-256k.service`)
  — `-c 262144 -np 1` → **`-c 230400 -np 6 --kv-unified`**, i.e. **225K per
  agent across six slots**. ⚠️ **`-c` is PER SEQUENCE, not a total to divide by
  `-np`**: llama.cpp reports `n_ctx_seq == -c` and gives every slot its own
  full-size cache, so total KV = `-c` × `-np`. The first cutover attempt assumed
  the opposite (`-c 1382400`), tried to allocate 54 GiB of compressed KV, OOM'd
  and segfaulted — which measured the constant exactly:
  `57065472000 / (1382400 * 6)` = **6880 bytes/token**. Live verification:
  `n_slots = 6`, `new slot, n_ctx = 230400` ×6, GTT 94.56 GiB against a 96.2
  projection (conservative, the correct direction for an overcommit gate). Hermes main chat, the system pi-coding
  agent, all pi sub-agents and ARIA's background workers now share one server,
  one slot each, so each agent's prefix stays warm without evicting anyone.
  `--cache-ram` 4096 → 1024: the parked prompt-cache tier has logged `loads=0`
  on **every** DS4 run to date (one save, zero restores) and falls back to
  `forcing full prompt re-processing … (likely due to SWA or hybrid/recurrent
  memory)` at `n_swa = 128`, so nothing depends on it. In-slot context
  checkpoints DO work (measured: 22,091 tokens restored in ~0.5 s).
- **Served context is no longer copied by hand.** It lived in five places and
  only the unit's `ExecStart` was authoritative; the other four were all wrong,
  and Hermes's two disagreed with each other (262144 vs 131072) about one
  server. `model_servers.read_launch_geometry()` now parses `-c`/`-np` out of
  the systemd unit or compose file (mtime-cached), and
  `effective_resident_gib()` computes the footprint as
  `weights + KV(served -c) + buffers`. The 2026-08-05 staleness — declared
  86.5 GiB while really holding 94.08 after a `-c` change, under-counting the
  very gate meant to prevent overcommit — cannot recur.

### Added
- `served_ctx` / `slots` / `ctx_per_slot` / `geometry_source` on
  `GET /api/v1/infrastructure/model-servers`; the start-time GTT gate now
  projects from the real `-c` (`basis="projected"`).
- `ModelServerSpec.weights_gib` / `kv_kib_per_token` / `overhead_gib` — the two
  `-c`-invariant constants that replace a hand-maintained `resident_gib`
  (retained as fallback for uncharacterised entries).
- **`pi-code` concurrency cap** (`coding_max_concurrent_pi_sessions`=3,
  `coding_pi_reserved_slots`=3). The global cap could never protect llama.cpp
  slots: a `claude_code` session takes a global slot but **zero** local
  capacity, so four Claude sessions would block pi entirely while the GPU idled,
  and four pi sessions would over-subscribe their reserved slots.
  `check_pi_slot_budget()` reports drift — over-subscription otherwise presents
  as "the model got slow", not as an error.
- 9 tests covering the parse cases that would silently yield a wrong number:
  `--cache-ram`/`-cram` not matching `-c`, and `ExecStartPre`'s `sha256sum -c`
  being ignored.

### Fixed — dead endpoints
- `AGENTIC_URL` `:8105` → the `:8200` passthrough. It named a **stopped-by-
  default** server, so every `agentic` consumer dialled a port with no
  listener; `shells/extraction.py` did so every 10 minutes.
- `shells/extraction.py` no longer hardcodes `llm_backend="agentic"` —
  `SHELLS_EXTRACTION_BACKEND`/`_MODEL` route it to gemma-aux, which is what
  cheap bulk classification belongs on.
- `PI_CODING_PROVIDER_LLAMACPP`/`_AGENTIC` → `ds4`. pi's own
  `~/.pi/agent/models.json` is a second mapping layer that had drifted
  independently (`llama-cpp` → `:8103` qwen, down; `agentic` → `:8105`, stopped).
- `~/.pi/agent/settings.json` default provider/model → `ds4` (hand-run `pi`).
- Hermes `qwen-chat` `localhost:8107` → `:8103`. The 2026-07-30 move to `:8107`
  dodged `ridge-llama-proxy` but landed on DS4's port, which binds the
  **tailnet IP only** — refused on localhost, and a running DS4 would have
  answered qwen-labelled requests.
- Hermes `ds4`/`ds4-fast` `context_length` → both `230400`.
- Stale `id_slot` / `laguna-slot-proxy` prose removed from `config.py` and
  `agents/backends/pool.py` (those ports have not listened since 2026-07-28).

### Docs
- `docs/ops/LOCAL_INFERENCE_TOPOLOGY.md` — new **§11** (current topology, slot
  budget, single-source-of-truth rule, routing table, what was fixed, what is
  still unverified); §§1–10 kept as history behind a read-§11-first banner.
- `vault/ProjectAria/Design/COHERENCE_DESIGN.md` — decision-log **#36**; #16
  amended (pin-by-server is no longer available, so consumers are separated by
  slot again — but still never by an `id_slot`-rewriting proxy).

### Verified in production (2026-08-09 cutover)
- DS4 live at 6 slots × 230400; `aria-api` and `hermes-gateway` restarted clean;
  `GET /api/v1/infrastructure/model-servers` reports `ctx_per_slot=230400
  slots=6 est=96.2 measured=94.5`; an end-to-end completion through the `:8200`
  passthrough resolved to `DS4-0731-ROCMFPX-affine.gguf`.

### Open
- **llama.cpp segfaults on the KV-allocation failure path** (`status=11/SEGV`
  after `failed to allocate DeepSeek4 compressed KV cache buffer`), and
  `Restart=on-failure` retries it — an auto-restart of the pre-edit config raced
  an edit during the cutover and produced a misleading second failure. Stop the
  unit before changing `-c`.
- The parked prompt cache is still unproven on DS4 (`loads=0` historically).
  Nothing depends on it; re-check `loads=` before designing around it.

## [2026-08-07] - Ontology Memory Map + non-LLM service registry (Phases 0-5d)

### Added
- **Non-LLM service registry** (`api/aria/infrastructure/services.py`, new) —
  19 services (mongod, mongot, embeddings, aria-api, aria-tmux, aria-ui, tts,
  stt, hermes-gateway, hermes-webui, signal-cli, ridge/red proxies, samba,
  war-audio-*, obsidian bridge, ts-drop-capture), built from OBSERVED live
  state rather than documentation. **A deliberate sibling of
  `model_servers.REGISTRY`, never rows in it** — verified breakages if merged:
  `llm_route.match_requested` would proxy `model: "shared-mongod"` to :27017;
  `rank_resident` scores a missing `resident_gib_estimate` as `0.0` rather than
  excluding it; and `health.py`'s port-keyed `stopped_on_purpose` would make
  "mongod is down" read as "stopped on purpose". Disjointness by slug AND port
  is enforced by `tests/test_service_registry.py`.
  - New **`expected_state`** (`always_up` | `on_demand`) carries the semantic
    difference. 7 entries are flagged `needs_review` where the policy was
    inferred rather than confirmed.
  - **Found a live hole:** `aria-stt` had been EXITED for 7 days while
    `/health/services` probed it unconditionally — silently counted unhealthy
    every tick with no way to express "that's fine". `/health/services` now
    honours `expected_state`, and additionally reports the always_up services
    that have no HTTP surface (signal-cli, hermes-gateway, samba, aria-tmux),
    which nothing was watching before.
  - API: `GET /infrastructure/services`, `/services/{slug}`,
    `POST /services/{slug}/{start,stop}`, and **`GET /infrastructure/running`**
    — a union read over both registries.
- **Ontology Memory Map** (`api/aria/ontology/`, new: `models`, `store`,
  `seed`, `projection`, `crosslink`, `emitter`) — 100 entities / 102 relations
  live, with `ontology_vector_index` + `ontology_text_index` in mongot.
  - **Projected, not hand-seeded** (the doc's revised §4): 53 projects from
    `db.projects`, 32 services from both registries, remote machines from
    `db.nodes`. Only ~14 durable entities (machines, devices, datastores,
    networks, person) are hand-authored. Vanished things go `stale`.
  - S3 ownership enforced at the write boundary in `store.upsert_entity`, not
    per caller: a projection physically cannot overwrite `summary`/`aliases`/
    `tags`; contradictions go to `scan_review`.
  - `OntologyProjectionEmitter` rides the S2 scan worker. `ScanReconcileWorker`
    gained an **`always_run`** emitter flavour — the default contract fires only
    on a machine-snapshot diff, but this projection's inputs (registries,
    `db.projects`) change without one, so it would have refreshed only by
    coincidence.
  - **3,709 memories cross-linked with ZERO inference** via most-specific-root
    path-category mapping (§7a). Verified it does not regress to plain prefix
    matching: `project:aria` got 1,205 and the parent `project:development`
    only 246.
  - `kg` CLI (`~/.claude/skills/agent-memory/kg`) — **stdlib-only over HTTP**,
    so unlike its sibling `mem` it needs no venv and the same file runs from
    the MacBook/Red/Ridge.
  - MCP: `whats_running`, `list_services`, `start_service`, `stop_service`,
    `kg_search`, `kg_entity`, `kg_map`, `kg_memories` (restart
    `hermes-gateway.service` to load).

### Fixed / corrected during the build
- **Dropped bulk `mentions` edges** after measuring the damage: one edge per
  (memory, entity) duplicated `memories.entities[]`, grew ~670/day, and buried
  every structural edge — `project:aria` came back with 500 of 500 incoming
  edges being mentions, hiding `runs_on machine:corsair-ai`. `entities[]` (now
  indexed) IS the cross-link; `mentions` stays in the vocabulary for
  hand-authored edges and is excluded from neighborhood views by default.
- **Entity extraction pointed at `gemma-4-e4b-Q4` explicitly**, not the
  resident model. DS4 is a reasoning model that spent its whole 256-token
  budget thinking and got truncated before emitting JSON — failing *silently*
  as "zero entities" for every memory. `max_tokens` raised to 768 and
  `parse_json_object` now strips `<think>` blocks and fences, because the
  passthrough means the model on the other end is not fixed.
- **Added a verification gate** (`verify_slug`): the LLM proposes, the memory
  text disposes. It rejected a WRONG-QUANT model server
  (`ling-3.0-flash-q5km` → the MXFP4 entry — the most dangerous kind of wrong),
  an unrelated ephemeral container matched to `hermes-webui`, and entities
  absent from the text. `person` was dropped from the extraction catalog (one
  person in the graph; every `/home/ben/` path was tagging it).
- **Project slug derivation unified** in `project_entity_slug()` — the
  projection and the cross-link deriving slugs differently would point
  `entities[]` at entities the graph does not have. Prefers `db.projects.slug`
  over `name` so a rename cannot mint a second entity.
- `project_roots()` now reads `relevant_paths` as well as `path`: the harvested
  "ARIA" row has no `path` at all and was left with no location and no host
  edge.
- Deterministic tie-break in `PathProjectIndex` — "ARIA" and "ProjectAria" both
  genuinely claim `~/Development/ProjectAria`, so without a stable secondary
  sort the winner depended on Mongo's iteration order, reassigning thousands of
  memories between runs. The collision is now raised as a `scan_review` item
  rather than silently resolved.
- `INVERSE` no longer claims `member_of`/`part_of` are inverses — they are
  near-synonyms, and treating them as a pair would flip edge direction on
  traversal (`corsair-ai member_of tailnet` reading back as `tailnet part_of
  corsair-ai`). Caught by a test.

### Fixed — project harvester created duplicate rows (root cause + data)
- **`harvest.py` upserted on `{"slug": <directory name>}` and never checked
  whether an existing project already claimed that path.** So the hand-created
  "ARIA" (slug `aria`, real summary, claiming `~/Development/ProjectAria` via
  `relevant_paths` and carrying **no `path` at all**) was shadowed by a
  harvested twin "ProjectAria" with an empty summary. Both rows claimed the
  same root, which split that project's memories, cockpit rollups and path
  attribution, and made the winner depend on Mongo's iteration order. The
  harvester now resolves an existing project by `path`/`relevant_paths` before
  falling back to the directory slug; new directories still register normally.
  Regression tests in `tests/test_harvest_dedup.py`.
- **Merged the existing duplicate**: kept the curated `aria` row (its slug is
  what 1,246 memories already point at), folded in the harvested
  `path`/`activity_status`/`git`, deleted the twin. Zero memories orphaned —
  `project:projectaria` ended with 0 links. Its entity remains as a `stale`
  tombstone per S3 rather than being deleted, and the `scan_review` conflict
  was acked.

### Added — Services panel in the Web UI (`/operate`)
- `ui/src/app/operate/page.tsx` gained a **Services** card plus a `SERVICES`
  status stat, backed by new `listServices`/`startService`/`stopService` in
  `api-client.ts`. Rows sort **unhealthy first**, then always_up before
  on_demand — a 19-row alphabetical list buries the one row that matters.
  `manageable: false` entries (aria-api, aria-tmux, samba) render as `locked`
  rather than offering a button that would 409. The `needs_review` entries are
  marked ⚠ with a footer explaining that an outage on those will not alert
  until their `expected_state` is confirmed.
- The services fetch is tolerated independently of the model-server fetch, so
  the registry going quiet cannot blank the page's primary view.
- The ontology graph deliberately has **no** UI yet — reachable via HTTP, the
  `kg` CLI and MCP only.

### Scope
- Phase 5e (LLM pass over the 13,671 `shell_extraction` +
  `claude_session_digest` memories) remains **closed**, enforced by
  `BULK_SOURCE_TYPES`. The targeted backfill covers the 877 curated memories,
  is resumable via `entity_extraction_at`, and measured ~5-10s/memory.
- New settings: `ONTOLOGY_ENABLED` (on), `ONTOLOGY_PROJECTION_EMBED` (off —
  re-embedding 100 entities costs ~78s on the CPU service),
  `ONTOLOGY_EXTRACTION_ENABLED` (off), `ONTOLOGY_EXTRACTION_MODEL`,
  `ONTOLOGY_EXTRACTION_VERIFY`.
- Tests: `test_service_registry.py` (19) + `test_ontology.py` (39). Full suite
  1126 → 1184 passing.

## [2026-08-07] - ONTOLOGY design doc revised against live state (docs only, no code)

### Changed
- **`ONTOLOGY_MEMORY_DESIGN.md` (vault) re-verified and revised.** Still unbuilt
  — no `ontology_*` collections, no `/api/v1/ontology/*`, no `kg` command — but
  three weeks of drift had invalidated its numbers and its seed plan.
  - **Scale correction.** The doc was denominated in "~1,186 existing memories"
    (correct at writing: 1,219 predate 2026-07-18). `aria.memories` is now
    **14,590** — 13,371 of them produced *by the shell/scan workers this plan
    was blocked behind*. The dependency it waited on invalidated its own
    estimate. At ~670/day, entity extraction is a continuous obligation, not a
    backfill; Phase 5 split into 5a–5e with the 13,671-doc bulk pass
    **deferred** (local-only host since 2026-07-26 — no burst capacity).
  - **Hand-written seed list deleted (§4).** It seeded `qwen-chat`,
    `qwen-agentic` (retired), `fireworks` (key gone 2026-07-23), and the
    `laguna`/`:8095` slot topology (retired), while missing the model-server
    registry, chadrock, gemma-aux and the `:8200/llm/v1` passthrough. New rule:
    **project what churns, hand-author what doesn't** — `project`/`service`
    entities derive from `db.projects` (53) and the model-server registry (15
    slugs); only ~14 durable machine/device/network entities stay hand-written.
    Hand-seeding services wrote a dead map in as ground truth and forked
    collections that already own those facts, against this plan's own S3
    convention.
  - **`categories[]` gap closed (§7a).** The doc never acknowledged that
    path-shaped categories (`~/Development/ProjectAria`, 1,202 docs) are already
    entity references; they now seed `entities[]` deterministically, LLM-free,
    before any extraction runs — with the most-specific-root caveat carried over
    from C4's `PathIndex`.
  - Confirmed intact: `memories.entities[]` is still **0 of 14,590** (the hook
    is unclaimed), and the S2 `ScanEmitter` substrate matches what the plan
    assumed. Minor: `consolidated_into` 227 → 250; `nodes` holds only remote
    nodes (1) and *not* corsair-ai, so it can't be the sole `machine` source.
  - Phase 4 flagged: no `kg` binary exists yet, and the native-Windows client
    for Red/Ridge remains unestimated with no prior art in this repo.

### Decided (2026-08-07, Ben)
- **Non-LLM services get registry coverage** ("need to know what is running") —
  mongod, mongot, embeddings, aria-api, hermes-gateway, signal-cli, samba, tts,
  stt, ui. New **Phase 0** in the ontology plan, but independently valuable and
  shippable on its own: `api/aria/infrastructure/services.py` + `db.services` +
  `GET /api/v1/infrastructure/running` (a union read over both registries).
  **Deliberately a SIBLING registry, not rows in `model_servers.REGISTRY`** —
  verified against the code, sharing it breaks three ways:
  (1) `match_requested()` matches on slug, so `model: "shared-mongod"` would
  proxy LLM traffic to :27017; (2) `rank_resident()` scores a missing
  `resident_gib_estimate` as `0.0` rather than excluding it
  (`llm_route.py:111`), making non-LLM rows auto-route candidates whenever no
  model is resident; (3) decisively, `health.py:230` builds `stopped_on_purpose`
  by port from `model_servers.status()` because the big LLM servers are
  RAM-exclusive and *are* meant to be down — non-LLM services invert that, so
  sharing the registry would make "mongod is down" read as "stopped on purpose"
  and silence the alert the decision exists to raise. New `expected_state`
  (`always_up` | `on_demand`) field carries that distinction.
- **The 13,671 machine-generated memories (`shell_extraction` +
  `claude_session_digest`) will not get an LLM extraction pass.** Phase 5e is
  closed rather than deferred; forward-only extraction plus the LLM-free
  path-category mapping covers the need.

## [2026-08-05] - Hermes follows ARIA's resident model; the passthrough learns to route

### Added
- **Hermes no longer names a model.** Its `model.default`/`model.provider` were
  a hand-edited pair that had to change on every swap (qwen → laguna → chadrock
  → DS4), and forgetting it left Hermes dialling a dead port. `~/.hermes/
  config.yaml` now carries a `custom:aria` provider pointing at ARIA's
  `/llm/v1` passthrough with the synthetic model id `aria-resident`, so
  **starting a model in ARIA is the only step** — no config edit, no gateway
  restart. `api_key: ${env:ARIA_API_KEY}` (already present in Hermes's `.env`
  and equal to ARIA's `API_KEY`), so no secret is stored in the config file.
  The edit was surgical: the file's ~36KB of load-bearing comments are intact,
  unlike Hermes's own config writers, which round-trip through `yaml.dump` and
  drop them. Pre-switch file: `config.yaml.bak-aria-follow-20260805T231323`.
- **The passthrough now routes, not just forwards** (`infrastructure/
  llm_route.py`, new). More than one model can be resident — gemma is CPU-only
  and coexists with anything, and the chadrock+qwen split is a deliberate
  ~89 GiB pair — so "the local model" was ambiguous. Precedence is now:
  1. the request's own `model` field when it names a running server (by slug or
     .gguf filename). llama.cpp ignores unknown `model` values entirely, so the
     field was free to use as a selector; this is what lets a consumer pick
     between loaded models with no restart;
  2. an operator pin (`GET`/`PUT /api/v1/infrastructure/llm-route`, stored in
     the fixed-`_id` `app_state` doc, same pattern as C4's active project);
  3. auto — largest `resident_gib` among running on-box servers (prior sole
     behaviour, unchanged as the default).
- **`GET /llm/v1/models` is now a catalogue** of every loaded server plus the
  synthetic `aria-resident` entry, with each model's real `n_ctx` read from its
  own backend. The `aria-resident` entry advertises the *smallest* resident
  context, since any loaded model may serve it and over-promising overflows the
  moment the auto pick moves.
- **`/operate` gained a "Local model route" panel** (auto / per-model pin, with
  the resolved server and the reason), a `SERVING` status stat, and a "Serve
  this" action on the model detail. Both ARIA and Hermes follow this one
  control, so it is the single place to look instead of inferring the active
  model from a completion.
- **MCP `get_llm_route` / `set_llm_route`** — Hermes can see and change the
  model backing its own replies. *Requires a `hermes-gateway.service` restart
  to appear in the toolset.*

### Notes
- Naming a server that ARIA knows but that is **stopped** is a 503 with the
  running/startable lists, not a silent downgrade to a different model. A
  **stale pin** does the opposite and degrades to auto — one forgotten setting
  must not take every consumer offline — but says so in `reason`.
- `context_length` for `aria-resident` in Hermes's config is set to 100000: the
  floor of the big models in the registry (qwen 100000, DS4 131072, chadrock
  262144), so a static value stays safe across a swap while clearing Hermes's
  hard 64,000 minimum. If only `gemma-4-e4b-Q4` (CPU, 65536) is resident this
  over-promises — and in practice gemma cannot serve Hermes's main chat at all:
  a ~26K-token system prompt takes minutes to prefill on CPU.

## [2026-08-05] - `LLAMACPP_URL` follows the resident model (kills the recurring "llm (ConnectError)" page)

### Fixed
- **`selfcheck` paged `DEGRADED: llm (ConnectError)` every 10 minutes about a
  server stopped on purpose.** DS4-0731 (`:8107`) became the resident big model
  earlier today and is RAM-exclusive with `ROCmFP4-qwen3.6-35b-a3b` (`:8103`),
  so `:8103` went permanently down — but `.env` still pointed `LLAMACPP_URL`
  there. Each tick enqueued an alert that woke the Hermes alert-triage cron to
  spawn a diagnostic coding agent for a non-incident. This is the *fourth*
  instance of the same failure (qwen → laguna → chadrock → DS4), so the fix is
  structural rather than another port edit: **`LLAMACPP_URL` now points at
  ARIA's own `/llm/v1` passthrough** (`api/routes/llm_proxy.py`, added earlier
  today), which resolves to whichever on-box server is actually resident. The
  `config.py` default was moved in step so an incomplete `.env` can't disagree.
  `LLAMACPP_API_KEY` must equal `API_KEY` — the proxy sits behind
  `api_key_middleware`, which accepts the key as `Authorization: Bearer`.
- **`GET /health/services` called deliberately-stopped servers unhealthy.** The
  big on-box servers are mutually RAM-exclusive, so all but one are stopped by
  design; the probe painted the TUI/web health screen permanently red. It now
  consults the model-server registry and reports
  `"<slug> stopped (start on demand)"` (ok) instead of `ConnectError` — the
  same reasoning as the existing `pool_enabled` skip in `selfcheck.py`. A
  registry failure degrades to probing, never to a 500.
- **Both LLM probes now send the ARIA key**, since `llamacpp_url` resolves to
  this app. Without it the probe 401s; a raw llama.cpp server ignores it.
- **`selfcheck._check_http` counted `401`/`403` as healthy** (`< 500`). A
  service that answers but rejects our credential is a real misconfiguration —
  now graded unhealthy, matching `/health/services`.
- **`test_port_allocation_skips_used_ports` asserted a literal port** (`8108`)
  and broke the moment `Ling-3.0-flash-MXFP4` was registered there. It now
  derives the expectation from `REGISTRY` and asserts the property (lowest free
  port in range), so registering a server is no longer a spurious failure.

### Known / not changed
- Agent `pi-coding` still routes `backend=agentic` → `:8105`
  (`Chadrock-ROCmFP6-qwen3.6-27b`), which is RAM-exclusive with DS4 and so
  cannot serve while DS4 is resident, while the DS4 registry entry records the
  pi coding agent as using provider `ds4`. Repointing an agent's model is a
  routing decision, left for Ben.
- `stt` (`:8003`) is down — the `shared-stt` container is not running. Not
  probed by `selfcheck`, so it never paged; plausibly stopped on purpose to
  free RAM for DS4 (86.5 of 124 GiB GTT).

## [2026-08-02] - Codex desk-path parity: `codex` routes through ARIA Shells

### Added
- **`codex()` wrapper in `~/.bashrc`** — `codex` (however invoked, e.g.
  `codex --yolo`) now gets the same ARIA Shells experience as `claude`: one
  persisted, watched, per-directory tmux session (`claude-codex-<dir>` — the
  `claude-` prefix keeps it inside the fleet's single adoption namespace),
  attach-if-live, stale-session clearing, the same never-freeze curl + poll
  contract, and `--no-aria` / `command codex` escape hatches.
- **`scripts/aria-codex-launch`** (symlinked into `~/.local/bin`) — the
  resume-aware launch shim: `codex resume --last` (cwd-filtered by default in
  codex ≥0.146) with a fast-non-zero-exit fallback to a fresh session, always
  under `--dangerously-bypass-approvals-and-sandbox` (--yolo).
- **`launch_command` exposed on `POST /api/v1/shells`** — the service layer
  already supported an arbitrary launch command (coding-session manager);
  the HTTP route now passes it through (takes precedence over
  `launch_claude`). Route test added.

- **Codex autotrust (`shells/codex_trust.py`)** — codex 0.146 shows its
  directory-trust dialog even under `--yolo`, which would hang a detached
  spawn. `create_shell` now pre-seeds `[projects."<workdir>"]
  trust_level = "trusted"` in `~/.codex/config.toml` (atomic append; refuses
  corrupt TOML; never flips an explicit non-trusted entry) when the launch
  command is codex, picking the right trust writer per CLI under the existing
  `shells_claude_autotrust` flag. Override path via
  `shells_codex_config_path`. Verified live: fresh spawn lands straight in
  the composer, no dialog.

### Fixed
- **`codex()` wrapper hijacked codex subcommands** — `codex exec`/`login`/
  `apply`/`resume` etc. from outside tmux were swallowed into a desk-session
  attach; known subcommands and `-h/--help/-V/--version` now pass through to
  the real binary.
- **`codex` was invisible to login shells** — the binary lived only in
  `~/.npm-global/bin`, which is added to PATH by an interactive-only line in
  `.bashrc`, so `bash -lc` (what tmux launch commands run under) couldn't
  find it. Symlinked `~/.local/bin/codex` → `~/.npm-global/bin/codex`,
  matching how `claude` resolves.

## [2026-08-02] - Docs cleanup: completed plans merged + retired

### Changed
- **Fully-implemented plan docs retired from the vault**, their durable content
  merged into the standard docs first: `SHARED_SERVICES_DESIGN.md` (S1–S5 all
  live; S3 ownership convention + S4 security posture + S2 status now in
  CLAUDE.md; open follow-ons → BACKLOG) and `PiFlow_Parity_Plan.md` (the
  "declarative engine, not a JS runtime" decision now in CLAUDE.md's Workflows
  section; open follow-ons → BACKLOG "Salvaged from retired plans"). Also
  deleted: the empty `Task Router.md` and two stale `COHERENCE_DESIGN.md.bak-*`
  backups (the vault has its own git-backed snapshots).
- **CLAUDE.md gained a "Coherence Layer" section** — the operational summary of
  C1/C2/C3/C4/C6/C8/C9 + nudge-paused-shells (seams, flags, and the
  most-specific-root attribution rule) — and the MCP tool list now includes the
  cockpit/obsidian/linear/nudge tools (~40 tools).
- Vault `ARCHITECTURE.md` got a status note (it predates the 2026-07-28
  Hermes-front-door clarification); `COHERENCE_DESIGN.md` /
  `ONTOLOGY_MEMORY_DESIGN.md` Related-links updated for the retired sibling.
  Kept as living docs: COHERENCE (C5 + C8 verify open), MULTI_MACHINE_FLEET
  (B2 live-verify open), ONTOLOGY (unbuilt), ARCHITECTURE, SPECIFICATION.

## [2026-08-02] - Coherence design completed (C8, C4, C6, C3) + nudge-paused-shells

### Added
- **C8 — Remote-node `run_command` + host-aware verification gate.** The
  `aria-node` agent gains a `run_command` op (subprocess in a given cwd,
  timeout-bounded, returns `{exit_code, output_tail}`; advertised in node
  capabilities); `ShellService.run_node_command()` dispatches it over the
  existing `shell_commands` queue with the TTL stretched to cover the command's
  own timeout. The C1 gate no longer skips remote sessions — the check runs ON
  the node via `watchdog._run_remote_gate_check()`; an unreachable node counts
  as a gate *failure* (verify, don't assume), bounded by the existing retry cap.
  Live end-to-end verification on the MacBook is still pending (needs
  `aria-node` running there).
- **C4 — Project Switcher + Per-Project Cockpit** on all three surfaces:
  - API (`api/routes/digest.py`): `GET /projects/overview` (attention-ranked:
    4×blocked shells + 3×gate-failed sessions + 2×unacked alerts + stale tasks
    + running sessions), `GET /projects/{slug}/cockpit` (git live+harvested,
    scoped shells/sessions with `gate_runs`, open+stale tasks, `machine_scan`
    what-changed memories, scoped alerts, Linear read cache, priced spend,
    vault folder), `GET/PUT /projects/active` (server-side focus in a
    fixed-`_id` `app_state` doc). Registered *before* the planning router so
    the literal paths beat `/projects/{project_id}`. Alerts gained optional
    `project_path` attribution.
  - Web (`ui/src/app/cockpit/`): switcher card grid + per-project panels,
    10s resilient polling; Cockpit card on the landing page.
  - TUI: `screenProjects` + `screenProjectCockpit` (Tools menu "Projects" /
    `j`), `f` sets the shared active project.
  - MCP: `projects_overview`, `project_cockpit`, `set_active_project`.
- **C6 — Obsidian long-form surface** (`integrations/obsidian.py`):
  `ObsidianWriter` — atomic temp+rename writes into
  `vault/<RepoName>/{Design,Specs,Analysis,Research,Planning}/`, never-clobber
  (timestamp-suffixed sibling), human-edit guard on co-drafted appends,
  write-only by design. Research runs auto-publish finished reports
  (best-effort, `vault_path` recorded on the run). `POST /obsidian/publish` +
  MCP `publish_to_obsidian`. Gated by `obsidian_enabled` (default off).
- **C3 — Linear sync + backlog reconciliation** (`planning/linear_sync.py`):
  per-project opt-in via `linear_project_map`; mirrors open issues into
  `tasks` (`source.type="import"`, Linear authoritative; upstream-closed →
  done); LLM judge audits tickets against local evidence (project activity,
  machine_scan memories, git, vault docs) — ≥0.9 confidence *with cited
  evidence* auto-resolves in Linear (+ evidence comment + alert, reversible),
  ≥0.75 proposes for one-tap confirm, else leaves open; human "keep" pauses
  re-judging 7 days. Routes `/linear/tickets` +
  `/linear/issues/{id}/{resolve,keep,do-now}` (do-now spawns a coding session
  in the project workspace); MCP `create_linear_ticket`. Fully dormant until
  `linear_enabled=true` + an API key land in `.env`.
- **Nudge-paused-shells** (Ben's request, outside the original design):
  `POST /shells/{name}/nudge` + MCP `nudge_paused_shell` — wakes a shell
  paused at a prompt (bare Enter at safe "press enter" prompts, else a
  configurable continue instruction). Attempts persist on the shell doc, so
  the new Hermes cron sweep (`ARIA paused-shell nudger`, */15, in
  `~/.hermes/cron/jobs.json`) gets three-strikes semantics across runs; after
  3 consecutive failed nudges an alert (`source="shells:nudge"`) rides the
  alerts → Hermes triage → Signal path, where the triage prompt now
  special-cases it into a reply/STOP/IGNORE confirm menu (no diagnostic agent
  spawn). Guards: killswitch/e-stop, `no-nudge` tag, min-paused threshold,
  per-shell debounce.

### Fixed
- **The C1 gate's give-up alert was silently dropped**: `notify()` classifies
  every `coding:*` source as informational, which swallowed
  `source="coding:gate"` — the one alert the gate design depends on. Carved
  out; the gate alert also now carries its workspace as `project_path`.

### Verified
- 1033 API tests pass (was 988; +45 covering the C8 gate branches, node
  run_command, cockpit read models, nudge endpoint, ObsidianWriter, Linear
  worker thresholds). `next build` clean; Go build/vet/test clean.

## [2026-07-31] - Pi Coding corrected to launch upstream Pi

### Changed
- Replaced the custom ARIA `pi-code` agent loop with the installed Pi 0.83
  `pi` executable. `PiCodeBackend` now builds
  an interactive `pi --provider ... --model ... <task>` command and uses the
  same generic watched-shell launch path as Claude Code and Codex.
- Deleted the obsolete `aria pi-code run` CLI wrapper and the special
  conversation/start/deferred launch path. Pi now owns its tools, context
  files, session JSONL, compaction, and TUI; ARIA retains worktrees, tmux fleet
  capture/control, concurrency, watchdog, review, notifications, and Ralph.
- Pi 0.83's `--session-id` is pinned to the ARIA coding-session UUID, giving
  both persistence layers one stable identity and deterministic resume.
- `pi_coding_agent` is now a compatibility launcher for a real Pi coding
  session and requires a workspace; it no longer creates an ARIA chat
  conversation or calls `Orchestrator.process_message()`.
- Added explicit Pi providers for local Qwen 35B (`:8103`), local Chadrockv2
  27B (`:8105`), and Ridge. `pi-coding`/`pi-coding-ridge` database rows now act
  as launch profiles selecting provider/model and appended role instructions.

### Verified
- Pi enumerates all three configured providers/models.
- 988 API tests and all Go TUI tests pass. A live managed session produced two
  requested responses across separate turns through ARIA's shell input/output
  API while remaining inside the same Pi TUI process. A second smoke test on
  Pi 0.83 confirmed the Pi JSONL filename and header use ARIA's exact session
  UUID.

## [2026-07-31] - TUI: session delete/history browser; Fleet/Health/Memory rendering bugs; shell extraction gap closed

### Added
- **`DELETE /api/v1/coding/sessions/{id}`** (`agents/session.py` `delete_session()`,
  route in `api/routes/coding_sessions.py`) — permanently removes a
  `coding_sessions` record; refuses (409) while `status` is `running`/`queued`.
  Does not touch the underlying shell/shell_events (owned by the watched-shell
  subsystem's own retention, not this manager). Wired into the TUI: `d` on a
  coding-session sidebar row now deletes it (mirrors the existing conversation-
  delete convention), guarded client-side against deleting an active session.
- **TUI Shell History screen** (`y` hotkey, `tui/internal/ui/components/history_view.go`
  + `history_detail.go`) — the sidebar and Fleet only ever showed
  coding_sessions/active shells; the `shells` collection itself (hundreds of
  entries, months of history, decoupled from coding_sessions by design) had no
  browsing surface at all. New screen lists every shell regardless of status
  (`GET /shells`, already unfiltered/unbounded server-side — no new backend
  route needed for the list), client-side text filter (`/`), Enter drills into
  a read-only scrollback viewer sourced from `GET /shells/{name}/events`
  (works for stopped shells — reads `shell_events` directly, no live tmux pane
  needed).
- **`POST /api/v1/shells/extraction/backfill`** (`shells/extraction.py`
  `ShellExtractionWorker.backfill()`) — one-time (re-runnable) catch-up that
  drains every **stopped** shell's unextracted event backlog to completion,
  not just the single 1000-event chunk per shell per periodic tick. Capped at
  200 chunks/shell as a safety backstop.
- TUI: generic background-command error surfacing (`Model.lastErr`, footer
  banner) — `errMsg` used to be handled only on `screenChat`, so a failed API
  call on any other screen failed completely silently.

### Fixed
- **Shell-history memory extraction silently skipped ~all of it.**
  `ShellExtractionWorker._tick()` only ever queried
  `status=["active","idle"]` — once a shell went `stopped` (the status of the
  vast majority: 299/304 shells) it was excluded from extraction *permanently*,
  with no path back. Confirmed live: 215 shells with real captured events,
  some with 12,000+, had never been touched by extraction at all (42 shells
  above the 20-event minimum). `_tick()` now includes `stopped`; the one-time
  backfill above cleared the existing backlog (~209k events / 42 shells).
  First backfill attempt included active/idle shells too and hit a real
  infinite loop — an active shell's `line_count` kept growing while the loop
  held a stale copy of it, so the stale-cursor self-heal kept firing and
  resetting to the same point every pass. Fixed by scoping `backfill()` to
  `stopped` only (frozen `line_count`, no race) plus the chunk cap as
  defense in depth.
- **`Memory.Source` typed as `string` in the TUI's Go client** (`api/client.go`)
  but the real `/api/v1/memories` response's `source` field is an object
  (`{"type": ..., "project": ..., ...}`) — every single memory list/search
  call failed to decode, and because errors were only surfaced on
  `screenChat`, the Memory Browser just looked permanently empty with zero
  indication anything was wrong. Field was unused by the TUI; removed.
- **`lipgloss.Style.Render(text + "\n")`** — baking the trailing newline
  *inside* the styled string instead of appending it after `Render()`
  returns — desyncs a `bubbles` `viewport.Model`'s line handling and silently
  swallows whatever content line immediately follows. Ate the Fleet table's
  first session row (always the newest, e.g. a just-started session),
  Health's first service check, and Memory Browser's first result, on any
  sufficiently wide terminal. Fixed in `fleet_view.go`, `health_view.go`,
  `memory_browser.go`, `observations.go`, `tools_browser.go`,
  `usage_monitor.go`. Regression tests added (`fleet_view_test.go`,
  `history_view_test.go`) reproducing the exact width/content shape that
  triggered it live.
- Deleted 84 stale coding_sessions records (test artifacts + genuinely
  finished old sessions, oldest 85 of 96 total) accumulated across TUI
  worktree-feature testing; archived recoverable transcripts to
  `.aria-archived-sessions/` in their workspaces first.

### Notes
- `agent_mail (41)`, `coding_sessions (8)` etc. in the DB browser now line up
  with what Fleet/History actually show — this was the concrete symptom that
  surfaced the Fleet/Memory rendering bugs (reported as "gaps between what
  we discussed and what's showing up").
- Extraction backfill runs via `ClaudeRunner` (the Claude Code CLI,
  `use_claude_runner=True` default) when available, not the local
  `qwen3.6-35b-a3b` server — so it does not contend with Hermes's chat model
  as initially assumed.

## [2026-07-30 audit] - Model/agent/runtime pairing audit: 5 real mismatches fixed

Full cross-check of every agent → backend → URL → bound server → live alias,
plus registry ↔ compose ↔ disk ↔ listening ports, plus Hermes's own config.
All four ARIA agents now resolve MATCH (backend URL == bound server endpoint);
registry/compose wiring verified clean; no duplicate ports.

### Fixed
- **Hermes's primary model was named `laguna-s-2.1`** but has served
  Qwen3.6-35B-A3B-MTP **ROCmFP4** on :8103 since the 07-28 split — poolside
  Laguna (:8095) is stopped and hasn't backed Hermes since. Renamed to the
  alias the server actually reports (`qwen35b-a3b-mtp`). The provider KEY stays
  `laguna` on purpose: ~20 references point at it, so renaming the key risks
  more than the confusing name costs. Confirmed: Hermes IS on the ROCmFP4 Qwen.
- **Hermes's `model_aliases` silently overrode their own providers.**
  `qwen-chat` and `qwen-agentic` both carried `base_url: :8103` +
  `model: qwen35b-a3b-mtp`, so selecting "qwen-agentic" ran the 35B on :8103
  instead of the 27B on :8093 — wrong model, no error. Each alias now matches
  its provider.
- **`PLANNING_AMBIENT_MODEL` / `HEARTBEAT_MODEL` were still `laguna-s-2.1`**
  while :8103 serves `qwen35b-a3b-mtp`. Worked only because llama-server ignores
  the request model field in single-model mode. Ambient capture fires on EVERY
  conversation turn, so this was live config lying about which model runs.
- **`config.py` defaulted `llamacpp_url` to :8092 — now the Ridge WoL proxy.**
  A missing/incomplete `.env` would have pointed ARIA's PRIMARY chat backend at
  Ridge and woken a sleeping gaming PC on every call. Defaulted to :8103.
  `agentic_url` default likewise moved :8093 → :8105 to match its repurposing
  as the local coding backend (comment still described the retired qwen-agentic).
- **`context1` published on `0.0.0.0:8081`** — every other model server was moved
  to loopback+tailnet on 2026-07-21, but context1 was missed because it was
  already stopped. llama.cpp has no auth, so starting it would have offered the
  whole LAN free GPU inference. Now loopback + tailnet like the rest.

### Notes
- 985 tests pass. Hermes config backed up before editing.
- Deliberately left: the `aria` agent stays unbound (it is disabled by design;
  binding it would consume :8103's 1:1 slot for no benefit). `:8094`/`:8099`
  listeners are the wake-proxies, `:8101` is an unrelated `api` process —
  none are model servers.

## [2026-07-30 later] - No quota fallback; 1:1 agent↔model bindings; Hermes memory unstuck

### Changed
- **The Claude-quota fallback is OFF by default.** `coding_routing_fallback_backend`
  now defaults to `""` — an exhausted quota makes a coding task **fail and pause**
  instead of quietly finishing on a weaker local model (Ben's call: a silent
  downgrade produces work to a different standard than the one asked for). This
  needed a dedicated `QuotaCooldownError`, because `start_session` wraps routing
  in a broad `except Exception -> use defaults`; a generic raise would have been
  swallowed and the task would have run on claude_code straight into the dead
  quota. Setting a backend re-enables demotion (both paths tested).
- **Agents are 1:1 with model servers.** `pi-coding` and `pi-coding-ridge` were
  BOTH on Ridge; `pi-coding` now runs locally on Chadrockv2 via the `agentic`
  backend slot (`AGENTIC_URL` :8103 → :8105, that slot was documented unused),
  and `search-agent` moved from `llamacpp` to its own `context1` backend.
  Bound: pi-coding→Chadrockv2, pi-coding-ridge→Ridge, search-agent→context1-Q4.
  Verified the one-agent-per-server rule rejects a second bind with a 409.
- **`qwen3.6-35b-a3b-Q4` moved :8092 → :8107.** `ridge-llama-proxy` holds :8092
  on the tailnet IP, so that service could never have bound there.

### Fixed
- **selfcheck alerted on a deliberately-stopped server.** With chadrock shut
  down (`pool_enabled=false`) the probe reported DEGRADED every tick; each
  alert woke the Hermes triage cron, which spawned a diagnostic coding agent to
  investigate a server that is off on purpose. The probe is now gated on
  `pool_enabled`, matching how context1 and `/health/services` already omit
  disabled backends rather than counting them unhealthy. This was the upstream
  cause of the alert-triage loop, not just its symptom.
- **Hermes memory was permanently full at 2,198/2,200 chars**, so every write
  failed. Three causes, all fixed: (1) transient junk written by the stuck cron
  (an alert id, a "watch logic executed" note) — purged; (2) project detail that
  belongs in ARIA's unbounded vector memory — moved there via `add_memory`, with
  a pointer left behind; (3) the cap itself was too small for a five-machine
  setup — raised 2200 → 3500 (~875 tokens/prompt, negligible against the 64k
  floor every model now clears). Content also had stale ports; rewritten with
  the current map and an explicit "never write transient state here" rule.
  Headroom went from -9 chars to 1,333.
- Triage cron no longer curls the dead `8081/8092/8093` endpoints; it calls
  `list_model_servers` and is told that stopped servers are not incidents.

### Notes
- 981 tests pass. Hermes config + cron + memory each backed up before editing.
- Hermes can now select `chadrockv2` and `qwythos` as models (providers +
  aliases added; both clear its 64,000-token floor).

## [2026-07-30] - Chadrockv2 + Qwythos wired up; MCP `id`-alias fix for the alert-triage loop

### Fixed
- **`ack_alert` (and 9 sibling MCP tools) rejected the `id` spelling**, which
  put the gemma-backed "ARIA alert triage" cron into an infinite retry loop
  ("I cannot pass the id argument... despite the tool schema"). Root cause was
  a pure contract mismatch, not a backend fault: every listing endpoint
  serializes its primary key as plain `id`, but the matching action tools
  required `alert_id`/`session_id`/`conversation_id`/`workflow_id`. A model
  that reads a list and acts on an item naturally passes `id`. All ten tools
  now accept either spelling via a shared `_one_id` resolver, with a clear
  error when both are absent. VERIFIED end-to-end against the live MCP server
  over JSON-RPC: `ack_alert(id=...)` returns ok and the DB shows `acked=true`.
- **Test suite could shell out to the real docker daemon.** Proven, not
  theoretical: `test_start_unstartable_without_force_raises` called
  `manager.start()` un-mocked, relying on the unstartable gate to raise first;
  the moment Chadrockv2 became startable the test fell through and really ran
  `docker compose up -d chadrockv2`, launching a 27 GiB model server from a
  unit test. Added an autouse fixture that makes any un-patched `_run` fail
  loudly, and rebuilt the unstartable/unwired tests on a synthetic spec so
  they no longer depend on which model happens to be un-wired.

### Added
- **Chadrock-ROCmFP6-qwen3.6-27b is startable** (`:8105`,
  `infrastructure/chadrockv2/`). **No new image was needed** — the registry's
  "needs a HIP build" blocker was wrong. The model card's bundled profile sets
  `DEVICE=ROCm0` and points `LLAMA_SERVER_BIN` at a
  `build-strix-rocmfp4-quality-hip` path on the *author's* machine, which read
  as a hard requirement; in fact the ROCmFPX tensor types are a FORK feature,
  not a backend one, and the FP6 types are already in the pinned 090e317b
  commit. Verified by loading the exact GGUF on the existing
  `chadrock-rocmfpx:latest` Vulkan build and generating correctly. Configured
  per the profile: 65536 ctx, q8_0 KV, MTP draft (n_max 6), greedy sampling,
  reasoning off, text-only. MEASURED ~30 GiB resident at 65536 ctx.
- **Qwythos-27b-Q8 is startable** (`:8106`, `infrastructure/qwythos/`), on the
  same already-built Vulkan image, driving **both** of its extras at once —
  the F16 vision projector (`--mmproj`, the only vision-capable local model on
  this box) and the native MTP head (`--spec-type draft-mtp`). 65536 ctx
  (weights go to 1M; KV cost is why they don't here, and it clears Hermes's
  hard 64,000-token floor). MEASURED ~32 GiB resident.
- Both entries carry measured `resident_gib` rather than file-size guesses,
  and both were started AND stopped through ARIA's own control plane as the
  integration test (all three GPU servers co-resident peaked at 88.1/124 GiB,
  well under the 92% gate; teardown returned GTT to its 26.1 GiB baseline).

### Notes
- 976 tests pass.
- Neither model is left running — they are wired and verified, then stopped,
  matching the "release idle resources" preference. Start either from the
  dashboard, `POST /infrastructure/model-servers/{slug}/start`, or Hermes.

## [2026-07-29] - Model-server control plane: registry of the local LLM servers + agent binding

Prompted by downloading two new local models (Chadrockv2, Qwythos) with no
consistent way to know which docker-compose service / llama.cpp runtime fork
each model on this box actually needs, or to start/stop them safely. As of
this change, ALL model-server start/stop on corsair-ai goes through ARIA —
no more hand-run `docker`/`docker compose` commands.

### Added
- **`api/aria/infrastructure/model_servers.py`** (replaces the retired
  `LlamaCppModelSwitcher`, which targeted the single-`llamacpp`-on-:8080
  topology and had no concept of per-service compose files, profile gating,
  runtime forks, or RAM exclusivity): a static registry of every local model
  server plus the off-box Ridge entry, each carrying its compose file/
  service/container name, port, runtime fork (repo + branch/commit),
  backend device (Vulkan/HIP/CPU/remote), a RAM SWAG (`resident_gib`), and
  which other servers it's mutually exclusive with. Renamed per Ben to
  track quant/runtime at a glance: `Laguna-S-2.1`, `Chadrock-Laguna-S-2.1`,
  `ROCmFP4-qwen3.6-35b-a3b`, `qwen3.6-35b-a3b-Q4`, `qwen3.6-27b-Q8`,
  `context1-Q4`, `gemma-4-e4b-Q4`, `Chadrock-ROCmFP6-qwen3.6-27b` (Chadrockv2,
  not yet startable — needs a HIP build that doesn't exist on this box),
  `Qwythos-27b-Q8` (not yet wired to a service), `Ridge-Qwen3.6-35B-A3B`
  (off-box, informational only).
- **`ModelServerManager`**: `status()` (live docker state + a live GTT-usage
  read, same sysfs signal `shells/selfcheck.py` already alerts on, since
  docker/cgroup memory limits don't see GPU-offloaded allocations on this
  unified-memory box); `start()`/`stop()` (raw `docker start`/`stop` when the
  container already exists — sidesteps the compose-stop-is-a-silent-noop
  gotcha for hand-run containers — falling back to `docker compose up -d`
  only when it doesn't); `bind()`/`unbind()` (pair a model server with an
  agent, descriptive only — does not change the agent's actual `llm.backend`/
  `model` routing). `start()` hard-refuses on a RAM-exclusivity conflict or a
  projected-GTT-usage overflow (SWAG, not exact) unless `force=True`;
  `bind()` hard-refuses a second binding to an already-bound server unless
  `force=True` adds an extra slot.
- `AgentResponse` gained a `model_server` field (read-only via `AgentUpdate`
  — only settable through bind/unbind, so the one-agent-per-service
  enforcement can't be bypassed by a raw `PUT /agents/{id}`).
- API: `GET /infrastructure/model-servers[/{slug}]`,
  `POST .../{slug}/start|stop|bind`, `POST .../unbind`.
- MCP: `list_model_servers`, `start_model_server`, `stop_model_server`,
  `bind_model_server`, `unbind_model_server` (`mcp/server.py` — restart
  `hermes-gateway.service` to pick these up).
- 22 new tests (`api/tests/test_model_servers.py`): exclusivity refusal, RAM
  SWAG refusal, force bypass, raw-start-vs-compose-up dispatch, bind conflict
  + force override, off-box/unstartable refusals.

### Removed
- `api/aria/infrastructure/model_switcher.py`, `tools/builtin/model_switch.py`
  (`list_llamacpp_models`/`switch_llamacpp_model` tools) — dead code against
  the retired single-server topology, replaced rather than kept alongside.

### Fixed (same day, via a 3-agent adversarial review of the diff)
- **start()/bind() TOCTOU race**: all check-then-act sequences now serialize
  on one `asyncio.Lock` in the singleton manager — two concurrent MCP calls
  could previously both pass the exclusivity/RAM gates and launch two ~90 GiB
  servers together.
- **Noop-before-gates**: the already-running check now precedes the safety
  gates, which used to double-count a running server's own memory (already in
  GTT-used) and refuse an idempotent restart.
- **Daemon-down conflation**: a docker-daemon failure is no longer read as
  "container doesn't exist" — previously `stop()` returned success while the
  server was still up, and `status()` reported the whole fleet absent. Now
  raises (routes map it to 503); only "No such object" means not-created.
- **Stale-config resurrection**: compose-managed containers now start via
  `docker compose up -d` (natively reconciles compose-file edits); raw
  `docker start` is reserved for hand-run containers (compose can't adopt
  those — name conflict), and its response carries an explicit config-drift
  note. Also fixed an unwired-entry crash in the same branch (force-start of
  a no-container entry hit the raw-start path with `container_name=None`).
- **Exclusivity data rebuilt from ground truth**: the blanket 5-way exclusion
  group forbade pairs that are fine (qwen3.6-35b-a3b-Q4 + qwen3.6-27b-Q8 are
  designed to start together; context1 coexists with everything) and missed
  the one dangerous unguarded pair (Laguna-S-2.1 + ROCmFP4-qwen3.6-35b-a3b,
  87+29 > margin). Now explicit symmetric pairs, all laguna-centric, with a
  symmetry test. Exclusivity also counts paused/restarting containers (frozen
  processes keep their GTT allocations), and paused gets a clear
  unpause-manually error instead of a failed `docker start`.
- **Chadrock RAM SWAG 90→60 GiB**: the "~90 GiB class" guess in chadrock's
  own compose header is superseded by the measured ~89.4 GiB chadrock+qwen
  combined (qwen's compose header, 2026-07-28) — at 90 the gate refused the
  exact coexistence the two-server split was designed for.
- **CPU-only servers skip the GTT gate** (`gtt_resident=False` on gemma):
  CPU allocations never appear in `mem_info_gtt_used`, so the projection was
  a category error (conservative, but could false-refuse gemma).
- **Two wrong model_file paths**: qwen3.6-35b-a3b-Q4/qwen3.6-27b-Q8 GGUFs
  live under `qwen-rocmfp4/models/`, not `models/llm/` — all paths are now
  explicitly relative to the infrastructure root.
- **Web UI dashboard**: the Settings-tab panel still called the removed
  `/infrastructure/llamacpp/models` route (silent 404, permanently empty
  card) — repointed to `/infrastructure/model-servers` and rendered as
  slug/state/device/port/bound-agent.
- **MCP start timeout**: `start_model_server` uses a 180s per-request timeout
  (global default 20s would false-fail a cold `compose up`).
- Stale leftovers removed: `list_llamacpp_models` from `tool_allowed_names`,
  `switch_llamacpp_model` from `tool_sensitive_names` (config.py).

### Added (same day, follow-on requests)
- **Web UI model-server controls** (dashboard → Settings tab): Start/Stop
  buttons per server with the 409 refusal text surfaced inline and a
  "Force start" retry when the refusal mentions force; live GTT usage line.
- **Hugging Face pull & provision pipeline**
  (`api/aria/infrastructure/model_pull.py` + UI form + MCP
  `pull_model`/`list_model_pulls`): give it a repo, GGUF filename, name, and
  a runtime template (mainline-vulkan / mainline-cpu / rocmfp4-fork /
  rocmfpx-vulkan-fork — all images already on this box) and it downloads
  into `infrastructure/models/llm/<name>/` (positional filename, not
  `--include`, per the known hf-CLI gotcha), generates a compose service
  under `infrastructure/generated/<name>/`, auto-allocates a port from
  8105+, and registers a DYNAMIC registry entry (`db.model_servers`) that
  start/stop/bind treat exactly like static ones (RAM SWAG estimated from
  file size; no static exclusivity — the live GTT gate guards it). Jobs
  tracked in `db.model_pulls` with log tail + a `stale` flag for
  api-restart-orphaned pulls; one pull at a time.
- **Ridge sleep** (`sleep_command` on off-box specs, POST
  `.../{slug}/sleep`, MCP `sleep_model_server`, UI Sleep button): suspends
  Ridge over `ssh ridge` (SetSuspendState) after a reachability probe —
  already-asleep noops cleanly, and the suspend dropping the ssh connection
  is treated as the success shape. Wake stays automatic via
  ridge-llama-proxy WoL; ARIA only owns the sleep direction.

### Changed (same day)
- **Compose services renamed to match the registry slugs** (safe — the
  containers are not created): `qwen-chat` → `qwen3.6-35b-a3b-Q4`,
  `qwen-agentic` → `qwen3.6-27b-Q8` in `infrastructure/qwen-rocmfp4/`
  (docker-compose.yml service keys/container_name/hostname/depends_on +
  serve.sh), with comment cross-references updated in laguna's and the root
  compose files.

### Notes
- 961 Python tests pass (removed the old switcher's 3 dead tests; the
  registry/manager suite is now 33 tests including the race, daemon-down,
  paused-state, CPU-skip, and exclusivity-symmetry regressions).
- Chadrockv2 and Qwythos are registered but `startable=False`: Chadrockv2
  needs a new HIP build of the ROCmFPX-family fork (its bundled profile
  points at a binary path from the author's machine); Qwythos is a standard
  GGUF (any existing MTP-capable runtime works) but isn't wired to a compose
  service yet. Both can be flipped on once that infra work happens.

## [2026-07-30 evening] - Fleet unification: pi-code on the shell substrate, verified idle-reaper, Ridge concurrency limit, restructured sidebar; pool disabled

Prompted by a user observation that Agents/Shells/Coding Sessions/
Conversations "seem overlapping" in the TUI — they were, in two different
ways, one already by design (a coding session and its shell are the same
live process) and one a real gap (pi-code had no shell at all). This pass
closes both, plus three related pieces agreed along the way.

### Added
- **Ridge concurrency limiter**: mirrors chadrock/pool's existing
  single-consumer ceiling (`coding_max_concurrent_ridge_sessions`, default 1)
  — Ridge/NInfer has no continuous batching, so a second concurrent session
  there would queue messily at the inference layer instead of ARIA's own
  queue. Generalized the old laguna-only `_is_laguna_session`/`_laguna_limit`
  into `_limited_backend`/`_backend_limits` (keyed by canonical backend name)
  so a third such backend is one line, not four new methods. **Found and
  fixed a real pre-existing bug while wiring this in**: the main
  `start_session()` launch path never actually passed `backend` into
  `_try_acquire_slot_nowait`/`_acquire_slot` at all — the pool/laguna limiter
  had been silently non-functional in production despite existing since the
  two-server split.
- **pi-code runs on the shell substrate**: `_start_pi_code_session` now
  launches a real tmux-backed shell (`aria pi-code run` — new CLI command,
  `cli/aria_cli/main.py`) instead of driving the orchestrator in-process.
  Removed the three pi-code-specific branches in `stop_session`/`get_output`/
  `send_input` (`shell_name`-based fallthrough into the generic shell paths
  the rest of the fleet already uses); deleted the now-dead
  `_run_pi_code_session`/`_finalize_pi_code`. `PiCodeBackend.is_in_process`
  renamed to `needs_custom_launch` (it was never really "in-process" as a
  concept — it's launch sequencing that needs a conversation created first).
  Verified live end-to-end: spawn → real shell → streamed response →
  `send_input` follow-up → `stop_session`, all through the same paths every
  other backend uses, no special-casing left.
- **Universal, verified capture-then-reap** (`shells/reaper.py`, Coherence
  C9): scope is now ANY idle watched shell, not just ARIA coding sessions —
  a hand-run shell gets the same save-then-reap treatment instead of blanket
  exclusion. Before reaping, independently verifies the save happened
  (`<project_dir>/HANDOFF.md` must exist AND be modified after the save
  prompt was sent) rather than trusting the agent's self-reported `REAP_SAVED`
  token alone — same lesson as C1's verification gate applied here. Neither
  signal alone is sufficient; an unconfirmed save is skipped and alerted on,
  never reaped anyway.
- **Restructured TUI sidebar** (`tui/internal/ui/components/sidebar.go`):
  Conversations now nest under their owning Agent instead of a disconnected
  flat list; coding sessions render as ONE row (using their live shell's
  `activity_state`), not a coding-session row and a separate shell row for
  the same process; Pool/Ridge get their own groups showing "x/1 active"
  (the real ceiling, visible instead of implicit); Claude Code/Codex show
  unbounded "N active"; hand-run shells get their own "Your Shells" group,
  explicitly excluding anything a coding session already claims. Same
  dedup fix applied to `fleet_view.go` (the Fleet screen had the identical
  double-listing). New `CodingSessionResponse.llm` / `CodingSession.LLM`
  field (server + Go client) — needed to tell a Ridge-backed pi-code session
  apart from a local one, since both share `backend="pi-code"`.
- **`pool_enabled` setting**: master switch, checked in `start_session()`
  before ever dialing chadrock. Set to `false` (2026-07-30 — chadrock
  physically shut down) so a pool-backed request fails with a clear 409
  instead of a confusing connection error. Config and `db.agents`-adjacent
  wiring left intact, not removed, for a one-line re-enable.

### Notes
- `coding_routing_fallback_backend` still defaults to `"pool"` (the
  sub-Sonnet quota-exhaustion fallback) — with pool disabled, that path now
  fails loudly instead of connection-refusing silently, but there is
  currently no *working* quota fallback while both are true. Flagged, not
  silently reconfigured.
- 931 Python tests pass (was 920 at the start of this session's fleet work);
  Go: build/vet/test all clean, including new coverage for the sidebar's
  grouping/dedup logic and the reaper's verification logic (neither had any
  tests before this pass).
- Design-level writeup: `vault/ProjectAria/Design/COHERENCE_DESIGN.md` (C9's
  "still open" decisions — save-timeout behavior and reap scope — are now
  both resolved: skip-and-alert on an unconfirmed save, universal scope).

## [2026-07-30] - Fixed: TUI chat responses vanishing on reload; agents advertising delegation tools they don't have

### Fixed
- **TUI chat responses disappeared after the first render, on every agent,
  every conversation.** `GET /api/v1/conversations/{id}` returned `created_at`
  timestamps with no timezone offset (`"2026-07-29T23:41:21.003000"`, not
  valid RFC3339) because Motor returns naive datetimes from MongoDB by
  default. The Go TUI's `time.Time` JSON unmarshaling fails hard on the first
  such field, so the *entire* conversation payload silently failed to decode
  on every reload. Symptom: your own messages kept appearing (added
  optimistically client-side at send time — `model.go` `Messages = append`),
  but the assistant's responses — only ever populated by a successful reload —
  vanished within seconds of being streamed. Root-caused via a raw Mongo
  query on a live conversation showing every message intact server-side (so
  it was never a persistence bug), then confirmed via the raw JSON response
  showing the offset-less timestamps directly. Fixed with a single
  `tz_aware=True` on the app's `AsyncIOMotorClient` (`db/mongodb.py`) —
  Motor now attaches UTC tzinfo to every datetime it returns, so every
  existing `if x.tzinfo is None: x = x.replace(tzinfo=timezone.utc)` guard
  scattered through the codebase (shells/service.py, shells/notifier.py,
  shells/selfcheck.py, etc. — all silent workarounds for this exact root
  cause) becomes a harmless no-op instead of load-bearing. Verified the raw
  API response now returns `...003000Z` (valid RFC3339) and the Go TUI still
  builds clean.
- **An agent's system prompt advertised sub-agent delegation tools
  (`claude_agent`, `pi_coding_agent`) regardless of whether that agent
  actually had them.** `core/context.py`'s "Sub-Agent Coordination" /
  "Reasoning Delegation" prompt block was gated only by the global
  `settings.deep_think_enabled` flag, not by the current agent's own
  `enabled_tools`. Concretely: `pi-coding-ridge` (`enabled_tools =
  ["filesystem", "shell", "web", "deep_think"]` — no `claude_agent`, no
  `pi_coding_agent`) was still told "you can delegate substantial tasks via
  the `claude_agent` and `pi_coding_agent` tools," so a plain "review this
  project" request got answered with an attempt to delegate to a Claude Code
  sub-session instead of just using its own direct filesystem/shell tools —
  same failure class as the Stock Scanner cron job's `send_message` bug
  (2026-07-29): a prompt promising a capability the execution context never
  actually wired up. Fixed by building the delegation guidance from the
  agent's real `enabled_tools` — `deep_think` guidance only if `deep_think`
  is enabled; the sub-agent section (and each tool's own bullet) only for
  whichever of `claude_agent`/`pi_coding_agent` are actually present; the
  whole block omitted if neither applies. Added a regression test
  (`test_build_messages_deep_think_enabled_globally_but_not_for_agent`).

### Notes
- 916 tests pass (was 915 — one new regression test); one existing test
  (`test_build_messages_with_deep_think`) updated to declare `deep_think` in
  its mock agent's `enabled_tools`, since it was implicitly relying on the
  old, buggy blanket-enable behavior.
- Neither fix required touching `pi-coding-ridge`'s own system prompt, which
  was already correct and explicit about the shell tool's no-chaining
  (`&&`/`;`) restriction — a separately-observed retry loop where the agent
  kept retrying chained shell commands despite that guidance is a model
  reliability limitation, not a missing-information bug, and wasn't changed.

---

## [2026-07-29] - Coherence C1 (Verification Gate) + C2 (Repo-Change → Memory); herdr.dev-inspired shell activity_state

### Added
- **C1 Verification Gate** (`agents/watchdog.py`): a Ralph-looped session's
  `RALPH_DONE` token is no longer honored at face value. `_verify_session_done()`
  runs a check command (`loop_config.gate_command` → `projects.check_command`
  → global `coding_gate_command`, default `"make check"`) via
  `_run_gate_check()` before ending the loop; a failing check re-nudges the
  session with the check's output instead of promoting to done, up to
  `coding_gate_max_retries` consecutive failures (then gives up and alerts,
  `source="coding:gate"`). A `make`-specific "no such target" heuristic treats
  an unconfigured check as skip-not-block, per design. Off by default
  (`coding_gate_enabled=False`). Gate history recorded on the session doc
  (`gate_runs: [{at, passed, tail}]`) and surfaced via `CodingSessionResponse`
  and the MCP `get_coding_session` tool. `projects.check_command` added
  (`planning/models.py`) for a per-project override. `SessionLoopRequest` and
  the MCP `set_coding_loop` tool both gained `gate_command`/`gate_timeout`/
  `gate_max_retries`. Remote-node sessions are skipped for now (host-aware
  gating needs C8's `run_command`, not yet built).
- **C2 Repo-Change → Memory** (`shared/scan.py`): a new `GitChangeEmitter`
  rides the existing S2 scan/reconcile worker, independent of its
  container/service snapshot-diff — walks its own repo list
  (`git_scan_roots`, default: the project harvester's roots) and tracks a
  per-repo `rev-parse HEAD` cursor in `scan_state`. New commits since the
  cursor, over `git_scan_min_change_lines` changed lines
  (`git diff --shortstat`), mint a private memory summarizing the commit
  subjects. Commits-only by design, not uncommitted/dirty-tree changes.
  Gated by `git_scan_enabled` (on by default, but inert unless
  `shared_scan_enabled` is also on) and registered in `main.py` alongside
  `MachineScanMemoryEmitter`.
- **C2a fix**: `shells/extraction.py` was extracting memories from shell
  activity every tick and then only logging the count — the extracted list
  was never persisted. Now calls `create_memory()` per extracted dict, same
  pattern as `MemoryExtractor.extract_from_conversation()`.
- **Shell `activity_state`** (`shells/service.py fleet_overview()`): a
  herdr.dev-inspired fourth semantic state on top of the existing
  `awaiting_input` boolean — `working`/`blocked`/`done`/`idle`. "done" is new:
  a batched `coding_sessions` lookup by `shell_name` lets an idle shell
  backing a session with a terminal status show as done, distinct from a
  shell that's just idle with nothing happening. Threaded through
  `ShellOverviewItem`/`ShellOverviewResponse` (new `blocked_count`/
  `done_count`) and the Go TUI (`client.go`, `fleet_view.go`, `sidebar.go`).

### Notes
- Design-level writeup and the seams actually used (several had drifted from
  the original design doc's line numbers): `vault/ProjectAria/Design/
  COHERENCE_DESIGN.md`, the C1/C2 "✅ Implemented" callouts and decision-log
  entry 29.
- 915 tests pass (was 909); Go TUI (`tui/`) builds clean.

---

## [2026-07-28] - Coding-session inspection tools for Hermes; qwen crash root-caused and mitigated; chadrock/GPU-memory monitoring closed

### Added
- **MCP coding-session inspection tools** (`mcp/server.py`): `get_coding_session`
  (structured single-session status/error/result_summary), `wait_for_coding_session`
  (blocks up to a timeout for a terminal state — the `wait_for_session` join
  primitive, previously only reachable from inside workflow fan-out, now callable
  directly), `get_coding_diff` (working-tree diff). New `GET
  /coding/sessions/{id}/wait` route (`routes/coding_sessions.py`) backs the second.
  Lets Hermes join/inspect a spawned coding sub-agent by id instead of polling raw
  terminal output or telling the human to run a CLI command themselves.
- **`gemma-aux`** (`infrastructure/gemma-aux/`, `:8104`) — a third local model
  server, Gemma 4 E4B (Q4_0 GGUF), CPU-only. Takes Hermes's ~16 "auxiliary"
  side-tasks (title generation, compression, curator, approval, triage_specifier,
  mcp, etc.) and both cron jobs (alert triage, stock scanner) off qwen entirely.
  Ships with `--reasoning off --reasoning-budget 0` (Gemma 4's reasoning mode
  fires stochastically and can silently consume an entire `max_tokens` budget —
  confirmed live, fixed before rollout) and `--kv-unified` (without it, `-c 8192`
  with `--parallel 2` silently halves to 4096 usable tokens per request).
- **`gpu_memory` and `chadrock` checks in `selfcheck.py`.** Chadrock had zero
  automated health monitoring despite carrying the same crash risk as qwen (both
  `restart: "no"` by design); added a `pool_api_url` probe. `gpu_memory` reads
  `/sys/class/drm/card0/device/mem_info_gtt_{used,total}` and alerts >90% — the
  real ground-truth signal for GPU memory pressure on this hardware (see Fixed).

### Fixed
- **qwen (`:8103`) crashed** (`vk::DeviceLostError`, "Not enough memory for
  command submission") under GPU command-submission contention with chadrock
  during simultaneous long-context checkpoint activity. Root cause: a tiny
  (~1 GiB) dedicated VRAM aperture used for GPU command submission — not the
  large GTT/system-RAM pool — sits near-permanently full on this hardware.
  Mitigated (unverified by repeat-crash testing): qwen's `-ctxcp` 32→10,
  `--cache-ram` 8192→2560; chadrock's own config untouched (kept at max context
  by design). qwen's `-c` also trimmed 131072→100000 to free real GTT headroom.
- **Docker/cgroup memory limits do not see GPU-offloaded memory on this
  unified-memory (Strix Halo) box** — confirmed empirically: `docker stats`
  showed ~5 GiB combined for chadrock+qwen while real GTT usage was ~97 GiB.
  `mem_limit` is a no-op safeguard for any GPU-offloaded (`-ngl 999`) server here;
  it only works for a genuinely CPU-only one (gemma-aux). Documented so it isn't
  rediscovered the hard way.
- **Hermes's two cron jobs** (`~/.hermes/cron/jobs.json`) were hardcoded to a
  slot-proxy port retired the same day as the two-server split — silently
  failing to connect on every run since. Repointed alongside the auxiliary-task
  move to gemma-aux.

### Notes
- Full incident writeup: `docs/ops/LOCAL_INFERENCE_TOPOLOGY.md` §10.
- Design-level consequences: `vault/ProjectAria/Design/COHERENCE_DESIGN.md` §5
  entries 24–28.
- `PROJECT_STATUS.md` item 000 tracks the one open follow-up: the checkpoint
  mitigation is a plausible hypothesis about the VRAM-aperture mechanism, not a
  proven fix — watch for a recurrence.

---

## [2026-07-25] - Pi-Flow parity: fan-out workflows, concurrency limiter, cache metrics

Closes the gaps between ARIA and [pi-flow](https://github.com/kky42/pi-flow) (multi-agent
orchestration). Plan: `vault/ProjectAria/Planning/PiFlow_Parity_Plan.md`.

### Added
- **Global concurrency limiter + queue for coding sub-agents** (`agents/session.py`).
  A session holds a "slot" while running; spawns beyond `coding_max_concurrent_sessions`
  (default 4; 0 = unbounded) sit in a new `queued` state and launch as slots free.
  `coding_queue_max` (default 64) hard-caps the wait queue (fail loud beyond it).
  CV-guarded, idempotent set-based slot release across every finalize path; the fast
  path (free slot → inline launch) is unchanged. Gauge at `GET /coding/sessions/concurrency`
  and merged into MCP `fleet_status`. Applies to CLI/shell/subprocess/remote **and**
  pi-code substrates.
- **`wait_for_session(session_id, timeout)`** — the join primitive: polls a session to a
  terminal state (restart-safe) and attaches the `TASK_DONE` result summary from the mailbox.
- **Fan-out workflow orchestration** (`workflows/engine.py`). New actions on top of the
  linear DAG: `parallel` (concurrent explicit sub-steps, bounded by `max_concurrent`),
  `map` (one `template` over a list, with `{{item}}`/`{{index}}` scope), `code_session`
  with `await:true` (join a spawned sub-agent and capture its result_summary), and
  `synthesize` (reduce prior results into one answer via an agent turn, optional
  backend/model — e.g. merge on Opus). Sub-step results nest under the group as
  `results`/`records`, addressable via `{{steps.N.results.M.path}}` (dotted paths now walk
  lists too). Exposed to Hermes as MCP `list_workflows`/`create_workflow`/`run_workflow`/
  `get_workflow_status`.
- **Prompt-cache metrics.** Anthropic adapter now captures `cache_read`/`cache_write`
  tokens; persisted by `UsageRepo.record` and aggregated into a weighted
  `cache_hit_rate` on `/usage/summary` and `/usage/by-model` (Pi-Flow `cacheHitRate` parity).
- **Declarative specialist profiles.** `start_coding_session(subagent_profile=<slug>)`
  resolves a `db.agents` row and applies its backend/model + `system_prompt` (role
  preamble); an explicit backend/model still wins. Threaded through the REST model + MCP
  `create_coding_session`.

### Notes
- Slot semantics: a slot is held while a session is actively running (has a live watch
  task), so long-lived Ralph-loop sessions occupy a slot for their whole life — size the
  cap accordingly. Visible-tmux sessions (which have no completion watcher) hold a slot
  until stopped. Queued sessions are lost on an `aria-api` restart (rare edge).
- 873 tests pass; new coverage in `test_coding_concurrency.py`, extended
  `test_workflow_engine.py`, `test_usage_repo.py`. After deploying, restart
  `aria-api` (limiter + engine) and `hermes-gateway.service` (new MCP tools).

## [2026-07-24] - Desk-path auto-routing reverted; routing is spawn-path only

### Changed
- **Complexity routing no longer intercepts the desk `claude` command.** Claude Code is one-model-per-session — you pick the model at launch or with `/model`, and no hook can swap it per prompt — so the sit-down interactive REPL has no single task to classify and can't be dynamically re-routed. The desk wrapper also fit the primary habit poorly: `claude --dangerously-skip-permissions` starts with a flag, which the wrapper bailed on, so routing never ran for it. Reverted `~/.bashrc` on corsair: `claude()` is once again just the saved-state per-directory shell attach (the aria-shells workflow that was working well). **Routing is unchanged on the automated spawn path** (`start_session()` — Hermes/MCP/TUI), where one task really is one session.
- The desk-path scripts (`scripts/aria-claude.sh`, `aria-route-task`, `aria-desk-install-mac`) are kept in the repo but no longer sourced. `aria-route-task` remains a useful manual client for `POST /api/v1/routing/classify`.

### Notes
- **MacBook still needs one manual edit:** the installer had added the wrapper to `~/.zshrc`. Remove the `. ~/.config/aria/aria-claude.sh` line there so bare `claude` is the real binary again (the export line is harmless to keep).

## [2026-07-23] - Complexity routing actually reaches its callers

### Fixed
- **`backend="claude_code"` silently disabled routing.** `start_session()` only routed when *both* `backend` and `model` were unset, so any caller naming the backend — which Hermes does as belt-and-suspenders, and the `route-coding-to-aria.py` hook text encourages — got the default model instead of the tier's. Since Hermes is the main non-desk spawner, this was most of the traffic. Routing now treats an explicit `model` as the only unconditional pin; an explicit backend suppresses it only when the router wouldn't have chosen that backend itself (`routing.is_routable_backend`: `codex`/`pi-code` = a real pin, `claude_code` = agreement). Verified live: `backend=claude_code` + a design prompt → `claude-opus-4-8`; `backend=codex` → unrouted, as before.

### Added
- **The desk wrapper is live on corsair.** `scripts/aria-claude.sh` was complete but had never been sourced, so `claude "<task>"` still launched on the default model. `~/.bashrc` already defined its own `claude()` (attach/spawn the persisted per-directory shell), so rather than clobber it, that function is renamed `_aria_claude_dir_session` and the routing wrapper — sourced after it — delegates the no-task case back via the new `ARIA_CLAUDE_BARE_COMMAND` hook. Net: `claude` alone behaves exactly as before, `claude "<task>"` routes, and `claude --no-aria …` (the old escape hatch, now also honoured by the routing wrapper) skips both. Backup at `~/.bashrc.bak-20260723-aria-routing`.
  - **`scripts/aria-desk-install-mac`**: emits a self-contained installer (both scripts + the API key embedded as heredocs) to be piped into `sh` from the Mac — `ssh ben@corsair-ai 'bash Development/ProjectAria/scripts/aria-desk-install-mac' | sh`. No scp, no repo checkout, and no long command line on the Mac side, because a long line pasted into a terminal can be split mid-command — and a split `scp remote:a remote:b` is a silent remote-to-remote overwrite (it clobbered `scripts/aria-route-task` once; recovered from the session transcript). Idempotent, and refuses to touch `~/.zshrc` if it already defines its own `claude()`.

## [2026-07-23] - Backend cleanup: context-1 off, Fireworks key removed

### Changed
- **context-1 is now explicitly disabled** via `context1_enabled` (`CONTEXT1_ENABLED=false` in `.env`). The container isn't part of the normal stack, so probing it produced a permanent DEGRADED. With the flag off, `is_backend_available("context1")` reports "disabled", the Search Agent tool isn't registered, and `/health/services` skips `:8081` entirely.
- **`FIREWORKS_API_KEY` removed from `.env`** — it had started returning 401 on every call. No agent was on Fireworks any more (`db.agents` has ARIA on `llamacpp` and Pi Coding Agent on `agentic`, both pointed at the local `laguna` server on `:8095`), so nothing regressed. The adapter and the `fireworks`/`glm` aliases stay; re-add a key to reactivate.
- **Routing's quota fallback now points at the local open-weights server** (`coding_routing_fallback_llm` `fireworks` → `agentic`, model → `default`) instead of Fireworks GLM 5.2, so the fallback still works with no key and no spend.

### Fixed
- **`/health/services` counted a rejected credential as healthy.** The probe used `ok = status_code < 500`, so Fireworks' `401` reported green while every call was failing. `401`/`403` are now unhealthy, and disabled/unconfigured backends are omitted from the result rather than counted against `healthy` (so the ratio reflects only what is meant to be up).

### Notes
- Cleared two zombie coding sessions (`1ad4d84d…` from 07-07, `c0d521dd…` from 07-22) left in `status: running` with no surviving pane. The watchdog had been logging `stuck: idle` for both every ~6s ever since — that was the entire content of the journal. There is still **no reaper for coding sessions whose substrate has vanished** (the C9 reaper covers idle *shells*).
- Complexity routing verified end-to-end for the first time: an unpinned `POST /api/v1/coding/sessions` with a design prompt spawned a real Claude Code session on `claude-opus-4-8` with the verdict persisted on the session doc.

## [2026-07-23] - Fix: restarting aria-api destroyed every watched shell

### Fixed
- **The tmux server lived inside `aria-api.service`'s cgroup**, so systemd's default `KillMode=control-group` killed it — and every watched `claude-*` session with it — on each `systemctl --user restart aria-api`. A tmux server inherits the cgroup of the client that first spawns it, so whenever ARIA's adopt/spawn path beat a login shell to it, the server was owned by the API. Symptom: you never got dropped back into an existing thread, because there was no session left to reattach to; the `claude()` wrapper correctly found nothing live and spawned a fresh one.
  - Confirmed against the journal: the stop at `2026-07-21 23:10:15` froze ~10 sessions' `last_activity_at` at `23:10:17` (`claude-emu_fleet_monitor`, `claude-strategy-tenets`, `claude-macbook-pro`, `claude-scenarios`, `claude-sm8550`, `claude-lieutenant-lab`, `claude-campaigns`, `claude-Emulation`, `claude-emuDeviceConfig`); the stop at `21:52:51` on 07-23 took `claude-coding-{699f58fc,645c3e45,0e43dfc0}` and `claude-Development` at `:51`–`:54`.

### Added
- **`aria-tmux.service`** (`scripts/systemd/aria-tmux.service` + `scripts/aria-tmux-server`): a dedicated unit that owns the tmux server, ordered `Before=aria-api.service`. `Type=oneshot` + `RemainAfterExit=yes` — the server daemonises out of `ExecStart`, and RemainAfterExit is what stops systemd tearing the cgroup down behind it. aria-api now only ever connects as a *client*.
  - `exit-empty off` is load-bearing: at the tmux default (`on`) the server exits when its last session is destroyed, and the next caller would rebuild it in *their* cgroup — straight back into the bug.
- **`scripts/aria-claude-launch`**: resume-aware launch shim, now the default `shells_claude_launch_command`. When the workdir already has Claude Code history (`~/.claude/projects/<munged cwd>/*.jsonl`) it launches `claude --continue`, so a respawned session picks up its thread instead of coming back empty. Falls back to a fresh thread if `--continue` fails within 5s (corrupt history) rather than leaving a dead pane. Coding sub-agents are unaffected — they pass an explicit `launch_command`, which takes precedence.
- **`scripts/aria-tmux-cutover`**: one-time takeover (tmux can't migrate sessions between servers, so the old server must die once). Refuses to run inside tmux, since `tmux kill-server` would kill the pane running it partway through and leave aria-api stopped.

### Notes
- **Requires the one-time cutover** to take effect; it costs every currently-live session one last time. Run it detached:
  `systemd-run --user --collect --unit=aria-tmux-cutover /home/ben/Development/ProjectAria/scripts/aria-tmux-cutover`
- Pane processes were never the problem — tmux 3.4 already puts each pane in its own transient `tmux-spawn-*.scope`. Only the *server* was captured.
- Unrelated bug noticed while cleaning up: `DELETE /api/v1/shells/{name}` 500s when the tmux session is already gone (`tmux kill-session failed: can't find session`) instead of just marking the row stopped. `?purge=true` works. Not fixed here.

## [2026-07-23] - Complexity routing: coding tasks pick their own model

### Added
- **`ComplexityRouter`** (`api/aria/agents/routing.py`): classifies a coding task into a tier and picks the backend/model. Three stages, cheap-first — (1) a heuristic prefilter for unambiguous phrasing, (2) a Sonnet-class judge returning strict JSON, (3) an availability filter that demotes while the Claude quota is cooling down. Never raises: any failure degrades to the standard tier.
  - `deep` (planning / design / strategy / trade-offs) → `claude-opus-4-8`
  - `standard` (scoped implementation, bug fixes, tests) → `claude-sonnet-5`
  - `light` (research / information gathering) → `claude-sonnet-5`, and on the desk path the judge may **answer inline** so trivial lookups never spawn a session at all.
  - Sonnet is the floor for normal routing. The sub-Sonnet fallback (`pi-code` / Fireworks GLM 5.2) is reachable *only* via the quota cooldown.
- **`POST /api/v1/routing/classify`** (`api/aria/api/routes/routing.py`) plus `GET/POST/DELETE /api/v1/routing/availability[/cooldown]` — a thin cross-machine surface so a client on any host can ask "what model does this need?" without a local venv. Inherits the global X-API-Key auth.
- **Desk-path wrapper** (`scripts/aria-claude.sh` + `scripts/aria-route-task`): source the `.sh` from `~/.bashrc` (corsair) or `~/.zshrc` (MacBook) and typing `claude "<task>"` routes the task, then launches Claude Code on the chosen model inside a `claude-*` tmux session — which both hosts already auto-adopt into the fleet (tmux hook on corsair, `aria-node`'s capture loop on the MacBook). Identical on both; only the resolved API URL differs (same `~/.config/aria/hosts` convention as the TUI). `aria-route-task` is pure stdlib Python 3, no venv needed.
- **Quota cooldown state** (`model_availability` collection): ARIA can't query the Claude subscription quota — there's no API for it — so the watchdog records a cooldown when it sees rate-limit/quota text in a `claude_code` session's pane output, and the router demotes new sessions until it expires. Detect-then-degrade, not prediction.
- Tests: `tests/test_complexity_routing.py` (52 tests).

### Changed
- `CodingSessionManager.start_session()` routes when **both** `backend` and `model` are unset; an explicit pin always wins. Every existing caller — Hermes MCP `create_coding_session`, `/code`, the TUI, the coding-mode autostart — inherits routing for free, including sessions on remote nodes (the chosen `--model` is already part of the launch string shipped to `aria-node`).
- Coding session docs and `GET /api/v1/coding/sessions` now carry `routing: {tier, why, confidence, source, judge_model, decided_at}` — so every surface can show *why* a session is on the model it's on. `None` means the caller pinned it.
- Coding backends now set `ARIA_MANAGED=1` in `CommandSpec.env` (`agents/backends/claude_code.py`, `codex.py`). The shell substrate launches agents under `bash -lc`, which sources the user's rc files — where the desk wrapper defines a `claude` function. Without the flag an ARIA-spawned session would re-enter the wrapper and recursively spawn more sessions.

### Notes
- The judge transport is configurable (`coding_routing_judge_transport`): `api` (one small Anthropic call, sub-second, fractions of a cent — right for the interactive desk path) or `cli` (`claude -p` via `ClaudeRunner`, burns the subscription instead of API tokens but costs several seconds of CLI startup — right for background/Hermes-initiated routing). Default `api`.
- **Requires an `aria-api` restart** for the new routes to serve.
- A shell you started by hand *outside* the wrapper still can't be routed — ARIA sees it only after `claude` is already running. Adoption and fleet visibility work as before; only the model choice is missed.

## [2026-07-18] - Shared Services (S1–S5) - Foundation for the Coherence & Ontology plans

### Added
- **S1 — Memory HTTP API** (`api/aria/api/routes/memory_api.py`): `POST /api/v1/memory/recall` and `POST /api/v1/memory/store` — a minimal cross-machine surface wrapping `LongTermMemory` (embeds server-side). Inherits the global X-API-Key auth.
- **S2 — Scan/Reconcile worker substrate** (`api/aria/shared/scan.py`): one periodic worker (`ScanReconcileWorker`) observes live machine state (`docker ps`, `systemctl`, `ss`) and feeds pluggable emitters. Ships with `MachineScanMemoryEmitter` (Coherence C2: machine-change → memory). Flag-gated OFF by default (`shared_scan_enabled`).
- **S3 — Freshness/ownership convention** (`api/aria/shared/ownership.py` `merge_owned`) + **review surface** (`api/aria/shared/review.py`, `GET/POST /api/v1/shared/review`): worker writes only worker-owned fields; human-curated conflicts are flagged for review, never clobbered.
- **S5 — Native vector storage**: embeddings now stored as MongoDB's native BSON vector (Binary **subtype 9**, float32) via `Binary.from_vector`. Backfill script `aria/scripts/migrate_embeddings_vector_subtype9.py` (idempotent; migrated 1245 existing docs, no re-embedding). Vector-search failures now log loudly instead of silently degrading recall to lexical-only.
- Tests: `tests/test_shared_services.py` (9 tests).

### Changed
- `memory/long_term.py`: `embedding_to_binary`/`binary_to_embedding` use subtype 9, with backward-compatible decode of legacy subtype-0 docs.
- **S4 — Auth:** confirmed the existing global `api_key_middleware` already protects all non-health endpoints; the new memory/store + shared routes inherit it (no separate write-auth needed).
- Design docs (`SHARED_SERVICES_DESIGN.md`, `ONTOLOGY_MEMORY_DESIGN.md`, `COHERENCE_DESIGN.md`) now live in the Obsidian vault under `ProjectAria/Design/`.

Format:
```
## [Date] - Phase X - [Summary]

### Added
- New features

### Changed
- Changes to existing features

### Fixed
- Bug fixes

### Removed
- Removed features

### Notes
- Important notes for future work
```

## [2026-07-07] - Multi-machine fleet: deployed live + robustness fixes

### Added
- **Deployed the fleet across machines.** `aria-node` runs as a persistent launchd agent on the MacBook Pro (`bens-macbook-pro`), registered with corsair over the tailnet. Verified live: capture + drive of Mac tmux shells, and a **Claude Code session in a Mac project driven end-to-end from corsair** (send instruction → the Mac's claude submits + responds → screen returns). This is the "talk to the MCP → drive a Claude Code instance on the Mac" workflow.

### Fixed
- **Reliable TUI submit** (`shells/tmux.py`): send the text and the submit `Enter` as separate `send-keys` calls — combined, TUIs using bracketed paste (Claude Code) absorb the Enter, so the text lands in the input box but never submits.
- **Node keepalive** (`aria/node/agent.py`): re-assert a live-but-idle shell as active each cycle (throttled) so an idle session never gets stuck `stopped` centrally and refuses input; plus **register-with-retry** so the node never crash-loops if the API is momentarily unreachable.
- **Remote `send_input` tolerates status flap** (`shells/service.py`): the owning node is authoritative on liveness, so remote input dispatches regardless of the cached (occasionally-flapping) `stopped` status; only local shells honor the stopped row.

## [2026-07-06] - Multi-machine fleet: Layer B2 (`aria-node` agent)

### Added
- **The fleet now spans machines.** A remote `aria-node` agent registers its host with the central brain, captures its local `claude-*` tmux shells (pushing events + snapshots over the API), and long-polls a command queue to be driven back — so a MacBook's shells and coding sessions appear in, and are drivable from, the one corsair fleet. One brain (corsair Mongo/memory), many hands.
  - **Central:** `api/aria/nodes/` (`NodeService`, `models`, `commands` queue) + `POST/GET /api/v1/nodes/*` (register, heartbeat, events, snapshot, command long-poll + result, list); `nodes` + `shell_commands` collections (with a TTL) in migrations; `local_node_id` + node timeouts in config; `get_node_service` DI.
  - **Host-aware `ShellService`:** `register_shell`/`insert_events_batch` accept a `host` override; `send_input`/`current_screen`/`session_alive`/`kill_shell` dispatch by the shell's host — **local → tmux (unchanged, zero regression); remote → the node command queue** (`_dispatch`/`_remote_command`). The reconciler skips remote shells. Reads (`fleet_overview`, scrollback) were already host-agnostic.
  - **Remote coding sessions + Ralph over the wire:** `start_coding_session(host=<node>)` runs the session on that node (it creates the `claude-coding-*` shell locally); `host`/`node_id` on `coding_sessions`; the watchdog + **Ralph loop drive remote sessions for free** through the host-aware manager. MCP gains `list_nodes` + `create_coding_session(host=…)`; the TUI fleet HOST column now shows remote coding sessions.
  - **The node agent** (`aria/node/`, run via `python -m aria.node` / `scripts/aria-node`, + a macOS launchd plist): outbound-only, depends on httpx + the local tmux driver (no Mongo), so it runs on a MacBook against corsair over the tailnet with the shared `X-API-Key`.
  - Tests: `api/tests/test_nodes.py` (command queue, NodeService ingest/registry, ShellService local-vs-remote dispatch, remote-session routing, node-agent handlers). Full suite 755 green. Live loopback verification pending an `aria-api` restart (loads the new routes/migrations).

## [2026-07-04] - Telegram removal + documentation streamline

### Removed
- **Telegram integration deleted as dead code.** After the 2026-06 move to the Hermes/MCP alert queue, the Telegram bot was disused. Removed `api/aria/telegram/` (bot + handler), the `/api/v1/telegram` routes, `get_telegram_handler` + startup/shutdown wiring in `main.py`/`deps.py`, all `telegram_*` settings in `config.py`, the `set_telegram_bot` no-op in `NotificationService`, and the wizard's Telegram section. Full suite green (732). Signal is unaffected.

### Changed
- **Documentation streamlined** to reduce overlap/drift (from a 15-doc root):
  - **Archived** 5 historical artifacts to `docs/archive/` (with an index): `IMPLEMENTATION_PLAN.md` (Signal-centric ABP→ARIA plan), `SHELLS_DESIGN.md` + `SHELLS_CHAT_TRANSCRIPT.md` + `ARIA_SHELLS_MERGE_PLAN.md` (post-merge shells design/history), `REVIEW_SUMMARY.md` (superseded review snapshot).
  - **Merged** `IDEAS.md` + `FUTURE_INVESTIGATIONS.md` → one **`BACKLOG.md`** (shipped items — Dream Cycle, Ambient Awareness, proactive behavior — pruned).
  - **Slimmed** `PROJECT_STATUS.md` (612→~90 lines) to a living status page; `CHANGELOG.md` is now the single source of shipped history. Rewrote `GETTING_STARTED.md` (~580→~330) correct + non-duplicative (native-systemd API, three-server LLM topology, no `:8080`/`:8000`).
  - **Scrubbed** stale facts: `:8000`→`:8200` (ui/README, SPEC, wizard), Signal/Telegram-as-live-channel lines (README, ARCHITECTURE), Pi-Coding-on-llama.cpp framing (ARCHITECTURE), + a living-truth banner on `SPECIFICATION.md`.
  - Added a **Documentation Map** (source-of-truth per doc) to `README.md`.

## [2026-07-04] - Multi-machine cockpit: design doc + Layer A (remote cockpit)

### Added
- **`MULTI_MACHINE_FLEET_DESIGN.md`** — design doc for making the TUI a cross-machine cockpit and letting the fleet span corsair-ai (the brain) and the MacBook (iOS/Xcode work). Two layers: **A** remote cockpit (native macOS TUI build + `aria tui --host` profiles + a fleet HOST column — the TUI is already a thin pure-HTTP client, so this is near-free), and **B2** an API-mediated, pull-based `aria-node` agent that registers the Mac's tmux shells/coding sessions into the single corsair brain and is driven back through a `shell_commands` queue via a host-aware `ShellService._dispatch`. Principle: one brain (corsair Mongo/memory), many hands; the watchdog + Ralph loop inherit remote sessions for free.
- **Layer A — remote cockpit (implemented).** The Go TUI can now run natively on any machine and point at a remote ARIA over the tailnet:
  - `tui/Makefile` with `build` / `build-darwin` (Apple-Silicon, `CGO_ENABLED=0`) / `build-linux` / `install` — verified the macOS cross-compile produces a working `Mach-O arm64` binary with no code changes.
  - **Host profiles**: `aria-tui --host <name|host:port|url>` resolves against a dependency-free `~/.config/aria/hosts` file (`$ARIA_HOSTS` override); precedence is flag → `ARIA_API_URL` env/.env → `default` profile → `http://localhost:8200`. `aria tui` (CLI) forwards extra args through to the binary. Covered by `tui/main_test.go`.
  - **`.env` fallback chain** in `tui/main.go`: `$ARIA_ENV` → `~/.config/aria/env` → the repo `.env`, so a MacBook needs no repo checkout to configure the cockpit.
  - **Fleet HOST column**: `host` added to `ShellOverviewItem` + `/shells/overview` (from the already-stamped `Shell.host`) and to the Go `Shell` struct; the TUI fleet view renders a compact HOST column (blank for coding sessions until Layer B2 adds session host).

## [2026-07-03] - Ralph loop (keep-a-session-going) + `aria tui` launcher

### Added
- **Ralph loop — opt-in, per-session "keep it going".** A coding session carrying a `loop_config` is nudged forward by the watchdog whenever it idles at its prompt (re-checking killswitch/e-stop **every nudge**) until it emits the done token (`coding_loop_done_regex`, default `RALPH_DONE`) or hits `max_nudges`/`deadline_minutes`. Absent `loop_config` = one-shot (unchanged; not default). Reuses the existing safety net (killswitch, e-stop, spend-cap, context-budget) and, because it drives through `session_manager.send_input`, works for any substrate.
  - New: `POST /api/v1/coding/sessions/{id}/loop {enabled, …overrides}` toggle; `loop_config` + bookkeeping on the `coding_sessions` doc + `set_loop_config()` (`agents/session.py`); `_maybe_nudge`/`_end_loop` in `agents/watchdog.py`; `loop_enabled` on `CodingSessionResponse`; `coding_loop_*` settings in `config.py`.
  - Surfaces: MCP `set_coding_loop` tool + `create_coding_session(loop=true)`; `start_coding_session` `loop` param; TUI toggle with `l` on both the **session** screen and the **fleet** screen (`⟳` marks looping sessions).
  - Tests: `api/tests/test_coding_loop.py` (normalize/toggle + watchdog nudge/done/cap/deadline/e-stop/killswitch/debounce).
- **`aria tui`** — the Python CLI now launches the Go TUI (resolves the binary from `$ARIA_TUI_BIN` / the repo `tui/` dir / PATH; `--build` to rebuild; extra args pass through).

## [2026-06-28] - Multi-runtime fleet, cost/health, model pinning, search, routines, PWA, backups, and computer-use

### Added
- **qwen-agentic addressable as backend `agentic`** — a 3rd coresident local llama.cpp server (Qwen3.6-27B, `:8093`, `agentic_url`) is now reachable as backend `"agentic"`. Local topology is now `llamacpp`/qwen-chat (`:8092`), `agentic`/qwen-agentic (`:8093`), `context1` (`:8081`) — all coresident on the APU. Aliases: `agentic`/`qwen-agentic`, plus `fireworks`/`glm` for Fireworks GLM 5.2.
- **pi-code as a first-class coding-session backend** — `start_coding_session(backend="pi-code", llm=<fireworks|agentic|llamacpp>, model=<id>)` runs ARIA's own agentic loop as a supervised, spawnable coding session alongside `claude_code` and `codex`. The LLM/model is **pinned** for the session and it inherits the watchdog + e-stop/killswitch gates. The three deployable runtimes are now Claude Code, pi-code+GLM (Fireworks), and pi-code+local-qwen — one unified `start_coding_session` path. Orchestrator default model is Fireworks GLM 5.2.
- **Cost accounting + spend circuit-breaker** — new `llm/pricing.py` price table (local backends + "default" = $0; cloud models priced; unknown cloud → conservative default). New endpoints `GET /usage/cost`, `/usage/by-session`, `/usage/by-conversation`; `/usage/by-model` now includes cost. Usage records now carry `backend` + `session_id`. A spend circuit-breaker (`spend_cap_usd_per_hour`, 0=off) trips the global e-stop (and escalates) when the last hour's priced spend exceeds the cap; the rate-limit watchdog also now watches fireworks.
- **`GET /health/services`** — concurrently probes all backing services (mongod, mongot, qwen-chat, qwen-agentic, context-1, embeddings, tts, stt, fireworks) returning per-service `{name, ok, latency_ms, detail}`. context-1/fireworks also added to the deep `/health` and `/health/llm` coverage.
- **Model pinning + routing** — `/model <backend> [<model-id>]` pins a conversation to a specific model (ids with `/` preserved); `/model` shows the current pin; `/model auto` unpins. An explicit pin is now **strict/fallback-free**. `/route <task>` applies an advisory heuristic suggestion as a pin you can override.
- **TUI fleet/health/search screens** — Fleet view (`f`: all coding sessions + watched shells with backend/model, status, idle, tokens + $cost), Health view (`h`: per-service status from `/health/services`), Search view (`s`: runs the search agent). Binary rebuilt.
- **Search agent easily invokable** — `/search <query>` (runs the context-1 search agent inline, shows ranked documents), `aria search <query>` CLI command, and the TUI search screen.
- **Scheduled autonomous routines** — the scheduler gained an `autopilot` action: schedule an autonomous goal (e.g. "every morning, triage my repos") that decomposes + executes via the autopilot service on a local-time cadence.
- **`browse_page` tool** — a built-in tool that fetches a URL and returns readable text (title + main content), following redirects (allowlisted).
- **Full computer-use via Playwright MCP** — ARIA drives a real headless browser (23 `browser_*` tools: navigate, accessibility snapshot, click/type by ref, screenshot, evaluate JS, tabs, network). Enabled via `tool_allowed_prefixes` (`browser_*` passes the allowlist) and a `"*"` wildcard in agent `enabled_tools` (one `browser_*` entry enables the family; the ARIA + pi-code agents have it). See the README "Computer Use (Playwright)" setup section.
- **Installable PWA** — the Next.js web UI now ships a manifest, service worker (network-first, bypasses `/api` + SSE), icons, and is installable.
- **Automated backups** — `scripts/aria-backup.sh` (mongodump of the `aria` DB via the mongod container + SOUL/journals/skills, with rotation), installed as a daily systemd user timer (`aria-backup.timer` @ 03:30). Restore is documented via mongorestore.
- **`/forget <query>`** — removes the single best-matching long-term memory (reviewable, one at a time).

### Changed
- The default ARIA orchestrator model remains Fireworks GLM 5.2, but conversations can now be pinned to any backend/model strictly (no fallback) via `/model`.
- `/models` (a.k.a. `/backends`) now lists `agentic` (qwen-agentic) alongside the other backends.
- The rate-limit / spend watchdog now also covers the `fireworks` backend.

### Notes
- Conversation branching/export already existed; this batch adds `/forget` for single-memory removal.

## [2026-06-26] - Absorb aria-shells; GLM 5.2; unified sub-agents; self-healing alerts

### Added
- **Fireworks LLM backend** (`llm/fireworks.py`) serving **GLM 5.2**; ARIA orchestrator + Pi Coding Agent switched to it (hard-pinned). `FIREWORKS_API_KEY` / `fireworks_base_url` in config.
- **Watched-shells / fleet subsystem** absorbed from the standalone `aria-shells` service (`api/aria/shells/`): auto-adopt via tmux hook + `ShellAdoptWorker`, capture, snapshot, extraction, `prune`, `selfcheck`, weekly `report`, project `harvest`. `fleet_overview`/`current_screen`/`send_input(wait_ms)`; routes under `/api/v1/shells`.
- **MCP server** (`mcp/server.py`) exposing ~31 tools to the Hermes agent — fleet, chat (ARIA orchestrator), conversations, agents, memory, coding sub-agents, projects/tasks, alerts.
- **Planning** projects fed by both the LLM `TaskExtractor` and the deterministic `ProjectHarvestWorker`; `/api/v1/todos` + `/projects/{id|slug}` (slug-or-id).
- **Alert queue** (`/api/v1/alerts`) + **Hermes self-healing triage**: on each alert Hermes spawns a diagnostic coding sub-agent, relays a root-cause + proposed fix to Signal, and applies on `APPLY` (verified end-to-end).
- **TUI** shows watched shells as a labeled sidebar group (with awaiting-input highlighting).
- Local LLMs **context-1** (`:8081`) and qwen **27B** (`:8093`) deployed alongside **qwen-chat 35B-A3B** (`:8092`) via `infrastructure/qwen-rocmfp4/`.

### Changed
- **ARIA is the single always-on service on `:8200`** (took over the port from the retired `aria-shells`; systemd drop-in). UI/CLI/TUI default to `:8200`.
- **Coding sub-agents run on the watched-shell substrate** (`coding_use_shell_substrate`): each is an interactive `claude-coding-*` shell — unified with the fleet, drivable via the same tools, visible in the TUI/MCP. Watchdog/checkpoint/review overlay retained.
- `llamacpp_url` → `:8092` (qwen-chat); `context1_url` → `:8081`. The old single llama.cpp on `:8080` retired (compose `legacy` profile).
- Notifications: ARIA no longer sends Signal/Telegram; `selfcheck` alerts **once per state-transition** (not hourly).

### Fixed
- `NotificationService.notify()` drops `coding:*` / `task` lifecycle events — they were polluting the alert queue and creating a triage feedback loop.
- TUI default port `:8000`→`:8200` and dotenv path; dashboard panel height clamp (header no longer scrolls off).
- Tolerant memory-extraction parser; per-call llama.cpp timeout; shells ANSI hardening.

### Removed
- ~187 GB of unused docker images + build cache (old llama.cpp variants).

### Notes
- `aria-shells` repo retained as reference only; its service is decommissioned.

## [2026-06-24] - Remove iOS client; ARIA is Linux-only

### Removed
- **`ios/` directory** — the native SwiftUI/iPadOS client (AriaMobile + AriaKit)
  is gone. ARIA is now exclusively a service that runs on a Linux machine,
  accessed via the Web UI, TUI, CLI, desktop widget, Signal, Telegram, and the
  REST API.
- **APNs push delivery** (`api/aria/shells/apns.py`) and all `apns_*` /
  `shells_apns_enabled` settings in `config.py` — Apple Push was only used by
  the iOS client.
- **Device registration API** (`api/aria/api/routes/devices.py`,
  `POST /api/v1/devices` and friends) plus its router registration in
  `main.py` and the `test_devices_routes.py` suite — device tokens existed
  solely to target APNs.

### Changed
- `shells/notifier.py` no longer attempts APNs delivery; idle-prompt alerts
  continue to route through Signal/Telegram via `NotificationService`.
- Dropped incidental iOS references from code comments
  (`core/orchestrator.py`, `shells/capture.py`), the task-extraction prompt,
  a planning unit test, and the project docs (README already had none).

### Notes
- The watched-shells subsystem itself is unchanged and fully retained — it is a
  Linux/tmux feature consumed by the Web UI dashboard, TUI, and CLI.
- The 2026-04-18 entry below is kept as historical record.

## [2026-04-18] - Native iOS / iPadOS client + shells/devices API

### Added
- **`ios/` subfolder** — native SwiftUI app targeting iOS 17+, Swift 6, Xcode 26.
  Project is generated via XcodeGen (`ios/project.yml`). See `ios/README.md`.
  - **AriaKit** local SPM package: `Sendable` Codable models, `URLSession`-based
    `AriaClient` with optional `X-API-Key`, `AsyncThrowingStream` SSE parser,
    Keychain helper. Typed API clients: Shells, Conversations, Memories,
    Health, Devices.
  - **AriaMobile** app target: TabView root on iPhone, `NavigationSplitView`
    on iPad. Full shells coverage — list/filter/search, create sheet, SwiftTerm
    live ANSI terminal on detail with 2000-event backfill + SSE tail + auto
    reconnect, input bar + key accessory bar (Esc/Tab/arrows/⏎/Ctrl-?/yes/no),
    kill session, edit tags, 3s snapshot view, noise filter toggle. Also
    chat with streaming SSE replies + steer, memory search (debounced
    hybrid search).
  - **SwiftTerm** SPM dependency for real VT100 rendering from day one.
- **`POST /api/v1/shells`** — create a detached tmux session and register it
  as a watched shell. Body: `{name, workdir?, launch_claude=true}`. Name is
  prefixed with the configured shells prefix (`claude-`) if not already.
  Launches Claude Code in the new session by default.
- **`DELETE /api/v1/shells/{name}`** — kill a tmux session and mark its
  shell row stopped.
- `ShellService.create_shell` / `kill_shell` and `TmuxClient.new_session`
  wrap these operations; existing tmux hooks (`session-created`) continue
  to wire up pipe-pane capture for new sessions.
- **`POST /api/v1/devices`** / **`DELETE /api/v1/devices/{token}`** — APNs
  device-token registration for mobile push. Stored in `devices` collection.
- **APNs idle alerts** (feature-flagged) — `IdleNotifier` now fans out to
  `send_apns_alert` when `shells_apns_enabled=true`. Transport itself is a
  stub (`api/aria/shells/apns.py`) with clear config-check + logging; flip
  the flag and drop in `httpx[http2]`/`aioapns` when ready to deliver.

### Changed
- `api/aria/main.py` registers the new `devices` router.
- `api/aria/config.py` gains `shells_apns_enabled`, `apns_team_id`,
  `apns_key_id`, `apns_bundle_id`, `apns_auth_key_path`, `apns_use_sandbox`.

### Notes
- No change needed to the existing shells SSE payload: `ShellEvent` was
  already serialized via `model_dump_json()` which includes `text_raw`, so
  SwiftTerm gets raw ANSI from the existing `/shells/{name}/stream` route.
- Route tests: `tests/test_shells_routes.py` (+6 cases) and
  `tests/test_devices_routes.py` (+5 cases), all green.

## [2026-04-15] - Local Agentic Search (Chroma context-1)

### Added
- **context-1 LLM backend** (`api/aria/llm/context1.py`) — new "context1" backend
  registered in `llm/manager.py`, pointing at a second llama.cpp instance that
  serves the chromadb/context-1 20B GGUF
  (https://huggingface.co/ryancook/chromadb-context-1-gguf). Configured via
  `context1_url`, `context1_model`, `context1_max_iterations`, `context1_max_docs`,
  and `context1_fs_allowed_roots` in `config.py`.
- **Search Agent tool** (`api/aria/tools/builtin/search_agent.py`) — agentic
  observe/reason/act retrieval loop driven by context-1. Exposes six tools to
  the model: `memory_search` (hybrid vector+BM25 over `memories`), `web_search`
  + `web_read` (existing search provider + WebTool), `fs_grep` + `fs_read`
  (ripgrep + bounded file reads over allowed roots), `prune`, and `finalize`.
  Returns a ranked list of documents with stable `mem:`/`web:`/`file:` ids.
  Registered at startup when the context1 backend is available.
- **Search Agent profile** (`slug=search-agent`, seeded in `db/migrations.py`) —
  named agent profile with `mode_category=research`, `backend=context1`, and
  `enabled_tools=[search_agent, web, filesystem, deep_think]`.
- **Research service integration** (`api/aria/research/service.py`) — when the
  research run's backend is `context1`, the branch loop now gathers sources via
  `search_agent` (over memory + web + local files) instead of the web-only
  provider path. Falls back to the web provider if the tool errors.
- **Infrastructure service** (`infrastructure/docker-compose.yml`) — new
  `llamacpp-context1` service (profile `context1`) on port 8081 that reuses the
  existing ROCm llama.cpp build to serve the context-1 GGUF from
  `${LLAMACPP_MODELS_DIR}/context-1/`.

### Notes
- The GGUF must be downloaded once into `infrastructure/models/llm/context-1/`
  (e.g. `chromadb-context-1-Q4_K_M.gguf`). Start the service with
  `docker compose --profile context1 up -d llamacpp-context1`.
- The model does not have upstream tool-calling templates documented; the
  service launches with `--jinja` so llama.cpp's OpenAI-compatible tool-call
  path is active. Adjust `CONTEXT1_ARGS` if the template needs tuning.

## [2026-04-13] - Agent Safety Subsystems & Escalation

### Added
- **Context Budget Guard** (`api/aria/agents/budget_guard.py`) — monitors coding
  sessions for context-window exhaustion via heuristic signals (provider limit
  messages, Claude Code compaction notices, latency spikes, explicit mentions).
  Thresholds: WARN 75%, SOFT 85% (checkpoint + notify), HARD 92% (checkpoint +
  stop + suggest resume).
- **Session Checkpointing** (`api/aria/agents/checkpoint.py`) — persists coding
  session state (current task, modified files, branch, last commit, notes) to
  MongoDB so crashed agents can be resumed with full context.
- **Emergency Stop / Rate-Limit Watchdog** (`api/aria/agents/estop.py`) —
  MongoDB-backed global estop that freezes all agent activity on API rate
  limits or critical errors. Visible across processes, persists across restarts,
  auto-thaws when clear.
- **Inter-Agent Mail Protocol** (`api/aria/agents/mail.py`) — structured
  agent-to-agent messages (`TASK_DONE`, `HANDOFF`, `RESULT`, `ERROR`,
  `CHECKPOINT`) stored in MongoDB and polled by the orchestrator.
- **Tmux Agent Backend** (`api/aria/agents/backends/tmux.py`) — spawns coding
  agents in visible, color-coded tmux panes in a dedicated `aria-agents`
  session so the user can watch multiple agents work in parallel.
- **Escalation Protocol** (`api/aria/notifications/escalation.py`) — severity
  routing (CRITICAL/HIGH/MEDIUM/LOW) with auto-resolution attempts before user
  notification and auto-re-escalation of stale items.
- Broad test coverage across new and existing subsystems: agent mail, autopilot,
  awareness, budget guard, builtin tools, checkpoint, coding session, context
  builder, db models, dream service, embeddings, escalation, estop, killswitch,
  llm manager, mcp, orchestrator, orchestrator tool loop, short-term memory,
  steering, usage repo, watchdog.

### Changed
- Expanded `agents/watchdog.py` and `agents/session.py` to integrate budget
  guard, checkpoint, estop, and mail signals.
- Extended `dreams/service.py` and `prompts/dream_reflection.md` with deeper
  reflection flow.
- Wired new safety dependencies through `api/deps.py`, `main.py`, and DB
  migrations (`db/migrations.py`).

### Notes
- Inspired by Gas Town's context-budget-guard, checkpoint, rate-limit-watchdog,
  mail, and tiered escalation patterns.

---

## [2026-02-16] - Phase 6 - Voice I/O (TTS + STT)

### Added
- TTS microservice (`tts/`) running Qwen3-TTS 0.6B CustomVoice model on CPU
- `POST /v1/tts/synthesize` endpoint for speech synthesis (returns WAV audio)
- `GET /v1/tts/speakers` endpoint listing 9 available voice speakers
- `GET /v1/tts/health` endpoint for TTS service health checks
- API proxy routes forwarding TTS requests to the microservice
- Widget play button on assistant messages for reading responses aloud
- Docker Compose `tts` service on port 8002
- `TTS_URL` configuration in `.env.example` and API settings
- STT microservice (`stt/`) running whisper-large-v3-turbo via faster-whisper on CPU (int8)
- `POST /v1/stt/transcribe` endpoint accepting audio file upload, returns transcribed text
- `GET /v1/stt/health` endpoint for STT service health checks
- API proxy routes forwarding STT requests to the microservice
- Widget mic button now functional: click to record, click again to stop and transcribe
- Recording indicator (pulsing red) and transcribing state on mic button
- Docker Compose `stt` service on port 8003
- `STT_URL` configuration in `.env.example` and API settings

---

## [2025-12-28] - Phase 2 - Fix Memory Extraction Background Tasks

### Fixed
- **Memory extraction now uses FastAPI BackgroundTasks instead of asyncio.create_task()**
  - Orchestrator now accepts `background_tasks` parameter from API routes
  - Proper lifecycle management for background memory extraction tasks
  - Prevents memory extraction tasks from being cancelled prematurely
  - Fallback to asyncio.create_task for non-HTTP contexts (CLI, tests)
  - Location: `api/aria/core/orchestrator.py:280-309`

- **Manual memory extraction API now uses agent's LLM configuration**
  - Fixed `/api/v1/memories/extract/{conversation_id}` endpoint
  - Previously used hardcoded defaults (ollama/llama3.2:latest)
  - Now correctly looks up and uses the agent's configured LLM backend and model
  - Ensures extraction works with OpenRouter, Anthropic, OpenAI, etc.
  - Location: `api/aria/api/routes/memories.py:232-251`

- **Message sending routes now pass BackgroundTasks to orchestrator**
  - Updated `/api/v1/conversations/{id}/messages` endpoint
  - Passes BackgroundTasks to orchestrator for proper memory extraction scheduling
  - Works for both streaming and non-streaming modes
  - Location: `api/aria/api/routes/conversations.py:154-196`

### Changed
- Added logging to memory extraction with `[MEMORY EXTRACTION]` prefix for easier debugging
- Memory extraction errors now print to logs for troubleshooting

### Notes
- **Action Required**: Restart API container to apply these fixes
- These fixes resolve the issue where memories were not being automatically extracted from conversations
- Memory extraction will now work reliably with any LLM backend (Ollama, OpenRouter, Anthropic, OpenAI)

---

## [2025-12-27] - Phase 5 - Fix Missing UI API Client

### Fixed
- Created missing `ui/src/lib/api-client.ts` file
  - Implements complete API client for ARIA API
  - Methods for health check, conversations, agents, memories, tools, MCP
  - Streaming message support with Server-Sent Events
  - TypeScript types integration
- Resolves UI build errors ("Module not found: Can't resolve '@/lib/api-client'")

### Added
- Complete API client implementation with:
  - Health check endpoint
  - Conversation CRUD and streaming
  - Agent management
  - Memory operations and search
  - Tool listing and execution
  - MCP server management

### Notes
- This file was referenced in UI code but was missing from the repository
- Phase 5 Web UI can now build successfully

---

## [2025-12-27] - Infrastructure - MongoDB Community Search Update

### Changed
- Updated MongoDB Community Search (mongot) to version 0.55.0 (from 0.53.1)
  - `docker-compose.yml` - Updated mongot image version
  - `SPECIFICATION.md` - Updated documentation to reflect latest version
- Latest mongot version provides improved performance and bug fixes

### Notes
- Version 0.55.0 is the latest stable release of MongoDB Community Search
- No breaking changes from 0.53.1 to 0.55.0
- Existing mongot data volumes remain compatible

---

## [2025-12-27] - Phase 4 - OpenRouter Health Check Fix

### Fixed
- Added OpenRouter to health check endpoint (`api/aria/api/routes/health.py`)
  - OpenRouter now included in `/api/v1/health/llm` status checks
  - Completes OpenRouter integration (was missing from health check backends list)

---

## [2025-12-25] - Phase 4 - OpenRouter Support

### Added
- OpenRouter adapter (`api/aria/llm/openrouter.py`)
  - OpenAI-compatible API for unified access to multiple LLM providers
  - Supports models from OpenAI, Anthropic, Google, Meta, and more
  - Streaming support with proper message formatting
  - Tool use (function calling) support
  - Optional HTTP-Referer and X-Title headers for app rankings
  - Uses OpenAI SDK with custom base URL (https://openrouter.ai/api/v1)
- OpenRouter configuration
  - `OPENROUTER_API_KEY` environment variable
  - Added to `.env.example` with other API keys
  - Configuration in `api/aria/config.py`
  - Docker compose environment variable pass-through

### Changed
- Updated LLM manager (`api/aria/llm/manager.py`)
  - Added "openrouter" backend support
  - Backend availability check for OpenRouter
  - Error messages for missing OpenRouter API key
- Updated documentation
  - `CLAUDE.md` - Added OpenRouter to adapter list and configuration
  - `SPECIFICATION.md` - Added OpenRouter to cloud API options
  - `README.md` - Updated LLM agnostic description
  - Added API key security section to CLAUDE.md

### Notes
- OpenRouter reuses the OpenAI SDK (no additional dependencies)
- Model names in OpenRouter use provider prefixes (e.g., "openai/gpt-4")
- API keys stored in `.env` file (git-ignored for security)
- OpenRouter provides cost-effective access to multiple providers through one API

---

## [2025-12-25] - Documentation & Configuration - CLAUDE.md and Embedding Standardization

### Added
- `CLAUDE.md` - Comprehensive guide for Claude Code sessions
  - Quick start instructions (PROJECT_STATUS.md, CHANGELOG.md, SPECIFICATION.md)
  - Architecture overview (core flow, memory system, tools, LLM adapters)
  - MongoDB 8.2 + mongot setup details
  - Development commands (Docker, API, UI, CLI, Database)
  - Code patterns and conventions
  - Key files reference organized by layer
  - Configuration examples
  - Important gotchas and best practices
  - Current phase summary

### Changed
- **Embedding configuration standardized across entire codebase**
  - Model: Changed from `qwen3:8b` to `qwen3-embedding:0.6b`
  - Dimensions: Changed from 4096 to 1024
  - Updated files:
    - `.env.example` - Default embedding model and dimensions
    - `api/aria/config.py` - Configuration class defaults
    - `SPECIFICATION.md` - All architecture diagrams, code examples, and index definitions
    - `README.md` - Key design decisions
    - `CLAUDE.md` - All embedding references
- Improved clarity in embedding configuration
  - Comment: "Using qwen3-embedding:0.6b model with 1024-dimensional embeddings for optimal balance of quality and performance"
  - Note: docker-compose.yml and scripts/init-mongo.js were already correct at 1024 dims

### Fixed
- Configuration inconsistencies between environment files, code defaults, and documentation
- All vector search index definitions now consistently use 1024 dimensions
- All embedding service references now use correct model name

### Notes
- This standardization ensures vector search works correctly with actual embedding dimensions
- MongoDB vector index in `scripts/init-mongo.js` already used 1024 dims
- Docker compose defaults already used `qwen3-embedding:0.6b`
- Changes align runtime configuration with actual deployed setup

---

## [2025-12-06] - Phase 4 - Cloud LLM Adapters

### Added
- Anthropic/Claude adapter (`api/aria/llm/anthropic.py`)
  - Streaming support with proper message formatting
  - Tool use (function calling) support
  - Support for all Claude 3 models (Opus, Sonnet, Haiku)
  - System prompt handling
  - Error handling with proper error messages
- OpenAI adapter (`api/aria/llm/openai.py`)
  - Streaming support with chunk accumulation
  - Function calling support
  - Support for GPT-4, GPT-4 Turbo, GPT-3.5
  - Proper message role handling (including tool role)
  - Error handling
- LLM backend availability check (`api/aria/llm/manager.py`)
  - Check if API keys are configured
  - Verify SDK packages are installed
  - Helpful error messages for missing configuration
- LLM health endpoint (`api/aria/api/routes/health.py`)
  - `GET /api/v1/health/llm` - Check all LLM backend status
  - Returns availability and reason for each backend

### Changed
- Updated LLM manager (`api/aria/llm/manager.py`)
  - Register Anthropic adapter when API key present
  - Register OpenAI adapter when API key present
  - Lazy import of cloud adapters to avoid import errors
  - Validate API keys before creating adapters
- Updated orchestrator (`api/aria/core/orchestrator.py`)
  - Added `_get_llm_with_fallback()` method
  - Automatic fallback to cloud LLMs on error
  - User notification when fallback is used
  - Support for configurable fallback conditions
- Updated configuration
  - API keys already present in config (ANTHROPIC_API_KEY, OPENAI_API_KEY)
  - Already in `.env.example` file

### Notes
- Cloud LLM packages (anthropic, openai) already in requirements.txt
- API keys must be set in environment variables
- Fallback chain configured per agent in agent.fallback_chain
- Fallback conditions: on_error, on_context_overflow (future)
- Cloud LLMs automatically used when primary LLM fails

---

## [2025-12-06] - Phase 3 - Tools & MCP

### Added
- Tool infrastructure (`api/aria/tools/`)
  - BaseTool abstract class with parameter validation
  - ToolRouter for tool registration and execution
  - ToolDefinition and ToolParameter models
  - ToolResult with status tracking and metadata
- Built-in tools (`api/aria/tools/builtin/`)
  - Filesystem tool: read/write files, list directories, manage files
  - Shell tool: execute commands with timeout and sandboxing
  - Web tool: HTTP GET requests with size limits
- MCP (Model Context Protocol) integration (`api/aria/tools/mcp/`)
  - MCP client with JSON-RPC 2.0 over stdio
  - MCP manager for multi-server lifecycle management
  - MCPToolWrapper for BaseTool compatibility
  - Server health tracking and tool registration
- Tool management API routes (`api/aria/api/routes/tools.py`)
  - List tools (with type filtering)
  - Get tool details
  - Execute tools directly
  - MCP server CRUD (add, remove, list)
  - List tools per MCP server
  - Tool statistics endpoint
- CLI tool commands
  - `aria tools list` - List available tools
  - `aria tools info <name>` - Show tool details
  - `aria tools execute <name> <args>` - Execute a tool
  - `aria mcp list` - List MCP servers
  - `aria mcp add <id> <command>` - Add MCP server
  - `aria mcp remove <id>` - Remove MCP server
  - `aria mcp tools <id>` - List server's tools

### Changed
- Updated orchestrator (`api/aria/core/orchestrator.py`)
  - Added tool_router parameter
  - Tool definitions passed to LLM when tools enabled
  - Handle tool calls from LLM responses
  - Execute tools and save results to conversation
  - Tool results included in streaming response
- Updated main app (`api/aria/main.py`)
  - Initialize built-in tools on startup
  - Register tools with tool router
  - Shutdown MCP servers on app shutdown
  - Added tools routes
- Updated API dependencies (`api/aria/api/deps.py`)
  - Added get_tool_router() dependency
  - Added get_mcp_manager() dependency
  - Pass tool_router to orchestrator
- Updated CLI with tool and MCP management commands
- Updated `PROJECT_STATUS.md` to Phase 3

### Notes
- Tools must be explicitly enabled per agent (capabilities.tools_enabled)
- Agents can specify which tools they can use (enabled_tools list)
- Filesystem tool is sandboxed to allowed paths (default: user home)
- Shell commands can be filtered with allow/deny lists
- MCP servers communicate via stdio transport
- Tool execution has configurable timeout (default: 5 minutes)

---

## [2025-11-29] - Phase 2 - Memory System

### Added
- Embedding service (`api/aria/memory/embeddings.py`)
  - Ollama embeddings using Qwen3-8b (4096 dimensions)
  - Voyage AI fallback provider
  - Batch embedding support
- Short-term memory (`api/aria/memory/short_term.py`)
  - Current conversation context retrieval
  - Recent conversations context
  - Token budget management
- Long-term memory (`api/aria/memory/long_term.py`)
  - Vector search using MongoDB Atlas Vector Search
  - Lexical search using MongoDB Atlas Search (BM25)
  - Hybrid search with Reciprocal Rank Fusion (RRF)
  - Memory CRUD operations with automatic embedding generation
  - Access tracking and statistics
- Memory extraction pipeline (`api/aria/memory/extraction.py`)
  - LLM-based extraction from conversations
  - Batch message processing
  - Source tracking and confidence scoring
  - Manual extraction from arbitrary text
- Context builder (`api/aria/core/context.py`)
  - Memory injection into system prompts
  - Short-term + long-term memory integration
  - Relevance-based memory retrieval
- Memory API routes (`api/aria/api/routes/memories.py`)
  - List, create, get, update, delete memories
  - Hybrid search endpoint
  - Background extraction trigger
- CLI memory commands
  - `aria memories list` - List all memories
  - `aria memories search` - Search with hybrid search
  - `aria memories add` - Manually add memories
  - `aria memories extract` - Trigger extraction

### Changed
- Updated orchestrator (`api/aria/core/orchestrator.py`)
  - Integrated context builder for memory-aware responses
  - Automatic memory extraction in background
  - Access tracking for retrieved memories
- Updated main app to include memory routes
- Updated CLI with memory management commands
- Updated `PROJECT_STATUS.md` to Phase 2

### Notes
- Requires MongoDB search indexes for vector and lexical search
- Embeddings require Ollama with embedding model (e.g., qwen3:8b)
- Memory extraction runs asynchronously to avoid blocking chat
- Hybrid search combines best of lexical and semantic search

## [2025-11-29] - Phase 1 - Core Implementation

### Added
- Docker infrastructure (`docker-compose.yml`, Dockerfile, `.env.example`)
- MongoDB initialization script with replica set and search indexes
- FastAPI application foundation
  - Main app with lifespan management (`api/aria/main.py`)
  - Configuration management (`api/aria/config.py`)
  - MongoDB connection layer (`api/aria/db/mongodb.py`)
  - Pydantic models for API (`api/aria/db/models.py`)
- LLM adapter layer
  - Base adapter interface (`api/aria/llm/base.py`)
  - Ollama adapter with streaming support (`api/aria/llm/ollama.py`)
  - LLM manager for backend selection (`api/aria/llm/manager.py`)
- Agent orchestrator (`api/aria/core/orchestrator.py`)
  - Message processing with streaming
  - Conversation context assembly
  - Response persistence
- API routes
  - Health check endpoint
  - Conversations CRUD with SSE streaming
  - Agents CRUD
- CLI client (`cli/aria_cli/main.py`)
  - Interactive chat mode
  - Conversation management commands
  - Agent listing

### Changed
- Updated `PROJECT_STATUS.md` to reflect Phase 1 progress
- All core Phase 1 checklist items marked complete

### Notes
- Phase 1 implementation complete, ready for testing
- Requires Ollama running locally for testing
- MongoDB 8.2 with mongot for vector search
- Testing infrastructure not yet implemented

## [Unreleased]

### Added
- Initial project specification (`SPECIFICATION.md`)
- Project status tracking (`PROJECT_STATUS.md`)
- This changelog

### Changed
- Updated MongoDB configuration to use Community Server 8.2 + mongot
- Switched from `mongodb-atlas-local` to separate `mongod` + `mongot` services
- Updated embedding dimension to 4096 (Qwen3-8b)
- Added hybrid search (BM25 + Vector) with RRF fusion for long-term memory

### Notes
- See `PROJECT_STATUS.md` for current checklist
- MongoDB 8.2 Vector Search is in Public Preview
- mongot image: `mongodb/mongodb-community-search:0.53.1`
- mongod image: `mongodb/mongodb-community-server:8.2.0-ubi9`

---

<!-- 
Template for new entries:

## [YYYY-MM-DD] - Phase X - [Summary]

### Added
- 

### Changed
- 

### Fixed
- 

### Notes
- 

-->
