package components

import (
	"fmt"
	"strings"

	"github.com/ben/aria-tui/internal/api"
	"github.com/ben/aria-tui/internal/ui/styles"
	"github.com/charmbracelet/lipgloss"
)

// VitalsPanel shows system health, usage stats, and recent observations.
type VitalsPanel struct {
	Width  int
	Height int

	Health       *api.HealthStatus
	Usage        *api.UsageSummary
	ActiveConvs  int
	ActiveSess   int
	Observations []api.Observation
}

func NewVitalsPanel() *VitalsPanel {
	return &VitalsPanel{}
}

func (v *VitalsPanel) SetSize(w, h int) {
	v.Width = w
	v.Height = h
}

func (v *VitalsPanel) Update(snap *api.DashboardSnapshot) {
	if snap == nil {
		return
	}
	v.Health = snap.Health
	v.Usage = snap.Usage
	v.ActiveConvs = len(snap.Conversations)
	v.ActiveSess = len(snap.CodingSessions)
	v.Observations = snap.Observations
}

// RenderContent returns the vitals content without border wrapping.
func (v *VitalsPanel) RenderContent(width, height int) string {
	if width < 5 {
		width = 20
	}

	var b strings.Builder

	// Health row
	healthIcon := styles.LifecycleIcon("failed")
	healthText := "disconnected"
	version := ""
	if v.Health != nil && v.Health.Status == "healthy" {
		healthIcon = styles.LifecycleIcon("active")
		healthText = "connected"
		version = v.Health.Version
	}

	healthLine := fmt.Sprintf(" %s %s", healthIcon, healthText)
	if version != "" {
		healthLine += lipgloss.NewStyle().Foreground(styles.Muted).Render(" v" + version)
	}
	b.WriteString(healthLine + "\n")

	// Stats grid
	if v.Usage != nil {
		b.WriteString("\n")
		b.WriteString(styles.SectionTitle.Render(" Usage (24h)") + "\n")
		b.WriteString(v.statLine("Requests", fmt.Sprintf("%d", v.Usage.TotalRequests), width))
		b.WriteString(v.statLine("In tokens", formatTokens(v.Usage.TotalInputTokens), width))
		b.WriteString(v.statLine("Out tokens", formatTokens(v.Usage.TotalOutputTokens), width))
	}

	// Active counts
	b.WriteString("\n")
	b.WriteString(styles.SectionTitle.Render(" Active") + "\n")
	b.WriteString(v.statLine("Conversations", fmt.Sprintf("%d", v.ActiveConvs), width))
	b.WriteString(v.statLine("Coding Sess.", fmt.Sprintf("%d", v.ActiveSess), width))

	// Recent observations
	if len(v.Observations) > 0 {
		b.WriteString("\n")
		b.WriteString(styles.SectionTitle.Render(" Observations") + "\n")
		maxObs := height - 12
		if maxObs < 1 {
			maxObs = 1
		}
		if maxObs > len(v.Observations) {
			maxObs = len(v.Observations)
		}
		for i := 0; i < maxObs; i++ {
			obs := v.Observations[i]
			sevIcon := severityIcon(obs.Severity)
			summary := truncate(obs.Summary, width-6)
			b.WriteString(fmt.Sprintf(" %s %s\n", sevIcon, summary))
		}
	}

	return b.String()
}

func (v *VitalsPanel) View() string {
	if v.Width < 10 || v.Height < 3 {
		return ""
	}

	contentWidth := v.Width - 4
	var b strings.Builder

	// Health row
	healthIcon := styles.LifecycleIcon("failed")
	healthText := "disconnected"
	version := ""
	if v.Health != nil && v.Health.Status == "healthy" {
		healthIcon = styles.LifecycleIcon("active")
		healthText = "connected"
		version = v.Health.Version
	}

	b.WriteString(styles.SectionTitle.Render("System"))
	b.WriteString("\n")

	healthLine := fmt.Sprintf(" %s %s", healthIcon, healthText)
	if version != "" {
		healthLine += lipgloss.NewStyle().Foreground(styles.Muted).Render(" v" + version)
	}
	b.WriteString(healthLine)
	b.WriteString("\n")

	// Stats grid
	if v.Usage != nil {
		b.WriteString("\n")
		b.WriteString(styles.SectionTitle.Render("Usage (24h)"))
		b.WriteString("\n")
		b.WriteString(v.statLine("Requests", fmt.Sprintf("%d", v.Usage.TotalRequests), contentWidth))
		b.WriteString(v.statLine("In tokens", formatTokens(v.Usage.TotalInputTokens), contentWidth))
		b.WriteString(v.statLine("Out tokens", formatTokens(v.Usage.TotalOutputTokens), contentWidth))
	}

	// Active counts
	b.WriteString("\n")
	b.WriteString(styles.SectionTitle.Render("Active"))
	b.WriteString("\n")
	b.WriteString(v.statLine("Conversations", fmt.Sprintf("%d", v.ActiveConvs), contentWidth))
	b.WriteString(v.statLine("Coding Sess.", fmt.Sprintf("%d", v.ActiveSess), contentWidth))

	// Recent observations
	if len(v.Observations) > 0 {
		b.WriteString("\n")
		b.WriteString(styles.SectionTitle.Render("Observations"))
		b.WriteString("\n")

		maxObs := v.Height - 14 // rough remaining lines
		if maxObs < 1 {
			maxObs = 1
		}
		if maxObs > len(v.Observations) {
			maxObs = len(v.Observations)
		}

		for i := 0; i < maxObs; i++ {
			obs := v.Observations[i]
			sevIcon := severityIcon(obs.Severity)
			summary := truncate(obs.Summary, contentWidth-6)
			b.WriteString(fmt.Sprintf(" %s %s\n", sevIcon, summary))
		}
	}

	content := b.String()
	return styles.PaneBorder.Width(v.Width - 2).Height(v.Height - 2).Render(content)
}

func (v *VitalsPanel) statLine(label, value string, width int) string {
	gap := width - len(label) - len(value) - 4
	if gap < 1 {
		gap = 1
	}
	return fmt.Sprintf(" %s%s%s\n",
		styles.VitalLabel.Render(label),
		strings.Repeat(" ", gap),
		styles.VitalValue.Render(value))
}

func formatTokens(n int) string {
	if n >= 1_000_000 {
		return fmt.Sprintf("%.1fM", float64(n)/1_000_000)
	}
	if n >= 1_000 {
		return fmt.Sprintf("%.1fK", float64(n)/1_000)
	}
	return fmt.Sprintf("%d", n)
}

func severityIcon(sev string) string {
	switch sev {
	case "critical":
		return lipgloss.NewStyle().Foreground(styles.Danger).Render("▲")
	case "warning":
		return lipgloss.NewStyle().Foreground(styles.Accent).Render("▲")
	case "info":
		return lipgloss.NewStyle().Foreground(styles.Info).Render("●")
	default:
		return lipgloss.NewStyle().Foreground(styles.Muted).Render("●")
	}
}
