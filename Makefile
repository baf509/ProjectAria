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
	@echo "ui-deploy  stage a Mac API/UI/node release; activation is explicit"
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

# Production uses a reviewed, manifest-pinned Mac release. Staging prints the
# explicit activation command boundary; it never restarts production itself.
ui-deploy:
	./scripts/aria-deploy-mac stage

# Agents may not change host network/Tailscale settings. Print the exact Mac
# command for Ben instead of silently altering whichever host ran make.
ui-https:
	@echo "Human-only on the MacBook Pro:"
	@echo "/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg --https=443 http://127.0.0.1:3000"

api-test:
	cd api && python3 -m pytest tests/ -q

check: ui-check
