package components

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/ben/aria-tui/internal/api"
	"github.com/ben/aria-tui/internal/ui/styles"
	"github.com/charmbracelet/bubbles/textinput"
	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

// SearchView provides an agentic search box backed by the search_agent tool.
type SearchView struct {
	Search    textinput.Model
	Viewport  viewport.Model
	Width     int
	Height    int
	Focused   bool
	Searching bool

	Result *api.ToolExecuteResult
	Err    string
}

func NewSearchView() *SearchView {
	ti := textinput.New()
	ti.Placeholder = "Search the web with search_agent..."
	ti.CharLimit = 512

	vp := viewport.New(80, 20)

	return &SearchView{
		Search:   ti,
		Viewport: vp,
	}
}

func (sv *SearchView) SetSize(w, h int) {
	sv.Width = w
	sv.Height = h

	contentWidth := w - 4
	sv.Search.Width = contentWidth - 2
	sv.Viewport.Width = contentWidth
	sv.Viewport.Height = h - 8 // header + search + footer
	if sv.Viewport.Height < 1 {
		sv.Viewport.Height = 1
	}
}

func (sv *SearchView) Focus() {
	sv.Focused = true
	sv.Search.Focus()
}

func (sv *SearchView) Blur() {
	sv.Focused = false
	sv.Search.Blur()
}

func (sv *SearchView) GetQuery() string { return sv.Search.Value() }

func (sv *SearchView) SetSearching(v bool) {
	sv.Searching = v
	if v {
		sv.Result = nil
		sv.Err = ""
	}
	sv.refreshContent()
}

func (sv *SearchView) SetResult(result *api.ToolExecuteResult, err string) {
	sv.Searching = false
	sv.Result = result
	sv.Err = err
	sv.refreshContent()
}

func (sv *SearchView) Update(msg tea.Msg) (*SearchView, tea.Cmd) {
	var cmds []tea.Cmd

	if sv.Focused {
		var cmd tea.Cmd
		sv.Search, cmd = sv.Search.Update(msg)
		cmds = append(cmds, cmd)
	}

	var cmd tea.Cmd
	sv.Viewport, cmd = sv.Viewport.Update(msg)
	cmds = append(cmds, cmd)

	return sv, tea.Batch(cmds...)
}

func (sv *SearchView) refreshContent() {
	if sv.Width < 5 {
		return
	}

	var b strings.Builder

	if sv.Searching {
		b.WriteString(styles.SpinnerStyle.Render("\n  ◐ searching…"))
		sv.Viewport.SetContent(b.String())
		return
	}

	if sv.Err != "" {
		b.WriteString(lipgloss.NewStyle().Foreground(styles.Danger).Render("\n  Error: " + sv.Err))
		sv.Viewport.SetContent(b.String())
		return
	}

	if sv.Result == nil {
		b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render(
			"\n  Type a query and press Enter to search."))
		sv.Viewport.SetContent(b.String())
		return
	}

	if sv.Result.Error != "" {
		b.WriteString(lipgloss.NewStyle().Foreground(styles.Danger).Render("\n  " + sv.Result.Error))
		sv.Viewport.SetContent(b.String())
		return
	}

	text := extractOutputText(sv.Result.Output)
	b.WriteString(lipgloss.NewStyle().Foreground(styles.Text).Render(text))
	sv.Viewport.SetContent(b.String())
	sv.Viewport.GotoTop()
}

// extractOutputText pretty-prints the tool output, preferring output["output"].
func extractOutputText(out map[string]interface{}) string {
	if out == nil {
		return "  (no output)"
	}
	if inner, ok := out["output"]; ok {
		switch v := inner.(type) {
		case string:
			return v
		default:
			if pretty, err := json.MarshalIndent(v, "  ", "  "); err == nil {
				return "  " + string(pretty)
			}
		}
	}
	if pretty, err := json.MarshalIndent(out, "  ", "  "); err == nil {
		return "  " + string(pretty)
	}
	return fmt.Sprintf("  %v", out)
}

func (sv *SearchView) View() string {
	if sv.Width < 10 || sv.Height < 5 {
		return ""
	}

	header := styles.TitleStyle.Render("Search")
	searchView := sv.Search.View()
	vpView := sv.Viewport.View()

	status := "Enter: search │ Esc: back"
	if sv.Searching {
		status = "searching… │ Esc: back"
	}
	footer := lipgloss.NewStyle().Foreground(styles.Muted).Render("  " + status)

	content := lipgloss.JoinVertical(lipgloss.Left,
		header,
		"",
		" "+searchView,
		"",
		vpView,
		footer,
	)

	border := styles.PaneBorder
	if sv.Focused {
		border = styles.PaneBorderActive
	}

	return border.Width(sv.Width - 2).Height(sv.Height - 2).Render(content)
}
