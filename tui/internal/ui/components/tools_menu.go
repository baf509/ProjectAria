package components

import (
	"strings"

	"github.com/ben/aria-tui/internal/ui/styles"
	"github.com/charmbracelet/lipgloss"
)

// MenuItem defines a navigation entry in the tools menu.
type MenuItem struct {
	Key   string
	Label string
	Desc  string
}

// ToolsMenu is the quick-nav panel shown on the dashboard.
type ToolsMenu struct {
	Width  int
	Height int
	Items  []MenuItem
}

func NewToolsMenu() *ToolsMenu {
	return &ToolsMenu{
		Items: []MenuItem{
			{Key: "c", Label: "ARIA Chat", Desc: "Talk to ARIA"},
			{Key: "m", Label: "Memories", Desc: "Search & browse memories"},
			{Key: "u", Label: "Usage", Desc: "Token usage & LLM status"},
			{Key: "t", Label: "Tools", Desc: "Registered tools & MCP"},
			{Key: "o", Label: "Observations", Desc: "Awareness sensor data"},
			{Key: "s", Label: "Sessions", Desc: "Coding sessions"},
		},
	}
}

func (tm *ToolsMenu) SetSize(w, h int) {
	tm.Width = w
	tm.Height = h
}

// RenderItems returns the menu items as raw content without border wrapping.
func (tm *ToolsMenu) RenderItems(maxLines int) string {
	var b strings.Builder
	for i, item := range tm.Items {
		if i >= maxLines {
			break
		}
		key := styles.HelpKey.Render("[" + item.Key + "]")
		label := lipgloss.NewStyle().Foreground(styles.Text).Render(" " + item.Label)
		desc := lipgloss.NewStyle().Foreground(styles.Muted).Render("  " + item.Desc)
		b.WriteString(key + label + desc + "\n")
	}
	return b.String()
}

func (tm *ToolsMenu) View() string {
	if tm.Width < 10 || tm.Height < 3 {
		return ""
	}

	var b strings.Builder
	b.WriteString(styles.SectionTitle.Render("Quick Nav"))
	b.WriteString("\n")
	b.WriteString(lipgloss.NewStyle().Foreground(styles.BorderColor).Render(
		strings.Repeat("─", tm.Width-6)))
	b.WriteString("\n")

	for _, item := range tm.Items {
		if b.Len() > tm.Height*tm.Width {
			break // don't overflow
		}
		key := styles.HelpKey.Render("[" + item.Key + "]")
		label := lipgloss.NewStyle().Foreground(styles.Text).Render(" " + item.Label)
		b.WriteString(key + label + "\n")
	}

	return b.String()
}
