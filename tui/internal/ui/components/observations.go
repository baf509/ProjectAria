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

// ObservationsView shows awareness sensor observations.
type ObservationsView struct {
	Viewport     viewport.Model
	Width        int
	Height       int
	Focused      bool
	Observations []api.Observation
}

func NewObservationsView() *ObservationsView {
	vp := viewport.New(80, 20)
	return &ObservationsView{Viewport: vp}
}

func (ov *ObservationsView) SetSize(w, h int) {
	ov.Width = w
	ov.Height = h
	ov.Viewport.Width = w - 4
	ov.Viewport.Height = h - 6
	if ov.Viewport.Height < 1 {
		ov.Viewport.Height = 1
	}
}

func (ov *ObservationsView) Focus() { ov.Focused = true }
func (ov *ObservationsView) Blur()  { ov.Focused = false }

func (ov *ObservationsView) SetData(obs []api.Observation) {
	ov.Observations = obs
	ov.refreshContent()
}

func (ov *ObservationsView) Update(msg tea.Msg) (*ObservationsView, tea.Cmd) {
	var cmd tea.Cmd
	ov.Viewport, cmd = ov.Viewport.Update(msg)
	return ov, cmd
}

func (ov *ObservationsView) refreshContent() {
	if ov.Width < 10 {
		return
	}

	cw := ov.Width - 8
	var b strings.Builder

	if len(ov.Observations) == 0 {
		b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render("\n  No observations recorded yet.\n"))
		b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render("  Awareness sensors monitor git, filesystem, and system state.\n"))
		ov.Viewport.SetContent(b.String())
		return
	}

	// Group by category
	groups := make(map[string][]api.Observation)
	var order []string
	for _, o := range ov.Observations {
		if _, exists := groups[o.Category]; !exists {
			order = append(order, o.Category)
		}
		groups[o.Category] = append(groups[o.Category], o)
	}

	for _, cat := range order {
		obs := groups[cat]
		catLabel := categoryLabel(cat)
		b.WriteString(styles.SectionTitle.Render(fmt.Sprintf("  %s (%d)", catLabel, len(obs))))
		b.WriteString("\n\n")

		for _, o := range obs {
			sevIcon := observationSevIcon(o.Severity)
			sensor := lipgloss.NewStyle().Foreground(styles.Muted).Render("[" + o.Sensor + "]")
			ts := ""
			if !o.CreatedAt.IsZero() {
				ts = lipgloss.NewStyle().Foreground(styles.Muted).Render(
					o.CreatedAt.Format("15:04:05"))
			}

			summary := truncate(o.Summary, cw-20)
			b.WriteString(fmt.Sprintf("  %s %s %s %s\n",
				ts, sevIcon, sensor,
				lipgloss.NewStyle().Foreground(styles.Text).Render(summary)))
		}
		b.WriteString("\n")
	}

	ov.Viewport.SetContent(b.String())
}

func (ov *ObservationsView) View() string {
	if ov.Width < 10 || ov.Height < 5 {
		return ""
	}

	header := styles.TitleStyle.Render(fmt.Sprintf("Awareness (%d observations)", len(ov.Observations)))
	vpView := ov.Viewport.View()
	footer := lipgloss.NewStyle().Foreground(styles.Muted).Render(
		"  r: refresh │ Esc: back")

	content := lipgloss.JoinVertical(lipgloss.Left, header, "", vpView, footer)

	border := styles.PaneBorder
	if ov.Focused {
		border = styles.PaneBorderActive
	}

	return border.Width(ov.Width - 2).Height(ov.Height - 2).Render(content)
}

func categoryLabel(cat string) string {
	switch cat {
	case "git":
		return "Git Activity"
	case "filesystem":
		return "Filesystem"
	case "system":
		return "System"
	case "process":
		return "Processes"
	default:
		return strings.Title(cat)
	}
}

func observationSevIcon(sev string) string {
	switch sev {
	case "critical":
		return lipgloss.NewStyle().Foreground(styles.Danger).Bold(true).Render("▲")
	case "warning":
		return lipgloss.NewStyle().Foreground(styles.Accent).Render("▲")
	case "info":
		return lipgloss.NewStyle().Foreground(styles.Info).Render("●")
	default:
		return lipgloss.NewStyle().Foreground(styles.Muted).Render("○")
	}
}
