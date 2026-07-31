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

// HistoryView browses EVERY watched shell regardless of status -- unlike
// Fleet (active/idle only) or the sidebar (coding_sessions only), this is
// the one place the full shell history (hundreds of stopped sessions going
// back to whenever capture started) is actually reachable, not just present
// in the database. Filtering is client-side text match against
// name/short_name/project_dir -- GET /shells returns everything unbounded
// already, so there's no server round-trip per keystroke to avoid.
type HistoryView struct {
	Viewport viewport.Model
	Filter   textinput.Model

	All      []api.ShellRecord // unfiltered, as loaded
	Shells   []api.ShellRecord // filtered view
	Cursor   int
	Offset   int
	Width    int
	Height   int
	Focused  bool
	Filtered bool
	// Editing gates whether keys route to the filter textinput vs. list
	// navigation -- matches DBBrowser's "/"-to-edit convention rather than
	// an always-focused search box, since History also needs plain
	// up/down/enter for row selection and a permanently focused textinput
	// would fight over which keys go where.
	Editing bool
}

func NewHistoryView() *HistoryView {
	ti := textinput.New()
	ti.Placeholder = "Filter by name or path..."
	vp := viewport.New(80, 20)
	return &HistoryView{Viewport: vp, Filter: ti}
}

func (hv *HistoryView) SetSize(w, h int) {
	hv.Width = w
	hv.Height = h
	hv.Viewport.Width = w - 4
	// Header + filter box + column header + separator + footer.
	hv.Viewport.Height = h - 8
	if hv.Viewport.Height < 1 {
		hv.Viewport.Height = 1
	}
}

func (hv *HistoryView) Update(msg tea.Msg) (*HistoryView, tea.Cmd) {
	var cmd tea.Cmd
	hv.Viewport, cmd = hv.Viewport.Update(msg)
	return hv, cmd
}

func (hv *HistoryView) Focus() { hv.Focused = true }
func (hv *HistoryView) Blur()  { hv.Focused = false; hv.Editing = false; hv.Filter.Blur() }

func (hv *HistoryView) EnterFilterEdit() {
	hv.Editing = true
	hv.Filter.Focus()
}

func (hv *HistoryView) ExitFilterEdit() {
	hv.Editing = false
	hv.Filter.Blur()
}

func (hv *HistoryView) SetShells(shells []api.ShellRecord) {
	hv.All = shells
	hv.applyFilter()
}

func (hv *HistoryView) applyFilter() {
	q := strings.ToLower(strings.TrimSpace(hv.Filter.Value()))
	if q == "" {
		hv.Shells = hv.All
		hv.Filtered = false
	} else {
		hv.Filtered = true
		hv.Shells = hv.Shells[:0]
		for _, s := range hv.All {
			if strings.Contains(strings.ToLower(s.Name), q) ||
				strings.Contains(strings.ToLower(s.ProjectDir), q) {
				hv.Shells = append(hv.Shells, s)
			}
		}
	}
	hv.clampCursor()
	hv.refreshContent()
}

func (hv *HistoryView) clampCursor() {
	if hv.Cursor < 0 {
		hv.Cursor = 0
	}
	if hv.Cursor >= len(hv.Shells) {
		hv.Cursor = len(hv.Shells) - 1
	}
}

func (hv *HistoryView) MoveCursor(delta int) {
	hv.Cursor += delta
	hv.clampCursor()
	visible := hv.Viewport.Height
	if hv.Cursor < hv.Offset {
		hv.Offset = hv.Cursor
	} else if hv.Cursor >= hv.Offset+visible {
		hv.Offset = hv.Cursor - visible + 1
	}
	hv.refreshContent()
}

// Selected returns the currently highlighted shell, or nil when the list is
// empty (including "filtered down to zero results").
func (hv *HistoryView) Selected() *api.ShellRecord {
	if hv.Cursor < 0 || hv.Cursor >= len(hv.Shells) {
		return nil
	}
	return &hv.Shells[hv.Cursor]
}

// UpdateFilterInput forwards a key to the filter textinput and re-filters.
// Returns whether the key was consumed as text-input (vs. a navigation key
// the caller should handle itself).
func (hv *HistoryView) UpdateFilterInput(msg tea.Msg) tea.Cmd {
	var cmd tea.Cmd
	hv.Filter, cmd = hv.Filter.Update(msg)
	hv.applyFilter()
	return cmd
}

func (hv *HistoryView) refreshContent() {
	if hv.Width < 10 {
		return
	}
	cw := hv.Width - 8
	var b strings.Builder

	headerFmt := "  %-20s %-10s %-9s %10s %8s %10s\n"
	b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render(
		fmt.Sprintf(strings.TrimSuffix(headerFmt, "\n"), "NAME", "HOST", "STATUS", "CREATED", "LINES", "LAST ACTIVE")) + "\n")
	b.WriteString(lipgloss.NewStyle().Foreground(styles.BorderColor).Render(
		"  "+strings.Repeat("─", min(cw-4, 80))) + "\n")

	if len(hv.Shells) == 0 {
		msg := "  No shells recorded yet"
		if hv.Filtered {
			msg = "  No shells match this filter"
		}
		b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render(msg) + "\n")
		hv.Viewport.SetContent(b.String())
		return
	}

	for i, s := range hv.Shells {
		name := s.ShortName
		if name == "" {
			name = s.Name
		}
		row := fmt.Sprintf(headerFmt,
			truncate(name, 20),
			truncate(s.Host, 10),
			truncate(s.Status, 9),
			relativeTime(s.CreatedAt),
			fmt.Sprintf("%d", s.LineCount),
			relativeTime(s.LastActivityAt),
		)
		if i == hv.Cursor {
			b.WriteString(lipgloss.NewStyle().Foreground(styles.Accent).Bold(true).Render("▸") + row[1:])
		} else {
			b.WriteString(row)
		}
	}

	hv.Viewport.SetContent(b.String())
	// Keep the viewport's own scroll following the cursor -- SetContent
	// resets YOffset, so this must run after every refresh, not just on
	// MoveCursor (a filter change can also move the effective cursor row).
	visible := hv.Viewport.Height
	if hv.Cursor >= visible {
		hv.Viewport.YOffset = hv.Cursor - visible + 1
	} else {
		hv.Viewport.YOffset = 0
	}
}

func (hv *HistoryView) View() string {
	if hv.Width < 10 || hv.Height < 5 {
		return ""
	}

	header := styles.TitleStyle.Render(fmt.Sprintf("Shell History (%d/%d)", len(hv.Shells), len(hv.All)))
	filterLabel := styles.HelpKey.Render("  /") + styles.HelpDesc.Render(" filter: ")
	if hv.Editing {
		filterLabel += lipgloss.NewStyle().Foreground(styles.Accent).Render(hv.Filter.Value() + "_")
	} else if hv.Filter.Value() != "" {
		filterLabel += lipgloss.NewStyle().Foreground(styles.Text).Render(hv.Filter.Value())
	} else {
		filterLabel += lipgloss.NewStyle().Foreground(styles.Muted).Render("(none)")
	}
	vpView := hv.Viewport.View()
	footer := lipgloss.NewStyle().Foreground(styles.Muted).Render(
		"  ↑↓: select │ ⏎: view scrollback │ /: filter │ r: refresh │ Esc: back")

	content := lipgloss.JoinVertical(lipgloss.Left, header, filterLabel, "", vpView, footer)

	border := styles.PaneBorder
	if hv.Focused {
		border = styles.PaneBorderActive
	}
	return border.Width(hv.Width - 2).Height(hv.Height - 2).Render(content)
}
