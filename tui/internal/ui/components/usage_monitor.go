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

// UsageMonitor displays token usage breakdowns by agent and model.
type UsageMonitor struct {
	Viewport viewport.Model
	Width    int
	Height   int
	Focused  bool

	Summary    *api.UsageSummary
	ByAgent    []api.AgentUsage
	ByModel    []api.ModelUsage
	LLMStatus  []api.LLMBackendStatus
}

func NewUsageMonitor() *UsageMonitor {
	vp := viewport.New(80, 20)
	return &UsageMonitor{Viewport: vp}
}

func (um *UsageMonitor) SetSize(w, h int) {
	um.Width = w
	um.Height = h
	um.Viewport.Width = w - 4
	um.Viewport.Height = h - 6
	if um.Viewport.Height < 1 {
		um.Viewport.Height = 1
	}
}

func (um *UsageMonitor) Focus()                                { um.Focused = true }
func (um *UsageMonitor) Blur()                                 { um.Focused = false }

func (um *UsageMonitor) SetData(summary *api.UsageSummary, byAgent []api.AgentUsage, byModel []api.ModelUsage, llm []api.LLMBackendStatus) {
	um.Summary = summary
	um.ByAgent = byAgent
	um.ByModel = byModel
	um.LLMStatus = llm
	um.refreshContent()
}

func (um *UsageMonitor) Update(msg tea.Msg) (*UsageMonitor, tea.Cmd) {
	var cmd tea.Cmd
	um.Viewport, cmd = um.Viewport.Update(msg)
	return um, cmd
}

func (um *UsageMonitor) refreshContent() {
	if um.Width < 10 {
		return
	}

	cw := um.Width - 8
	var b strings.Builder

	// ---- Summary (24h) ----
	b.WriteString(styles.SectionTitle.Render("  Token Summary (24h)"))
	b.WriteString("\n\n")

	if um.Summary != nil {
		total := um.Summary.TotalInputTokens + um.Summary.TotalOutputTokens
		b.WriteString(um.row("  Requests", fmt.Sprintf("%d", um.Summary.TotalRequests), cw))
		b.WriteString(um.row("  Input", formatTokensLong(um.Summary.TotalInputTokens), cw))
		b.WriteString(um.row("  Output", formatTokensLong(um.Summary.TotalOutputTokens), cw))
		b.WriteString(um.row("  Total", formatTokensLong(total), cw))
	} else {
		b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render("  No usage data available\n"))
	}

	// ---- By Agent ----
	if len(um.ByAgent) > 0 {
		b.WriteString("\n")
		b.WriteString(styles.SectionTitle.Render("  By Agent"))
		b.WriteString("\n\n")

		// Table header
		b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render(
			fmt.Sprintf("  %-20s %10s %10s %10s %6s\n", "Agent", "Input", "Output", "Total", "Calls")))
		b.WriteString(lipgloss.NewStyle().Foreground(styles.BorderColor).Render(
			"  " + strings.Repeat("─", cw-4) + "\n"))

		for _, a := range um.ByAgent {
			total := a.InputTokens + a.OutputTokens
			name := a.AgentName
			if name == "" {
				name = a.AgentID
				if len(name) > 20 {
					name = name[:20]
				}
			}
			b.WriteString(fmt.Sprintf("  %-20s %10s %10s %10s %6d\n",
				lipgloss.NewStyle().Foreground(styles.Text).Render(truncate(name, 20)),
				formatTokensLong(a.InputTokens),
				formatTokensLong(a.OutputTokens),
				styles.VitalValue.Render(formatTokensLong(total)),
				a.Requests))
		}
	}

	// ---- By Model ----
	if len(um.ByModel) > 0 {
		b.WriteString("\n")
		b.WriteString(styles.SectionTitle.Render("  By Model"))
		b.WriteString("\n\n")

		b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render(
			fmt.Sprintf("  %-14s %-22s %10s %10s %6s\n", "Backend", "Model", "Input", "Output", "Calls")))
		b.WriteString(lipgloss.NewStyle().Foreground(styles.BorderColor).Render(
			"  " + strings.Repeat("─", cw-4) + "\n"))

		for _, m := range um.ByModel {
			b.WriteString(fmt.Sprintf("  %-14s %-22s %10s %10s %6d\n",
				lipgloss.NewStyle().Foreground(styles.Info).Render(truncate(m.Backend, 14)),
				lipgloss.NewStyle().Foreground(styles.Text).Render(truncate(m.Model, 22)),
				formatTokensLong(m.InputTokens),
				formatTokensLong(m.OutputTokens),
				m.Requests))
		}
	}

	// ---- LLM Backend Status ----
	if len(um.LLMStatus) > 0 {
		b.WriteString("\n")
		b.WriteString(styles.SectionTitle.Render("  LLM Backends"))
		b.WriteString("\n\n")

		for _, llm := range um.LLMStatus {
			icon := styles.LifecycleIcon("failed")
			if llm.Available {
				icon = styles.LifecycleIcon("active")
			}
			line := fmt.Sprintf("  %s %-14s", icon, llm.Backend)
			if llm.Model != "" {
				line += lipgloss.NewStyle().Foreground(styles.Muted).Render(" " + llm.Model)
			}
			if llm.Error != "" {
				line += " " + lipgloss.NewStyle().Foreground(styles.Danger).Render(llm.Error)
			}
			b.WriteString(line + "\n")
		}
	}

	um.Viewport.SetContent(b.String())
}

func (um *UsageMonitor) row(label, value string, width int) string {
	gap := width - len(label) - len(value) - 4
	if gap < 1 {
		gap = 1
	}
	return fmt.Sprintf("%s%s%s\n",
		styles.VitalLabel.Render(label),
		strings.Repeat(" ", gap),
		styles.VitalValue.Render(value))
}

func (um *UsageMonitor) View() string {
	if um.Width < 10 || um.Height < 5 {
		return ""
	}

	header := styles.TitleStyle.Render("Usage Monitor")
	vpView := um.Viewport.View()
	footer := lipgloss.NewStyle().Foreground(styles.Muted).Render(
		"  r: refresh │ Esc: back")

	content := lipgloss.JoinVertical(lipgloss.Left, header, "", vpView, footer)

	border := styles.PaneBorder
	if um.Focused {
		border = styles.PaneBorderActive
	}

	return border.Width(um.Width - 2).Height(um.Height - 2).Render(content)
}

func formatTokensLong(n int) string {
	if n >= 1_000_000 {
		return fmt.Sprintf("%.2fM", float64(n)/1_000_000)
	}
	if n >= 1_000 {
		return fmt.Sprintf("%.1fK", float64(n)/1_000)
	}
	return fmt.Sprintf("%d", n)
}
