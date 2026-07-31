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

// ToolsMenu is the quick-nav panel shown on the dashboard. When focused it acts
// as a selectable list (arrows move the cursor, Enter launches the entry); the
// Key of each item matches its dashboard hotkey so both paths open the same
// screen.
type ToolsMenu struct {
	Width  int
	Height int
	Items  []MenuItem
	Cursor int
}

func NewToolsMenu() *ToolsMenu {
	return &ToolsMenu{
		Items: []MenuItem{
			{Key: "c", Label: "ARIA Chat", Desc: "Talk to ARIA"},
			{Key: "f", Label: "Fleet", Desc: "Coding sessions & shells"},
			{Key: "m", Label: "Memories", Desc: "Search & browse memories"},
			{Key: "u", Label: "Usage", Desc: "Token usage & LLM status"},
			{Key: "t", Label: "Tools", Desc: "Registered tools & MCP"},
			{Key: "s", Label: "Search", Desc: "Search the agent"},
			{Key: "o", Label: "Observations", Desc: "Awareness sensor data"},
			{Key: "b", Label: "Database", Desc: "Browse collections"},
			{Key: "h", Label: "Health", Desc: "Service health"},
			{Key: "y", Label: "History", Desc: "Browse all shells (any status)"},
		},
	}
}

func (tm *ToolsMenu) SetSize(w, h int) {
	tm.Width = w
	tm.Height = h
}

// Up moves the selection up (clamped).
func (tm *ToolsMenu) Up() {
	if tm.Cursor > 0 {
		tm.Cursor--
	}
}

// Down moves the selection down (clamped).
func (tm *ToolsMenu) Down() {
	if tm.Cursor < len(tm.Items)-1 {
		tm.Cursor++
	}
}

// Selected returns the highlighted item (falls back to the first entry).
func (tm *ToolsMenu) Selected() MenuItem {
	if tm.Cursor < 0 || tm.Cursor >= len(tm.Items) {
		return tm.Items[0]
	}
	return tm.Items[tm.Cursor]
}

// RenderItems returns the menu items as raw content without border wrapping.
// When focused, the selected row is highlighted so the user can see which entry
// Enter will launch.
func (tm *ToolsMenu) RenderItems(maxLines int, focused bool) string {
	var b strings.Builder
	for i, item := range tm.Items {
		if i >= maxLines {
			break
		}
		if focused && i == tm.Cursor {
			line := "[" + item.Key + "] " + item.Label + "  " + item.Desc
			b.WriteString(styles.SidebarSelected.Render(line) + "\n")
			continue
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
