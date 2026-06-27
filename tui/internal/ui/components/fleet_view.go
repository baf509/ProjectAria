package components

import (
	"fmt"
	"strings"
	"time"

	"github.com/ben/aria-tui/internal/api"
	"github.com/ben/aria-tui/internal/ui/styles"
	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

// FleetView displays a unified table of all coding sessions and watched shells.
type FleetView struct {
	Viewport viewport.Model
	Width    int
	Height   int
	Focused  bool

	Sessions []api.CodingSession
	Shells   []api.Shell
	Usage    []api.SessionUsage
}

func NewFleetView() *FleetView {
	vp := viewport.New(80, 20)
	return &FleetView{Viewport: vp}
}

func (fv *FleetView) SetSize(w, h int) {
	fv.Width = w
	fv.Height = h
	fv.Viewport.Width = w - 4
	fv.Viewport.Height = h - 6
	if fv.Viewport.Height < 1 {
		fv.Viewport.Height = 1
	}
}

func (fv *FleetView) Focus() { fv.Focused = true }
func (fv *FleetView) Blur()  { fv.Focused = false }

func (fv *FleetView) SetData(sessions []api.CodingSession, shells []api.Shell, usage []api.SessionUsage) {
	fv.Sessions = sessions
	fv.Shells = shells
	fv.Usage = usage
	fv.refreshContent()
}

func (fv *FleetView) Update(msg tea.Msg) (*FleetView, tea.Cmd) {
	var cmd tea.Cmd
	fv.Viewport, cmd = fv.Viewport.Update(msg)
	return fv, cmd
}

func (fv *FleetView) refreshContent() {
	if fv.Width < 10 {
		return
	}

	cw := fv.Width - 8
	var b strings.Builder

	// Index usage by session id.
	usageBySession := map[string]api.SessionUsage{}
	for _, u := range fv.Usage {
		usageBySession[u.SessionID] = u
	}

	// Table header.
	headerFmt := "  %-7s %-22s %-20s %-9s %8s %9s %9s\n"
	b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render(
		fmt.Sprintf(headerFmt, "TYPE", "NAME", "BACKEND/MODEL", "STATUS", "IDLE/AGE", "TOKENS", "COST $")))
	b.WriteString(lipgloss.NewStyle().Foreground(styles.BorderColor).Render(
		"  " + strings.Repeat("─", cw-4) + "\n"))

	var totalTokens int
	var totalCost float64

	// Coding sessions.
	for _, s := range fv.Sessions {
		name := s.Workspace
		if name == "" {
			name = s.ID
		}
		backendModel := s.Backend
		u, ok := usageBySession[s.ID]
		if ok {
			if u.LLM != nil && *u.LLM != "" {
				backendModel = *u.LLM
			}
			if u.Model != nil && *u.Model != "" {
				backendModel += "/" + *u.Model
			} else if s.Model != "" {
				backendModel += "/" + s.Model
			}
		} else if s.Model != "" {
			backendModel += "/" + s.Model
		}

		tokens := ""
		cost := ""
		if ok {
			tokens = formatTokensLong(u.TotalTokens)
			cost = fmt.Sprintf("%.4f", u.Cost)
			totalTokens += u.TotalTokens
			totalCost += u.Cost
		}

		age := relAge(s.CreatedAt)
		fv.writeRow(&b, headerFmt, "session", name, backendModel, s.Status, age, tokens, cost)
	}

	// Watched shells.
	for _, sh := range fv.Shells {
		name := sh.ShortName
		if name == "" {
			name = sh.Name
		}
		status := sh.Status
		if sh.AwaitingInput {
			status = "awaiting"
		}
		idle := fmt.Sprintf("%ds", sh.IdleSeconds)
		fv.writeRow(&b, headerFmt, "shell", name, "tmux", status, idle, "", "")
	}

	if len(fv.Sessions) == 0 && len(fv.Shells) == 0 {
		b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render("\n  No sessions or shells\n"))
	}

	// Totals row.
	b.WriteString(lipgloss.NewStyle().Foreground(styles.BorderColor).Render(
		"  " + strings.Repeat("─", cw-4) + "\n"))
	b.WriteString(fmt.Sprintf(headerFmt,
		styles.VitalLabel.Render("TOTAL"), "", "", "", "",
		styles.VitalValue.Render(formatTokensLong(totalTokens)),
		styles.VitalValue.Render(fmt.Sprintf("%.4f", totalCost))))

	fv.Viewport.SetContent(b.String())
}

func (fv *FleetView) writeRow(b *strings.Builder, format, typ, name, backendModel, status, idle, tokens, cost string) {
	typeStyle := lipgloss.NewStyle().Foreground(styles.Info)
	if typ == "shell" {
		typeStyle = lipgloss.NewStyle().Foreground(styles.Accent)
	}
	b.WriteString(fmt.Sprintf(format,
		typeStyle.Render(typ),
		truncate(name, 22),
		truncate(backendModel, 20),
		statusColor(status).Render(truncate(status, 9)),
		idle,
		tokens,
		cost))
}

func relAge(t time.Time) string {
	if t.IsZero() {
		return "-"
	}
	d := time.Since(t)
	if d < time.Minute {
		return fmt.Sprintf("%ds", int(d.Seconds()))
	}
	if d < time.Hour {
		return fmt.Sprintf("%dm", int(d.Minutes()))
	}
	if d < 24*time.Hour {
		return fmt.Sprintf("%dh", int(d.Hours()))
	}
	return fmt.Sprintf("%dd", int(d.Hours()/24))
}

func statusColor(status string) lipgloss.Style {
	switch status {
	case "running", "active":
		return lipgloss.NewStyle().Foreground(styles.Secondary)
	case "failed", "error":
		return lipgloss.NewStyle().Foreground(styles.Danger)
	case "awaiting":
		return lipgloss.NewStyle().Foreground(styles.Warning)
	default:
		return lipgloss.NewStyle().Foreground(styles.Muted)
	}
}

func (fv *FleetView) View() string {
	if fv.Width < 10 || fv.Height < 5 {
		return ""
	}

	header := styles.TitleStyle.Render(fmt.Sprintf("Fleet (%d sessions, %d shells)", len(fv.Sessions), len(fv.Shells)))
	vpView := fv.Viewport.View()
	footer := lipgloss.NewStyle().Foreground(styles.Muted).Render(
		"  r: refresh │ Esc: back")

	content := lipgloss.JoinVertical(lipgloss.Left, header, "", vpView, footer)

	border := styles.PaneBorder
	if fv.Focused {
		border = styles.PaneBorderActive
	}

	return border.Width(fv.Width - 2).Height(fv.Height - 2).Render(content)
}
