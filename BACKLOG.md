# ARIA — Backlog

Open work and unanswered questions. Shipped items live in `CHANGELOG.md`; the current
architecture is in `CLAUDE.md` and `vault/ProjectAria/START_HERE.md`.

**Last updated:** 2026-08-17T15:54:02-04:00 — added §6 (web UI rebuild). Previous: 2026-08-15T23:12:49-04:00 — rewritten from ~515 lines after a full docs
audit. The previous backlog was written for **an ARIA a human chats with**: its centrepiece
items (Mood Model, Curiosity Engine, Anticipatory Preparation, Ritual Engine) all keyed off
conversation signals, and `db.conversations` holds two documents, both from 2026-07-31, with
the default `aria` agent disabled since 2026-07-28. Those items were not deferred, they were
premised on a surface that no longer exists. What follows is what survived verification
against the code.

---

## 1. Type the API boundary

The highest-value item here, and the only one with a measured cost already paid.

`api/aria/db/models.py` has **zero** `Literal`/`Enum` types for any `backend`, `status`,
`type` or `kind` field — every one is a bare `str`. With no boundary type, **the docstring
becomes the only schema**, and nothing forces it to match the dispatch site.

The origin: on 2026-07-27 a one-word typo — `backend="pi"` instead of `"pi-code"` — cost a
debugging round-trip and silently handed back a Claude agent when a pi agent was asked for.
An audit for that shape then found the same defect in seven more places.

Verified still true (2026-08-15):

| | Literals |
|---|---|
| `api/aria/db/models.py` | **0** |
| `api/aria/planning/models.py` | 9 — *and the one module the audit found no defects in* |
| `api/aria/shells/models.py` | 5 |

Concrete pieces:

- **`MemoryCreate.content_type`** (`api/aria/api/routes/memories.py:29-34`) is a *comment*,
  not a Literal: `content_type: str  # "fact" | "preference" | ...`. So
  `content_type="note"` is stored with a 201 and no documented filter ever retrieves it —
  silently, forever.
- **`coding_sessions.py:104`** passes `llm=body.llm` straight through with no validation, so
  the API returns 201 and the session dies asynchronously.
- **A test asserting "every value named in an MCP docstring is accepted by its dispatch"**
  would have caught all eight original findings. Still unwritten, and now higher-stakes:
  `mcp/server.py` is a symlink into the repo and about 90 tools wide.

*(The sibling complaint about `/shells` accepting any status/kind is fixed —
`routes/shells.py:73-74,105,320` now validates both against `get_args(...)`.)*

---

## 2. Salvaged from retired plans — all verified unbuilt

- **Vector quantization experiment.** `Binary.from_vector` also supports `INT8` and
  `PACKED_BIT`; scalar/binary quantization could cut vector storage roughly 4–32×. Keep
  FLOAT32 until measured. (Neither constant appears anywhere in `api/aria/`.)
- **Ralph-loop slot release between nudges.** A looping session holds a concurrency slot for
  its whole life — intended backpressure — but it could release the slot while idle between
  nudges. (`_release_slot` exists only on finalize paths in `agents/session.py`;
  `agents/watchdog.py` never touches slots.)
- **`judge` / `vote` workflow action.** Score N fan-out candidates and return a winner,
  beyond what `synthesize` merges. Explicitly "not required for parity" at the time; no such
  action exists in `workflows/engine.py`.
- **File-based specialist profiles.** A loader importing `*.md` (frontmatter + body) into
  `db.agents`. More attractive now than when it was written: charters and steward plans
  already live as vault markdown read by `integrations/vault_reader.py`, so the machinery
  exists.

---

## 3. Memory confidence decay is documented but never runs

Raised 2026-04, **verified still true 2026-08-15**. The docs have long claimed that
"confidence scores decay over time; frequently accessed memories rank higher." The only
implementation is `POST /memories/maintenance`
(`api/aria/api/routes/memories.py:340-384`), and **nothing calls it** — no scheduler entry,
no worker, no reference anywhere outside its own definition.

Why it matters more than it looks: the store grows monotonically (20k+ memories, 82 of them
with `access_count > 0`), so "confidence" is permanently whatever the extraction LLM guessed
at write time. Retrieval quality degrades quietly, which is the worst way for it to degrade.

Salvaged from `docs/archive/SHELLS_CHAT_TRANSCRIPT.md` before that directory was deleted; it
was the one open defect in 142 KB of retired planning material.

---

## 4. Open operational questions

- **Qwen3.8 tool-call reliability with Hermes is unmeasured.** It has been Hermes's default
  since 2026-08-15T16:35 and is live at `:8080` (`-c 327680 -np 2`). Worth watching for a day
  before trusting it with anything that matters. *(The other topology follow-ups live in the
  vault plan's §P.4; this one is carried nowhere else.)*
- **Retrieval is still in `fallback` mode** (since 2026-08-15T17:19-04:00). Turning
  `embeddings` back on drains the 826 queued re-embeddings in one action; turning `search`
  back on needs `shared-mongot`, which is **shared with AgentBenchPlatform** — a cross-project
  call. Until then every memory search is the mongod-native scan, now bounded to the last 180
  days (`memory_fallback_recency_days`). This is D16's operational half from
  `vault/ProjectAria/Planning/PERFORMANCE_REVIEW_FIXES_20260817.md` (§E.3); the code half
  landed 2026-08-18.
- **Three Phase 4 retention defaults were assumed, not chosen** (2026-08-18):
  `conversation_message_cap=200`, `usage_retention_days=365`,
  `memory_fallback_recency_days=180`. Each is a setting. ⚠️ Lowering `usage_retention_days`
  deletes rows on the next TTL sweep — there is no undo.
- **The service registry's docker timeout is too short.**
  `api/aria/infrastructure/services.py:383` — `async def _run(*args, timeout: float = 10.0)`.
  Every slow container stop reports a false failure. The capabilities route re-checks real
  state to work around it; `_run` itself is still short.

---

## 5. Gated on evidence, not on effort

These are built and deliberately switched off. They are not backlog items in the usual sense
— nothing needs writing — but they need a human decision or a body of data:

| | Waiting on |
|---|---|
| Triage worker | Its first cloud DIAGNOSE run should happen with Ben watching |
| Research planner | An approved plan on a chartered project |
| Improver | ≥20 labelled session outcomes before it will propose anything |
| Local models at autonomy A3 | 20 clean A2 merges + tool-call reliability ≥98% over ≥200 calls |

See `vault/ProjectAria/Planning/ARIA_PROJECT_STEWARD_PROPOSAL_20260815.md` §E.

---

## 6. Web UI: responsive rebuild (planned 2026-08-17; **implementation in progress** as of 2026-08-17T16:38:48-04:00)

Ben's report: the web UI "does not auto-fit a phone screen, you see blank white sections and
need to zoom." A measured Playwright audit of the live UI (2026-08-17) confirmed it and found the
mechanisms: `/cockpit` overflows a 390 px viewport by 92–107 px (grid with no base column), `/operate`
by 40–55 px (unbreakable path in registry prose), the shell masks overflow with
`body{overflow-x:hidden}` + `minimumScale:1`, virtually every tap target is < 44 px, most text is
10–11 px `ink-faint` at 2.7–3.2:1 contrast, ~80 `bg-live/40`-style utilities compile to **nothing**
(bare `var()` tokens have no alpha — the shells page's light-mode buttons have invisible labels),
`/dashboard/shells` renders 6,046 DOM nodes and opens its SSE stream with no `since_line`,
`/dashboard` fires 15 requests gated on the 8.8 s `/infrastructure/model-servers` call, and the API
key is baked into eight JS chunks of an image six days behind source.

**Plan (the deliverable):** `vault/ProjectAria/Planning/WEB_UI_RESPONSIVE_REBUILD_20260817.md` —
decisions, measured baseline, root causes, target architecture (layout engine + tokens + shell,
same-origin BFF proxy + runtime config + HTTPS, SWR data layer, route-segment master/detail,
Playwright ratchet gate), page-by-page refit, 11 phases with exits, risks, and open questions for
Ben. Harness + baseline: `ui/e2e/audit-2026-08-17/` (untracked seed for Phase 0).

**API-side companions — status:**

- ✅ **`GET /infrastructure/model-servers` no longer stalls.** The plan blamed a
  serial `_inspect` loop and prescribed `asyncio.gather`. **That was wrong, and
  measuring it first is what caught it:** gathering made it *worse* (8.07 s vs
  0.25 s serial), because 27 concurrent rows each start their own probe before
  any cache write. The real cost is the two off-box specs (Ridge asleep = a 3 s
  health timeout + a 4 s reachability timeout) against a 20 s TTL cache — so the
  endpoint stalled ~8.8 s **once every 20 seconds**, while the page polled every
  10 s. Fixed in `model_servers.py` with stale-while-revalidate + single-flight:
  an expired entry is served immediately and refreshed behind the read, only an
  unknown remote blocks, and `fresh=True` (start/stop) still probes
  synchronously. Measured after: cold 8.4 s (once), every subsequent read
  0.30 s. Covered by four tests in `tests/test_model_servers.py`.
- ⬜ `response_model` on `/alerts`, `/infrastructure/model-servers`,
  `/projects/overview`, `/infrastructure/services` (all `{}` 200 schemas today)
  so UI types can be generated instead of hand-authored.
- ⬜ `content_hash`/ETag on `GET /shells/{name}/screen` so the terminal can skip
  a re-render when the pane is unchanged.
- ⬜ Record the model-server slug on benchmark runs (they are matched by port
  substring today, which attributes a run to a retired sibling on the same port).
- ⬜ `kind` / `charter.purpose` on `/projects/overview` so Supervise can filter
  inventory server-side instead of client-side.
- ⬜ **Who executes `APPLY`?** `POST /alerts/{id}/decide` records the decision and
  clears `needs_human`; nothing consumes `decision.value=='APPLY'` (only
  `outcomes.py:912` reads IGNORE/REJECT). The Inbox therefore says the decision
  is *recorded* and does not imply the fix ran.
- ⬜ Shrink `cors_origins`/`allow_origin_regex` to the Tauri widget origins now
  that the UI is same-origin — the regex is otherwise a second, unauthenticated
  door.


