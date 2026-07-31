package components

import (
	"strings"

	"github.com/ben/aria-tui/internal/api"
	"github.com/ben/aria-tui/internal/ui/styles"
	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

// HistoryDetail shows a single historical shell's stored scrollback,
// read-only -- there's no live tmux pane behind a stopped shell to send
// input to, unlike SessionView.
type HistoryDetail struct {
	Viewport viewport.Model
	Shell    *api.ShellRecord
	Width    int
	Height   int
	Focused  bool
}

func NewHistoryDetail() *HistoryDetail {
	return &HistoryDetail{Viewport: viewport.New(80, 20)}
}

func (hd *HistoryDetail) SetSize(w, h int) {
	hd.Width = w
	hd.Height = h
	hd.Viewport.Width = w - 4
	hd.Viewport.Height = h - 6
	if hd.Viewport.Height < 1 {
		hd.Viewport.Height = 1
	}
}

func (hd *HistoryDetail) Focus() { hd.Focused = true }
func (hd *HistoryDetail) Blur()  { hd.Focused = false }

// SetEvents renders the given events as plain scrollback text. Input events
// get a "> " prefix (matches the extraction worker's own convention) so a
// captured conversation reads the same way here as it did live.
func (hd *HistoryDetail) SetEvents(shell *api.ShellRecord, events []api.ShellEventRecord) {
	hd.Shell = shell
	var b strings.Builder
	for _, ev := range events {
		if ev.Kind == "input" {
			b.WriteString(lipgloss.NewStyle().Foreground(styles.Accent).Render("> " + ev.TextClean))
		} else {
			b.WriteString(ev.TextClean)
		}
		if !strings.HasSuffix(ev.TextClean, "\n") {
			b.WriteString("\n")
		}
	}
	if len(events) == 0 {
		b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render("  No stored scrollback for this shell (events likely pruned)."))
	}
	hd.Viewport.SetContent(b.String())
	hd.Viewport.GotoBottom()
}

func (hd *HistoryDetail) Update(msg tea.Msg) (*HistoryDetail, tea.Cmd) {
	var cmd tea.Cmd
	hd.Viewport, cmd = hd.Viewport.Update(msg)
	return hd, cmd
}

func (hd *HistoryDetail) View() string {
	if hd.Width < 10 || hd.Height < 5 {
		return ""
	}
	title := "Scrollback"
	if hd.Shell != nil {
		name := hd.Shell.ShortName
		if name == "" {
			name = hd.Shell.Name
		}
		title = "Scrollback — " + name + " (" + hd.Shell.Status + ")"
	}
	header := styles.TitleStyle.Render(title)
	vpView := hd.Viewport.View()
	footer := lipgloss.NewStyle().Foreground(styles.Muted).Render(
		"  ↑↓/pgup/pgdn: scroll │ Esc: back")

	content := lipgloss.JoinVertical(lipgloss.Left, header, "", vpView, footer)

	border := styles.PaneBorder
	if hd.Focused {
		border = styles.PaneBorderActive
	}
	return border.Width(hd.Width - 2).Height(hd.Height - 2).Render(content)
}
