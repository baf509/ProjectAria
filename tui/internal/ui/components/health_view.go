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

// HealthView displays the status of ARIA's backing services.
type HealthView struct {
	Viewport viewport.Model
	Width    int
	Height   int
	Focused  bool

	Health *api.ServicesHealth
}

func NewHealthView() *HealthView {
	vp := viewport.New(80, 20)
	return &HealthView{Viewport: vp}
}

func (hv *HealthView) SetSize(w, h int) {
	hv.Width = w
	hv.Height = h
	hv.Viewport.Width = w - 4
	hv.Viewport.Height = h - 6
	if hv.Viewport.Height < 1 {
		hv.Viewport.Height = 1
	}
}

func (hv *HealthView) Focus() { hv.Focused = true }
func (hv *HealthView) Blur()  { hv.Focused = false }

func (hv *HealthView) SetData(health *api.ServicesHealth) {
	hv.Health = health
	hv.refreshContent()
}

func (hv *HealthView) Update(msg tea.Msg) (*HealthView, tea.Cmd) {
	var cmd tea.Cmd
	hv.Viewport, cmd = hv.Viewport.Update(msg)
	return hv, cmd
}

func (hv *HealthView) refreshContent() {
	if hv.Width < 10 {
		return
	}

	cw := hv.Width - 8
	var b strings.Builder

	if hv.Health == nil {
		b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render("\n  No health data available\n"))
		hv.Viewport.SetContent(b.String())
		return
	}

	headerLabel := fmt.Sprintf("  Services — healthy %d/%d", hv.Health.Healthy, hv.Health.Total)
	b.WriteString(styles.SectionTitle.Render(headerLabel))
	b.WriteString("\n\n")

	b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render(
		fmt.Sprintf("  %-20s %-6s %8s  %s\n", "Service", "State", "Latency", "Detail")))
	b.WriteString(lipgloss.NewStyle().Foreground(styles.BorderColor).Render(
		"  " + strings.Repeat("─", cw-4) + "\n"))

	for _, s := range hv.Health.Services {
		var dot, state lipgloss.Style
		stateLabel := "DOWN"
		if s.OK {
			dot = lipgloss.NewStyle().Foreground(styles.Secondary)
			state = lipgloss.NewStyle().Foreground(styles.Secondary)
			stateLabel = "OK"
		} else {
			dot = lipgloss.NewStyle().Foreground(styles.Danger)
			state = lipgloss.NewStyle().Foreground(styles.Danger)
		}
		latency := fmt.Sprintf("%dms", s.LatencyMS)
		detail := lipgloss.NewStyle().Foreground(styles.SubText).Render(truncate(s.Detail, cw-40))
		b.WriteString(fmt.Sprintf("  %s %-18s %s %8s  %s\n",
			dot.Render("●"),
			truncate(s.Name, 18),
			state.Render(fmt.Sprintf("%-4s", stateLabel)),
			latency,
			detail))
	}

	hv.Viewport.SetContent(b.String())
}

func (hv *HealthView) View() string {
	if hv.Width < 10 || hv.Height < 5 {
		return ""
	}

	title := "Services Health"
	if hv.Health != nil {
		title = fmt.Sprintf("Services Health (%d/%d)", hv.Health.Healthy, hv.Health.Total)
	}
	header := styles.TitleStyle.Render(title)
	vpView := hv.Viewport.View()
	footer := lipgloss.NewStyle().Foreground(styles.Muted).Render(
		"  r: refresh │ Esc: back")

	content := lipgloss.JoinVertical(lipgloss.Left, header, "", vpView, footer)

	border := styles.PaneBorder
	if hv.Focused {
		border = styles.PaneBorderActive
	}

	return border.Width(hv.Width - 2).Height(hv.Height - 2).Render(content)
}
