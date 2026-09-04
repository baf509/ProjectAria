# ARIA — repo tasks.
#
# `make ui-check` is the responsive gate: it is what stops the phone layout from
# regressing, and it is registered as this project's check_command so an
# ARIA-spawned coding session cannot merge an overflow.

SHELL := /bin/bash
UI := ui
SHA := $(shell git rev-parse --short HEAD)
BRANCH := $(shell git rev-parse --abbrev-ref HEAD)

.PHONY: help ui-check ui-build ui-deploy ui-serve ui-gate ui-https api-test check

help:
	@echo "ui-check   typecheck + lint + build + responsive gate"
	@echo "ui-build   production build (no deploy)"
	@echo "ui-deploy  disabled until the atomic Mac launchd deploy is implemented"
	@echo "ui-serve   serve the production build on :3100 for the gate (STOP IT WHEN DONE)"
	@echo "ui-gate    run the responsive gate against :3100 (server must be up)"
	@echo "ui-https   print the human-only Mac Tailscale publication command"
	@echo "api-test   python test suite"

ui-build:
	cd $(UI) && BUILD_SHA=$(SHA) BUILD_BRANCH=$(BRANCH) npm run build

ui-serve:
	cd $(UI) && ./e2e/serve.sh

ui-gate:
	cd $(UI) && npm run gate

# Everything the gate covers, in the order that fails fastest.
ui-check:
	cd $(UI) && npm run typecheck
	cd $(UI) && node scripts/ui-lint-classes.mjs
	cd $(UI) && BUILD_SHA=$(SHA) npm run build
	@echo "--- starting the built UI on :3100 for the responsive gate ---"
	@cd $(UI) && (nohup ./e2e/serve.sh > /tmp/aria-ui-gate.log 2>&1 & echo $$! > /tmp/aria-ui-gate.pid); \
		sleep 8; \
		npm run gate; status=$$?; \
		kill $$(cat /tmp/aria-ui-gate.pid) 2>/dev/null || true; \
		exit $$status

# Production is native launchd on the Mac. The old implementation below built a
# Corsair Docker container and could deploy the wrong architecture. Fail closed
# until a tested atomic source -> /Users/ben/Services/apps/ProjectAria procedure
# is added; docs/ops/WEB_UI.md records the live layout and acceptance checks.
ui-deploy:
	@echo "ERROR: production is the Mac launchd service tree, not Docker." >&2
	@echo "Use the reviewed Mac deployment procedure and verify the live build; see docs/ops/WEB_UI.md." >&2
	@exit 2

# Agents may not change host network/Tailscale settings. Print the exact Mac
# command for Ben instead of silently altering whichever host ran make.
ui-https:
	@echo "Human-only on the MacBook Pro:"
	@echo "/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg --https=443 http://127.0.0.1:3000"

api-test:
	cd api && python3 -m pytest tests/ -q

check: ui-check
