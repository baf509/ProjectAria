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

// ProjectCockpitView is the Coherence C4 per-project cockpit: one scrollable
// aggregate of a project's git state, agents (shells), coding sessions, tasks,
// recent changes, alerts, and budget. Empty sections are skipped.
type ProjectCockpitView struct {
	Viewport viewport.Model
	Width    int
	Height   int
	Focused  bool

	Cockpit *api.ProjectCockpit
}

func NewProjectCockpitView() *ProjectCockpitView {
	return &ProjectCockpitView{Viewport: viewport.New(80, 20)}
}

func (pc *ProjectCockpitView) SetSize(w, h int) {
	pc.Width = w
	pc.Height = h
	pc.Viewport.Width = w - 4
	pc.Viewport.Height = h - 6
	if pc.Viewport.Height < 1 {
		pc.Viewport.Height = 1
	}
}

func (pc *ProjectCockpitView) Focus() { pc.Focused = true }
func (pc *ProjectCockpitView) Blur()  { pc.Focused = false }

func (pc *ProjectCockpitView) SetData(cockpit api.ProjectCockpit) {
	pc.Cockpit = &cockpit
	pc.refreshContent()
	pc.Viewport.GotoTop()
}

func (pc *ProjectCockpitView) Update(msg tea.Msg) (*ProjectCockpitView, tea.Cmd) {
	var cmd tea.Cmd
	pc.Viewport, cmd = pc.Viewport.Update(msg)
	return pc, cmd
}

// firstLine returns the first line of s, truncated to max runes.
func firstLine(s string, max int) string {
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		s = s[:i]
	}
	return truncate(strings.TrimSpace(s), max)
}

// refreshContent rebuilds the sectioned viewport body. As with FleetView (see
// the long comment there, regression-tested in fleet_view_test.go): trailing
// "\n" stays OUTSIDE every lipgloss Render() call, or the bubbles Viewport
// desyncs and swallows the following line.
func (pc *ProjectCockpitView) refreshContent() {
	if pc.Cockpit == nil || pc.Width < 10 {
		return
	}
	ck := pc.Cockpit
	var b strings.Builder

	section := func(title string) {
		if b.Len() > 0 {
			b.WriteString("\n")
		}
		b.WriteString(styles.SectionTitle.Render(title) + "\n")
	}
	muted := lipgloss.NewStyle().Foreground(styles.Muted)

	// ---- GIT ----
	if ck.Git.Live != nil || ck.Git.Harvested != nil {
		section("GIT")
		if lg := ck.Git.Live; lg != nil {
			dirty := fmt.Sprintf("%d dirty", lg.DirtyFiles)
			if lg.DirtyFiles > 0 {
				dirty = styles.VitalWarn.Render(dirty)
			} else {
				dirty = muted.Render("clean")
			}
			b.WriteString("  branch " + styles.VitalValue.Render(lg.Branch) + " · " + dirty + "\n")
		}
		if hg := ck.Git.Harvested; hg != nil {
			if ck.Git.Live == nil && hg.Branch != "" {
				b.WriteString("  branch " + styles.VitalValue.Render(hg.Branch) + "\n")
			}
			if hg.LastCommitSubject != "" {
				line := "  last commit: " + truncate(hg.LastCommitSubject, pc.Width-24)
				if !hg.LastCommitAt.IsZero() {
					line += " " + muted.Render("("+relAge(hg.LastCommitAt)+" ago)")
				}
				b.WriteString(line + "\n")
			}
		}
	}

	// ---- AGENTS (watched shells) ----
	if len(ck.Shells) > 0 {
		section("AGENTS")
		for _, sh := range ck.Shells {
			name := sh.ShortName
			if name == "" {
				name = sh.Name
			}
			state := sh.ActivityState
			if state == "" {
				state = sh.Status
			}
			stateCell := fmt.Sprintf("%-8s", truncate(state, 8))
			switch sh.ActivityState {
			case "blocked":
				stateCell = styles.VitalBad.Render(stateCell)
			case "working":
				stateCell = styles.VitalGood.Render(stateCell)
			default:
				stateCell = muted.Render(stateCell)
			}
			line := "  " + fmt.Sprintf("%-20s", truncate(name, 20)) + " " + stateCell
			if sh.Host != "" {
				line += " " + muted.Render(fmt.Sprintf("%-10s", truncate(sh.Host, 10)))
			}
			b.WriteString(line + "\n")
			// A blocked shell is waiting on a human -- surface what it's asking.
			if sh.ActivityState == "blocked" || sh.AwaitingInput {
				if prompt := firstLine(sh.PromptLine, pc.Width-12); prompt != "" {
					b.WriteString("      " + styles.VitalWarn.Render("❯ "+prompt) + "\n")
				}
			}
		}
	}

	// ---- SESSIONS ----
	if len(ck.Sessions) > 0 {
		section("SESSIONS")
		for _, s := range ck.Sessions {
			bm := s.Backend
			if s.Model != "" {
				bm += "/" + s.Model
			}
			status := s.Status
			if s.Looping {
				status += "⟳"
			}
			line := "  " + fmt.Sprintf("%-22s", truncate(bm, 22)) + " " +
				statusColor(s.Status).Render(fmt.Sprintf("%-10s", truncate(status, 10)))
			if len(s.GateRuns) > 0 {
				last := s.GateRuns[len(s.GateRuns)-1]
				if last.Passed {
					line += " " + styles.VitalGood.Render("gate:PASS")
				} else {
					line += " " + styles.VitalBad.Render("gate:FAIL")
				}
				if tail := firstLine(last.Tail, pc.Width-52); tail != "" {
					line += " " + muted.Render(tail)
				}
			}
			b.WriteString(line + "\n")
			if summary := firstLine(s.ResultSummary, pc.Width-12); summary != "" {
				b.WriteString("      " + muted.Render(summary) + "\n")
			}
		}
	}

	// ---- TASKS ----
	if len(ck.Tasks) > 0 {
		section("TASKS")
		for _, t := range ck.Tasks {
			line := "  " + muted.Render(fmt.Sprintf("%-9s", truncate(t.Status, 9))) + " " +
				truncate(t.Title, pc.Width-24)
			if t.Stale {
				line += " " + styles.VitalWarn.Render("STALE")
			}
			b.WriteString(line + "\n")
		}
	}

	// ---- CHANGED (recent machine-scan memories) ----
	if len(ck.Changed) > 0 {
		section("CHANGED")
		for _, m := range ck.Changed {
			line := "  " + firstLine(m.Content, pc.Width-20)
			if !m.CreatedAt.IsZero() {
				line += " " + muted.Render("("+relAge(m.CreatedAt)+" ago)")
			}
			b.WriteString(line + "\n")
		}
	}

	// ---- ALERTS ----
	if len(ck.Alerts) > 0 {
		section("ALERTS")
		for _, a := range ck.Alerts {
			tag := a.Source
			if a.EventType != "" {
				tag += "/" + a.EventType
			}
			line := "  " + styles.VitalWarn.Render(fmt.Sprintf("%-20s", truncate(tag, 20))) + " " +
				firstLine(a.Message, pc.Width-36)
			if !a.CreatedAt.IsZero() {
				line += " " + muted.Render("("+relAge(a.CreatedAt)+" ago)")
			}
			b.WriteString(line + "\n")
		}
	}

	// ---- BUDGET ----
	if ck.Budget.SessionsPriced > 0 || ck.Budget.Cost > 0 || ck.Budget.TotalTokens > 0 {
		section("BUDGET")
		b.WriteString("  " + styles.VitalValue.Render(fmt.Sprintf("$%.4f", ck.Budget.Cost)) +
			" · " + formatTokensLong(ck.Budget.TotalTokens) + " tokens" +
			" · " + fmt.Sprintf("%d sessions priced", ck.Budget.SessionsPriced) + "\n")
	}

	if b.Len() == 0 {
		b.WriteString(muted.Render("  Nothing to show for this project yet") + "\n")
	}

	pc.Viewport.SetContent(b.String())
}

func (pc *ProjectCockpitView) View() string {
	if pc.Width < 10 || pc.Height < 5 {
		return ""
	}

	title := "Cockpit"
	if pc.Cockpit != nil {
		name := pc.Cockpit.Project.Name
		if name == "" {
			name = pc.Cockpit.Project.Slug
		}
		title = "Cockpit — " + name
		if pc.Cockpit.AttentionScore > 0 {
			title += fmt.Sprintf(" (attention %d)", pc.Cockpit.AttentionScore)
		}
	}
	header := styles.TitleStyle.Render(title)
	vpView := pc.Viewport.View()
	footer := lipgloss.NewStyle().Foreground(styles.Muted).Render(
		"  ↑↓/pgup/pgdn: scroll │ r: refresh │ Esc: back")

	content := lipgloss.JoinVertical(lipgloss.Left, header, "", vpView, footer)

	border := styles.PaneBorder
	if pc.Focused {
		border = styles.PaneBorderActive
	}
	return border.Width(pc.Width - 2).Height(pc.Height - 2).Render(content)
}
