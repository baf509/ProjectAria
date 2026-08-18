# Responsive audit harness — 2026-08-17 baseline

Seed for Phase 0 of `vault/ProjectAria/Planning/WEB_UI_RESPONSIVE_REBUILD_20260817.md`.

- `measure.mjs` — headless-Chromium (Playwright 1.62, cached at ~/.npm/_npx/…/playwright + ~/.cache/ms-playwright/chromium-1234)
  audit of every route × {375, 390, 768, 1280} × light/dark: horizontal overflow px, tap targets < 44 px, text < 12 px,
  DOM nodes, API calls on load, screenshots. `node measure.mjs http://localhost:3000` → `audit.json` + `shots/`.
- `probe.mjs` — lists the leaf elements past `innerWidth` on /cockpit, /operate, /dashboard.
- `audit.json` / `audit-summary.md` — the 2026-08-17 baseline the plan's §1 is built from.

Phase 0 turns these into `ui/e2e/*.spec.ts` with fixture-mocked API responses and a `known-failures.json` ratchet.
Screenshots (46 MB) were not kept here; the above-the-fold crops are in the vault next to the plan.
