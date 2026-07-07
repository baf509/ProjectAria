package main

import (
	"bufio"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/ben/aria-tui/internal/api"
	"github.com/ben/aria-tui/internal/ui"
	tea "github.com/charmbracelet/bubbletea"
)

// loadKV reads key=value pairs from a file into a map (blank lines and #
// comments ignored). A missing/unreadable file yields an empty map. Used for
// both the .env fallback and the host-profiles file.
func loadKV(path string) map[string]string {
	m := make(map[string]string)
	if path == "" {
		return m
	}
	f, err := os.Open(path)
	if err != nil {
		return m
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		if k, v, ok := strings.Cut(line, "="); ok {
			m[strings.TrimSpace(k)] = strings.TrimSpace(v)
		}
	}
	return m
}

func firstExisting(paths ...string) string {
	for _, p := range paths {
		if p == "" {
			continue
		}
		if _, err := os.Stat(p); err == nil {
			return p
		}
	}
	return ""
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if v != "" {
			return v
		}
	}
	return ""
}

func envOr(key, fallback string, dotenv map[string]string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	if v, ok := dotenv[key]; ok && v != "" {
		return v
	}
	return fallback
}

func looksLikeURL(s string) bool {
	return strings.HasPrefix(s, "http://") || strings.HasPrefix(s, "https://")
}

// resolveTarget picks the API base URL + key. Precedence:
//  1. --host flag (a URL, a bare host[:port], or a profile name in the hosts file)
//  2. ARIA_API_URL env / .env
//  3. the hosts file's `default` profile
//  4. built-in http://localhost:8200
//
// The key follows its URL's source, but an explicit --api-key / ARIA_API_KEY
// always wins.
func resolveTarget(hostFlag, keyFlag string, dotenv, hosts map[string]string) (string, string) {
	envKey := firstNonEmpty(keyFlag, os.Getenv("ARIA_API_KEY"), os.Getenv("API_KEY"))

	if hostFlag != "" {
		switch {
		case looksLikeURL(hostFlag):
			return hostFlag, firstNonEmpty(envKey, dotenv["ARIA_API_KEY"], dotenv["API_KEY"])
		case hosts[hostFlag+".url"] != "":
			return hosts[hostFlag+".url"], firstNonEmpty(envKey, hosts[hostFlag+".key"])
		case strings.ContainsAny(hostFlag, ".:"):
			// Bare host[:port] — assume plain HTTP over the tailnet.
			return "http://" + hostFlag, firstNonEmpty(envKey, dotenv["ARIA_API_KEY"], dotenv["API_KEY"])
		default:
			fmt.Fprintf(os.Stderr, "aria-tui: unknown host profile %q; falling back to defaults\n", hostFlag)
		}
	}

	// Explicit env/.env URL.
	if u := envOr("ARIA_API_URL", "", dotenv); u != "" {
		return u, firstNonEmpty(envKey, dotenv["ARIA_API_KEY"], dotenv["API_KEY"])
	}

	// Default profile from the hosts file.
	if def := hosts["default"]; def != "" && hosts[def+".url"] != "" {
		return hosts[def+".url"], firstNonEmpty(envKey, hosts[def+".key"], dotenv["ARIA_API_KEY"], dotenv["API_KEY"])
	}

	return "http://localhost:8200", firstNonEmpty(envKey, dotenv["ARIA_API_KEY"], dotenv["API_KEY"])
}

func main() {
	var hostFlag, keyFlag string
	flag.StringVar(&hostFlag, "host", "", "Host profile name, bare host[:port], or full API URL (e.g. corsair, corsair-ai:8200, http://corsair-ai:8200)")
	flag.StringVar(&keyFlag, "api-key", "", "API key (overrides env and profile)")
	flag.Parse()

	home, _ := os.UserHomeDir()
	cfgDir := filepath.Join(home, ".config", "aria")

	// .env fallback chain: $ARIA_ENV → ~/.config/aria/env → the repo checkout.
	// (Real env vars still take precedence inside resolveTarget.)
	dotenv := loadKV(firstExisting(
		os.Getenv("ARIA_ENV"),
		filepath.Join(cfgDir, "env"),
		filepath.Join(home, "Development", "ProjectAria", ".env"),
	))

	// Host profiles: $ARIA_HOSTS → ~/.config/aria/hosts.
	hosts := loadKV(firstExisting(
		os.Getenv("ARIA_HOSTS"),
		filepath.Join(cfgDir, "hosts"),
	))

	baseURL, apiKey := resolveTarget(hostFlag, keyFlag, dotenv, hosts)
	client := api.NewClient(baseURL, apiKey)

	// Note: mouse motion reporting is intentionally NOT enabled. Nothing in the
	// UI consumes mouse events, and over a remote (SSH/mosh) link the flood of
	// motion escape-sequences queues ahead of keystrokes, making key presses
	// feel dropped/laggy. Keyboard-only keeps input responsive.
	p := tea.NewProgram(
		ui.NewModel(client),
		tea.WithAltScreen(),
	)

	if _, err := p.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
}
