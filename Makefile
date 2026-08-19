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
	@echo "ui-deploy  build the image, restart the container, verify the running sha == HEAD"
	@echo "ui-serve   serve the production build on :3100 for the gate (STOP IT WHEN DONE)"
	@echo "ui-gate    run the responsive gate against :3100 (server must be up)"
	@echo "ui-https   expose the UI over https on the tailnet"
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
		npm --prefix $(UI) run gate; status=$$?; \
		kill $$(cat /tmp/aria-ui-gate.pid) 2>/dev/null || true; \
		exit $$status

# A deploy that cannot silently serve a week-old image: the running build must
# report the sha we just built.
ui-deploy:
	BUILD_SHA=$(SHA) BUILD_BRANCH=$(BRANCH) docker compose build ui
	BUILD_SHA=$(SHA) BUILD_BRANCH=$(BRANCH) docker compose up -d ui
	@echo "waiting for the container to answer..."
	@for i in $$(seq 1 30); do \
		running=$$(curl -fsS http://127.0.0.1:3000/api/build 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("sha",""))' 2>/dev/null); \
		if [ "$$running" = "$(SHA)" ]; then echo "deployed $(SHA)"; exit 0; fi; \
		sleep 2; \
	done; \
	echo "DEPLOY MISMATCH: running=$$running expected=$(SHA)"; exit 1

# 443, not 8444: the dashboard is the front door of this host, so its URL has
# no port in it. 8443/8444 were removed 2026-08-19 -- they proxied a Hermes
# WebUI that has been disabled since 2026-08-13, and a duplicate of it, which
# is why finding the dashboard used to mean guessing a port.
ui-https:
	tailscale serve --bg --https=443 http://127.0.0.1:3000
	@echo "UI: https://$$(tailscale status --json | python3 -c 'import sys,json;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"

api-test:
	cd api && python3 -m pytest tests/ -q

check: ui-check
