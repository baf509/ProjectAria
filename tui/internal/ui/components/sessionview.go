package components

import (
	"fmt"
	"strings"

	"github.com/ben/aria-tui/internal/api"
	"github.com/ben/aria-tui/internal/ui/styles"
	"github.com/charmbracelet/bubbles/textarea"
	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

// SessionView displays a coding session's output log and allows input.
type SessionView struct {
	Viewport viewport.Model
	Input    textarea.Model

	Session *api.CodingSession
	Output  string // raw output from API
	Width   int
	Height  int
	Focused bool
}

func NewSessionView() *SessionView {
	ta := textarea.New()
	ta.Placeholder = "Send input to session... (Enter to send)"
	ta.CharLimit = 2048
	ta.SetHeight(2)
	ta.ShowLineNumbers = false
	ta.FocusedStyle.CursorLine = lipgloss.NewStyle()
	ta.FocusedStyle.Base = lipgloss.NewStyle()
	ta.BlurredStyle.Base = lipgloss.NewStyle()

	vp := viewport.New(80, 20)

	return &SessionView{
		Viewport: vp,
		Input:    ta,
	}
}

func (sv *SessionView) SetSize(w, h int) {
	sv.Width = w
	sv.Height = h

	contentWidth := w - 4
	inputHeight := 4
	vpHeight := h - inputHeight - 6
	if vpHeight < 1 {
		vpHeight = 1
	}

	sv.Viewport.Width = contentWidth
	sv.Viewport.Height = vpHeight
	sv.Input.SetWidth(contentWidth)
}

func (sv *SessionView) Focus() {
	sv.Focused = true
	sv.Input.Focus()
}

func (sv *SessionView) Blur() {
	sv.Focused = false
	sv.Input.Blur()
}

func (sv *SessionView) SetSession(session *api.CodingSession) {
	sv.Session = session
}

func (sv *SessionView) SetOutput(output string) {
	sv.Output = output
	sv.Viewport.SetContent(output)
	sv.Viewport.GotoBottom()
}

func (sv *SessionView) GetInput() string {
	v := sv.Input.Value()
	sv.Input.Reset()
	return v
}

func (sv *SessionView) Update(msg tea.Msg) (*SessionView, tea.Cmd) {
	var cmds []tea.Cmd

	if sv.Focused {
		var cmd tea.Cmd
		sv.Input, cmd = sv.Input.Update(msg)
		cmds = append(cmds, cmd)
	}

	var cmd tea.Cmd
	sv.Viewport, cmd = sv.Viewport.Update(msg)
	cmds = append(cmds, cmd)

	return sv, tea.Batch(cmds...)
}

func (sv *SessionView) View() string {
	if sv.Width < 5 || sv.Height < 5 {
		return ""
	}

	// Header with session metadata
	var headerParts []string
	if sv.Session != nil {
		icon := styles.LifecycleIcon(sv.Session.Status)
		title := fmt.Sprintf("%s Coding Session", icon)
		headerParts = append(headerParts, styles.TitleStyle.Render(title))

		// Metadata line
		meta := []string{}
		meta = append(meta, fmt.Sprintf("Backend: %s", sv.Session.Backend))
		if sv.Session.Model != "" {
			meta = append(meta, fmt.Sprintf("Model: %s", sv.Session.Model))
		}
		if sv.Session.Workspace != "" {
			meta = append(meta, fmt.Sprintf("Dir: %s", sv.Session.Workspace))
		}
		if sv.Session.Branch != "" {
			meta = append(meta, fmt.Sprintf("Branch: %s", sv.Session.Branch))
		}
		metaLine := lipgloss.NewStyle().Foreground(styles.Muted).Render(strings.Join(meta, " │ "))
		headerParts = append(headerParts, metaLine)

		// Prompt
		if sv.Session.Prompt != "" {
			prompt := truncate(sv.Session.Prompt, sv.Width-8)
			headerParts = append(headerParts,
				lipgloss.NewStyle().Foreground(styles.SubText).Italic(true).Render("❯ "+prompt))
		}
	} else {
		headerParts = append(headerParts, styles.TitleStyle.Render("No session selected"))
	}

	header := strings.Join(headerParts, "\n")
	vpView := sv.Viewport.View()
	inputView := sv.Input.View()

	content := lipgloss.JoinVertical(lipgloss.Left,
		header,
		"",
		vpView,
		"",
		inputView,
	)

	border := styles.PaneBorder
	if sv.Focused {
		border = styles.PaneBorderActive
	}

	return border.Width(sv.Width - 2).Height(sv.Height - 2).Render(content)
}
