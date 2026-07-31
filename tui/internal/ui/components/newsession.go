package components

import (
	"strings"

	"github.com/ben/aria-tui/internal/ui/styles"
	"github.com/charmbracelet/bubbles/textarea"
	"github.com/charmbracelet/bubbles/textinput"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

// NewSessionModal is the "pick a repo path -> optionally auto-worktree ->
// start a real coding session" form -- the TUI equivalent of the web UI's
// New Coding Session dialog on the Shells page. Reached by pressing Enter on
// any Coding Agents / Claude Code / Codex group header in the sidebar, which
// pre-fills which backend/profile this session will use; that choice isn't
// editable here, only the repo path, task, and worktree options are.
//
// Rendered as its own screen (screenNewSession in model.go), not a
// hand-rolled overlay -- that gets ESC-to-cancel and header/footer handling
// for free from the same pushScreen/popScreen machinery every other screen
// already uses, instead of a second, parallel mechanism.
type NewSessionModal struct {
	AgentLabel     string
	SessionBackend string // "claude_code" | "codex" | ""
	SessionProfile string // "pi-coding" | "pi-coding-ridge" | ""

	RepoPath     textinput.Model
	Prompt       textarea.Model
	UseWorktree  bool
	WorktreeName textinput.Model

	focusIdx int // 0=repo, 1=prompt, 2=worktree toggle, 3=worktree name
	Width    int
	Height   int

	Submitting bool
	Err        string
}

func NewNewSessionModal() *NewSessionModal {
	repo := textinput.New()
	repo.Placeholder = "/home/ben/Development/ProjectAria"
	repo.CharLimit = 500

	wtName := textinput.New()
	wtName.Placeholder = "e.g. fix-login-bug (optional)"
	wtName.CharLimit = 80

	prompt := textarea.New()
	prompt.Placeholder = "What should the agent do?"
	prompt.CharLimit = 4000
	prompt.SetHeight(4)
	prompt.ShowLineNumbers = false

	return &NewSessionModal{
		RepoPath:     repo,
		Prompt:       prompt,
		UseWorktree:  true,
		WorktreeName: wtName,
	}
}

// Reset re-initializes the form for a fresh open, pre-filled for one
// coding-session family. Exactly one of backend/profile should be non-empty.
func (m *NewSessionModal) Reset(agentLabel, backend, profile string) {
	m.AgentLabel = agentLabel
	m.SessionBackend = backend
	m.SessionProfile = profile
	m.RepoPath.SetValue("")
	m.Prompt.SetValue("")
	m.WorktreeName.SetValue("")
	m.UseWorktree = true
	m.Err = ""
	m.Submitting = false
	m.setFocus(0)
}

func (m *NewSessionModal) SetSize(w, h int) {
	m.Width = w
	m.Height = h
	innerW := w * 2 / 3
	if innerW < 40 {
		innerW = 40
	}
	if innerW > w-8 {
		innerW = w - 8
	}
	fieldW := innerW - 4
	if fieldW < 10 {
		fieldW = 10
	}
	m.RepoPath.Width = fieldW
	m.Prompt.SetWidth(fieldW)
	m.WorktreeName.Width = fieldW
}

// SetErr surfaces a failed submit (e.g. a worktree/git error) without
// resetting the form, so the user can fix the path and retry rather than
// re-typing everything.
func (m *NewSessionModal) SetErr(err string) {
	m.Err = err
	m.Submitting = false
}

func (m *NewSessionModal) Values() (repo, prompt string, useWorktree bool, worktreeName string) {
	return strings.TrimSpace(m.RepoPath.Value()), strings.TrimSpace(m.Prompt.Value()), m.UseWorktree, strings.TrimSpace(m.WorktreeName.Value())
}

func (m *NewSessionModal) CanSubmit() bool {
	repo, prompt, _, _ := m.Values()
	return repo != "" && prompt != "" && !m.Submitting
}

// numFields is 4 when the worktree toggle is on (repo, prompt, toggle, name)
// and 3 when off (nothing to tab into for a name field that isn't shown).
func (m *NewSessionModal) numFields() int {
	if m.UseWorktree {
		return 4
	}
	return 3
}

func (m *NewSessionModal) setFocus(idx int) {
	n := m.numFields()
	if idx < 0 {
		idx = n - 1
	}
	if idx >= n {
		idx = 0
	}
	m.focusIdx = idx
	m.RepoPath.Blur()
	m.Prompt.Blur()
	m.WorktreeName.Blur()
	switch idx {
	case 0:
		m.RepoPath.Focus()
	case 1:
		m.Prompt.Focus()
	case 3:
		m.WorktreeName.Focus()
	}
}

// HandleKey processes a key while this screen is active. consumed=true means
// the modal fully owns this keystroke (navigation, toggle, submit) and the
// caller must NOT also forward it to Update() — consumed=false means it's
// ordinary input (typing, including a literal space) that Update() should
// still receive.
func (m *NewSessionModal) HandleKey(key string) (submit, consumed bool) {
	switch key {
	case "tab":
		m.setFocus(m.focusIdx + 1)
		return false, true
	case "shift+tab":
		m.setFocus(m.focusIdx - 1)
		return false, true
	case " ":
		if m.focusIdx == 2 {
			m.UseWorktree = !m.UseWorktree
			m.setFocus(m.focusIdx) // re-clamp: toggling off removes field 3
			return false, true
		}
		return false, false
	case "enter":
		if m.focusIdx == 2 {
			m.UseWorktree = !m.UseWorktree
			m.setFocus(m.focusIdx)
			return false, true
		}
		if m.focusIdx == 1 {
			return false, false // newline inside the multi-line task field
		}
		return m.CanSubmit(), true
	case "ctrl+s":
		return m.CanSubmit(), true
	}
	return false, false
}

// Update forwards to whichever text field currently has focus. Only called
// when HandleKey reported consumed=false.
func (m *NewSessionModal) Update(msg tea.Msg) (*NewSessionModal, tea.Cmd) {
	var cmd tea.Cmd
	switch m.focusIdx {
	case 0:
		m.RepoPath, cmd = m.RepoPath.Update(msg)
	case 1:
		m.Prompt, cmd = m.Prompt.Update(msg)
	case 3:
		m.WorktreeName, cmd = m.WorktreeName.Update(msg)
	}
	return m, cmd
}

func (m *NewSessionModal) View() string {
	label := func(i int, text string) string {
		if m.focusIdx == i {
			return lipgloss.NewStyle().Foreground(styles.Accent).Bold(true).Render(text)
		}
		return lipgloss.NewStyle().Foreground(styles.Muted).Render(text)
	}

	var b strings.Builder
	b.WriteString(styles.TitleStyle.Render("New Coding Session") + "\n")
	b.WriteString(lipgloss.NewStyle().Foreground(styles.SubText).Render("  "+m.AgentLabel) + "\n\n")

	b.WriteString(label(0, "Repo path") + "\n")
	b.WriteString("  " + m.RepoPath.View() + "\n\n")

	b.WriteString(label(1, "Task") + "\n")
	b.WriteString("  " + m.Prompt.View() + "\n\n")

	check := "[ ]"
	if m.UseWorktree {
		check = "[x]"
	}
	b.WriteString(label(2, check+" Create an isolated git worktree for this session") + "\n")
	if m.UseWorktree {
		b.WriteString(label(3, "Worktree name (optional)") + "\n")
		b.WriteString("  " + m.WorktreeName.View() + "\n")
		b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render(
			"  If the repo path isn't a git repo yet, it's initialized first, then\n"+
				"  the worktree is created on a new branch under <repo>/.worktrees/.") + "\n")
	}

	if m.Err != "" {
		b.WriteString("\n" + lipgloss.NewStyle().Foreground(styles.Danger).Render("  "+m.Err) + "\n")
	}
	if m.Submitting {
		b.WriteString("\n" + lipgloss.NewStyle().Foreground(styles.Accent).Render("  Starting…") + "\n")
	}

	b.WriteString("\n" + styles.HelpKey.Render("tab") + styles.HelpDesc.Render(" next field  ") +
		styles.HelpKey.Render("space") + styles.HelpDesc.Render(" toggle  ") +
		styles.HelpKey.Render("^s") + styles.HelpDesc.Render(" start  ") +
		styles.HelpKey.Render("esc") + styles.HelpDesc.Render(" cancel"))

	content := b.String()
	boxW := m.Width * 2 / 3
	if boxW < 50 {
		boxW = 50
	}
	if boxW > m.Width-4 {
		boxW = m.Width - 4
	}
	box := styles.PaneBorderActive.Width(boxW).Padding(1, 2).Render(content)

	return lipgloss.Place(m.Width, m.Height, lipgloss.Center, lipgloss.Center, box)
}
