package components

import (
	"fmt"
	"strings"

	"github.com/ben/aria-tui/internal/api"
	"github.com/ben/aria-tui/internal/ui/styles"
	"github.com/charmbracelet/bubbles/textinput"
	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

// MemoryBrowser provides search and browsing of ARIA's long-term memories.
type MemoryBrowser struct {
	Search   textinput.Model
	Viewport viewport.Model
	Memories []api.Memory
	Width    int
	Height   int
	Focused  bool
	Cursor   int
}

func NewMemoryBrowser() *MemoryBrowser {
	ti := textinput.New()
	ti.Placeholder = "Search memories..."
	ti.CharLimit = 256

	vp := viewport.New(80, 20)

	return &MemoryBrowser{
		Search:   ti,
		Viewport: vp,
		Cursor:   -1,
	}
}

func (mb *MemoryBrowser) SetSize(w, h int) {
	mb.Width = w
	mb.Height = h

	contentWidth := w - 4
	mb.Search.Width = contentWidth - 2
	mb.Viewport.Width = contentWidth
	mb.Viewport.Height = h - 8 // header + search + footer
	if mb.Viewport.Height < 1 {
		mb.Viewport.Height = 1
	}
}

func (mb *MemoryBrowser) Focus() {
	mb.Focused = true
	mb.Search.Focus()
}

func (mb *MemoryBrowser) Blur() {
	mb.Focused = false
	mb.Search.Blur()
}

func (mb *MemoryBrowser) SetMemories(memories []api.Memory) {
	mb.Memories = memories
	mb.Cursor = -1
	mb.refreshContent()
}

func (mb *MemoryBrowser) GetQuery() string {
	return mb.Search.Value()
}

func (mb *MemoryBrowser) ClearSearch() {
	mb.Search.Reset()
}

func (mb *MemoryBrowser) Update(msg tea.Msg) (*MemoryBrowser, tea.Cmd) {
	var cmds []tea.Cmd

	if mb.Focused {
		var cmd tea.Cmd
		mb.Search, cmd = mb.Search.Update(msg)
		cmds = append(cmds, cmd)
	}

	var cmd tea.Cmd
	mb.Viewport, cmd = mb.Viewport.Update(msg)
	cmds = append(cmds, cmd)

	return mb, tea.Batch(cmds...)
}

func (mb *MemoryBrowser) refreshContent() {
	if mb.Width < 5 {
		return
	}

	contentWidth := mb.Width - 6
	var b strings.Builder

	if len(mb.Memories) == 0 {
		b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render("\n  No memories found."))
		mb.Viewport.SetContent(b.String())
		return
	}

	b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render(
		fmt.Sprintf("  %d memories\n\n", len(mb.Memories))))

	for i, mem := range mb.Memories {
		selected := i == mb.Cursor

		// Memory ID (truncated) + type badge
		idStr := mem.ID
		if len(idStr) > 8 {
			idStr = idStr[:8]
		}
		typeBadge := memoryTypeBadge(mem.ContentType)

		// Categories
		cats := ""
		if len(mem.Categories) > 0 {
			cats = " " + lipgloss.NewStyle().Foreground(styles.Accent).Render(
				"["+strings.Join(mem.Categories, ", ")+"]")
		}

		// Confidence
		confStr := ""
		if mem.Confidence > 0 {
			confStyle := styles.VitalGood
			if mem.Confidence < 0.5 {
				confStyle = styles.VitalBad
			} else if mem.Confidence < 0.8 {
				confStyle = styles.VitalWarn
			}
			confStr = " " + confStyle.Render(fmt.Sprintf("%.0f%%", mem.Confidence*100))
		}

		// Header line
		header := fmt.Sprintf("  %s %s%s%s",
			lipgloss.NewStyle().Foreground(styles.Muted).Render(idStr),
			typeBadge, cats, confStr)

		// Content preview (max 3 lines)
		preview := mem.Content
		lines := strings.Split(preview, "\n")
		if len(lines) > 3 {
			lines = lines[:3]
			lines = append(lines, "...")
		}
		preview = strings.Join(lines, "\n")
		if len([]rune(preview)) > contentWidth*3 {
			preview = string([]rune(preview)[:contentWidth*3]) + "…"
		}

		contentStyle := lipgloss.NewStyle().Foreground(styles.Text).Width(contentWidth - 4)
		if selected {
			contentStyle = contentStyle.Foreground(styles.Primary).Bold(true)
		}

		b.WriteString(header)
		b.WriteString("\n")
		b.WriteString("    " + contentStyle.Render(preview))
		b.WriteString("\n")

		// Separator
		b.WriteString(lipgloss.NewStyle().Foreground(styles.BorderColor).Render(
			"  " + strings.Repeat("─", contentWidth-4)))
		b.WriteString("\n")
	}

	mb.Viewport.SetContent(b.String())
}

func (mb *MemoryBrowser) View() string {
	if mb.Width < 10 || mb.Height < 5 {
		return ""
	}

	header := styles.TitleStyle.Render("Memory Browser")
	searchView := mb.Search.View()
	vpView := mb.Viewport.View()

	footer := lipgloss.NewStyle().Foreground(styles.Muted).Render(
		fmt.Sprintf("  %d memories │ Enter: search │ Esc: back", len(mb.Memories)))

	content := lipgloss.JoinVertical(lipgloss.Left,
		header,
		"",
		" "+searchView,
		"",
		vpView,
		footer,
	)

	border := styles.PaneBorder
	if mb.Focused {
		border = styles.PaneBorderActive
	}

	return border.Width(mb.Width - 2).Height(mb.Height - 2).Render(content)
}

func memoryTypeBadge(contentType string) string {
	switch contentType {
	case "fact":
		return lipgloss.NewStyle().Foreground(styles.Secondary).Render("[fact]")
	case "preference":
		return lipgloss.NewStyle().Foreground(styles.Accent).Render("[pref]")
	case "experience":
		return lipgloss.NewStyle().Foreground(styles.Info).Render("[exp]")
	case "relationship":
		return lipgloss.NewStyle().Foreground(styles.Primary).Render("[rel]")
	default:
		return lipgloss.NewStyle().Foreground(styles.Muted).Render("[" + contentType + "]")
	}
}
