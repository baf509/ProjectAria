# Archived documentation

These are **historical artifacts** — completed plans, superseded design specs,
and one-off snapshots. They are kept for decision provenance but do **not**
describe the current system. For current truth, see the root docs (`README.md`,
`CLAUDE.md`, `ARCHITECTURE.md`, `CHANGELOG.md`, `SPECIFICATION.md`).

| File | What it is | Superseded by |
|------|-----------|---------------|
| `IMPLEMENTATION_PLAN.md` | The ABP→ARIA consolidation roadmap (2026-03). Its headline feature — Signal as the mobile interface — was later dropped (Hermes owns notifications). | `CHANGELOG.md` (shipped features); `PROJECT_STATUS.md` |
| `SHELLS_DESIGN.md` | The original "Proposed" v1 design for the watched-shells subsystem (2026-04). The code was built and then extended well past it (auto-adopt, fleet, coding substrate, MCP). | `CLAUDE.md` "Watched Shells & Fleet" + `api/aria/shells/` |
| `ARIA_SHELLS_MERGE_PLAN.md` | The plan to fold the standalone `aria-shells` service into ProjectAria — self-labeled "IMPLEMENTED + CUT OVER" (2026-06). | `CLAUDE.md` (current architecture) |
| `SHELLS_CHAT_TRANSCRIPT.md` | Raw design chat log that produced `SHELLS_DESIGN.md` (2026-04); uses pre-final naming. | `SHELLS_DESIGN.md` (cleaner) |
| `REVIEW_SUMMARY.md` | A one-off Phase-5 (Web UI) code-review snapshot (2025-12); all issues since fixed. | `CHANGELOG.md` 2025-12-27 entries |
