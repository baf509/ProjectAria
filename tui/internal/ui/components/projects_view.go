package components

import (
	"fmt"
	"strings"

	"github.com/ben/aria-tui/internal/api"
	"github.com/ben/aria-tui/internal/ui/styles"
	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

// ProjectsView is the Coherence C4 Project Switcher: every project as one row,
// ranked by attention (server-side sort -- most-needs-attention first), with
// the shared "focused" project marked.
type ProjectsView struct {
	Viewport viewport.Model
	Width    int
	Height   int
	Focused  bool

	Projects      []api.ProjectOverviewRow
	ActiveProject string
	UnackedTotal  int

	Cursor int
}

func NewProjectsView() *ProjectsView {
	return &ProjectsView{Viewport: viewport.New(80, 20)}
}

func (pv *ProjectsView) SetSize(w, h int) {
	pv.Width = w
	pv.Height = h
	pv.Viewport.Width = w - 4
	pv.Viewport.Height = h - 6
	if pv.Viewport.Height < 1 {
		pv.Viewport.Height = 1
	}
}

func (pv *ProjectsView) Focus() { pv.Focused = true }
func (pv *ProjectsView) Blur()  { pv.Focused = false }

func (pv *ProjectsView) SetData(overview api.ProjectsOverview) {
	pv.Projects = overview.Projects
	pv.ActiveProject = overview.ActiveProject
	pv.UnackedTotal = overview.UnackedAlertsTotal
	pv.clampCursor()
	pv.refreshContent()
}

func (pv *ProjectsView) clampCursor() {
	if pv.Cursor < 0 {
		pv.Cursor = 0
	}
	if pv.Cursor >= len(pv.Projects) {
		pv.Cursor = len(pv.Projects) - 1 // -1 when there are no projects
	}
}

// MoveCursor moves the project selection by delta (clamped).
func (pv *ProjectsView) MoveCursor(delta int) {
	pv.Cursor += delta
	pv.clampCursor()
	pv.refreshContent()
}

// SelectedProject returns the currently selected project row, or nil when
// there are none.
func (pv *ProjectsView) SelectedProject() *api.ProjectOverviewRow {
	if pv.Cursor < 0 || pv.Cursor >= len(pv.Projects) {
		return nil
	}
	return &pv.Projects[pv.Cursor]
}

func (pv *ProjectsView) Update(msg tea.Msg) (*ProjectsView, tea.Cmd) {
	var cmd tea.Cmd
	pv.Viewport, cmd = pv.Viewport.Update(msg)
	return pv, cmd
}

// attnCell renders a right-aligned count, styled hot when non-zero. Padding
// happens BEFORE styling so the ANSI codes don't break column alignment.
func attnCell(n, width int, hot lipgloss.Style) string {
	cell := fmt.Sprintf("%*d", width, n)
	if n > 0 {
		return hot.Render(cell)
	}
	return lipgloss.NewStyle().Foreground(styles.Muted).Render(cell)
}

func (pv *ProjectsView) refreshContent() {
	if pv.Width < 10 {
		return
	}

	cw := pv.Width - 8
	var b strings.Builder

	// Table header. As with FleetView (see the long comment there, regression-
	// tested in fleet_view_test.go): every trailing "\n" must stay OUTSIDE the
	// lipgloss Render() call, or the bubbles Viewport desyncs by one line and
	// swallows the content line that follows.
	header := fmt.Sprintf("  %-26s %-8s %4s %5s %5s %6s %4s %6s",
		"NAME", "ACT", "BLK", "GATE", "ALRT", "STALE", "RUN", "SCORE")
	b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render(header) + "\n")
	b.WriteString(lipgloss.NewStyle().Foreground(styles.BorderColor).Render(
		"  "+strings.Repeat("─", cw-4)) + "\n")

	for i, p := range pv.Projects {
		name := p.Name
		if name == "" {
			name = p.Slug
		}
		// Mark the shared server-side focus. The star is prepended after
		// truncation so a long name can't push it out of the column.
		marker := "  "
		if p.Slug != "" && p.Slug == pv.ActiveProject {
			marker = "★ "
		}
		nameCell := fmt.Sprintf("%-26s", marker+truncate(name, 24))
		if p.Slug != "" && p.Slug == pv.ActiveProject {
			nameCell = lipgloss.NewStyle().Foreground(styles.Accent).Bold(true).Render(nameCell)
		} else {
			nameCell = lipgloss.NewStyle().Foreground(styles.Text).Render(nameCell)
		}

		act := p.ActivityStatus
		actCell := fmt.Sprintf("%-8s", truncate(act, 8))
		if act == "active" {
			actCell = lipgloss.NewStyle().Foreground(styles.Secondary).Render(actCell)
		} else {
			actCell = lipgloss.NewStyle().Foreground(styles.Muted).Render(actCell)
		}

		row := nameCell + " " + actCell + " " +
			attnCell(p.Attention.BlockedShells, 4, styles.VitalBad) + " " +
			attnCell(p.Attention.GateFailedSessions, 5, styles.VitalBad) + " " +
			attnCell(p.Attention.UnackedAlerts, 5, styles.VitalWarn) + " " +
			attnCell(p.Attention.StaleTasks, 6, styles.VitalWarn) + " " +
			attnCell(p.Attention.RunningSessions, 4, styles.VitalGood) + " " +
			fmt.Sprintf("%6d", p.AttentionScore)

		prefix := "  "
		if i == pv.Cursor {
			prefix = lipgloss.NewStyle().Foreground(styles.Accent).Bold(true).Render("▸") + " "
		}
		b.WriteString(prefix + row + "\n")
	}

	if len(pv.Projects) == 0 {
		b.WriteString("\n" + lipgloss.NewStyle().Foreground(styles.Muted).Render("  No projects") + "\n")
	}

	pv.Viewport.SetContent(b.String())
}

func (pv *ProjectsView) View() string {
	if pv.Width < 10 || pv.Height < 5 {
		return ""
	}

	title := fmt.Sprintf("Projects (%d)", len(pv.Projects))
	if pv.ActiveProject != "" {
		title += " — focus: " + pv.ActiveProject
	}
	if pv.UnackedTotal > 0 {
		title += fmt.Sprintf(" — %d unacked alerts", pv.UnackedTotal)
	}
	header := styles.TitleStyle.Render(title)
	vpView := pv.Viewport.View()
	footer := lipgloss.NewStyle().Foreground(styles.Muted).Render(
		"  ↑↓: select │ ⏎: cockpit │ f: focus │ r: refresh │ Esc: back")

	content := lipgloss.JoinVertical(lipgloss.Left, header, "", vpView, footer)

	border := styles.PaneBorder
	if pv.Focused {
		border = styles.PaneBorderActive
	}
	return border.Width(pv.Width - 2).Height(pv.Height - 2).Render(content)
}
