# ARIA — Backlog

Open work and unanswered questions. Shipped items live in `CHANGELOG.md`; the current
architecture is in `CLAUDE.md` and `vault/ProjectAria/START_HERE.md`.

**Last updated:** 2026-08-15T23:12:49-04:00 — rewritten from ~515 lines after a full docs
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
