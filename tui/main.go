package main

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/ben/aria-tui/internal/api"
	"github.com/ben/aria-tui/internal/ui"
	tea "github.com/charmbracelet/bubbletea"
)

// loadDotEnv reads key=value pairs from a .env file into a map.
func loadDotEnv(path string) map[string]string {
	m := make(map[string]string)
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

func envOr(key, fallback string, dotenv map[string]string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	if v, ok := dotenv[key]; ok && v != "" {
		return v
	}
	return fallback
}

func main() {
	// Load .env from ProjectAria root (two levels up from tui/)
	home, _ := os.UserHomeDir()
	dotenv := loadDotEnv(filepath.Join(home, "Dev", "ProjectAria", ".env"))

	baseURL := envOr("ARIA_API_URL", "http://localhost:8000", dotenv)
	apiKey := envOr("ARIA_API_KEY", "", dotenv)
	if apiKey == "" {
		apiKey = envOr("API_KEY", "", dotenv)
	}

	client := api.NewClient(baseURL, apiKey)

	p := tea.NewProgram(
		ui.NewModel(client),
		tea.WithAltScreen(),
		tea.WithMouseCellMotion(),
	)

	if _, err := p.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
}
