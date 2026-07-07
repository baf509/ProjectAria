# ARIA TUI — build targets
#
# `go` may not be on PATH (e.g. an SDK-managed toolchain); override with:
#   make build GO=/home/ben/go-sdk/go/bin/go
GO      ?= go
BIN     ?= aria-tui
PREFIX  ?= $(HOME)/.local/bin

.PHONY: build build-darwin build-linux build-all install vet clean

# Native build for the current machine (default).
build:
	$(GO) build -o $(BIN) .

# Apple-Silicon macOS build — the cross-machine cockpit binary. Pure Go, no cgo.
build-darwin:
	GOOS=darwin GOARCH=arm64 CGO_ENABLED=0 $(GO) build -o $(BIN)-darwin-arm64 .

# Linux amd64 build (for corsair-ai / servers).
build-linux:
	GOOS=linux GOARCH=amd64 CGO_ENABLED=0 $(GO) build -o $(BIN)-linux-amd64 .

build-all: build-darwin build-linux

# Install the native binary onto PATH.
install: build
	mkdir -p $(PREFIX)
	install -m 0755 $(BIN) $(PREFIX)/$(BIN)

vet:
	$(GO) vet ./...

clean:
	rm -f $(BIN) $(BIN)-darwin-arm64 $(BIN)-linux-amd64
