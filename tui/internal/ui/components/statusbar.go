package components

import (
	"fmt"

	"github.com/ben/aria-tui/internal/ui/styles"
	"github.com/charmbracelet/lipgloss"
)

type StatusBar struct {
	Width      int
	Connected  bool
	Version    string
	AgentName  string
	Backend    string
	Model      string
	Pane       string // "sidebar", "chat", "session", "vitals"
	Screen     string // "dashboard", "chat", "session"
	ActiveSess int
	TokensIn   int
	TokensOut  int
}

func (s *StatusBar) View() string {
	if s.Width < 10 {
		return ""
	}

	// Left side: connection + screen + agent info
	connIcon := "●"
	connStyle := lipgloss.NewStyle().Foreground(styles.Secondary)
	if !s.Connected {
		connStyle = lipgloss.NewStyle().Foreground(styles.Danger)
	}

	left := connStyle.Render(connIcon) + " "

	// Current screen indicator
	screenLabel := s.Screen
	if screenLabel == "" {
		screenLabel = "dashboard"
	}
	left += lipgloss.NewStyle().Foreground(styles.Primary).Bold(true).Render(screenLabel) + " "

	if s.AgentName != "" {
		left += styles.StatusKey.Render(s.AgentName)
		if s.Backend != "" {
			left += " " + lipgloss.NewStyle().Foreground(styles.Muted).Render(
				fmt.Sprintf("(%s/%s)", s.Backend, s.Model))
		}
	} else {
		left += lipgloss.NewStyle().Foreground(styles.Text).Render("ARIA")
		if s.Version != "" {
			left += " " + lipgloss.NewStyle().Foreground(styles.Muted).Render("v"+s.Version)
		}
	}

	// Middle: quick stats
	mid := ""
	if s.ActiveSess > 0 {
		mid += lipgloss.NewStyle().Foreground(styles.Secondary).Render(
			fmt.Sprintf(" ◉ %d sessions", s.ActiveSess))
	}
	if s.TokensIn > 0 || s.TokensOut > 0 {
		mid += lipgloss.NewStyle().Foreground(styles.Muted).Render(
			fmt.Sprintf(" │ ↓%s ↑%s", formatK(s.TokensIn), formatK(s.TokensOut)))
	}

	// Right side: help hints
	right := s.helpHints()

	// Layout
	gap := s.Width - lipgloss.Width(left) - lipgloss.Width(mid) - lipgloss.Width(right) - 2
	if gap < 0 {
		gap = 0
		mid = "" // drop middle if too narrow
		gap = s.Width - lipgloss.Width(left) - lipgloss.Width(right) - 2
		if gap < 0 {
			gap = 0
		}
	}

	bar := left + mid + lipgloss.NewStyle().Width(gap).Render("") + right
	return styles.StatusBar.Width(s.Width).Render(bar)
}

func (s *StatusBar) helpHints() string {
	switch s.Pane {
	case "chat":
		return helpItem("⏎", "send") + " " + helpItem("tab", "sidebar") + " " +
			helpItem("esc", "back") + " " + helpItem("ctrl+c", "quit")
	case "session":
		return helpItem("⏎", "input") + " " + helpItem("s", "stop") + " " +
			helpItem("r", "refresh") + " " + helpItem("esc", "back")
	case "memory":
		return helpItem("⏎", "search") + " " + helpItem("ctrl+l", "clear") + " " +
			helpItem("esc", "back")
	case "usage", "tools", "observations":
		return helpItem("r", "refresh") + " " + helpItem("esc", "back") + " " +
			helpItem("q", "quit")
	default: // sidebar
		return helpItem("↑↓", "nav") + " " + helpItem("⏎", "open") + " " +
			helpItem("c", "chat") + " " + helpItem("m", "mem") + " " +
			helpItem("u", "usage") + " " + helpItem("t", "tools") + " " +
			helpItem("o", "obs") + " " + helpItem("q", "quit")
	}
}

func helpItem(key, desc string) string {
	return styles.HelpKey.Render(key) + " " + styles.HelpDesc.Render(desc)
}

func formatK(n int) string {
	if n >= 1_000_000 {
		return fmt.Sprintf("%.1fM", float64(n)/1_000_000)
	}
	if n >= 1_000 {
		return fmt.Sprintf("%.1fK", float64(n)/1_000)
	}
	return fmt.Sprintf("%d", n)
}
